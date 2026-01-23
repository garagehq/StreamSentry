#!/usr/bin/env python3
"""
Minus - HDMI passthrough with ML-based ad detection.

Architecture:
- ustreamer captures from HDMI-RX and serves MJPEG stream + HTTP snapshot
- GStreamer with input-selector for instant video/blocking switching
- PaddleOCR on RKNN NPU detects ad-related text (~400ms)
- FastVLM-1.5B on Axera NPU provides visual understanding (~0.9s)
- Spanish vocabulary practice during ad blocks!

Key insight: Using GStreamer input-selector allows instant switching between
video and blocking overlay without any process restart or black screen gap.

Performance:
- Display: 30fps via GStreamer kmssink (NV12 → DRM plane 72)
- Snapshot: ~150ms non-blocking HTTP capture
- OCR: ~400-500ms per frame on RKNN NPU
- VLM: ~0.9s per frame on Axera NPU
- Ad blocking: INSTANT switching via input-selector
"""

import argparse
import gc
import os
import sys
import signal
import time
import logging
import logging.handlers
import threading
import subprocess
import re
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import numpy as np
import cv2


# =============================================================================
# Console Blanking - Hide dmesg/login screen before GStreamer takes over
# =============================================================================
# Add src directory to path early so we can import console module
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from console import blank_console, restore_console

# Blank the console immediately on import (before any output)
blank_console()


# Note: Previously had SuppressLibjpegWarnings context manager here but it caused
# file descriptor leaks over time (~500k calls over 13hrs exhausted FD limit).
# libjpeg warnings are harmless, so we just let them through now.

# Set up logging with rotation (max 5MB, keep 3 backups)
log_format = '%(asctime)s [%(levelname).1s] %(message)s'
log_datefmt = '%Y-%m-%d %H:%M:%S'

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Remove any existing handlers to prevent duplicates
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# Add file handler with rotation
# Use /tmp/minus.log - sudoers allows passwordless management
log_file = Path('/tmp/minus.log')
try:
    if log_file.exists():
        try:
            with open(log_file, 'a'):
                pass
        except PermissionError:
            # Use sudo to fix permissions (sudoers.d/minus allows this)
            import subprocess
            subprocess.run(['sudo', 'rm', '-f', str(log_file)], capture_output=True)
    if not log_file.exists():
        log_file.touch(mode=0o666)
except Exception:
    pass

file_handler = logging.handlers.RotatingFileHandler(
    log_file,
    maxBytes=5*1024*1024,  # 5MB
    backupCount=3
)
file_handler.setFormatter(logging.Formatter(log_format, log_datefmt))
file_handler.setLevel(logging.INFO)
root_logger.addHandler(file_handler)

# Add console handler for terminal output
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter(log_format, log_datefmt))
console_handler.setLevel(logging.INFO)
root_logger.addHandler(console_handler)

logger = logging.getLogger('Minus')

# Suppress OpenCV JPEG warnings (this only affects OpenCV's own logging, not libjpeg)
os.environ['OPENCV_LOG_LEVEL'] = 'ERROR'

# Import extracted modules
from drm import probe_drm_output
from v4l2 import probe_v4l2_device
from config import MinusConfig
from capture import UstreamerCapture
from screenshots import ScreenshotManager
from skip_detection import check_skip_opportunity

# Import OCR module
try:
    from ocr import PaddleOCR
    HAS_OCR = True
except ImportError as e:
    logger.warning(f"OCR module not available: {e}")
    HAS_OCR = False

# Import AdBlocker module
try:
    from ad_blocker import AdBlocker
    HAS_ADBLOCKER = True
except ImportError as e:
    logger.warning(f"AdBlocker module not available: {e}")
    HAS_ADBLOCKER = False

# Import VLM module
try:
    from vlm import VLMManager
    HAS_VLM = True
except ImportError as e:
    logger.warning(f"VLM module not available: {e}")
    HAS_VLM = False

# Import Audio module
try:
    from audio import AudioPassthrough
    HAS_AUDIO = True
except ImportError as e:
    logger.warning(f"Audio module not available: {e}")
    HAS_AUDIO = False

# Import Health Monitor
try:
    from health import HealthMonitor
    HAS_HEALTH = True
except ImportError as e:
    logger.warning(f"Health module not available: {e}")
    HAS_HEALTH = False

# Import Web UI
try:
    from webui import WebUI
    HAS_WEBUI = True
except ImportError as e:
    logger.warning(f"WebUI module not available: {e}")
    HAS_WEBUI = False

# Import Fire TV Setup Manager
try:
    from fire_tv_setup import FireTVSetupManager
    HAS_FIRE_TV = True
except ImportError as e:
    logger.warning(f"Fire TV module not available: {e}")
    HAS_FIRE_TV = False

# Import Notification Overlay
try:
    from overlay import NotificationOverlay, SystemNotification
    HAS_OVERLAY = True
except ImportError as e:
    logger.warning(f"Overlay module not available: {e}")
    HAS_OVERLAY = False


