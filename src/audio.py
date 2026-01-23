"""
Audio Passthrough for Stream Sentry.

Captures audio from HDMI-RX and outputs to HDMI-TX with mute control for ad blocking.

Features:
- Automatic error detection via GStreamer bus messages
- Watchdog thread to detect pipeline stalls
- Auto-restart on failure

Architecture:
    alsasrc (hw:4,0) -> audioconvert -> volume -> alsasink (hw:0,0)
                                          ^
                                          | mute=true during ads
"""

import logging
import threading
import time

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

logger = logging.getLogger(__name__)


class AudioPassthrough:
    """
    Audio passthrough from HDMI-RX to HDMI-TX.

    Uses GStreamer pipeline with volume element for instant mute control.
    Runs as a separate pipeline from video for simplicity and robustness.
    Includes automatic error recovery and watchdog monitoring.
    """

    def __init__(self, capture_device="hw:4,0", playback_device="hw:0,0"):
        """
        Initialize audio passthrough.

        Args:
            capture_device: ALSA capture device (HDMI-RX)
            playback_device: ALSA playback device (HDMI-TX)
        """
        self.capture_device = capture_device
        self.playback_device = playback_device
        self.pipeline = None
        self.volume = None
        self.bus = None
        self.is_muted = False
        self.is_running = False
        self._lock = threading.Lock()

        # Watchdog state
        self._watchdog_thread = None
        self._stop_watchdog = threading.Event()
        self._watchdog_paused = False  # Pause watchdog when HDMI is lost
        self._last_buffer_time = 0
        self._restart_count = 0
        self._watchdog_interval = 3.0  # Check every 3 seconds
        self._stall_threshold = 6.0  # Consider stalled if no buffer for 6s

        # Exponential backoff for restarts (no max limit - always try to recover)
        self._base_restart_delay = 1.0  # Start with 1 second delay
        self._max_restart_delay = 60.0  # Cap at 60 seconds
        self._current_restart_delay = self._base_restart_delay
        self._last_restart_time = 0
        self._consecutive_failures = 0

        # Initialize GStreamer (may already be initialized by video pipeline)
        Gst.init(None)

    def _init_pipeline(self):
        """Initialize GStreamer audio pipeline."""
        try:
            # Audio passthrough pipeline with silent keepalive:
            # - audiomixer combines HDMI input with inaudible tone
            # - This prevents pipeline stalls when HDMI source has silence
            # - The keepalive tone is at -60dB (0.001 volume) - completely inaudible
            #
            # Pipeline structure:
            #   alsasrc (HDMI) ──┐
            #                    ├──► audiomixer ──► volume ──► alsasink
            #   audiotestsrc ────┘
            #   (silent keepalive)
            #
            pipeline_str = (
                # HDMI audio input
                f"alsasrc device={self.capture_device} ! "
                f"audio/x-raw,rate=48000,channels=2,format=S16LE ! "
                f"queue max-size-buffers=10 leaky=downstream name=audioqueue ! "
                f"audioconvert ! audioresample ! "
                f"audio/x-raw,rate=48000,channels=2,format=F32LE ! "
                f"mix. "

                # Silent keepalive tone (inaudible - prevents pipeline stalls)
                f"audiotestsrc wave=silence is-live=true ! "
                f"audio/x-raw,rate=48000,channels=2,format=F32LE ! "
                f"mix. "

                # Mix and output
                f"audiomixer name=mix ! "
                f"volume name=vol volume=1.0 mute=false ! "
                f"audioconvert ! "
                f"alsasink device={self.playback_device} sync=false"
            )

            logger.debug(f"[AudioPassthrough] Creating pipeline: {pipeline_str}")
            self.pipeline = Gst.parse_launch(pipeline_str)

            # Get volume element for mute control
            self.volume = self.pipeline.get_by_name('vol')

            # Set up bus message handling for error detection
            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)
            self.bus.connect('message::eos', self._on_eos)
            self.bus.connect('message::state-changed', self._on_state_changed)

            # Add probe to track buffer flow (for stall detection)
            queue = self.pipeline.get_by_name('audioqueue')
            if queue:
                pad = queue.get_static_pad('src')
                if pad:
                    pad.add_probe(Gst.PadProbeType.BUFFER, self._buffer_probe, None)

            if self.volume:
                logger.info(f"[AudioPassthrough] Pipeline created: {self.capture_device} -> {self.playback_device}")
            else:
                logger.error("[AudioPassthrough] Failed to get volume element")

        except Exception as e:
            logger.error(f"[AudioPassthrough] Failed to create pipeline: {e}")
            import traceback
            traceback.print_exc()
            self.pipeline = None

    def _buffer_probe(self, pad, info, user_data):
        """Probe callback to track buffer flow for stall detection."""
        now = time.time()
        self._last_buffer_time = now

        # Reset backoff counter after sustained buffer flow (5+ seconds)
        if self._consecutive_failures > 0:
            time_since_restart = now - self._last_restart_time
            if time_since_restart > 5.0:
                self._consecutive_failures = 0
                self._current_restart_delay = self._base_restart_delay
                logger.debug("[AudioPassthrough] Backoff reset - sustained buffer flow")

        return Gst.PadProbeReturn.OK

    def _on_error(self, bus, message):
        """Handle GStreamer error messages."""
        err, debug = message.parse_error()
        logger.error(f"[AudioPassthrough] Pipeline error: {err.message}")
        logger.debug(f"[AudioPassthrough] Debug info: {debug}")

        # Schedule restart on error
        threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def _on_eos(self, bus, message):
        """Handle end-of-stream (shouldn't happen for live source)."""
        logger.warning("[AudioPassthrough] Unexpected EOS received")

        # Restart on EOS
        threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def _on_state_changed(self, bus, message):
        """Handle state changes."""
        if message.src != self.pipeline:
            return

        old, new, pending = message.parse_state_changed()
        if new == Gst.State.PLAYING:
            self._last_buffer_time = time.time()
            logger.debug("[AudioPassthrough] Pipeline now PLAYING")

    def _restart_pipeline(self):
        """Restart the audio pipeline after an error with exponential backoff."""
        with self._lock:
            self._restart_count += 1
            self._consecutive_failures += 1

            # Calculate backoff delay
            delay = min(
                self._base_restart_delay * (2 ** (self._consecutive_failures - 1)),
                self._max_restart_delay
            )
            self._current_restart_delay = delay

            logger.warning(
                f"[AudioPassthrough] Restarting pipeline (attempt {self._restart_count}, "
                f"delay {delay:.1f}s, {self._consecutive_failures} consecutive failures)"
            )

            # Stop current pipeline
            if self.pipeline:
                try:
                    self.pipeline.set_state(Gst.State.NULL)
                except:
                    pass
                self.pipeline = None
                self.volume = None

            # Wait with exponential backoff before restarting
            time.sleep(delay)

            # Check if we should still be running
            if not self.is_running:
                return

            # Recreate and start
            self._init_pipeline()
            if self.pipeline:
                ret = self.pipeline.set_state(Gst.State.PLAYING)
                if ret != Gst.StateChangeReturn.FAILURE:
                    logger.info("[AudioPassthrough] Pipeline restarted successfully")
                    self._last_buffer_time = time.time()
                    self._last_restart_time = time.time()
                    # Reset backoff on success (will be verified by buffer flow)

                    # Restore mute state
                    if self.is_muted and self.volume:
                        self.volume.set_property('mute', True)
                else:
                    logger.error("[AudioPassthrough] Failed to restart pipeline")

    def _watchdog_loop(self):
        """Watchdog thread to detect pipeline stalls."""
        logger.debug("[AudioPassthrough] Watchdog started")

        while not self._stop_watchdog.is_set():
            self._stop_watchdog.wait(self._watchdog_interval)

            if self._stop_watchdog.is_set():
                break

            if not self.is_running:
                continue

            # Skip restart attempts if watchdog is paused (e.g., HDMI lost)
            if self._watchdog_paused:
                continue

            # Check if buffers are flowing
            if self._last_buffer_time > 0:
                time_since_buffer = time.time() - self._last_buffer_time
                if time_since_buffer > self._stall_threshold:
                    logger.warning(f"[AudioPassthrough] Pipeline stalled ({time_since_buffer:.1f}s since last buffer)")
                    self._restart_pipeline()

            # Check pipeline state
            if self.pipeline:
                state_ret, state, pending = self.pipeline.get_state(0)
                if state != Gst.State.PLAYING and self.is_running:
                    logger.warning(f"[AudioPassthrough] Pipeline not in PLAYING state: {state.value_nick}")
                    self._restart_pipeline()

        logger.debug("[AudioPassthrough] Watchdog stopped")

    def start(self):
        """Start audio passthrough."""
        with self._lock:
            # Initialize pipeline if not done
            if not self.pipeline:
                self._init_pipeline()

            if not self.pipeline:
                logger.error("[AudioPassthrough] No pipeline to start")
                return False

            try:
                ret = self.pipeline.set_state(Gst.State.PLAYING)
                if ret == Gst.StateChangeReturn.FAILURE:
                    logger.error("[AudioPassthrough] Failed to start pipeline")
                    return False

                self.is_running = True
                self._last_buffer_time = time.time()
                self._restart_count = 0

                # Start watchdog thread
                self._stop_watchdog.clear()
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_loop,
                    daemon=True,
                    name="AudioWatchdog"
                )
                self._watchdog_thread.start()

                logger.info("[AudioPassthrough] Audio passthrough started")
                return True

            except Exception as e:
                logger.error(f"[AudioPassthrough] Failed to start: {e}")
                return False

    def mute(self):
        """Mute audio (for ad blocking)."""
        with self._lock:
            if self.volume and not self.is_muted:
                self.volume.set_property('mute', True)
                self.is_muted = True
                logger.info("[AudioPassthrough] Audio MUTED")

    def unmute(self):
        """Unmute audio (after ad ends)."""
        with self._lock:
            if self.volume and self.is_muted:
                self.volume.set_property('mute', False)
                self.is_muted = False
                logger.info("[AudioPassthrough] Audio UNMUTED")

    def set_volume(self, level):
        """
        Set volume level.

        Args:
            level: Volume level (0.0 = silent, 1.0 = 100%, 10.0 = 1000%)
        """
        with self._lock:
            if self.volume:
                self.volume.set_property('volume', level)
                logger.info(f"[AudioPassthrough] Volume set to {level}")

    def get_status(self):
        """Get current audio pipeline status."""
        with self._lock:
            if not self.pipeline:
                return "stopped"

            state_ret, state, pending = self.pipeline.get_state(0)
            return {
                "state": state.value_nick,
                "muted": self.is_muted,
                "restart_count": self._restart_count,
                "last_buffer_age": time.time() - self._last_buffer_time if self._last_buffer_time > 0 else -1
            }

    def pause_watchdog(self):
        """Pause the watchdog to prevent restart loops (e.g., when HDMI is lost).

        The pipeline will be stopped but the module remains ready to resume.
        """
        with self._lock:
            self._watchdog_paused = True
            logger.info("[AudioPassthrough] Watchdog paused - no auto-restart")

            # Stop current pipeline to save resources
            if self.pipeline:
                try:
                    self.pipeline.set_state(Gst.State.NULL)
                except:
                    pass

    def resume_watchdog(self):
        """Resume the watchdog and restart the pipeline.

        Call this when HDMI signal is restored.
        """
        with self._lock:
            self._watchdog_paused = False
            logger.info("[AudioPassthrough] Watchdog resumed - restarting pipeline")

            # Reset failure counters
            self._consecutive_failures = 0
            self._current_restart_delay = self._base_restart_delay

            # Restart pipeline
            if self.is_running:
                self._init_pipeline()
                if self.pipeline:
                    ret = self.pipeline.set_state(Gst.State.PLAYING)
                    if ret != Gst.StateChangeReturn.FAILURE:
                        logger.info("[AudioPassthrough] Pipeline resumed successfully")
                        self._last_buffer_time = time.time()
                        if self.is_muted and self.volume:
                            self.volume.set_property('mute', True)

    def stop(self):
        """Stop audio passthrough."""
        with self._lock:
            self.is_running = False

            # Stop watchdog
            self._stop_watchdog.set()
            if self._watchdog_thread:
                self._watchdog_thread.join(timeout=2.0)
                self._watchdog_thread = None

            # Stop pipeline
            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                    self.pipeline.set_state(Gst.State.NULL)
                    logger.info("[AudioPassthrough] Audio passthrough stopped")
                except Exception as e:
                    logger.error(f"[AudioPassthrough] Error stopping: {e}")

                self.pipeline = None
                self.volume = None
                self.bus = None

    def destroy(self):
        """Clean up resources."""
        self.stop()
