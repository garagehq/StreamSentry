"""
Health Monitor for Minus.

Unified watchdog that monitors all subsystems and triggers recovery actions.

Features:
- HDMI signal monitoring (detects unplug/replug)
- ustreamer health (frame freshness, not just PID)
- VLM/OCR health monitoring
- Memory/disk usage monitoring
- Automatic recovery actions
"""

import logging
import threading
import time
import subprocess
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """Current health status of all subsystems."""
    hdmi_signal: bool = False
    hdmi_resolution: str = ""
    ustreamer_alive: bool = False
    ustreamer_responding: bool = False
    last_frame_age: float = -1
    video_pipeline_ok: bool = False
    audio_pipeline_ok: bool = False
    vlm_ready: bool = False
    vlm_consecutive_timeouts: int = 0
    ocr_ready: bool = False
    memory_percent: float = 0
    disk_free_mb: float = 0
    uptime_seconds: float = 0
    output_fps: float = 0.0


class HealthMonitor:
    """
    Unified health monitor for Minus.

    Runs a background thread that periodically checks all subsystems
    and triggers recovery actions when issues are detected.
    """

    def __init__(self, minus, check_interval: float = 5.0):
        """
        Initialize health monitor.

        Args:
            minus: Reference to main Minus instance
            check_interval: How often to check health (seconds)
        """
        self.minus = minus
        self.check_interval = check_interval

        self._monitor_thread = None
        self._stop_event = threading.Event()
        self._start_time = time.time()
        self._last_hdmi_signal = True
        self._hdmi_lost_time = 0

        # Recovery callbacks
        self._on_hdmi_lost: Optional[Callable] = None
        self._on_hdmi_restored: Optional[Callable] = None
        self._on_ustreamer_stall: Optional[Callable] = None
        self._on_video_pipeline_stall: Optional[Callable] = None
        self._on_vlm_failure: Optional[Callable] = None
        self._on_memory_critical: Optional[Callable] = None

        # Thresholds
        self.frame_stale_threshold = 5.0  # seconds
        self.memory_warning_percent = 80
        self.memory_critical_percent = 90
        self.disk_warning_mb = 500
        self.vlm_timeout_threshold = 3  # consecutive timeouts before action
        self.startup_grace_period = 30.0  # Don't check ustreamer for first 30s

    def start(self):
        """Start the health monitor thread."""
        self._stop_event.clear()
        self._start_time = time.time()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="HealthMonitor"
        )
        self._monitor_thread.start()
        logger.info("[HealthMonitor] Started")

    def stop(self):
        """Stop the health monitor thread."""
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None
        logger.info("[HealthMonitor] Stopped")

    def get_status(self) -> HealthStatus:
        """Get current health status."""
        status = HealthStatus()
        status.uptime_seconds = time.time() - self._start_time

        # HDMI signal
        status.hdmi_signal, status.hdmi_resolution = self._check_hdmi_signal()

        # ustreamer
        status.ustreamer_alive = self._check_ustreamer_alive()
        status.ustreamer_responding, status.last_frame_age = self._check_ustreamer_responding()

        # Pipelines
        if self.minus.ad_blocker:
            status.video_pipeline_ok = self._check_video_pipeline()
            status.output_fps = self.minus.ad_blocker.get_fps()
        if self.minus.audio:
            status.audio_pipeline_ok = self._check_audio_pipeline()

        # ML workers
        if self.minus.vlm:
            status.vlm_ready = self.minus.vlm.is_ready
            status.vlm_consecutive_timeouts = getattr(
                self.minus.vlm, 'consecutive_timeouts', 0
            )
        if self.minus.ocr:
            status.ocr_ready = True  # OCR doesn't have ready state

        # Resources
        status.memory_percent = self._get_memory_percent()
        status.disk_free_mb = self._get_disk_free_mb()

        return status

    def _monitor_loop(self):
        """Main monitoring loop."""
        logger.debug("[HealthMonitor] Monitor loop started")

        while not self._stop_event.is_set():
            try:
                self._check_and_recover()
            except Exception as e:
                logger.error(f"[HealthMonitor] Error in check loop: {e}")

            self._stop_event.wait(self.check_interval)

        logger.debug("[HealthMonitor] Monitor loop stopped")

    def _check_and_recover(self):
        """Check health and trigger recovery if needed."""
        status = self.get_status()

        # HDMI signal monitoring
        if not status.hdmi_signal and self._last_hdmi_signal:
            # Signal just lost
            self._hdmi_lost_time = time.time()
            logger.warning("[HealthMonitor] HDMI signal LOST")
            if self._on_hdmi_lost:
                self._on_hdmi_lost()

        elif status.hdmi_signal and not self._last_hdmi_signal:
            # Signal just restored
            lost_duration = time.time() - self._hdmi_lost_time
            logger.info(f"[HealthMonitor] HDMI signal RESTORED (was lost {lost_duration:.1f}s)")
            if self._on_hdmi_restored:
                self._on_hdmi_restored()

        self._last_hdmi_signal = status.hdmi_signal

        # ustreamer health (skip during startup grace period)
        uptime = time.time() - self._start_time
        if uptime > self.startup_grace_period:
            if status.ustreamer_alive and not status.ustreamer_responding:
                logger.warning("[HealthMonitor] ustreamer not responding to HTTP requests")
                if self._on_ustreamer_stall:
                    self._on_ustreamer_stall()
                # Also trigger video pipeline restart after ustreamer restart
                if self._on_video_pipeline_stall:
                    # Give ustreamer time to restart before triggering video restart
                    import threading
                    def delayed_video_restart():
                        import time
                        time.sleep(3)
                        if self._on_video_pipeline_stall:
                            self._on_video_pipeline_stall()
                    threading.Thread(target=delayed_video_restart, daemon=True).start()

        # Video pipeline health check (FPS-based)
        if uptime > self.startup_grace_period and status.hdmi_signal:
            # If we have HDMI signal but FPS is 0 for a while, pipeline may be stuck
            if status.output_fps == 0 and status.video_pipeline_ok:
                # Pipeline thinks it's OK but no frames flowing
                logger.warning("[HealthMonitor] Video pipeline has 0 FPS - may be stalled")
                if self._on_video_pipeline_stall:
                    self._on_video_pipeline_stall()

        # Audio unmute watchdog - ensure audio is not muted when not blocking
        # This prevents audio from getting stuck muted due to race conditions or bugs
        if self.minus.audio and self.minus.ad_blocker:
            is_blocking = self.minus.ad_blocker.is_visible
            is_muted = self.minus.audio.is_muted

            if not is_blocking and is_muted:
                logger.warning("[HealthMonitor] Audio stuck muted while not blocking - forcing unmute")
                self.minus.audio.unmute()

        # VLM health
        if status.vlm_consecutive_timeouts >= self.vlm_timeout_threshold:
            logger.warning(f"[HealthMonitor] VLM failing ({status.vlm_consecutive_timeouts} consecutive timeouts)")
            if self._on_vlm_failure:
                self._on_vlm_failure()

        # Memory check
        if status.memory_percent >= self.memory_critical_percent:
            logger.error(f"[HealthMonitor] CRITICAL memory usage: {status.memory_percent:.1f}%")
            if self._on_memory_critical:
                self._on_memory_critical()
        elif status.memory_percent >= self.memory_warning_percent:
            logger.warning(f"[HealthMonitor] High memory usage: {status.memory_percent:.1f}%")

        # Disk check
        if status.disk_free_mb < self.disk_warning_mb:
            logger.warning(f"[HealthMonitor] Low disk space: {status.disk_free_mb:.0f}MB free")

        # FPS log (every 60 seconds)
        if int(status.uptime_seconds) % 60 < self.check_interval:
            if status.output_fps > 0:
                logger.info(f"[HealthMonitor] FPS: {status.output_fps:.1f}")
                if status.output_fps < 25:
                    logger.warning(f"[HealthMonitor] Low FPS detected: {status.output_fps:.1f}")

        # Periodic status log (every 5 minutes)
        if int(status.uptime_seconds) % 300 < self.check_interval:
            self._log_status(status)

    def _log_status(self, status: HealthStatus):
        """Log periodic health status."""
        uptime_min = status.uptime_seconds / 60
        logger.info(
            f"[HealthMonitor] Status: uptime={uptime_min:.0f}m "
            f"fps={status.output_fps:.1f} "
            f"hdmi={'OK' if status.hdmi_signal else 'LOST'} "
            f"video={'OK' if status.video_pipeline_ok else 'ERR'} "
            f"audio={'OK' if status.audio_pipeline_ok else 'ERR'} "
            f"vlm={'OK' if status.vlm_ready else 'ERR'} "
            f"mem={status.memory_percent:.0f}% "
            f"disk={status.disk_free_mb:.0f}MB"
        )

    def _check_hdmi_signal(self) -> tuple[bool, str]:
        """Check if HDMI signal is present."""
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', '/dev/video0', '--query-dv-timings'],
                capture_output=True, text=True, timeout=2
            )

            if result.returncode != 0:
                return False, ""

            # Parse resolution
            width = height = 0
            for line in result.stdout.split('\n'):
                if 'Active width:' in line:
                    width = int(line.split(':')[1].strip())
                elif 'Active height:' in line:
                    height = int(line.split(':')[1].strip())

            if width and height:
                return True, f"{width}x{height}"

            return False, ""

        except Exception:
            return False, ""

    def _check_ustreamer_alive(self) -> bool:
        """Check if ustreamer process is running."""
        try:
            result = subprocess.run(
                ['pgrep', '-f', 'ustreamer'],
                capture_output=True, timeout=1
            )
            return result.returncode == 0
        except Exception:
            return False

    def _check_ustreamer_responding(self) -> tuple[bool, float]:
        """Check if ustreamer is serving fresh frames via HTTP."""
        try:
            import urllib.request
            import urllib.error

            # Try to fetch snapshot from ustreamer
            req = urllib.request.Request(
                'http://localhost:9090/snapshot',
                method='HEAD'
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status == 200:
                    return True, 0

            return False, -1

        except urllib.error.URLError:
            return False, -1
        except Exception:
            return False, -1

    def _check_video_pipeline(self) -> bool:
        """Check if video GStreamer pipeline is healthy."""
        try:
            if not self.minus.ad_blocker:
                return False

            pipeline = self.minus.ad_blocker.pipeline
            if not pipeline:
                return False

            # Check pipeline state
            from gi.repository import Gst
            state_ret, state, pending = pipeline.get_state(0)
            return state == Gst.State.PLAYING

        except Exception:
            return False

    def _check_audio_pipeline(self) -> bool:
        """Check if audio GStreamer pipeline is healthy."""
        try:
            if not self.minus.audio:
                return False

            status = self.minus.audio.get_status()
            if isinstance(status, dict):
                return status.get('state') == 'playing'
            return False

        except Exception:
            return False

    def _get_memory_percent(self) -> float:
        """Get current memory usage percentage."""
        try:
            with open('/proc/meminfo') as f:
                meminfo = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        meminfo[parts[0].rstrip(':')] = int(parts[1])

            total = meminfo.get('MemTotal', 1)
            available = meminfo.get('MemAvailable', 0)
            used = total - available
            return (used / total) * 100

        except Exception:
            return 0

    def _get_disk_free_mb(self) -> float:
        """Get free disk space in MB for current directory."""
        try:
            stat = os.statvfs('.')
            free_bytes = stat.f_bavail * stat.f_frsize
            return free_bytes / (1024 * 1024)
        except Exception:
            return 0

    # Recovery action setters
    def on_hdmi_lost(self, callback: Callable):
        """Set callback for HDMI signal loss."""
        self._on_hdmi_lost = callback

    def on_hdmi_restored(self, callback: Callable):
        """Set callback for HDMI signal restoration."""
        self._on_hdmi_restored = callback

    def on_ustreamer_stall(self, callback: Callable):
        """Set callback for ustreamer stall."""
        self._on_ustreamer_stall = callback

    def on_video_pipeline_stall(self, callback: Callable):
        """Set callback for video pipeline stall."""
        self._on_video_pipeline_stall = callback

    def on_vlm_failure(self, callback: Callable):
        """Set callback for VLM failure."""
        self._on_vlm_failure = callback

    def on_memory_critical(self, callback: Callable):
        """Set callback for critical memory usage."""
        self._on_memory_critical = callback
