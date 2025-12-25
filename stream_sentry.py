#!/usr/bin/env python3
"""
Stream Sentry - HDMI passthrough with ML-based ad detection.

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

# Redirect stderr temporarily during cv2 operations to suppress libjpeg warnings
# These warnings (from libjpeg) go directly to stderr, bypassing Python logging
class SuppressLibjpegWarnings:
    """Context manager to suppress libjpeg warnings from cv2.imread."""
    def __init__(self):
        self._stderr_fd = None
        self._saved_stderr_fd = None
        self._devnull_fd = None

    def __enter__(self):
        try:
            self._stderr_fd = sys.stderr.fileno()
            self._saved_stderr_fd = os.dup(self._stderr_fd)
            self._devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(self._devnull_fd, self._stderr_fd)
        except Exception:
            pass  # If we can't suppress, just continue
        return self

    def __exit__(self, *args):
        try:
            if self._saved_stderr_fd is not None:
                os.dup2(self._saved_stderr_fd, self._stderr_fd)
                os.close(self._saved_stderr_fd)
            if self._devnull_fd is not None:
                os.close(self._devnull_fd)
        except Exception:
            pass

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
log_file = Path(__file__).parent / 'stream_sentry.log'
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

logger = logging.getLogger('StreamSentry')

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


@dataclass
class StreamConfig:
    """Configuration for the stream pipeline."""
    device: str = "/dev/video0"
    screenshot_dir: str = "screenshots"
    ocr_timeout: float = 1.5
    ustreamer_port: int = 9090
    max_screenshots: int = 50
    drm_connector_id: int = 215  # HDMI-A-1 connector
    drm_plane_id: int = 72  # Video overlay plane (supports NV12)


class UstreamerCapture:
    """Frame capture using ustreamer's HTTP snapshot endpoint."""

    def __init__(self, port=9090):
        self.port = port
        self.snapshot_url = f'http://localhost:{port}/snapshot'
        self.screenshot_path = '/dev/shm/stream_sentry_frame.jpg'

    def capture(self):
        """Capture frame via HTTP snapshot and return as numpy array."""
        try:
            result = subprocess.run(
                ['curl', '-s', '-o', self.screenshot_path, self.snapshot_url],
                capture_output=True, timeout=3
            )

            if result.returncode == 0:
                # Suppress libjpeg warnings during imread (corrupt JPEG warnings)
                with SuppressLibjpegWarnings():
                    img = cv2.imread(self.screenshot_path)
                if img is not None:
                    # Scale to 720p for OCR - model uses 960x960 anyway
                    if img.shape[0] > 720 or img.shape[1] > 1280:
                        img = cv2.resize(img, (1280, 720))
                    return img

            return None
        except Exception as e:
            logger.error(f"Snapshot capture error: {e}")
            return None


