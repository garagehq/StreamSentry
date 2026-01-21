#!/usr/bin/env python3
"""
Minus - HDMI passthrough with ML-based ad detection.

Architecture:
- ustreamer captures from HDMI-RX and serves MJPEG stream + HTTP snapshot
- GStreamer with input-selector for instant video/blocking switching
- PaddleOCR on RKNN NPU detects ad-related text (~400ms)
- Qwen3-VL-2B on Axera NPU provides visual understanding (~1.5s)
- Spanish vocabulary practice during ad blocks!

Key insight: Using GStreamer input-selector allows instant switching between
video and blocking overlay without any process restart or black screen gap.

Performance:
- Display: 30fps via GStreamer kmssink (NV12 → DRM plane 72)
- Snapshot: ~150ms non-blocking HTTP capture
- OCR: ~400-500ms per frame on RKNN NPU
- VLM: ~1.5s per frame on Axera NPU
- Ad blocking: INSTANT switching via input-selector
"""

import argparse
import os
import sys
import signal
import time
import logging
import logging.handlers
import threading
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import numpy as np
import cv2

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

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

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


def probe_drm_output() -> dict:
    """
    Probe DRM outputs to find connected HDMI display and its preferred resolution.

    Returns dict with:
        - connector_id: int (e.g., 215 for HDMI-A-1, 231 for HDMI-A-2)
        - connector_name: str (e.g., 'HDMI-A-1', 'HDMI-A-2')
        - width: int (preferred resolution width)
        - height: int (preferred resolution height)
        - plane_id: int (suitable plane that supports NV12)
        - crtc_id: int (CRTC connected to this connector)
        - audio_device: str (ALSA playback device, e.g., 'hw:0,0' or 'hw:1,0')
    """
    result = {
        'connector_id': None,
        'connector_name': None,
        'width': 1920,  # fallback
        'height': 1080,  # fallback
        'plane_id': 72,  # fallback (known to support NV12)
        'crtc_id': None,
        'audio_device': 'hw:0,0',  # fallback
    }

    try:
        # Run modetest to get connector info
        proc = subprocess.run(
            ['modetest', '-M', 'rockchip', '-c'],
            capture_output=True, text=True, timeout=5
        )

        if proc.returncode != 0:
            logger.warning(f"modetest failed: {proc.stderr}")
            return result

        # Parse connectors - look for connected HDMI
        # Format: "id  encoder  status  name  size (mm)  modes  encoders"
        # Example: "231  230  connected  HDMI-A-2  1150x650  25  230"
        lines = proc.stdout.split('\n')
        in_connectors = False
        connected_hdmi = None

        for line in lines:
            if 'Connectors:' in line:
                in_connectors = True
                continue
            if in_connectors and line.strip() and not line.startswith(' ') and not line.startswith('\t'):
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        conn_id = int(parts[0])
                        status = parts[2]
                        name = parts[3]
                        if status == 'connected' and 'HDMI' in name:
                            connected_hdmi = {'id': conn_id, 'name': name}
                            logger.info(f"Found connected HDMI output: {name} (connector {conn_id})")
                            break
                    except (ValueError, IndexError):
                        continue

        if not connected_hdmi:
            logger.warning("No connected HDMI output found")
            return result

        result['connector_id'] = connected_hdmi['id']
        result['connector_name'] = connected_hdmi['name']

        # Get preferred resolution from modetest
        # Run modetest again to get modes for this connector
        proc = subprocess.run(
            ['modetest', '-M', 'rockchip', '-c'],
            capture_output=True, text=True, timeout=5
        )

        # Look for preferred mode after the connector line
        lines = proc.stdout.split('\n')
        found_connector = False
        for line in lines:
            # Find our connector by ID
            if line.strip().startswith(str(connected_hdmi['id'])):
                found_connector = True
                continue
            if found_connector:
                # Look for "preferred" in mode line
                # Format: "#0 1920x1080 60.00 ... flags: phsync, pvsync; type: preferred, driver"
                if 'preferred' in line and 'x' in line:
                    # Extract resolution like "1920x1080"
                    match = re.search(r'(\d+)x(\d+)', line)
                    if match:
                        result['width'] = int(match.group(1))
                        result['height'] = int(match.group(2))
                        logger.info(f"Found preferred resolution: {result['width']}x{result['height']}")
                        break
                # Stop if we hit next connector
                elif line.strip() and not line.startswith(' ') and not line.startswith('\t') and not line.startswith('#'):
                    if re.match(r'^\d+\s', line.strip()):
                        break

        # Find a suitable plane that supports NV12 and is an Overlay type
        # On RK3588 VOP2:
        #   - type=0 (Overlay) - best for video overlay
        #   - type=1 (Primary) - typically doesn't support NV12
        #   - type=2 (Cursor) - can work but not ideal
        # Planes 192, 152, 112, 72 typically support NV12 on RK3588
        proc = subprocess.run(
            ['modetest', '-M', 'rockchip', '-p'],
            capture_output=True, text=True, timeout=5
        )

        if proc.returncode == 0:
            lines = proc.stdout.split('\n')
            in_planes = False
            best_plane = None
            best_plane_type = 3  # Start with invalid type (lower is better: Overlay=0, Primary=1, Cursor=2)

            i = 0
            while i < len(lines):
                line = lines[i]

                if 'Planes:' in line:
                    in_planes = True
                    i += 1
                    continue

                if in_planes and line.strip() and not line.startswith(' ') and not line.startswith('\t'):
                    # Plane header line: "192  0  0  0,0  0,0  0  0x00000007"
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].isdigit():
                        plane_id = int(parts[0])

                        # Check next line for formats
                        if i + 1 < len(lines) and 'formats:' in lines[i + 1]:
                            has_nv12 = 'NV12' in lines[i + 1]

                            # Check for plane type in subsequent lines
                            plane_type = 3  # Default to invalid
                            for j in range(i + 2, min(i + 15, len(lines))):
                                if 'type:' in lines[j]:
                                    # Next few lines should have the type value
                                    for k in range(j + 1, min(j + 5, len(lines))):
                                        if 'value:' in lines[k]:
                                            try:
                                                plane_type = int(lines[k].split(':')[1].strip())
                                            except (ValueError, IndexError):
                                                pass
                                            break
                                    break

                            # Prefer Overlay planes (type=0) that support NV12
                            if has_nv12 and plane_type < best_plane_type:
                                best_plane = plane_id
                                best_plane_type = plane_type
                                type_name = {0: 'Overlay', 1: 'Primary', 2: 'Cursor'}.get(plane_type, 'Unknown')
                                logger.info(f"Found NV12-capable {type_name} plane: {plane_id}")

                i += 1

            if best_plane is not None:
                result['plane_id'] = best_plane
                type_name = {0: 'Overlay', 1: 'Primary', 2: 'Cursor'}.get(best_plane_type, 'Unknown')
                logger.info(f"Selected plane {best_plane} (type={type_name}) for NV12 output")

        # Determine audio output device based on connector
        # On RK3588: HDMI-A-1 -> hw:0,0 (rockchip-hdmi0), HDMI-A-2 -> hw:1,0 (rockchip-hdmi1)
        if result['connector_name']:
            if 'HDMI-A-1' in result['connector_name']:
                result['audio_device'] = 'hw:0,0'
            elif 'HDMI-A-2' in result['connector_name']:
                result['audio_device'] = 'hw:1,0'
            logger.info(f"Audio output device: {result['audio_device']} (based on {result['connector_name']})")

        logger.info(f"DRM output probe result: connector={result['connector_id']} ({result['connector_name']}), "
                   f"resolution={result['width']}x{result['height']}, plane={result['plane_id']}, "
                   f"audio={result['audio_device']}")

        return result

    except subprocess.TimeoutExpired:
        logger.warning("Timeout probing DRM output")
        return result
    except Exception as e:
        logger.warning(f"Error probing DRM output: {e}")
        return result