class Minus:
    """
    Minus - HDMI passthrough with ML-based ad detection.

    Uses a single GStreamer pipeline with input-selector for instant
    switching between video and blocking overlay.
    """

    def __init__(self, config: MinusConfig = None):
        if config is None:
            config = MinusConfig()
        self.config = config
        self.device = config.device
        self.ustreamer_process = None
        self.frame_capture = None
        self.running = False
        self.blocking_active = False
        self._hdmi_recovery_in_progress = False  # Prevent main loop interference during HDMI recovery
        self._hdmi_signal_lost = False  # Pause detection workers when HDMI signal is lost

        # ML processing
        self.ocr = None
        self.vlm = None
        self.ad_blocker = None
        self.audio = None
        self.health_monitor = None
        self.ml_thread = None
        self.vlm_thread = None

        # VLM degradation state
        self.vlm_disabled = False
        self.vlm_consecutive_timeouts = 0
        self.vlm_max_timeouts = 5  # Disable VLM after this many consecutive timeouts

        # OCR detection state (PRIMARY - high trust)
        self.ocr_ad_detected = False
        self.ocr_ad_detection_count = 0
        self.ocr_no_ad_count = 0
        self.last_ocr_ad_time = 0

        # VLM detection state (SECONDARY - contextual trust)
        self.vlm_ad_detected = False
        self.vlm_frame_count = 0
        self.vlm_consecutive_ad_count = 0
        self.vlm_no_ad_count = 0

        # VLM stability system - sliding window approach to prevent waffling
        # Tracks recent VLM decisions and requires sustained agreement to change state
        # NOTE: FastVLM-1.5B is smarter, so thresholds can be more relaxed
        self.vlm_decision_history = []      # List of (timestamp, is_ad) tuples
        self.vlm_history_window = 45.0      # Look at last 45 seconds of decisions
        self.vlm_min_decisions = 4          # Need at least 4 decisions to act
        self.vlm_start_agreement = 0.80     # Need 80% ad agreement to START blocking
        self.vlm_stop_agreement = 0.75      # Need 75% no-ad agreement to STOP blocking
        self.vlm_hysteresis_boost = 0.10    # Extra agreement needed to change current state

        # State change rate limiting
        self.vlm_last_state_change = 0      # When VLM state last changed
        self.vlm_min_state_duration = 8.0   # Min seconds before allowing state change
        self.vlm_cooldown_active = False    # Currently in cooldown period

        # Legacy counters (still used for some logic)
        self.vlm_waffle_count = 0           # How many recent flip-flops (used for logging)
        self.vlm_last_state = None          # Last VLM state ('ad' or 'no-ad')
        self.vlm_state_change_time = 0      # When state last changed

        # Home screen detection - suppress ad detection on streaming app interfaces
        # When OCR detects these keywords, both OCR and VLM ad detection is suppressed
        # (e.g., "Sponsored" rows on Fire TV home are promotional but not video ads)
        self.home_screen_keywords = {
            'home', 'disney+', 'netflix', 'youtube', 'hulu', 'prime video',
            'amazon', 'settings', 'search', 'library', 'watchlist', 'my stuff',
            'continue watching', 'recommended', 'trending', 'popular', 'new releases',
            'categories', 'genres', 'apps', 'channels', 'live tv',
            # Fire TV specific
            'surprise me', 'see more', 'for you',
            'recently added', 'top picks', 'movies', 'tv shows'
        }

        # Video player interface keywords - suppress VLM false positives on video UIs
        # VLM often thinks video player interfaces are ads
        self.video_interface_keywords = {
            # Video player controls/info
            'subscribe', 'subscribed', 'description', 'comments',
            'views', 'likes', 'share', 'save', 'download',
            # Time indicators (e.g., "3 years ago", "5 months ago")
            'ago', 'year', 'month', 'week', 'day', 'hour',
            # Music/video platforms
            'colors', 'vevo', 'official', 'music video', 'lyric',
            # Channel indicators
            'channel', 'playlist', 'queue', 'autoplay',
            # YouTube specific
            'show more', 'show less', 'read more',
        }

        self.last_ocr_texts = []            # Last OCR detected texts
        self.home_screen_detected = False   # True if home screen keywords found
        self.home_screen_detect_time = 0    # When home screen was last detected
        self.video_interface_detected = False  # True if video player interface detected
        self.video_interface_detect_time = 0   # When video interface was last detected

        # Combined ad detection state
        self.ad_detected = False
        self.frame_count = 0
        self.blocking_start_time = 0
        self.blocking_source = None

        # Weighted detection parameters
        self.OCR_TRUST_WINDOW = 5.0
        self.VLM_ALONE_THRESHOLD = 5  # Require 5 consecutive VLM detections to trigger alone
        self.MIN_BLOCKING_DURATION = 3.0
        self.OCR_STOP_THRESHOLD = 3
        self.VLM_STOP_THRESHOLD = 2
        self.SKIP_DELAY_SECONDS = 4.5  # Wait 4s after ad starts before attempting skip (skip buttons rarely appear sooner)

        self._state_lock = threading.Lock()

        # Scene change detection
        self.prev_frame = None
        self.prev_frame_had_ad = False
        self.scene_skip_count = 0
        self.scene_change_threshold = 0.01
        self.max_scene_skip = 30  # Force OCR after this many consecutive skips

        # Static screen suppression - disable blocking for still ads
        # (e.g., paused video with ad, YouTube landing page with sponsored content)
        self.STATIC_TIME_THRESHOLD = 2.5  # Seconds of static screen to trigger suppression
        self.STATIC_OCR_THRESHOLD = 4     # OCR iterations without scene change
        self.DYNAMIC_COOLDOWN = 0.5       # Keep suppression for this long after screen becomes dynamic (reduced from 1.5s)
        self.static_since_time = 0        # When screen became static (0 = not static)
        self.static_ocr_count = 0         # OCR iterations without scene change
        self.static_blocking_suppressed = False  # Currently suppressing due to static
        self.screen_became_dynamic_time = 0      # When screen went from static to dynamic

        self.vlm_prev_frame = None
        self.vlm_prev_frame_had_ad = False
        self.vlm_scene_skip_count = 0
        self.vlm_max_scene_skip = 10  # Force VLM after this many consecutive skips

        # Screenshot manager (organizes into ads/, non_ads/, vlm_spastic/, static/ subdirs)
        self.screenshot_manager = ScreenshotManager(
            base_dir=Path(config.screenshot_dir),
            max_screenshots=config.max_screenshots
        )

        # Web UI state
        self.webui = None
        self.start_time = time.time()
        self.blocking_paused_until = 0  # Timestamp when pause expires
        from collections import deque
        self.detection_history = deque(maxlen=50)  # Recent detections for web UI

        # Fire TV state
        self.fire_tv_setup = None
        self.fire_tv_controller = None
        self._fire_tv_setup_thread = None

        # Skip opportunity state - CONSERVATIVE approach to avoid accidental pauses
        # Key principle: Only try to skip ONCE per ad. If it doesn't work, don't retry.
        self.auto_skip_enabled = True  # Enable auto-skip (fixed: now properly detects countdown)
        self.skip_available = False  # True when "Skip" button is ready (no countdown)
        self.skip_countdown = None   # Current countdown value (for tracking transitions)
        self.last_skip_countdown = None  # Previous countdown value (for detecting 1->0 transition)
        self.last_skip_text = None   # The detected skip text
        self.skip_attempted_this_ad = False  # Have we already tried to skip this ad?
        self.last_skip_attempt_time = 0  # When we last attempted a skip
        self.SKIP_ATTEMPT_COOLDOWN = 10.0  # Don't try again for 10s after ANY attempt (prevents pause spam)

        # Accidental pause detection
        self.blocking_end_time = 0  # When blocking last ended
        self.PAUSE_DETECT_WINDOW = 1.5  # Window after skip to detect accidental pause
        self.accidental_pause_detected = False

        # Probe DRM output to auto-detect connector, plane, resolution, and audio device
        if config.drm_connector_id is None or config.drm_plane_id is None or config.output_width is None or config.audio_playback_device is None:
            logger.info("Probing DRM output for connected HDMI display...")
            drm_info = probe_drm_output()
            if drm_info['connector_id'] is not None:
                if config.drm_connector_id is None:
                    config.drm_connector_id = drm_info['connector_id']
                if config.drm_plane_id is None:
                    config.drm_plane_id = drm_info['plane_id']
                if config.output_width is None:
                    config.output_width = drm_info['width']
                if config.output_height is None:
                    config.output_height = drm_info['height']
                if config.audio_playback_device is None:
                    config.audio_playback_device = drm_info['audio_device']
                logger.info(f"DRM output: connector={config.drm_connector_id}, plane={config.drm_plane_id}, "
                           f"resolution={config.output_width}x{config.output_height}, "
                           f"audio={config.audio_playback_device}")
            else:
                # Fallback to defaults if no display detected
                logger.warning("No HDMI output detected, using defaults")
                config.drm_connector_id = config.drm_connector_id or 215
                config.drm_plane_id = config.drm_plane_id or 72
                config.output_width = config.output_width or 1920
                config.output_height = config.output_height or 1080
                config.audio_playback_device = config.audio_playback_device or 'hw:0,0'

        # Initialize OCR
        if HAS_OCR:
            det_model, rec_model, dict_path = self._find_model_paths()
            if det_model:
                self.ocr = PaddleOCR(
                    det_model_path=det_model,
                    rec_model_path=rec_model,
                    dict_path=dict_path
                )
                if self.ocr.load_models():
                    logger.info("OCR models loaded successfully")
                else:
                    self.ocr = None
                    logger.warning("Failed to load OCR models")

        # Initialize ad blocker (manages display pipeline with input-selector)
        if HAS_ADBLOCKER:
            try:
                self.ad_blocker = AdBlocker(
                    connector_id=config.drm_connector_id,
                    plane_id=config.drm_plane_id,
                    minus_instance=self,
                    ustreamer_port=config.ustreamer_port,
                    output_width=config.output_width,
                    output_height=config.output_height
                )
                logger.info("AdBlocker initialized (instant input-selector switching)")
            except Exception as e:
                logger.exception(f"AdBlocker init failed: {e}")

        # Initialize VLM
        if HAS_VLM:
            try:
                self.vlm = VLMManager()
                logger.info("VLM manager initialized")
            except Exception as e:
                logger.warning(f"VLM init failed: {e}")
                self.vlm = None

        # Initialize Audio passthrough
        if HAS_AUDIO:
            try:
                self.audio = AudioPassthrough(
                    capture_device=config.audio_capture_device,  # HDMI-RX audio (hw:4,0)
                    playback_device=config.audio_playback_device  # HDMI-TX audio (auto-detected)
                )
                # Link audio to ad_blocker for mute control
                if self.ad_blocker:
                    self.ad_blocker.set_audio(self.audio)
                logger.info(f"Audio passthrough initialized ({config.audio_capture_device} -> {config.audio_playback_device})")
            except Exception as e:
                logger.warning(f"Audio init failed: {e}")
                self.audio = None

        # Initialize Health Monitor
        if HAS_HEALTH:
            try:
                self.health_monitor = HealthMonitor(self, check_interval=5.0)
                self.health_monitor.on_hdmi_lost(self._on_hdmi_lost)
                self.health_monitor.on_hdmi_restored(self._on_hdmi_restored)
                self.health_monitor.on_ustreamer_stall(self._restart_ustreamer)
                self.health_monitor.on_video_pipeline_stall(self._on_video_pipeline_stall)
                self.health_monitor.on_vlm_failure(self._handle_vlm_failure)
                self.health_monitor.on_memory_critical(self._handle_memory_critical)
                logger.info("Health monitor initialized")
            except Exception as e:
                logger.warning(f"Health monitor init failed: {e}")
                self.health_monitor = None

        # Initialize System Notification overlay (for VLM status, etc.)
        self.system_notification = None
        if HAS_OVERLAY:
            try:
                self.system_notification = SystemNotification(ustreamer_port=config.ustreamer_port)
                logger.info("System notification overlay initialized")
            except Exception as e:
                logger.warning(f"System notification init failed: {e}")
                self.system_notification = None

    def _find_model_paths(self):
        """Find PaddleOCR model paths."""
        search_paths = [
            Path(__file__).parent / 'models' / 'paddleocr',
            Path('/home/radxa/rknn-llm/examples/multimodal_model_demo/deploy/install/demo_Linux_aarch64/models/paddleocr'),
        ]

        for base_path in search_paths:
            det_model = list(base_path.glob('ppocrv3_det_*.rknn'))
            rec_model = list(base_path.glob('ppocrv3_rec_*.rknn'))
            dict_path = base_path / 'ppocr_keys_v1.txt'

            if det_model and rec_model and dict_path.exists():
                logger.info(f"Found OCR models at: {base_path}")
                return str(det_model[0]), str(rec_model[0]), str(dict_path)

        return None, None, None

    # ===== Health Recovery Methods =====

    def _on_hdmi_lost(self):
        """Handle HDMI signal loss."""
        logger.warning("[Recovery] HDMI signal lost - showing NO SIGNAL display")
        # Pause detection workers to prevent memory leak from repeated snapshot timeouts
        self._hdmi_signal_lost = True
        # Switch to standalone NO SIGNAL display (doesn't depend on ustreamer)
        if self.ad_blocker:
            self.ad_blocker.start_no_signal_mode()
        if self.audio:
            # Pause watchdog to prevent restart loops (source is unavailable)
            self.audio.pause_watchdog()
            self.audio.mute()

    def _on_hdmi_restored(self):
        """Handle HDMI signal restoration."""
        logger.info("[Recovery] HDMI signal restored - showing loading screen")

        # Resume detection workers
        self._hdmi_signal_lost = False

        # Set flag to prevent main loop from interfering with recovery
        self._hdmi_recovery_in_progress = True

        try:
            # Switch to loading display while we restart everything
            if self.ad_blocker:
                self.ad_blocker.start_loading_mode()

            # Restart ustreamer first to pick up new signal
            self._restart_ustreamer()

            # Wait for ustreamer to be fully ready before restarting video pipeline
            time.sleep(2)

            # Start the video pipeline (will transition from loading to live)
            if self.ad_blocker:
                logger.info("[Recovery] Starting video pipeline...")
                self.ad_blocker.start()

            if self.audio:
                # Resume watchdog and restart pipeline (source is available again)
                self.audio.resume_watchdog()
                self.audio.unmute()

            logger.info("[Recovery] HDMI recovery complete")
        finally:
            self._hdmi_recovery_in_progress = False

    def _on_video_pipeline_stall(self):
        """Handle video pipeline stall detected by health monitor."""
        logger.warning("[Recovery] Video pipeline stall detected - showing loading and restarting")
        if self.ad_blocker:
            # Show loading while we restart the pipeline
            self.ad_blocker.start_loading_mode()
            # Start will transition from loading to live
            self.ad_blocker.start()

    def _restart_ustreamer(self):
        """Restart ustreamer process."""
        logger.warning("[Recovery] Restarting ustreamer...")
        try:
            # Kill existing ustreamer
            if self.ustreamer_process:
                self.ustreamer_process.terminate()
                try:
                    self.ustreamer_process.wait(timeout=2)
                except:
                    self.ustreamer_process.kill()

            subprocess.run(['pkill', '-9', 'ustreamer'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Also kill anything on the port
            subprocess.run(['fuser', '-k', f'{self.config.ustreamer_port}/tcp'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)

            # Re-probe the device in case format changed
            device_info = probe_v4l2_device(self.device)
            video_format = device_info.get('ustreamer_format') or getattr(self, '_detected_format', 'NV12')
            resolution = f"{device_info.get('width') or 3840}x{device_info.get('height') or 2160}"

            # Restart with patched ustreamer using detected format and MPP encoder
            port = self.config.ustreamer_port
            ustreamer_cmd = [
                '/home/radxa/ustreamer-patched',
                f'--device={self.device}',
                f'--format={video_format}',
                f'--resolution={resolution}',
                '--persistent',
                f'--port={port}',
                '--host=0.0.0.0',          # Bind to all interfaces for remote access
                '--encoder=mpp-jpeg',      # Use RK3588 VPU hardware encoding
                '--encode-scale=4k',       # Native 4K output (no downscaling)
                '--quality=80',
                '--workers=4',             # 4 parallel MPP encoders (optimal)
                '--buffers=5',
            ]

            logger.info(f"[Recovery] Starting ustreamer: {' '.join(ustreamer_cmd)}")

            self.ustreamer_process = subprocess.Popen(
                ustreamer_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            time.sleep(2)

            if self.ustreamer_process.poll() is None:
                logger.info("[Recovery] ustreamer restarted successfully")
                # Also restart video pipeline to reconnect to new ustreamer
                if self.ad_blocker:
                    logger.info("[Recovery] Also restarting video pipeline...")
                    self.ad_blocker.restart()
            else:
                logger.error("[Recovery] ustreamer failed to restart")

        except Exception as e:
            logger.error(f"[Recovery] Error restarting ustreamer: {e}")

    def _handle_vlm_failure(self):
        """Handle VLM consecutive failures - degrade to OCR-only mode."""
        if not self.vlm_disabled:
            self.vlm_disabled = True
            logger.warning("[Recovery] VLM disabled due to failures - running OCR-only mode")
            # Try to restart VLM in background
            threading.Thread(target=self._try_restart_vlm, daemon=True).start()

    def _try_restart_vlm(self):
        """Attempt to restart VLM after a delay."""
        time.sleep(30)  # Wait 30 seconds before retry

        if self.vlm and not self.vlm.is_ready:
            logger.info("[Recovery] Attempting VLM restart...")
            try:
                self.vlm.release()
                time.sleep(2)
                if self.vlm.load_model():
                    self.vlm_disabled = False
                    self.vlm_consecutive_timeouts = 0
                    logger.info("[Recovery] VLM restarted successfully")
                else:
                    logger.warning("[Recovery] VLM restart failed - staying in OCR-only mode")
            except Exception as e:
                logger.error(f"[Recovery] VLM restart error: {e}")

    def _handle_memory_critical(self):
        """Handle critical memory usage."""
        logger.warning("[Recovery] Critical memory usage - cleaning up")
        # Force garbage collection
        import gc
        gc.collect()

        # Clear frame buffers
        self.prev_frame = None
        self.vlm_prev_frame = None

        # Clear old screenshots beyond minimum
        try:
            if self.screenshot_manager:
                # Clean up all screenshot subdirectories
                for subdir in ['ads', 'non_ads', 'vlm_spastic', 'static']:
                    dir_path = self.screenshot_manager.base_dir / subdir
                    if dir_path.exists():
                        screenshots = sorted(
                            dir_path.glob("*.png"),
                            key=lambda p: p.stat().st_mtime
                        )
                        # Keep only last 10 in emergency
                        for old_file in screenshots[:-10]:
                            old_file.unlink()
                            logger.debug(f"[Recovery] Deleted {old_file.name}")
        except Exception as e:
            logger.error(f"[Recovery] Error cleaning screenshots: {e}")

    # ===== Fire TV Setup Methods =====

    def _start_fire_tv_setup_delayed(self, delay_seconds: float = 15.0):
        """Start Fire TV setup after a delay (to let display stabilize first)."""
        if not HAS_FIRE_TV:
            logger.info("[FireTV] Fire TV module not available")
            return

        def delayed_start():
            logger.info(f"[FireTV] Waiting {delay_seconds}s before starting Fire TV setup...")
            time.sleep(delay_seconds)

            if not self.running:
                return

            logger.info("[FireTV] Initializing Fire TV setup manager...")
            self.fire_tv_setup = FireTVSetupManager(
                ad_blocker=self.ad_blocker,
                ocr_worker=self.ocr,
                ustreamer_port=self.config.ustreamer_port
            )

            # Set callbacks
            self.fire_tv_setup.set_callbacks(
                on_state_change=self._on_fire_tv_state_change,
                on_connected=self._on_fire_tv_connected
            )

            # Start the setup flow
            self.fire_tv_setup.start_setup()

        self._fire_tv_setup_thread = threading.Thread(
            target=delayed_start,
            daemon=True,
            name="FireTVSetupDelay"
        )
        self._fire_tv_setup_thread.start()

    def _on_fire_tv_state_change(self, new_state: str):
        """Handle Fire TV setup state changes."""
        logger.info(f"[FireTV] State changed to: {new_state}")

        # If we're waiting for auth, hook into OCR to detect the dialog
        if new_state == FireTVSetupManager.STATE_WAITING_AUTH:
            logger.info("[FireTV] Waiting for ADB authorization - OCR will detect dialog")

    def _on_fire_tv_connected(self, device_info: dict):
        """Handle successful Fire TV connection."""
        self.fire_tv_controller = self.fire_tv_setup.get_controller()

        manufacturer = device_info.get('manufacturer', 'Fire TV')
        model = device_info.get('model', '')
        ip = device_info.get('ip', '')

        logger.info(f"[FireTV] Connected to {manufacturer} {model} at {ip}")
        logger.info("[FireTV] Ad skipping enabled - will send skip commands during ads")

        # Add to detection history
        self.add_detection('FireTV', [f"Connected to {manufacturer} {model}"])

    def _check_ocr_for_fire_tv_dialog(self, ocr_results: list) -> bool:
        """
        Check OCR results for Fire TV ADB auth dialog.

        This is called from the OCR worker when Fire TV is waiting for authorization.
        If we detect the auth dialog, we can provide better guidance.

        Returns:
            True if auth dialog detected
        """
        if not self.fire_tv_setup:
            return False

        if self.fire_tv_setup.state != FireTVSetupManager.STATE_WAITING_AUTH:
            return False

        return self.fire_tv_setup.check_for_auth_dialog(ocr_results)

    def try_skip_ad(self):
        """Attempt to skip ad on Fire TV if connected."""
        if self.fire_tv_controller and self.fire_tv_controller.is_connected():
            try:
                result = self.fire_tv_controller.skip_ad()
                if result:
                    logger.info("[FireTV] Sent skip command")
                return result
            except Exception as e:
                logger.warning(f"[FireTV] Skip command failed: {e}")
        return False

    # ===== Web UI Methods =====

    def pause_blocking(self, duration_seconds: int = 120):
        """Pause ad blocking for specified duration."""
        with self._state_lock:
            self.blocking_paused_until = time.time() + duration_seconds
            logger.info(f"[WebUI] Blocking paused for {duration_seconds}s")

        # Capture non-ad screenshot for future VLM training
        # This helps collect examples of content that should NOT be classified as ads
        if self.frame_capture:
            frame = self.frame_capture.capture()
            self.screenshot_manager.save_non_ad_screenshot(frame)

        # Immediately hide blocking overlay and unmute
        if self.ad_blocker:
            self.ad_blocker.hide()
        if self.audio:
            self.audio.unmute()

    def resume_blocking(self):
        """Resume ad blocking immediately."""
        with self._state_lock:
            self.blocking_paused_until = 0
            logger.info("[WebUI] Blocking resumed")

        # Re-evaluate current state
        self._update_blocking_state()

    def is_blocking_paused(self) -> bool:
        """Check if blocking is currently paused."""
        return time.time() < self.blocking_paused_until

    def get_pause_remaining(self) -> int:
        """Get seconds remaining in pause, or 0 if not paused."""
        remaining = self.blocking_paused_until - time.time()
        return max(0, int(remaining))

    def add_detection(self, source: str, texts: list, matched_keywords: list = None):
        """Add a detection to history for web UI display."""
        from datetime import datetime
        self.detection_history.append({
            'time': datetime.now().strftime('%H:%M:%S'),
            'timestamp': time.time(),
            'source': source,
            'texts': texts[:5] if texts else [],  # Limit to first 5 texts
            'keywords': [kw for kw, _ in matched_keywords] if matched_keywords else [],
        })

    def get_status_dict(self) -> dict:
        """Get current status as dictionary for web API."""
        # Get health status if available
        health_status = None
        if self.health_monitor:
            try:
                health_status = self.health_monitor.get_status()
            except Exception:
                pass

        # Get FPS from ad_blocker if available
        fps = 0
        if self.ad_blocker:
            try:
                fps = self.ad_blocker.get_fps()
            except Exception:
                pass

        uptime = int(time.time() - self.start_time)

        # Check if blocking is active (either via detection or test mode)
        is_blocking = (self.ad_detected and not self.is_blocking_paused() and not self.static_blocking_suppressed)
        # Also check if ad_blocker is directly visible (test mode)
        if self.ad_blocker and self.ad_blocker.is_visible:
            is_blocking = True

        return {
            # Blocking state
            'blocking': is_blocking,
            'blocking_source': self.blocking_source,
            'paused': self.is_blocking_paused(),
            'pause_remaining': self.get_pause_remaining(),
            'static_suppressed': self.static_blocking_suppressed,

            # Detection counts
            'ocr_detected': self.ocr_ad_detected,
            'vlm_detected': self.vlm_ad_detected,
            'ocr_frame_count': self.frame_count,
            'vlm_frame_count': self.vlm_frame_count,
            'total_detections': self.screenshot_manager.ads_count if self.screenshot_manager else 0,

            # System status
            'fps': fps,
            'uptime': uptime,
            'uptime_str': f"{uptime // 3600}h {(uptime % 3600) // 60}m",
            'hdmi_signal': health_status.hdmi_signal if health_status else True,
            'vlm_ready': not self.vlm_disabled and (self.vlm is not None and self.vlm.is_ready if self.vlm else False),
            'vlm_disabled': self.vlm_disabled,

            # Memory/health
            'memory_percent': health_status.memory_percent if health_status else 0,
            'ustreamer_ok': health_status.ustreamer_responding if health_status else True,
            'video_ok': health_status.video_pipeline_ok if health_status else True,

            # Skip status (Fire TV integration)
            'skip_available': self.skip_available,
            'skip_countdown': self.skip_countdown,
            'skip_text': self.last_skip_text,
            'skip_attempted': self.skip_attempted_this_ad,

            # Fire TV status
            'fire_tv_connected': self.fire_tv_controller is not None and self.fire_tv_controller.is_connected() if self.fire_tv_controller else False,
            'fire_tv_setup_state': self.fire_tv_setup.state if self.fire_tv_setup else None,
        }

    def _compare_frames(self, frame, prev_frame):
        """Compare two frames and return normalized mean difference (0-1)."""
        if frame is None or prev_frame is None:
            return 1.0

        try:
            curr = cv2.resize(frame, (160, 90))
            prev = cv2.resize(prev_frame, (160, 90))
            curr_gray = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
            prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
            diff = cv2.absdiff(curr_gray, prev_gray)
            return diff.mean() / 255.0
        except Exception:
            return 1.0

    def is_scene_changed(self, frame):
        """Check if scene changed (should run OCR)."""
        if self.prev_frame is None:
            return True
        return self._compare_frames(frame, self.prev_frame) > self.scene_change_threshold

    def is_vlm_scene_changed(self, frame):
        """Check if scene changed (should run VLM)."""
        if self.vlm_prev_frame is None:
            return True
        return self._compare_frames(frame, self.vlm_prev_frame) > self.scene_change_threshold

    def _add_vlm_decision(self, is_ad: bool, confidence: float = 0.75):
        """Add a VLM decision to the sliding window history with confidence."""
        now = time.time()
        self.vlm_decision_history.append((now, is_ad, confidence))

        # Prune old decisions outside the window
        cutoff = now - self.vlm_history_window
        self.vlm_decision_history = [
            entry for entry in self.vlm_decision_history
            if entry[0] >= cutoff
        ]

    def _is_transition_frame(self, frame, threshold=15, black_threshold=30, uniformity_threshold=0.95) -> tuple:
        """
        Detect if a frame is a transition screen (mostly black or single solid color).

        These frames often appear between ads or between an ad and content.
        When blocking, we should hold through these rather than unblocking.

        Args:
            frame: BGR image (numpy array)
            threshold: Max std dev to consider "uniform" color
            black_threshold: Max brightness to consider "black"
            uniformity_threshold: Min fraction of pixels that must be similar

        Returns:
            (is_transition, reason) - reason is 'black', 'solid_color', or None
        """
        try:
            import numpy as np

            if frame is None or frame.size == 0:
                return False, None

            # Convert to grayscale for analysis
            if len(frame.shape) == 3:
                gray = np.mean(frame, axis=2)
            else:
                gray = frame

            mean_brightness = np.mean(gray)
            std_brightness = np.std(gray)

            # Check if mostly black (common ad transition)
            if mean_brightness < black_threshold and std_brightness < threshold:
                return True, 'black'

            # Check if solid/uniform color (fade transitions)
            if std_brightness < threshold:
                return True, 'solid_color'

            # Check if most pixels are very similar (near-uniform with minor noise)
            median_val = np.median(gray)
            similar_pixels = np.sum(np.abs(gray - median_val) < 20) / gray.size
            if similar_pixels > uniformity_threshold:
                return True, 'uniform'

            return False, None

        except Exception as e:
            logger.debug(f"Transition detection error: {e}")
            return False, None

    def _get_vlm_agreement(self) -> tuple:
        """
        Calculate VLM agreement percentage from sliding window using confidence-weighted votes.

        High-confidence decisions count more than low-confidence ones.

        Returns:
            (ad_ratio, no_ad_ratio, total_decisions)
            - ad_ratio: confidence-weighted fraction of 'ad' decisions (0.0-1.0)
            - no_ad_ratio: confidence-weighted fraction of 'no-ad' decisions (0.0-1.0)
            - total_decisions: number of decisions in window
        """
        if not self.vlm_decision_history:
            return 0.0, 0.0, 0

        now = time.time()
        cutoff = now - self.vlm_history_window

        # Filter to recent decisions (handle both old and new tuple formats)
        recent = []
        for entry in self.vlm_decision_history:
            if entry[0] >= cutoff:
                if len(entry) == 3:
                    recent.append(entry)  # (time, is_ad, confidence)
                else:
                    # Legacy format without confidence - use default 0.75
                    recent.append((entry[0], entry[1], 0.75))

        if not recent:
            return 0.0, 0.0, 0

        total = len(recent)

        # Confidence-weighted voting
        ad_weight = sum(conf for _, is_ad, conf in recent if is_ad)
        no_ad_weight = sum(conf for _, is_ad, conf in recent if not is_ad)
        total_weight = ad_weight + no_ad_weight

        if total_weight == 0:
            return 0.0, 0.0, total

        return ad_weight / total_weight, no_ad_weight / total_weight, total

    def _should_vlm_start_blocking(self) -> bool:
        """
        Determine if VLM should trigger blocking based on sliding window agreement.

        Uses hysteresis: if we're NOT currently blocking, we need higher agreement to START.
        """
        ad_ratio, _, total = self._get_vlm_agreement()

        if total < self.vlm_min_decisions:
            return False  # Not enough data

        # Check cooldown
        now = time.time()
        if self.vlm_cooldown_active:
            time_since_change = now - self.vlm_last_state_change
            if time_since_change < self.vlm_min_state_duration:
                return False  # Still in cooldown

        # Need strong ad agreement to start
        threshold = self.vlm_start_agreement
        if not self.vlm_ad_detected:
            # Not currently detecting - need even stronger evidence to start
            threshold += self.vlm_hysteresis_boost

        return ad_ratio >= threshold

    def _should_vlm_stop_blocking(self) -> bool:
        """
        Determine if VLM should stop blocking based on sliding window agreement.

        Uses hysteresis: if we ARE currently blocking, we need higher agreement to STOP.
        """
        _, no_ad_ratio, total = self._get_vlm_agreement()

        if total < self.vlm_min_decisions:
            return False  # Not enough data - keep current state

        # Check cooldown
        now = time.time()
        if self.vlm_cooldown_active:
            time_since_change = now - self.vlm_last_state_change
            if time_since_change < self.vlm_min_state_duration:
                return False  # Still in cooldown

        # Need strong no-ad agreement to stop
        threshold = self.vlm_stop_agreement
        if self.vlm_ad_detected:
            # Currently detecting - need even stronger evidence to stop
            threshold += self.vlm_hysteresis_boost

        return no_ad_ratio >= threshold

    def check_hdmi_signal(self):
        """Check HDMI signal and return resolution."""
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', self.device, '--query-dv-timings'],
                capture_output=True, text=True
            )

            if result.returncode != 0:
                return None

            width = height = fps = 0
            for line in result.stdout.split('\n'):
                if 'Active width:' in line:
                    width = int(line.split(':')[1].strip())
                elif 'Active height:' in line:
                    height = int(line.split(':')[1].strip())
                elif 'frames per second' in line:
                    match = re.search(r'\((\d+\.?\d*) frames', line)
                    if match:
                        fps = float(match.group(1))

            if width and height:
                return (width, height, fps)
        except Exception as e:
            logger.error(f"Signal check error: {e}")

        return None

    def _init_v4l2_device(self):
        """Initialize V4L2 device with proper DV timings before starting ustreamer.

        The RK3588 HDMI-RX driver requires DV timings to be set before format
        configuration. Without this, some HDMI sources fail with format mismatch errors.
        """
        try:
            # Step 1: Query current DV timings from the source
            logger.info(f"Querying DV timings from {self.device}...")
            result = subprocess.run(
                ['v4l2-ctl', '-d', self.device, '--query-dv-timings'],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode != 0:
                logger.warning(f"Failed to query DV timings: {result.stderr}")
                return False

            # Parse resolution from output for logging
            width = height = 0
            for line in result.stdout.split('\n'):
                if 'Active width:' in line:
                    width = int(line.split(':')[1].strip())
                elif 'Active height:' in line:
                    height = int(line.split(':')[1].strip())

            logger.info(f"Detected input: {width}x{height}")

            # Step 2: Set the DV timings from query (this is the critical step)
            logger.info("Setting DV timings on device...")
            result = subprocess.run(
                ['v4l2-ctl', '-d', self.device, '--set-dv-bt-timings', 'query'],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode != 0:
                logger.warning(f"Failed to set DV timings: {result.stderr}")
                return False

            # Give the driver time to stabilize after timing change
            time.sleep(0.3)

            # Step 3: Explicitly set pixel format to BGR3
            # This ensures the device is not stuck in a different format (e.g., NV12)
            # which would cause ustreamer's format negotiation to fail
            logger.info("Setting pixel format to BGR3...")
            result = subprocess.run(
                ['v4l2-ctl', '-d', self.device, '--set-fmt-video', 'pixelformat=BGR3'],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode != 0:
                # Not fatal - ustreamer may still work
                logger.warning(f"Failed to set pixel format: {result.stderr}")

            time.sleep(0.2)

            logger.info("V4L2 device initialized successfully")
            return True

        except subprocess.TimeoutExpired:
            logger.warning("Timeout during V4L2 device initialization")
            return False
        except Exception as e:
            logger.warning(f"V4L2 device initialization error: {e}")
            return False

    def start_display(self):
        """Start ustreamer and display pipeline."""
        # Kill any existing processes
        subprocess.run(['pkill', '-9', 'ustreamer'], capture_output=True)
        subprocess.run(['pkill', '-9', 'gst-launch'], capture_output=True)
        time.sleep(0.5)

        port = self.config.ustreamer_port

        # Probe the device to get current format and resolution
        device_info = probe_v4l2_device(self.device)

        # Use detected format or fall back to defaults
        video_format = device_info.get('ustreamer_format') or 'NV12'
        width = device_info.get('width') or 3840
        height = device_info.get('height') or 2160

        # Store for later reference (health monitor, recovery)
        self._detected_format = video_format
        self._detected_resolution = f"{width}x{height}"

        # Start ustreamer with detected format and MPP hardware encoder
        ustreamer_cmd = [
            '/home/radxa/ustreamer-patched',
            f'--device={self.device}',
            f'--format={video_format}',
            f'--resolution={width}x{height}',
            '--persistent',
            f'--port={port}',
            '--host=0.0.0.0',          # Bind to all interfaces for remote access
            '--encoder=mpp-jpeg',      # Use RK3588 VPU hardware encoding
            '--encode-scale=4k',       # Native 4K output (no downscaling)
            '--quality=80',
            '--workers=4',             # 4 parallel MPP encoders (optimal)
            '--buffers=5',
        ]

        logger.info(f"Starting ustreamer: {' '.join(ustreamer_cmd)}")

        # Clean up any stale resources from previous runs
        subprocess.run(['fuser', '-k', f'{port}/tcp'],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Remove stale frame files that might be owned by root (use glob for PID-based names)
        stale_patterns = ['minus_frame*.jpg', 'minus_vlm_frame*.jpg']
        for pattern in stale_patterns:
            for f in Path('/dev/shm').glob(pattern):
                try:
                    f.unlink(missing_ok=True)
                except PermissionError:
                    # File owned by root, use sudo fallback
                    subprocess.run(['sudo', 'rm', '-f', str(f)], capture_output=True)
        time.sleep(0.5)

        self.ustreamer_process = subprocess.Popen(
            ustreamer_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        time.sleep(2)

        if self.ustreamer_process.poll() is not None:
            logger.error("ustreamer failed to start")
            return False

        logger.info(f"ustreamer started on port {port}")

        # Initialize frame capture
        self.frame_capture = UstreamerCapture(port=port)

        # Start the display pipeline (managed by ad_blocker)
        if self.ad_blocker:
            # Wait if pipeline is currently being restarted (avoid race condition)
            if hasattr(self.ad_blocker, '_pipeline_restarting') and self.ad_blocker._pipeline_restarting:
                logger.info("Pipeline restart in progress, waiting...")
                for _ in range(10):  # Wait up to 10 seconds
                    time.sleep(1)
                    if not self.ad_blocker._pipeline_restarting:
                        break
                if self.ad_blocker._pipeline_restarting:
                    logger.warning("Pipeline still restarting after 10s")
                    return False

            logger.info("Starting display pipeline...")
            if self.ad_blocker.start():
                logger.info("Display pipeline started - 30 FPS with instant ad blocking")

                # Start audio passthrough
                if self.audio:
                    if self.audio.start():
                        logger.info("Audio passthrough started")
                    else:
                        logger.warning("Audio passthrough failed to start")

                return True
            else:
                logger.error("Display pipeline failed to start")
                return False

        logger.error("No ad_blocker available")
        return False

    def _update_blocking_state(self):
        """Update combined blocking state using weighted OCR/VLM model."""
        with self._state_lock:
            now = time.time()
            ocr_recent = (now - self.last_ocr_ad_time) < self.OCR_TRUST_WINDOW

            # Starting blocking
            if not self.ad_detected:
                should_start = False
                source = None

                if self.ocr_ad_detected:
                    # OCR is primary - always trust it immediately
                    should_start = True
                    source = "both" if self.vlm_ad_detected else "ocr"
                elif self.vlm_ad_detected:
                    # VLM alone - vlm_ad_detected is now managed by sliding window
                    # which already requires sustained agreement before setting True
                    # BUT suppress if OCR recently detected home screen or video interface keywords
                    # ALSO suppress if screen is static (prevents blocking on paused video interfaces)
                    if self.home_screen_detected:
                        ad_ratio, _, total = self._get_vlm_agreement()
                        logger.info(f"VLM suppressed - home screen detected (OCR cross-validation). Agreement was {ad_ratio*100:.0f}% of {total}")
                    elif self.video_interface_detected:
                        ad_ratio, _, total = self._get_vlm_agreement()
                        logger.info(f"VLM suppressed - video interface detected (prevents false positive on player UI). Agreement was {ad_ratio*100:.0f}% of {total}")
                    elif self.static_blocking_suppressed:
                        ad_ratio, _, total = self._get_vlm_agreement()
                        logger.info(f"VLM suppressed - static screen detected (prevents false positive on paused content). Agreement was {ad_ratio*100:.0f}% of {total}")
                    else:
                        should_start = True
                        source = "vlm"
                        ad_ratio, _, total = self._get_vlm_agreement()
                        logger.info(f"VLM triggered alone (agreement: {ad_ratio*100:.0f}% of {total} decisions)")

                if should_start:
                    self.ad_detected = True
                    self.blocking_start_time = now
                    self.blocking_source = source
                    # Reset skip and pause detection for new ad
                    self.accidental_pause_detected = False
                    self.skip_attempted_this_ad = False
                    self.last_skip_countdown = None
                    source_display = source.upper() if source != "both" else "OCR+VLM"
                    logger.warning(f"AD BLOCKING STARTED ({source_display})")

                    # NOTE: Ad skipping is handled separately based on skip button detection
                    # We only skip when "Skip" appears without countdown (handled in OCR worker)

            # While blocking
            elif self.ad_detected:
                if self.ocr_ad_detected and self.vlm_ad_detected and self.blocking_source != "both":
                    self.blocking_source = "both"

                blocking_elapsed = now - self.blocking_start_time
                should_stop = False

                if blocking_elapsed >= self.MIN_BLOCKING_DURATION:
                    ocr_says_stop = (self.ocr_no_ad_count >= self.OCR_STOP_THRESHOLD)
                    # For VLM stopping, use consecutive no-ad count (not sliding window)
                    # This ensures responsive stopping after ad ends
                    vlm_says_stop = (self.vlm_no_ad_count >= self.VLM_STOP_THRESHOLD)

                    if self.blocking_source == "vlm":
                        # VLM triggered alone - VLM must also agree to stop
                        # (OCR never detected the ad, so OCR's opinion is unreliable here)
                        # Use simple consecutive count, not sliding window (for responsiveness)
                        should_stop = vlm_says_stop

                        # SAFEGUARD: Auto-stop VLM-only blocking after 90 seconds
                        # This prevents extended false positives on video interfaces
                        # Real ads rarely last more than 60-90 seconds
                        if blocking_elapsed >= 90.0 and not should_stop:
                            logger.warning(f"[VLM] Auto-stopping VLM-only blocking after {blocking_elapsed:.0f}s (safeguard)")
                            should_stop = True
                    else:
                        # OCR triggered (alone or with VLM) - OCR is authoritative for stopping
                        # This ensures we don't block longer than necessary after ad ends
                        should_stop = ocr_says_stop

                if should_stop:
                    self.ad_detected = False
                    source_was = self.blocking_source
                    self.blocking_source = None
                    # Also clear VLM state so it doesn't immediately re-trigger
                    self.vlm_ad_detected = False
                    self.vlm_decision_history.clear()
                    # Track when blocking ended (for accidental pause detection)
                    self.blocking_end_time = time.time()
                    # Reset skip state for next ad
                    self.skip_available = False
                    self.skip_attempted_this_ad = False
                    self.last_skip_countdown = None
                    self.skip_countdown = None
                    logger.warning(f"AD BLOCKING ENDED after {blocking_elapsed:.1f}s (stopped by {source_was.upper() if source_was else 'unknown'})")

            # Update overlay (respect pause state and static screen suppression)
            # Static screen suppression: don't block still ads (paused video, landing pages)
            # so user can interact with UI
            should_show_blocking = (
                self.ad_detected and
                self.blocking_source and
                not self.is_blocking_paused() and
                not self.static_blocking_suppressed
            )

            if self.ad_blocker:
                if should_show_blocking:
                    self.ad_blocker.show(self.blocking_source)
                else:
                    self.ad_blocker.hide()

            # Also control audio based on blocking state (same logic)
            # But respect ad_blocker test mode - don't unmute during tests
            if self.audio:
                if should_show_blocking:
                    self.audio.mute()
                elif not (self.ad_blocker and self.ad_blocker.is_test_mode_active()):
                    self.audio.unmute()

    def ml_worker(self):
        """OCR processing thread."""
        # Lower priority so video passthrough takes precedence
        try:
            os.nice(10)  # Higher nice = lower priority
        except OSError:
            pass  # May fail without permissions
        logger.info("OCR worker thread started")
        time.sleep(2)

        if self.frame_capture is None:
            logger.error("Frame capture not initialized")
            return

        logger.info(f"Using HTTP snapshot at {self.frame_capture.snapshot_url}")

        # Create a single ThreadPoolExecutor for OCR timeout handling
        # CRITICAL: Creating this inside the loop caused massive memory/FD leak!
        ocr_executor = ThreadPoolExecutor(max_workers=1)

        while self.running:
            try:
                # Pause when HDMI signal is lost to prevent memory leak from repeated timeouts
                if self._hdmi_signal_lost:
                    time.sleep(1.0)
                    continue

                start_time = time.time()
                frame = self.frame_capture.capture()
                capture_time = (time.time() - start_time) * 1000

                if frame is None:
                    time.sleep(0.5)
                    continue

                self.frame_count += 1

                # Scene change detection (with max skip cap to catch missed ads)
                scene_changed = self.is_scene_changed(frame)
                now = time.time()

                # Track static screen state for suppression of still-ad blocking
                if scene_changed:
                    # Screen became dynamic - reset static tracking
                    if self.static_blocking_suppressed and self.screen_became_dynamic_time == 0:
                        # First scene change after suppression - start cooldown
                        self.screen_became_dynamic_time = now
                        logger.info(f"[Static] Screen became dynamic - cooldown {self.DYNAMIC_COOLDOWN}s before allowing blocking")
                    self.static_since_time = 0
                    self.static_ocr_count = 0
                else:
                    # Screen is static - track duration
                    self.static_ocr_count += 1
                    if self.static_since_time == 0:
                        self.static_since_time = now

                # Check if we should suppress blocking due to static screen
                static_time = (now - self.static_since_time) if self.static_since_time > 0 else 0

                if static_time >= self.STATIC_TIME_THRESHOLD or self.static_ocr_count >= self.STATIC_OCR_THRESHOLD:
                    if not self.static_blocking_suppressed:
                        logger.info(f"[Static] Screen static for {static_time:.1f}s / {self.static_ocr_count} OCR cycles - suppressing blocking")
                        self.static_blocking_suppressed = True
                        self.screen_became_dynamic_time = 0  # Reset cooldown timer

                        # Accidental pause detection: if screen went static right after we skipped,
                        # we may have accidentally paused the video. Send PLAY to resume.
                        time_since_blocking_end = now - self.blocking_end_time if self.blocking_end_time > 0 else float('inf')
                        time_since_skip_success = now - self.last_skip_success_time if self.last_skip_success_time > 0 else float('inf')

                        if (time_since_blocking_end < self.PAUSE_DETECT_WINDOW and
                            time_since_skip_success < self.PAUSE_DETECT_WINDOW and
                            not self.accidental_pause_detected):
                            logger.warning(f"[PAUSE] Detected potential accidental pause! Screen static {time_since_blocking_end:.1f}s after blocking ended, {time_since_skip_success:.1f}s after skip. Sending PLAY...")
                            self.accidental_pause_detected = True  # Only try once per ad
                            if self.fire_tv_controller and self.fire_tv_controller.is_connected():
                                if self.fire_tv_controller.send_command("play"):
                                    logger.info("[PAUSE] PLAY command sent - video should resume")
                                else:
                                    logger.warning("[PAUSE] Failed to send PLAY command")

                        # Save screenshot as non-ad training data (still ads shouldn't be blocked)
                        if self.ad_detected:
                            self.screenshot_manager.save_static_ad_screenshot(frame)
                        # If currently blocking, hide the overlay
                        if self.ad_detected:
                            self._update_blocking_state()
                elif self.screen_became_dynamic_time > 0:
                    # In cooldown period after screen became dynamic
                    cooldown_elapsed = now - self.screen_became_dynamic_time
                    if cooldown_elapsed >= self.DYNAMIC_COOLDOWN:
                        logger.info(f"[Static] Cooldown complete - blocking re-enabled")
                        self.static_blocking_suppressed = False
                        self.screen_became_dynamic_time = 0
                elif not self.static_blocking_suppressed:
                    # Normal state - not suppressed and not in cooldown
                    pass

                # Skip OCR processing if scene unchanged (unless forced or was blocking)
                if not self.ad_detected and not scene_changed and not self.prev_frame_had_ad:
                    self.scene_skip_count += 1
                    # Cap consecutive skips to catch ads that appear without scene change
                    if self.scene_skip_count < self.max_scene_skip:
                        if self.scene_skip_count % 10 == 1:
                            logger.info(f"OCR #{self.frame_count}: SKIPPED - scene unchanged (skipped {self.scene_skip_count} total)")
                        time.sleep(0.1)
                        continue
                    else:
                        logger.debug(f"OCR #{self.frame_count}: Force run after {self.scene_skip_count} skips")

                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                # Run OCR with timeout using pre-created executor
                future = ocr_executor.submit(self.ocr.ocr, frame_rgb)
                try:
                    ocr_results = future.result(timeout=self.config.ocr_timeout)
                except FuturesTimeoutError:
                    logger.warning(f"OCR #{self.frame_count}: TIMEOUT ({self.config.ocr_timeout}s) - assuming no ad")
                    self.ocr_no_ad_count += 1
                    self.ocr_ad_detection_count = 0
                    if self.ocr_ad_detected and self.ocr_no_ad_count >= self.OCR_STOP_THRESHOLD:
                        self.ocr_ad_detected = False
                        logger.info(f"OCR: ad no longer detected (after {self.OCR_STOP_THRESHOLD} timeouts)")
                        self._update_blocking_state()
                    continue

                ocr_time = (time.time() - start_time) * 1000 - capture_time

                # Check for Fire TV ADB authorization dialog (if waiting for auth)
                if self.fire_tv_setup and ocr_results:
                    # Convert OCR results to format expected by Fire TV checker
                    ocr_text_list = ocr_results if ocr_results else []
                    if self._check_ocr_for_fire_tv_dialog(ocr_text_list):
                        logger.info("[FireTV] ADB authorization dialog detected on screen!")

                ad_detected, matched_keywords, all_texts, is_terminal = self.ocr.check_ad_keywords(ocr_results)

                # Store OCR texts and check for home screen / video interface keywords
                self.last_ocr_texts = all_texts
                if all_texts:
                    combined_text = ' '.join(all_texts).lower()

                    # Home screen detection
                    home_keywords_found = [kw for kw in self.home_screen_keywords if kw in combined_text]
                    if len(home_keywords_found) >= 2:  # Require 2+ keywords to confirm home screen
                        self.home_screen_detected = True
                        self.home_screen_detect_time = time.time()
                    elif time.time() - self.home_screen_detect_time > 5.0:  # Clear after 5s
                        self.home_screen_detected = False

                    # Video player interface detection (suppresses VLM false positives)
                    video_keywords_found = [kw for kw in self.video_interface_keywords if kw in combined_text]
                    if len(video_keywords_found) >= 2:  # Require 2+ keywords to confirm video interface
                        self.video_interface_detected = True
                        self.video_interface_detect_time = time.time()
                    elif time.time() - self.video_interface_detect_time > 5.0:  # Clear after 5s
                        self.video_interface_detected = False

                # Check for skip opportunity (for Fire TV ad skipping)
                # CONSERVATIVE APPROACH: Only try to skip ONCE per ad to avoid accidental pauses
                is_skippable, skip_text, countdown = check_skip_opportunity(all_texts)

                # Calculate time since ad blocking started
                time_since_blocking = 0
                if self.ad_detected and self.blocking_start_time > 0:
                    time_since_blocking = time.time() - self.blocking_start_time

                # Track countdown transitions (for pre-emptive skip at 1->0)
                countdown_just_hit_zero = (
                    self.last_skip_countdown is not None and
                    self.last_skip_countdown == 1 and
                    (countdown == 0 or is_skippable)
                )

                # Update countdown tracking
                if countdown is not None:
                    self.last_skip_countdown = countdown
                    self.skip_countdown = countdown
                    self.last_skip_text = skip_text
                    if self.ad_blocker:
                        # 99 = special value meaning "OCR detected 'Skip in' but missed the digit"
                        if countdown == 99:
                            self.ad_blocker.set_skip_status(False, "Skip pending...")
                        else:
                            self.ad_blocker.set_skip_status(False, f"Skip in {countdown}s")
                elif is_skippable:
                    self.skip_countdown = 0
                    self.last_skip_countdown = 0

                # Only allow skip after delay period
                skip_delay_passed = time_since_blocking >= self.SKIP_DELAY_SECONDS

                # Check cooldown since last attempt
                time_since_attempt = time.time() - self.last_skip_attempt_time
                in_cooldown = time_since_attempt < self.SKIP_ATTEMPT_COOLDOWN

                # Determine if we should try to skip
                # Conditions: skippable, delay passed, haven't tried this ad, not in cooldown
                should_skip = (
                    is_skippable and
                    skip_delay_passed and
                    not self.skip_attempted_this_ad and
                    not in_cooldown
                )

                # Also skip on countdown 1->0 transition (pre-emptive)
                if countdown_just_hit_zero and not self.skip_attempted_this_ad and not in_cooldown:
                    should_skip = True
                    logger.info("[SKIP] Countdown hit zero - attempting skip")

                if should_skip:
                    self.skip_available = True
                    self.last_skip_text = skip_text

                    # Check if auto-skip is enabled
                    if not self.auto_skip_enabled:
                        logger.info(f"[SKIP] Skip available but auto-skip DISABLED. Text: '{skip_text}'")
                        if self.ad_blocker:
                            self.ad_blocker.set_skip_status(True, "Manual skip")
                    else:
                        self.skip_attempted_this_ad = True  # Mark as attempted - NO RETRIES
                        self.last_skip_attempt_time = time.time()

                        logger.warning(f"[SKIP] >>> Attempting skip (ONE attempt only). Text: '{skip_text}'")
                        if self.ad_blocker:
                            self.ad_blocker.set_skip_status(True, "Skipping...")

                        if self.try_skip_ad():
                            logger.info(f"[SKIP] Skip command sent! Waiting to see if it worked...")
                            self.last_skip_success_time = time.time()
                            if self.ad_blocker:
                                self.ad_blocker.add_time_saved(30.0)
                        else:
                            logger.warning(f"[SKIP] Skip command failed (Fire TV not connected?)")

                elif is_skippable and not skip_delay_passed:
                    wait_remaining = int(self.SKIP_DELAY_SECONDS - time_since_blocking)
                    if wait_remaining > 0 and self.ad_blocker:
                        self.ad_blocker.set_skip_status(False, f"Wait {wait_remaining}s")

                elif is_skippable and self.skip_attempted_this_ad:
                    # Already tried - don't retry (this prevents pause spam)
                    pass

                elif is_skippable and in_cooldown:
                    remaining = int(self.SKIP_ATTEMPT_COOLDOWN - time_since_attempt)
                    logger.debug(f"[SKIP] In cooldown, {remaining}s remaining")

                elif not is_skippable and countdown is None:
                    # No skip button detected
                    if self.skip_available:
                        logger.info("[SKIP] Skip button no longer visible")
                    self.skip_available = False
                    self.skip_countdown = None
                    if self.ad_blocker:
                        self.ad_blocker.set_skip_status(False, None)

                if ad_detected and not is_terminal:
                    # Suppress OCR ad detection if home screen is detected
                    # (e.g., "Sponsored" content rows on Fire TV home are not video ads)
                    if self.home_screen_detected:
                        keywords_found = [kw for kw, txt in matched_keywords]
                        logger.info(f"OCR suppressed - home screen detected. Keywords would have been: {keywords_found}")
                    else:
                        self.ocr_ad_detection_count += 1
                        self.ocr_no_ad_count = 0
                        self.last_ocr_ad_time = time.time()

                        if self.ocr_ad_detection_count >= 1 and not self.ocr_ad_detected:
                            self.ocr_ad_detected = True
                        keywords_found = [kw for kw, txt in matched_keywords]
                        logger.info(f"OCR detected ad keywords: {keywords_found}")
                        self.screenshot_manager.save_ad_screenshot(frame, matched_keywords, all_texts)
                        self.add_detection('OCR', all_texts, matched_keywords)
                else:
                    # Check if this is a transition frame (black/solid color)
                    # If we're blocking and see a transition, hold through it
                    is_transition, transition_type = self._is_transition_frame(frame)

                    if self.ad_detected and is_transition:
                        # Don't count transition frames as "no ad" - likely between ads
                        logger.info(f"OCR #{self.frame_count}: Transition frame ({transition_type}) - holding block")
                    else:
                        self.ocr_no_ad_count += 1
                        self.ocr_ad_detection_count = 0

                        if self.ocr_ad_detected and self.ocr_no_ad_count >= self.OCR_STOP_THRESHOLD:
                            self.ocr_ad_detected = False
                            logger.info(f"OCR: ad no longer detected (after {self.OCR_STOP_THRESHOLD} no-ads)")

                self._update_blocking_state()

                # Log
                total_time = (time.time() - start_time) * 1000
                blocking_info = ""
                if self.ad_detected:
                    if self.static_blocking_suppressed:
                        blocking_info = " [AD DETECTED - STATIC SUPPRESSED]"
                    elif self.ocr_ad_detected and self.vlm_ad_detected:
                        blocking_info = " [BLOCKING OCR+VLM]"
                    elif self.ocr_ad_detected:
                        blocking_info = " [BLOCKING OCR]"
                    elif self.vlm_ad_detected:
                        blocking_info = " [BLOCKING VLM]"

                if all_texts:
                    text_preview = ' | '.join(all_texts)[:120]
                    logger.info(f"OCR #{self.frame_count}: cap={capture_time:.0f}ms ocr={ocr_time:.0f}ms{blocking_info} - {text_preview}")
                elif self.frame_count % 10 == 0:
                    logger.info(f"OCR #{self.frame_count}: cap={capture_time:.0f}ms ocr={ocr_time:.0f}ms, no text{blocking_info}")

                self.prev_frame = frame.copy()
                self.prev_frame_had_ad = ad_detected and not is_terminal
                self.scene_skip_count = 0  # Reset skip counter after processing

                # Periodic garbage collection to prevent memory leak
                if self.frame_count % 100 == 0:
                    gc.collect()

            except Exception as e:
                logger.exception(f"OCR worker error: {e}")

            time.sleep(0.1)

        logger.info("OCR worker thread stopped")
        ocr_executor.shutdown(wait=False)

    def vlm_worker(self):
        """VLM processing thread."""
        # Lower priority so video passthrough takes precedence
        try:
            os.nice(10)  # Higher nice = lower priority
        except OSError:
            pass  # May fail without permissions
        logger.info("VLM worker thread started")
        time.sleep(3)

        if self.frame_capture is None:
            logger.error("Frame capture not initialized for VLM")
            return

        if not self.vlm or not self.vlm.is_ready:
            logger.error("VLM not ready")
            return

        vlm_image_path = f'/dev/shm/minus_vlm_frame_{os.getpid()}.jpg'

        while self.running:
            try:
                # Pause when HDMI signal is lost to prevent memory leak from repeated timeouts
                if self._hdmi_signal_lost:
                    time.sleep(1.0)
                    continue

                start_time = time.time()
                frame = self.frame_capture.capture()

                if frame is None:
                    time.sleep(0.5)
                    continue

                self.vlm_frame_count += 1

                # Scene change detection (with max skip cap)
                if not self.ad_detected and not self.is_vlm_scene_changed(frame) and not self.vlm_prev_frame_had_ad:
                    self.vlm_scene_skip_count += 1
                    # Cap consecutive skips to catch ads
                    if self.vlm_scene_skip_count < self.vlm_max_scene_skip:
                        if self.vlm_scene_skip_count % 10 == 1:
                            logger.info(f"VLM #{self.vlm_frame_count}: SKIPPED - scene unchanged (skipped {self.vlm_scene_skip_count} total)")
                        time.sleep(0.5)
                        continue
                    else:
                        logger.debug(f"VLM #{self.vlm_frame_count}: Force run after {self.vlm_scene_skip_count} skips")

                cv2.imwrite(vlm_image_path, frame)
                is_ad, response, elapsed, confidence = self.vlm.detect_ad(vlm_image_path)

                # Discard slow VLM responses - scene likely changed during inference
                VLM_MAX_RELEVANT_TIME = 2.0
                if elapsed > VLM_MAX_RELEVANT_TIME:
                    ad_status = "AD" if is_ad else "NO-AD"
                    response_preview = response[:30] if response else "no response"
                    logger.warning(f"VLM #{self.vlm_frame_count}: {elapsed:.1f}s [{ad_status}] DISCARDED (took >{VLM_MAX_RELEVANT_TIME}s) \"{response_preview}\"")
                    self.vlm_prev_frame = frame.copy()
                    self.vlm_scene_skip_count = 0
                    time.sleep(0.5)
                    continue

                # Add decision to sliding window history with confidence
                now = time.time()
                self._add_vlm_decision(is_ad, confidence)

                # Track state changes for waffle detection and logging
                current_state = 'ad' if is_ad else 'no-ad'
                if self.vlm_last_state is not None and current_state != self.vlm_last_state:
                    time_since_last_change = now - self.vlm_state_change_time
                    if time_since_last_change < 15.0:  # Quick flip-flop
                        self.vlm_waffle_count = min(self.vlm_waffle_count + 1, 10)
                    self.vlm_state_change_time = now
                self.vlm_last_state = current_state

                # Update legacy counters (for logging and spastic detection)
                if is_ad:
                    self.vlm_consecutive_ad_count += 1
                    self.vlm_no_ad_count = 0
                else:
                    # Check for transition frame - don't count as "no ad" if blocking
                    is_transition, transition_type = self._is_transition_frame(frame)
                    if self.ad_detected and is_transition:
                        logger.info(f"VLM #{self.vlm_frame_count}: Transition frame ({transition_type}) - holding block")
                    else:
                        self.vlm_no_ad_count += 1
                        # VLM "spastic" detection: save screenshot for training
                        if 2 <= self.vlm_consecutive_ad_count <= 5:
                            self.screenshot_manager.save_vlm_spastic_screenshot(frame, self.vlm_consecutive_ad_count)
                        self.vlm_consecutive_ad_count = 0

                # Get current agreement stats for logging
                ad_ratio, no_ad_ratio, total_decisions = self._get_vlm_agreement()

                # Use sliding window approach for state changes
                prev_vlm_ad_detected = self.vlm_ad_detected

                if not self.vlm_ad_detected:
                    # Not currently detecting - check if we should START
                    if self._should_vlm_start_blocking():
                        self.vlm_ad_detected = True
                        self.vlm_last_state_change = now
                        self.vlm_cooldown_active = True
                        logger.warning(f"VLM detected ad (agreement: {ad_ratio*100:.0f}% of {total_decisions} decisions): \"{response[:50]}\"")
                        self.add_detection('VLM', [response[:100]] if response else [])
                else:
                    # Currently detecting - check if we should STOP
                    if self._should_vlm_stop_blocking():
                        self.vlm_ad_detected = False
                        self.vlm_last_state_change = now
                        self.vlm_cooldown_active = True
                        self.vlm_waffle_count = max(0, self.vlm_waffle_count - 1)  # Decay on stable stop
                        logger.warning(f"VLM: ad no longer detected (agreement: {no_ad_ratio*100:.0f}% no-ad of {total_decisions} decisions)")

                # Clear cooldown after minimum state duration
                if self.vlm_cooldown_active and (now - self.vlm_last_state_change) >= self.vlm_min_state_duration:
                    self.vlm_cooldown_active = False

                self._update_blocking_state()

                ad_status = "AD" if is_ad else "NO-AD"
                response_preview = response[:40] if response else "no response"
                logger.info(f"VLM #{self.vlm_frame_count}: {elapsed:.1f}s [{ad_status}] conf={confidence:.0%} \"{response_preview}\"")

                self.vlm_prev_frame = frame.copy()
                self.vlm_prev_frame_had_ad = is_ad
                self.vlm_scene_skip_count = 0  # Reset skip counter after processing

                # Periodic garbage collection to prevent memory leak
                if self.vlm_frame_count % 50 == 0:
                    gc.collect()

            except Exception as e:
                logger.exception(f"VLM worker error: {e}")

            time.sleep(0.5)

        # Clean up VLM frame file
        try:
            Path(vlm_image_path).unlink(missing_ok=True)
        except Exception:
            pass

        logger.info("VLM worker thread stopped")

    def run(self):
        """Start the stream processing."""
        logger.info("Starting Minus...")

        # Check signal
        signal_info = self.check_hdmi_signal()
        if not signal_info:
            logger.warning("No HDMI signal detected - starting in no-signal mode")
            # Start display in no-signal mode to show "NO HDMI INPUT"
            if self.ad_blocker:
                if self.ad_blocker.start_no_signal_mode():
                    logger.info("Display showing NO SIGNAL message - waiting for HDMI...")
                    # Poll for HDMI signal every 2 seconds
                    self.running = True
                    try:
                        poll_count = 0
                        while self.running:
                            time.sleep(2)
                            poll_count += 1
                            if poll_count % 15 == 0:  # Log every 30 seconds
                                logger.info("Still waiting for HDMI input...")
                            signal_info = self.check_hdmi_signal()
                            if signal_info:
                                width, height, fps = signal_info
                                logger.info(f"HDMI signal detected: {width}x{height} @ {fps}fps - switching to loading mode")
                                # Switch to loading mode while we start the display
                                self.ad_blocker.start_loading_mode()
                                break
                    except KeyboardInterrupt:
                        self.stop()
                        return True

                    if not self.running:
                        self.stop()
                        return True
                else:
                    logger.error("Failed to start no-signal display")
                    return False
            else:
                logger.error("No ad_blocker available for no-signal display")
                return False

        width, height, fps = signal_info
        logger.info(f"HDMI signal: {width}x{height} @ {fps}fps")

        # If ad_blocker doesn't have a loading/no-signal screen showing, start loading now
        # This ensures we always show loading during ustreamer startup
        if self.ad_blocker and self.ad_blocker.current_source not in ('loading', 'no_hdmi_device'):
            logger.info("Starting loading display while initializing...")
            self.ad_blocker.start_loading_mode()

        # Start display (will transition from loading to live when ready)
        if not self.start_display():
            logger.error("Failed to start display")
            return False

        logger.info("Display running at 30 FPS with instant ad blocking")

        # Start ML threads
        self.running = True

        if self.ocr:
            self.ml_thread = threading.Thread(target=self.ml_worker, daemon=True)
            self.ml_thread.start()

            all_keywords = PaddleOCR.AD_KEYWORDS_EXACT + PaddleOCR.AD_KEYWORDS_WORD
            logger.info(f"OCR watching for ad keywords: {all_keywords}")

        # Start health monitor (before VLM loading)
        if self.health_monitor:
            self.health_monitor.start()
            logger.info("Health monitor started")

        # Start web UI (before VLM loading so it's immediately accessible)
        if HAS_WEBUI:
            try:
                self.webui = WebUI(
                    minus_instance=self,
                    port=self.config.webui_port,
                    ustreamer_port=self.config.ustreamer_port
                )
                self.webui.start()
                logger.info(f"Web UI available at http://0.0.0.0:{self.config.webui_port}")
            except Exception as e:
                logger.warning(f"Failed to start Web UI: {e}")
                self.webui = None

        # Start Fire TV setup early (runs in parallel with VLM loading)
        # 5 second delay ensures display is stable before scanning
        self._start_fire_tv_setup_delayed(delay_seconds=5.0)

        # Load VLM model (takes ~40s, so start after WebUI is up)
        if self.vlm:
            # Show loading notification
            if self.system_notification:
                self.system_notification.show_vlm_loading()

            logger.info("Loading VLM model (FastVLM-1.5B)...")
            if self.vlm.load_model():
                logger.info("VLM model loaded successfully")
                self.vlm_thread = threading.Thread(target=self.vlm_worker, daemon=True)
                self.vlm_thread.start()
                logger.info(f"VLM worker started with prompt: \"{self.vlm.AD_PROMPT}\"")

                # Show success notification (auto-hides after 5s)
                if self.system_notification:
                    self.system_notification.show_vlm_ready()
            else:
                logger.warning("VLM model failed to load - VLM detection disabled")
                self.vlm = None

                # Show failure notification (auto-hides after 8s)
                if self.system_notification:
                    self.system_notification.show_vlm_failed()

        logger.info("Minus running - press Ctrl+C to stop")

        # Monitor ustreamer
        try:
            restart_failures = 0
            while self.running:
                # Skip main loop restart if HDMI recovery is in progress (health monitor handles it)
                if self._hdmi_recovery_in_progress:
                    time.sleep(1)
                    continue

                if self.ustreamer_process and self.ustreamer_process.poll() is not None:
                    logger.warning("ustreamer process died, restarting...")
                    if not self.start_display():
                        restart_failures += 1
                        logger.error(f"Failed to restart display (attempt {restart_failures})")
                        # Don't exit - wait and retry (health monitor may also be handling this)
                        if restart_failures >= 5:
                            logger.error("Too many restart failures, exiting")
                            break
                        time.sleep(5)  # Wait before retry
                    else:
                        restart_failures = 0  # Reset on success
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        self.stop()
        return True

    def stop(self):
        """Stop everything."""
        logger.info("Stopping...")
        self.running = False

        # Stop Fire TV setup first
        if self.fire_tv_setup:
            self.fire_tv_setup.destroy()
            self.fire_tv_setup = None
            self.fire_tv_controller = None

        # Stop health monitor
        if self.health_monitor:
            self.health_monitor.stop()

        # Stop web UI
        if self.webui:
            self.webui.stop()

        # Clean up frame capture temp file
        if self.frame_capture:
            self.frame_capture.cleanup()

        if self.ustreamer_process:
            self.ustreamer_process.terminate()
            try:
                self.ustreamer_process.wait(timeout=5)
            except:
                self.ustreamer_process.kill()

        if self.audio:
            self.audio.destroy()

        if self.ad_blocker:
            self.ad_blocker.destroy()

        if self.ocr:
            self.ocr.release()

        if self.vlm:
            self.vlm.release()

        # Restore console settings (show cursor, unblank, restore dmesg level)
        restore_console()

        logger.info("Stopped")


def main():
    parser = argparse.ArgumentParser(
        description='Minus - HDMI passthrough with ML-based ad detection'
    )
    parser.add_argument(
        '--device', '-d',
        default='/dev/video0',
        help='Video device path (default: /dev/video0)'
    )
    parser.add_argument(
        '--screenshot-dir', '-s',
        default='screenshots',
        help='Directory to save screenshots (default: screenshots)'
    )
    parser.add_argument(
        '--check-signal',
        action='store_true',
        help='Just check HDMI signal and exit'
    )
    parser.add_argument(
        '--ocr-timeout',
        type=float,
        default=1.5,
        help='Skip OCR frames taking longer than this (seconds, default: 1.5)'
    )
    parser.add_argument(
        '--max-screenshots',
        type=int,
        default=0,
        help='Keep only this many recent screenshots (0=unlimited for training, default: 0)'
    )
    parser.add_argument(
        '--connector-id',
        type=int,
        default=None,
        help='DRM connector ID for HDMI output (auto-detected if not specified)'
    )
    parser.add_argument(
        '--plane-id',
        type=int,
        default=None,
        help='DRM plane ID for video overlay (auto-detected if not specified)'
    )
    parser.add_argument(
        '--webui-port',
        type=int,
        default=8080,
        help='Web UI port (default: 8080)'
    )

    args = parser.parse_args()

    config = MinusConfig(
        device=args.device,
        screenshot_dir=args.screenshot_dir,
        ocr_timeout=args.ocr_timeout,
        max_screenshots=args.max_screenshots,
        drm_connector_id=args.connector_id,
        drm_plane_id=args.plane_id,
        webui_port=args.webui_port,
    )

    minus = Minus(config)

    if args.check_signal:
        signal_info = minus.check_hdmi_signal()
        if signal_info:
            width, height, fps = signal_info
            print(f"HDMI signal detected: {width}x{height} @ {fps}fps")
            sys.exit(0)
        else:
            print("No HDMI signal detected")
            sys.exit(1)

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        minus.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    minus.run()


if __name__ == '__main__':
    main()