class StreamSentry:
    """
    Stream Sentry - HDMI passthrough with ML-based ad detection.

    Uses a single GStreamer pipeline with input-selector for instant
    switching between video and blocking overlay.
    """

    def __init__(self, config: StreamConfig = None):
        if config is None:
            config = StreamConfig()
        self.config = config
        self.device = config.device
        self.ustreamer_process = None
        self.frame_capture = None
        self.running = False
        self.blocking_active = False

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

        # Combined ad detection state
        self.ad_detected = False
        self.frame_count = 0
        self.blocking_start_time = 0
        self.blocking_source = None

        # Weighted detection parameters
        self.OCR_TRUST_WINDOW = 5.0
        self.VLM_ALONE_THRESHOLD = 3
        self.MIN_BLOCKING_DURATION = 3.0
        self.OCR_STOP_THRESHOLD = 3
        self.VLM_STOP_THRESHOLD = 2

        self._state_lock = threading.Lock()

        # Scene change detection
        self.prev_frame = None
        self.prev_frame_had_ad = False
        self.scene_skip_count = 0
        self.scene_change_threshold = 0.01
        self.max_scene_skip = 30  # Force OCR after this many consecutive skips

        self.vlm_prev_frame = None
        self.vlm_prev_frame_had_ad = False
        self.vlm_scene_skip_count = 0
        self.vlm_max_scene_skip = 10  # Force VLM after this many consecutive skips

        # Screenshot directory
        self.screenshot_dir = Path(config.screenshot_dir) / "ocr"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_count = 0
        self.screenshot_hashes = set()  # For deduplication (O(1) lookup)

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
                    stream_sentry=self,
                    ustreamer_port=config.ustreamer_port
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
                    capture_device="hw:4,0",   # HDMI-RX audio
                    playback_device="hw:0,0"   # HDMI-TX0 audio
                )
                # Link audio to ad_blocker for mute control
                if self.ad_blocker:
                    self.ad_blocker.set_audio(self.audio)
                logger.info("Audio passthrough initialized (hw:4,0 -> hw:0,0)")
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
                self.health_monitor.on_vlm_failure(self._handle_vlm_failure)
                self.health_monitor.on_memory_critical(self._handle_memory_critical)
                logger.info("Health monitor initialized")
            except Exception as e:
                logger.warning(f"Health monitor init failed: {e}")
                self.health_monitor = None

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
        logger.warning("[Recovery] HDMI signal lost - showing placeholder")
        # Show blocking screen with HDMI lost message
        if self.ad_blocker:
            self.ad_blocker.show('hdmi_lost')
        if self.audio:
            self.audio.mute()

    def _on_hdmi_restored(self):
        """Handle HDMI signal restoration."""
        logger.info("[Recovery] HDMI signal restored - restarting capture")
        # Hide blocking screen
        if self.ad_blocker:
            self.ad_blocker.hide()
        if self.audio:
            self.audio.unmute()
        # Restart ustreamer to pick up new signal
        self._restart_ustreamer()

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

            subprocess.run(['pkill', '-9', 'ustreamer'], capture_output=True)
            # Also kill anything on the port
            subprocess.run(['fuser', '-k', f'{self.config.ustreamer_port}/tcp'],
                          capture_output=True, stderr=subprocess.DEVNULL)
            time.sleep(1)

            # Restart
            port = self.config.ustreamer_port
            ustreamer_cmd = [
                'ustreamer',
                f'--device={self.device}',
                '--format=BGR24',
                f'--port={port}',
                '--quality=75',
                '--workers=8',
                '--buffers=8',
            ]

            self.ustreamer_process = subprocess.Popen(
                ustreamer_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            time.sleep(2)

            if self.ustreamer_process.poll() is None:
                logger.info("[Recovery] ustreamer restarted successfully")
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

    def start_display(self):
        """Start ustreamer and display pipeline."""
        # Kill any existing processes
        subprocess.run(['pkill', '-9', 'ustreamer'], capture_output=True)
        subprocess.run(['pkill', '-9', 'gst-launch'], capture_output=True)
        time.sleep(0.5)

        port = self.config.ustreamer_port

        # Start ustreamer (color correction done via GStreamer videobalance)
        # Note: HDMI-RX device doesn't support V4L2 image controls
        ustreamer_cmd = [
            'ustreamer',
            f'--device={self.device}',
            '--format=BGR24',
            f'--port={port}',
            '--quality=75',
            '--workers=8',
            '--buffers=8',
        ]

        logger.info(f"Starting ustreamer: {' '.join(ustreamer_cmd)}")

        # Clean up any stale resources from previous runs
        subprocess.run(['fuser', '-k', f'{port}/tcp'],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Remove stale frame files that might be owned by root
        for f in ['/dev/shm/stream_sentry_frame.jpg', '/dev/shm/stream_sentry_vlm_frame.jpg']:
            try:
                Path(f).unlink(missing_ok=True)
            except PermissionError:
                # File owned by root, try to work around it
                subprocess.run(['sudo', 'rm', '-f', f], capture_output=True)
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

            # Update overlay
            if self.ad_blocker:
                if self.ad_detected and self.blocking_source:
                    self.ad_blocker.show(self.blocking_source)
                else:
                    self.ad_blocker.hide()

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
                if not self.ad_detected and not self.is_scene_changed(frame) and not self.prev_frame_had_ad:
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
                ad_detected, matched_keywords, all_texts, is_terminal = self.ocr.check_ad_keywords(ocr_results)

                if ad_detected and not is_terminal:
                    self.ocr_ad_detection_count += 1
                    self.ocr_no_ad_count = 0
                    self.last_ocr_ad_time = time.time()

                    if self.ocr_ad_detection_count >= 1 and not self.ocr_ad_detected:
                        self.ocr_ad_detected = True
                        keywords_found = [kw for kw, txt in matched_keywords]
                        logger.info(f"OCR detected ad keywords: {keywords_found}")
                        self._save_screenshot(frame, matched_keywords, all_texts)
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
                    if self.ocr_ad_detected and self.vlm_ad_detected:
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

        vlm_image_path = f'/dev/shm/stream_sentry_vlm_frame_{os.getpid()}.jpg'

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

                if is_ad:
                    self.vlm_consecutive_ad_count += 1
                    self.vlm_no_ad_count = 0

                    if not self.vlm_ad_detected:
                        self.vlm_ad_detected = True
                        logger.info(f"VLM detected ad (x{self.vlm_consecutive_ad_count}): \"{response[:50]}\"")
                else:
                    self.vlm_no_ad_count += 1
                    self.vlm_consecutive_ad_count = 0

                    if self.vlm_ad_detected and self.vlm_no_ad_count >= self.VLM_STOP_THRESHOLD:
                        self.vlm_ad_detected = False
                        logger.info(f"VLM: ad no longer detected (after {self.VLM_STOP_THRESHOLD} no-ads)")

                self._update_blocking_state()

                ad_status = "AD" if is_ad else "NO-AD"
                response_preview = response[:40] if response else "no response"
                logger.info(f"VLM #{self.vlm_frame_count}: {elapsed:.1f}s [{ad_status}] \"{response_preview}\"")

                self.vlm_prev_frame = frame.copy()
                self.vlm_prev_frame_had_ad = is_ad
                self.vlm_scene_skip_count = 0  # Reset skip counter after processing

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

    def _save_screenshot(self, frame, matched_keywords, all_texts):
        """Save screenshot when ad detected (with deduplication)."""
        # Check for duplicate using perceptual hash
        img_hash = self._compute_image_hash(frame)
        if img_hash is not None and img_hash in self.screenshot_hashes:
            return  # Skip duplicate

        # Add hash to set
        if img_hash is not None:
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
        logger.info("Starting Stream Sentry...")

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
                        while self.running:
                            time.sleep(2)
                            signal_info = self.check_hdmi_signal()
                            if signal_info:
                                width, height, fps = signal_info
                                logger.info(f"HDMI signal detected: {width}x{height} @ {fps}fps - switching to normal mode")
                                # Stop no-signal display and transition to normal operation
                                self.ad_blocker.destroy()
                                # Reinitialize ad_blocker for normal operation
                                self.ad_blocker = AdBlocker(
                                    connector_id=self.config.drm_connector_id,
                                    plane_id=self.config.drm_plane_id,
                                    stream_sentry=self,
                                    ustreamer_port=self.config.ustreamer_port
                                )
                                if self.audio:
                                    self.ad_blocker.set_audio(self.audio)
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

        # Start display
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

        if self.vlm:
            logger.info("Loading VLM model (Qwen3-VL-2B-INT4)...")
            if self.vlm.load_model():
                logger.info("VLM model loaded successfully")
                self.vlm_thread = threading.Thread(target=self.vlm_worker, daemon=True)
                self.vlm_thread.start()
                logger.info(f"VLM worker started with prompt: \"{self.vlm.AD_PROMPT}\"")
            else:
                logger.warning("VLM model failed to load - VLM detection disabled")
                self.vlm = None

        # Start health monitor
        if self.health_monitor:
            self.health_monitor.start()
            logger.info("Health monitor started")

        logger.info("Stream Sentry running - press Ctrl+C to stop")

        # Monitor ustreamer
        try:
            while self.running:
                if self.ustreamer_process and self.ustreamer_process.poll() is not None:
                    logger.warning("ustreamer process died, restarting...")
                    if not self.start_display():
                        logger.error("Failed to restart display")
                        break
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        self.stop()
        return True

    def stop(self):
        """Stop everything."""
        logger.info("Stopping...")
        self.running = False

        # Stop health monitor first
        if self.health_monitor:
            self.health_monitor.stop()

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
        description='Stream Sentry - HDMI passthrough with ML-based ad detection'
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
        default=215,
        help='DRM connector ID for HDMI output (default: 215)'
    )
    parser.add_argument(
        '--plane-id',
        type=int,
        default=72,
        help='DRM plane ID for video overlay (default: 72)'
    )

    args = parser.parse_args()

    config = StreamConfig(
        device=args.device,
        screenshot_dir=args.screenshot_dir,
        ocr_timeout=args.ocr_timeout,
        max_screenshots=args.max_screenshots,
        drm_connector_id=args.connector_id,
        drm_plane_id=args.plane_id,
    )

    sentry = StreamSentry(config)

    if args.check_signal:
        signal_info = sentry.check_hdmi_signal()
        if signal_info:
            width, height, fps = signal_info
            print(f"HDMI signal detected: {width}x{height} @ {fps}fps")
            sys.exit(0)
        else:
            print("No HDMI signal detected")
            sys.exit(1)

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        sentry.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    sentry.run()


if __name__ == '__main__':
    main()