def probe_v4l2_device(device: str) -> dict:
    """
    Probe a V4L2 device to get its current format and resolution.

    Returns dict with:
        - format: V4L2 pixel format string (e.g., 'NV12', 'BGR3', 'YUYV')
        - width: int
        - height: int
        - ustreamer_format: format string for ustreamer (e.g., 'NV12', 'BGR24')
    """
    result = {
        'format': None,
        'width': 0,
        'height': 0,
        'ustreamer_format': None,
    }

    try:
        # Run v4l2-ctl to get format info
        proc = subprocess.run(
            ['v4l2-ctl', '-d', device, '--get-fmt-video'],
            capture_output=True, text=True, timeout=5
        )

        if proc.returncode != 0:
            logger.warning(f"Failed to probe {device}: {proc.stderr}")
            return result

        output = proc.stdout

        # Parse width/height
        wh_match = re.search(r'Width/Height\s*:\s*(\d+)/(\d+)', output)
        if wh_match:
            result['width'] = int(wh_match.group(1))
            result['height'] = int(wh_match.group(2))

        # Parse pixel format - look for the 4-character code
        # Example: "Pixel Format      : 'NV12' (Y/UV 4:2:0)"
        # Example: "Pixel Format      : 'BGR3' (24-bit BGR 8-8-8)"
        fmt_match = re.search(r"Pixel Format\s*:\s*'(\w+)'", output)
        if fmt_match:
            v4l2_format = fmt_match.group(1)
            result['format'] = v4l2_format

            # Map V4L2 format codes to ustreamer format names
            format_map = {
                'NV12': 'NV12',
                'NV16': 'NV16',
                'NV24': 'NV24',
                'BGR3': 'BGR24',
                'RGB3': 'RGB24',
                'YUYV': 'YUYV',
                'UYVY': 'UYVY',
                'MJPG': 'MJPEG',
                'JPEG': 'MJPEG',
            }
            result['ustreamer_format'] = format_map.get(v4l2_format, v4l2_format)

        logger.info(f"Probed {device}: {result['width']}x{result['height']} format={result['format']} -> {result['ustreamer_format']}")

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout probing {device}")
    except Exception as e:
        logger.warning(f"Error probing {device}: {e}")

    return result


@dataclass
class MinusConfig:
    """Configuration for the Minus pipeline."""
    device: str = "/dev/video0"
    screenshot_dir: str = "screenshots"
    ocr_timeout: float = 1.5
    ustreamer_port: int = 9090
    max_screenshots: int = 50
    drm_connector_id: int = None  # Auto-detect HDMI output connector
    drm_plane_id: int = None  # Auto-detect NV12-capable overlay plane
    output_width: int = None  # Auto-detect from display EDID
    output_height: int = None  # Auto-detect from display EDID
    audio_capture_device: str = "hw:4,0"  # HDMI-RX audio input (always card 4)
    audio_playback_device: str = None  # Auto-detect based on connected HDMI output
    webui_port: int = 8080  # Web UI port


class UstreamerCapture:
    """Frame capture using ustreamer's HTTP snapshot endpoint.

    Uses /snapshot/raw which:
    - Returns raw video when blocking is active (for OCR to see ad content)
    - Redirects to /snapshot when not blocking (normal operation)
    """

    def __init__(self, port=9090):
        self.port = port
        # Use /snapshot/raw to always get raw video, even during blocking
        # This is critical for OCR to detect when ads end
        self.snapshot_url = f'http://localhost:{port}/snapshot/raw'
        # Use PID-based filename to avoid conflicts with root-owned stale files
        self.screenshot_path = f'/dev/shm/minus_frame_{os.getpid()}.jpg'

    def cleanup(self):
        """Remove the temporary screenshot file."""
        try:
            Path(self.screenshot_path).unlink(missing_ok=True)
        except Exception:
            pass

    def capture(self):
        """Capture frame via HTTP snapshot and return as numpy array."""
        try:
            # Use -L to follow redirects (when not blocking, /snapshot/raw redirects to /snapshot)
            result = subprocess.run(
                ['curl', '-s', '-L', '-o', self.screenshot_path, self.snapshot_url],
                capture_output=True, timeout=3
            )

            if result.returncode == 0:
                img = cv2.imread(self.screenshot_path)
                if img is not None:
                    # Scale to 960x540 for OCR - model uses 960x960 anyway
                    # Using INTER_AREA for best quality downscaling, fast on 4K->540p
                    h, w = img.shape[:2]
                    if h > 540 or w > 960:
                        img = cv2.resize(img, (960, 540), interpolation=cv2.INTER_AREA)
                    return img

            return None
        except Exception as e:
            logger.error(f"Snapshot capture error: {e}")
            return None


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

        # VLM waffle detection - prevent rapid ad/no-ad oscillation
        # When VLM waffles (ad->no-ad->ad repeatedly), increase threshold for state changes
        self.vlm_waffle_count = 0           # How many times VLM has flip-flopped recently
        self.vlm_last_state = None          # Last VLM state ('ad' or 'no-ad')
        self.vlm_state_change_time = 0      # When state last changed
        self.vlm_waffle_decay_time = 30.0   # Decay waffle count if consistent for this long
        self.vlm_max_waffle_penalty = 6     # Max extra no-ads required when waffling
        self.vlm_consistent_count = 0       # Consecutive same-state responses

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
        self.SKIP_DELAY_SECONDS = 11.0  # Wait 11s after ad starts before attempting skip

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
        self.DYNAMIC_COOLDOWN = 1.5       # Keep suppression for this long after screen becomes dynamic
        self.static_since_time = 0        # When screen became static (0 = not static)
        self.static_ocr_count = 0         # OCR iterations without scene change
        self.static_blocking_suppressed = False  # Currently suppressing due to static
        self.screen_became_dynamic_time = 0      # When screen went from static to dynamic

        self.vlm_prev_frame = None
        self.vlm_prev_frame_had_ad = False
        self.vlm_scene_skip_count = 0
        self.vlm_max_scene_skip = 10  # Force VLM after this many consecutive skips

        # Screenshot directories
        self.screenshot_dir = Path(config.screenshot_dir) / "ocr"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.non_ad_screenshot_dir = Path(config.screenshot_dir) / "non_ad"
        self.non_ad_screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_count = 0
        self.non_ad_screenshot_count = 0
        self.screenshot_hashes = set()  # For deduplication (O(1) lookup)

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

        # Skip opportunity state
        self.skip_available = False  # True when "Skip" button is ready (no countdown)
        self.skip_countdown = None   # Countdown seconds if not yet skippable
        self.last_skip_text = None   # The detected skip text
        self.last_skip_time = 0      # When skip was last detected
        self.skip_attempts = 0       # How many times we would have skipped

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
        # Switch to standalone NO SIGNAL display (doesn't depend on ustreamer)
        if self.ad_blocker:
            self.ad_blocker.start_no_signal_mode()
        if self.audio:
            self.audio.mute()

    def _on_hdmi_restored(self):
        """Handle HDMI signal restoration."""
        logger.info("[Recovery] HDMI signal restored - showing loading screen")

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
            screenshots = sorted(
                self.screenshot_dir.glob("*.png"),
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

    def check_skip_opportunity(self, all_texts: list) -> tuple:
        """
        Check OCR results for skippable "Skip" button (no countdown).

        For YouTube ads:
        - "Skip" alone = skippable NOW
        - "Skip Ad" = skippable NOW
        - "Skip 5" or "Skip Ad in 5" = NOT skippable (countdown active)

        Args:
            all_texts: List of detected text strings from OCR

        Returns:
            Tuple of (is_skippable, skip_text, countdown_seconds)
            - is_skippable: True if skip button is ready to press
            - skip_text: The detected skip-related text
            - countdown_seconds: Countdown remaining (0 if skippable, >0 if countdown)
        """
        import re

        for text in all_texts:
            text_lower = text.lower().strip()

            # Check for "Skip" with countdown number
            # Patterns: "Skip 5", "Skip Ad in 5", "Skip in 5s", "Skip 10", etc.
            countdown_match = re.search(r'skip\s*(?:ad\s*)?(?:in\s*)?(\d+)\s*s?', text_lower)
            if countdown_match:
                countdown = int(countdown_match.group(1))
                return (False, text, countdown)

            # Check for standalone "Skip" or "Skip Ad" (no number = skippable)
            # Must be short text to avoid false positives like "Skip this step"
            if re.search(r'^skip\s*(?:ad|ads)?$', text_lower) and len(text_lower) <= 10:
                return (True, text, 0)

            # Also check "Skip Ad" button variant
            if text_lower in ['skip', 'skip ad', 'skip ads', 'skipad']:
                return (True, text, 0)

        return (False, None, None)

    # ===== Web UI Methods =====

    def pause_blocking(self, duration_seconds: int = 120):
        """Pause ad blocking for specified duration."""
        with self._state_lock:
            self.blocking_paused_until = time.time() + duration_seconds
            logger.info(f"[WebUI] Blocking paused for {duration_seconds}s")

        # Capture non-ad screenshot for future VLM training
        # This helps collect examples of content that should NOT be classified as ads
        self._save_non_ad_screenshot()

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

        return {
            # Blocking state
            'blocking': self.ad_detected and not self.is_blocking_paused() and not self.static_blocking_suppressed,
            'blocking_source': self.blocking_source,
            'paused': self.is_blocking_paused(),
            'pause_remaining': self.get_pause_remaining(),
            'static_suppressed': self.static_blocking_suppressed,

            # Detection counts
            'ocr_detected': self.ocr_ad_detected,
            'vlm_detected': self.vlm_ad_detected,
            'ocr_frame_count': self.frame_count,
            'vlm_frame_count': self.vlm_frame_count,
            'total_detections': self.screenshot_count,

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
            'skip_attempts': self.skip_attempts,

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
                    should_start = True
                    source = "both" if self.vlm_ad_detected else "ocr"
                elif self.vlm_ad_detected and not ocr_recent:
                    if self.vlm_consecutive_ad_count >= self.VLM_ALONE_THRESHOLD:
                        should_start = True
                        source = "vlm"
                        logger.info(f"VLM triggered alone after {self.vlm_consecutive_ad_count} consecutive detections")
                elif self.vlm_ad_detected and ocr_recent:
                    should_start = True
                    source = "vlm"

                if should_start:
                    self.ad_detected = True
                    self.blocking_start_time = now
                    self.blocking_source = source
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

                    if ocr_recent:
                        vlm_says_stop = (self.vlm_no_ad_count >= self.VLM_STOP_THRESHOLD)
                        should_stop = ocr_says_stop and vlm_says_stop
                    else:
                        should_stop = ocr_says_stop

                if should_stop:
                    self.ad_detected = False
                    self.blocking_source = None
                    logger.warning(f"AD BLOCKING ENDED after {blocking_elapsed:.1f}s")

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

        while self.running:
            try:
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
                        # Save screenshot as non-ad training data (still ads shouldn't be blocked)
                        if self.ad_detected:
                            self._save_static_ad_screenshot(frame)
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

                # Run OCR with timeout
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self.ocr.ocr, frame_rgb)
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

                # Check for skip opportunity (for Fire TV ad skipping)
                # Only attempt skip after SKIP_DELAY_SECONDS since ad blocking started
                is_skippable, skip_text, countdown = self.check_skip_opportunity(all_texts)

                # Calculate time since ad blocking started
                time_since_blocking = 0
                if self.ad_detected and self.blocking_start_time > 0:
                    time_since_blocking = time.time() - self.blocking_start_time

                # Only allow skip after delay period (skip button never appears in first few seconds)
                skip_delay_passed = time_since_blocking >= self.SKIP_DELAY_SECONDS

                if is_skippable and skip_delay_passed:
                    if not self.skip_available:
                        self.skip_available = True
                        self.last_skip_text = skip_text
                        self.last_skip_time = time.time()
                        self.skip_attempts += 1
                        logger.warning(f"[SKIP] >>> SKIP BUTTON READY! Pressing CENTER to skip. Text: '{skip_text}' (attempt #{self.skip_attempts})")
                        # Actually skip the ad by pressing the center/select button
                        if self.try_skip_ad():
                            logger.info(f"[SKIP] Skip command sent successfully!")
                            # Record time saved (estimate ~30s per skipped ad)
                            if self.ad_blocker:
                                self.ad_blocker.add_time_saved(30.0)
                        else:
                            logger.warning(f"[SKIP] Skip command failed or Fire TV not connected")
                    self.skip_countdown = 0
                elif is_skippable and not skip_delay_passed:
                    # Skip button visible but delay not passed yet
                    wait_remaining = int(self.SKIP_DELAY_SECONDS - time_since_blocking)
                    if wait_remaining > 0:
                        logger.debug(f"[SKIP] Skip visible but waiting {wait_remaining}s more (delay: {self.SKIP_DELAY_SECONDS}s)")
                        if self.ad_blocker:
                            self.ad_blocker.set_skip_status(False, f"Wait {wait_remaining}s")
                elif countdown is not None:
                    self.skip_available = False
                    self.skip_countdown = countdown
                    self.last_skip_text = skip_text
                    logger.info(f"[SKIP] Skip countdown: {countdown}s remaining. Text: '{skip_text}'")
                    if self.ad_blocker:
                        self.ad_blocker.set_skip_status(False, f"Skip in {countdown}s")
                else:
                    # No skip button detected
                    if self.skip_available:
                        logger.info("[SKIP] Skip button no longer visible")
                    self.skip_available = False
                    self.skip_countdown = None
                    if self.ad_blocker:
                        self.ad_blocker.set_skip_status(False, None)

                if ad_detected and not is_terminal:
                    self.ocr_ad_detection_count += 1
                    self.ocr_no_ad_count = 0
                    self.last_ocr_ad_time = time.time()

                    if self.ocr_ad_detection_count >= 1 and not self.ocr_ad_detected:
                        self.ocr_ad_detected = True
                        keywords_found = [kw for kw, txt in matched_keywords]
                        logger.info(f"OCR detected ad keywords: {keywords_found}")
                        self._save_screenshot(frame, matched_keywords, all_texts)
                        self.add_detection('OCR', all_texts, matched_keywords)
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
                is_ad, response, elapsed = self.vlm.detect_ad(vlm_image_path)

                # Track VLM state changes for waffle detection
                current_state = 'ad' if is_ad else 'no-ad'
                now = time.time()

                # Detect state change (waffle)
                if self.vlm_last_state is not None and current_state != self.vlm_last_state:
                    # State changed - this is a potential waffle
                    time_since_last_change = now - self.vlm_state_change_time
                    if time_since_last_change < 10.0:  # Quick flip-flop (within 10s)
                        self.vlm_waffle_count = min(self.vlm_waffle_count + 1, self.vlm_max_waffle_penalty)
                        logger.warning(f"VLM waffle detected ({current_state}), waffle_count={self.vlm_waffle_count}")
                    self.vlm_state_change_time = now
                    self.vlm_consistent_count = 1
                else:
                    # Same state - increase consistency, potentially decay waffle count
                    self.vlm_consistent_count += 1
                    if self.vlm_consistent_count >= 5 and self.vlm_waffle_count > 0:
                        # Been consistent for 5+ frames, reduce waffle penalty
                        self.vlm_waffle_count = max(0, self.vlm_waffle_count - 1)
                        logger.info(f"VLM consistent (x{self.vlm_consistent_count}), reduced waffle_count to {self.vlm_waffle_count}")

                self.vlm_last_state = current_state

                # Calculate effective stop threshold with waffle penalty
                effective_vlm_stop_threshold = self.VLM_STOP_THRESHOLD + self.vlm_waffle_count

                if is_ad:
                    self.vlm_consecutive_ad_count += 1
                    self.vlm_no_ad_count = 0

                    if not self.vlm_ad_detected:
                        self.vlm_ad_detected = True
                        logger.info(f"VLM detected ad (x{self.vlm_consecutive_ad_count}): \"{response[:50]}\"")
                        self.add_detection('VLM', [response[:100]] if response else [])
                else:
                    self.vlm_no_ad_count += 1

                    # VLM "spastic" detection: if VLM detected ads 2-5 times then changed its mind,
                    # save screenshot for training - this might be a false positive case
                    if 2 <= self.vlm_consecutive_ad_count <= 5:
                        self._save_vlm_spastic_screenshot(frame, self.vlm_consecutive_ad_count)

                    self.vlm_consecutive_ad_count = 0

                    # Use effective threshold (with waffle penalty) for stopping
                    if self.vlm_ad_detected and self.vlm_no_ad_count >= effective_vlm_stop_threshold:
                        self.vlm_ad_detected = False
                        logger.info(f"VLM: ad no longer detected (after {self.vlm_no_ad_count} no-ads, threshold={effective_vlm_stop_threshold})")

                self._update_blocking_state()

                ad_status = "AD" if is_ad else "NO-AD"
                response_preview = response[:40] if response else "no response"
                logger.info(f"VLM #{self.vlm_frame_count}: {elapsed:.1f}s [{ad_status}] \"{response_preview}\"")

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

    def _compute_image_hash(self, frame):
        """Compute a fast perceptual hash for deduplication.

        Resizes to 8x8 grayscale and hashes the bytes.
        O(1) lookup in hash set, robust to minor variations.
        """
        try:
            # Resize to 8x8 and convert to grayscale
            small = cv2.resize(frame, (8, 8), interpolation=cv2.INTER_AREA)
            if len(small.shape) == 3:
                small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            # Return hash of the bytes
            return hash(small.tobytes())
        except Exception:
            return None

    def _save_non_ad_screenshot(self):
        """
        Save screenshot when user pauses blocking (for VLM training).

        These screenshots represent content that should NOT be classified as ads.
        When the user pauses blocking, they're indicating the current content
        is NOT an ad (false positive), so we save it for training data.
        """
        try:
            if self.frame_capture is None:
                logger.warning("[WebUI] Cannot save non-ad screenshot: no frame capture")
                return

            frame = self.frame_capture.capture()
            if frame is None:
                logger.warning("[WebUI] Cannot save non-ad screenshot: capture failed")
                return

            self.non_ad_screenshot_count += 1
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = f"non_ad_{timestamp}_{self.non_ad_screenshot_count:04d}.png"
            filepath = self.non_ad_screenshot_dir / filename

            cv2.imwrite(str(filepath), frame)
            logger.info(f"[WebUI] Non-ad screenshot saved: {filename}")

        except Exception as e:
            logger.error(f"[WebUI] Failed to save non-ad screenshot: {e}")

    def _save_static_ad_screenshot(self, frame):
        """
        Save screenshot when static screen suppression kicks in (for VLM training).

        These screenshots represent still/static ads that should NOT trigger blocking
        (e.g., paused video with ad overlay, YouTube landing page with sponsored content).
        Training the VLM on these helps it learn to NOT classify static ads as blockable.
        """
        try:
            if frame is None:
                return

            self.non_ad_screenshot_count += 1
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = f"static_ad_{timestamp}_{self.non_ad_screenshot_count:04d}.png"
            filepath = self.non_ad_screenshot_dir / filename

            cv2.imwrite(str(filepath), frame)
            logger.info(f"[Static] Saved static ad screenshot for training: {filename}")

        except Exception as e:
            logger.error(f"[Static] Failed to save static ad screenshot: {e}")

    def _save_vlm_spastic_screenshot(self, frame, consecutive_count):
        """
        Save screenshot when VLM is "spastic" - detected ads 2-5 times then changed its mind.

        This captures potential false positive cases where VLM was uncertain.
        These screenshots can be used to improve VLM training.
        """
        try:
            if frame is None:
                return

            self.non_ad_screenshot_count += 1
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = f"vlm_spastic_{consecutive_count}x_{timestamp}_{self.non_ad_screenshot_count:04d}.png"
            filepath = self.non_ad_screenshot_dir / filename

            cv2.imwrite(str(filepath), frame)
            logger.info(f"[VLM] Saved spastic screenshot ({consecutive_count}x ad then no-ad): {filename}")

        except Exception as e:
            logger.error(f"[VLM] Failed to save spastic screenshot: {e}")

    def _save_screenshot(self, frame, matched_keywords, all_texts):
        """Save screenshot when ad detected (with deduplication)."""
        # Check for duplicate using perceptual hash
        img_hash = self._compute_image_hash(frame)
        if img_hash is not None and img_hash in self.screenshot_hashes:
            return  # Skip duplicate

        # Add hash to set (cap at 1000 entries to prevent unbounded memory growth)
        if img_hash is not None:
            if len(self.screenshot_hashes) >= 1000:
                self.screenshot_hashes.clear()  # Reset when full
            self.screenshot_hashes.add(img_hash)

        self.screenshot_count += 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"ad_{timestamp}_{self.screenshot_count:04d}.png"
        filepath = self.screenshot_dir / filename

        cv2.imwrite(str(filepath), frame)

        keywords_str = ', '.join([f"'{kw}' in '{txt}'" for kw, txt in matched_keywords])
        logger.info(f"  Screenshot saved: {filename}")
        logger.info(f"  Keywords: {keywords_str}")
        logger.info(f"  All texts: {all_texts}")

        if self.config.max_screenshots > 0:
            self._truncate_screenshots()

    def _truncate_screenshots(self):
        """Remove oldest screenshots if we exceed the max limit."""
        try:
            screenshots = sorted(self.screenshot_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
            excess = len(screenshots) - self.config.max_screenshots
            if excess > 0:
                for old_file in screenshots[:excess]:
                    old_file.unlink()
        except Exception as e:
            logger.warning(f"Failed to truncate screenshots: {e}")

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
                    logger.info("Display showing NO HDMI INPUT message - waiting for HDMI...")
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

            logger.info("Loading VLM model (Qwen3-VL-2B-INT4)...")
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
        default=50,
        help='Keep only this many recent screenshots (0=unlimited, default: 50)'
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
