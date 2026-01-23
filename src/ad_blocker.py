"""
Ad Blocker Overlay for Minus.

Displays a blocking overlay when ads are detected on screen.
Uses ustreamer's native blocking mode for smooth 60fps overlays and animations.

Architecture:
- Simple GStreamer pipeline with queue element for smooth video display
- All overlay compositing done in ustreamer's MPP encoder (60fps preview!)
- Control via HTTP API to ustreamer's /blocking endpoints

Features:
- 60fps live preview window (vs ~4fps with GStreamer gdkpixbufoverlay)
- Smooth animations via rapid API updates
- Spanish vocabulary practice during ad blocks
- Pixelated background from pre-ad content
"""

import os
import threading
import time
import random
import logging
import urllib.request
import urllib.parse
import json
from collections import deque

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

# Import vocabulary from extracted module
from vocabulary import SPANISH_VOCABULARY

# Set up logging
logger = logging.getLogger(__name__)



class DRMAdBlocker:
    """
    DRM-based ad blocker using ustreamer's native blocking mode.

    Uses a simple GStreamer pipeline for display with queue element for smooth playback.
    All overlay compositing (background, preview, text) done in ustreamer's MPP encoder.
    """

    def __init__(self, connector_id=215, plane_id=72, minus_instance=None, ustreamer_port=9090,
                 output_width=1920, output_height=1080):
        self.is_visible = False
        self.current_source = None
        self.connector_id = connector_id
        self.plane_id = plane_id
        self.ustreamer_port = ustreamer_port
        self.minus = minus_instance
        self.output_width = output_width or 1920
        self.output_height = output_height or 1080
        self._lock = threading.Lock()

        # GStreamer pipeline
        self.pipeline = None
        self.bus = None

        # Audio passthrough reference
        self.audio = None

        # Pipeline health tracking
        self._pipeline_errors = 0
        self._last_error_time = 0
        self._pipeline_restarting = False
        self._restart_lock = threading.Lock()

        # FPS tracking
        self._frame_count = 0
        self._fps_start_time = time.time()
        self._current_fps = 0.0
        self._fps_lock = threading.Lock()

        # Video buffer watchdog
        self._last_buffer_time = 0
        self._watchdog_thread = None
        self._stop_watchdog = threading.Event()
        self._watchdog_interval = 3.0
        self._stall_threshold = 10.0
        self._restart_count = 0
        self._last_restart_time = 0
        self._consecutive_failures = 0
        self._base_restart_delay = 1.0
        self._max_restart_delay = 30.0
        self._success_reset_time = 10.0

        # Text rotation
        self._rotation_thread = None
        self._stop_rotation = threading.Event()

        # Debug overlay
        self._debug_overlay_enabled = True
        self._debug_thread = None
        self._stop_debug = threading.Event()
        self._debug_interval = 2.0
        self._total_blocking_time = 0.0
        self._current_block_start = None
        self._total_ads_blocked = 0

        # Preview settings - use actual capture resolution for positioning
        self._preview_enabled = True
        self._frame_width, self._frame_height = self._detect_frame_resolution()
        self._preview_w = int(self._frame_width * 0.20)
        self._preview_h = int(self._frame_height * 0.20)
        self._preview_padding = int(self._frame_height * 0.02)

        # Skip status
        self._skip_available = False
        self._skip_text = None

        # Time saved tracking
        self._total_time_saved = 0.0

        # Animation settings
        self._animation_thread = None
        self._stop_animation = threading.Event()
        self._animation_duration_start = 0.3  # Reduced from 1.25s for faster response
        self._animation_duration_end = 0.25   # Reduced from 0.5s for faster unblock
        self._animating = False
        self._animation_direction = None
        self._animation_source = None

        # Text background box opacity (0=transparent, 255=opaque)
        # Default was 180, increased for better readability
        self._box_alpha = 220

        # Text color in YUV (white - clean and readable, doesn't distract from vocabulary)
        # White: Y=235, U=128, V=128
        self._text_y = 235
        self._text_u = 128
        self._text_v = 128

        # Current vocabulary word tracking
        self._current_vocab = None  # (spanish, pronunciation, english, example)

        # Test mode
        self._test_blocking_until = 0

        # Snapshot buffer
        self._snapshot_buffer = deque(maxlen=3)
        self._snapshot_buffer_thread = None
        self._stop_snapshot_buffer = threading.Event()
        self._snapshot_interval = 2.0

        # Initialize GStreamer
        Gst.init(None)
        self._init_pipeline()
        self._start_snapshot_buffer()

    def _detect_frame_resolution(self):
        """Detect actual capture frame resolution from ustreamer."""
        try:
            url = f"http://localhost:{self.ustreamer_port}/state"
            with urllib.request.urlopen(url, timeout=2.0) as response:
                data = json.loads(response.read().decode('utf-8'))
                width = data.get('result', {}).get('source', {}).get('resolution', {}).get('width', 1920)
                height = data.get('result', {}).get('source', {}).get('resolution', {}).get('height', 1080)
                logger.info(f"[DRMAdBlocker] Detected frame resolution: {width}x{height}")
                return width, height
        except Exception as e:
            logger.warning(f"[DRMAdBlocker] Could not detect frame resolution: {e}, using 1920x1080")
            return 1920, 1080

    def _blocking_api_call(self, endpoint, params=None, data=None, method='GET', timeout=0.1):
        """Make an API call to ustreamer blocking endpoint."""
        try:
            url = f"http://localhost:{self.ustreamer_port}{endpoint}"
            if params:
                url += '?' + urllib.parse.urlencode(params)

            if method == 'POST' and data:
                req = urllib.request.Request(url, data=data, method='POST')
                req.add_header('Content-Type', 'image/jpeg')
            else:
                req = urllib.request.Request(url)

            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            logger.debug(f"[DRMAdBlocker] API call error ({endpoint}): {e}")
            return None

    def _init_pipeline(self):
        """Initialize simple GStreamer display pipeline with queue element."""
        try:
            # Simple pipeline with queue element to prevent buffer buildup
            pipeline_str = (
                f"souphttpsrc location=http://localhost:{self.ustreamer_port}/stream blocksize=524288 ! "
                f"multipartdemux ! jpegparse ! mppjpegdec ! video/x-raw,format=NV12 ! "
                f"videobalance saturation=0.85 name=colorbalance ! "
                f"queue max-size-buffers=3 leaky=downstream name=videoqueue ! "
                f"identity name=fpsprobe ! "
                f"kmssink plane-id={self.plane_id} connector-id={self.connector_id} sync=false"
            )

            logger.debug("[DRMAdBlocker] Creating pipeline with queue element...")
            self.pipeline = Gst.parse_launch(pipeline_str)

            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)
            self.bus.connect('message::eos', self._on_eos)
            self.bus.connect('message::warning', self._on_warning)

            fpsprobe = self.pipeline.get_by_name('fpsprobe')
            if fpsprobe:
                srcpad = fpsprobe.get_static_pad('src')
                srcpad.add_probe(Gst.PadProbeType.BUFFER, self._fps_probe_callback, None)

            logger.info("[DRMAdBlocker] Pipeline created (ustreamer blocking mode)")

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to initialize GStreamer: {e}")
            self.pipeline = None

    def _fps_probe_callback(self, pad, info, user_data):
        current_time = time.time()
        self._last_buffer_time = current_time

        if self._consecutive_failures > 0:
            if current_time - self._last_restart_time > self._success_reset_time:
                self._consecutive_failures = 0

        with self._fps_lock:
            self._frame_count += 1
            elapsed = current_time - self._fps_start_time
            if elapsed >= 1.0:
                self._current_fps = self._frame_count / elapsed
                self._frame_count = 0
                self._fps_start_time = current_time

        return Gst.PadProbeReturn.OK

    def get_fps(self):
        with self._fps_lock:
            return self._current_fps

    def start(self):
        # Stop any animations before starting normal pipeline
        self._stop_loading_animation()
        self._stop_no_signal_animation()

        # If we're in loading or no-signal mode, need to reinitialize the normal pipeline
        if self.current_source in ('loading', 'no_hdmi_device'):
            logger.info(f"[DRMAdBlocker] Transitioning from {self.current_source} to normal pipeline")
            # Stop and destroy the standalone pipeline
            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self.pipeline = None
            # Reinitialize the normal pipeline
            self._init_pipeline()

        if not self.pipeline:
            logger.error("[DRMAdBlocker] No pipeline to start")
            return False

        try:
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("[DRMAdBlocker] Failed to start pipeline")
                return False

            logger.info("[DRMAdBlocker] Pipeline started")
            self._start_watchdog()

            # Re-detect frame resolution now that ustreamer should be running
            self._update_frame_resolution()

            # Clear loading state
            self.current_source = None
            self.is_visible = False

            return True

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start pipeline: {e}")
            return False

    def _update_frame_resolution(self):
        """Update frame resolution and recalculate preview dimensions."""
        new_w, new_h = self._detect_frame_resolution()
        if new_w != self._frame_width or new_h != self._frame_height:
            self._frame_width = new_w
            self._frame_height = new_h
            self._preview_w = int(self._frame_width * 0.20)
            self._preview_h = int(self._frame_height * 0.20)
            self._preview_padding = int(self._frame_height * 0.02)
            logger.info(f"[DRMAdBlocker] Updated preview size to {self._preview_w}x{self._preview_h}")

    def _start_watchdog(self):
        self._stop_watchdog.clear()
        self._last_buffer_time = time.time()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="VideoWatchdog")
        self._watchdog_thread.start()

    def _stop_watchdog_thread(self):
        self._stop_watchdog.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None

    def _watchdog_loop(self):
        while not self._stop_watchdog.is_set():
            self._stop_watchdog.wait(self._watchdog_interval)
            if self._stop_watchdog.is_set():
                break
            if self._pipeline_restarting:
                continue
            if self._last_buffer_time > 0:
                time_since_buffer = time.time() - self._last_buffer_time
                if time_since_buffer > self._stall_threshold:
                    logger.warning(f"[DRMAdBlocker] Pipeline stalled ({time_since_buffer:.1f}s)")
                    self._restart_pipeline()
            if self.pipeline:
                try:
                    state_ret, state, pending = self.pipeline.get_state(0)
                    if state not in (Gst.State.PLAYING, Gst.State.PAUSED):
                        self._restart_pipeline()
                except Exception:
                    pass

    def _restart_pipeline(self):
        with self._restart_lock:
            if self._pipeline_restarting:
                return
            self._pipeline_restarting = True

        try:
            self._restart_count += 1
            self._consecutive_failures += 1
            delay = min(self._base_restart_delay * (2 ** (self._consecutive_failures - 1)), self._max_restart_delay)
            logger.warning(f"[DRMAdBlocker] Restarting pipeline (attempt {self._restart_count}, delay {delay:.1f}s)")

            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self.pipeline = None

            time.sleep(delay)
            self._init_pipeline()

            if self.pipeline:
                ret = self.pipeline.set_state(Gst.State.PLAYING)
                if ret != Gst.StateChangeReturn.FAILURE:
                    logger.info("[DRMAdBlocker] Pipeline restarted successfully")
                    self._last_buffer_time = time.time()
                    self._last_restart_time = time.time()
        finally:
            self._pipeline_restarting = False

    def restart(self):
        logger.info("[DRMAdBlocker] External restart requested")
        threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def start_no_signal_mode(self):
        """Start a standalone display for 'No Signal' message with DVD-style bouncing.

        This creates a simple pipeline using videotestsrc that doesn't depend on ustreamer.
        The text bounces around the screen like the classic DVD screensaver.
        """
        try:
            # Stop any existing animations
            self._stop_loading_animation()
            self._stop_no_signal_animation()

            # Stop existing pipeline if any
            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self.pipeline = None

            # Create a standalone pipeline for no-signal display with positioned text
            # Uses valignment=position and halignment=position to enable xpos/ypos control
            no_signal_pipeline = (
                f"videotestsrc pattern=black ! "
                f"video/x-raw,width=1920,height=1080,framerate=30/1 ! "
                f"textoverlay name=no_signal_text text=\"[ NO SIGNAL ]\" "
                f"valignment=position halignment=position xpos=0.5 ypos=0.5 "
                f"font-desc=\"Sans Bold 24\" ! "
                f"videoconvert ! video/x-raw,format=NV12 ! "
                f"kmssink plane-id={self.plane_id} connector-id={self.connector_id} sync=false"
            )

            logger.debug("[DRMAdBlocker] Creating no-signal pipeline with bounce animation...")
            self.pipeline = Gst.parse_launch(no_signal_pipeline)

            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)

            # Get the textoverlay element for animation
            self._no_signal_textoverlay = self.pipeline.get_by_name('no_signal_text')

            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("[DRMAdBlocker] Failed to start no-signal pipeline")
                return False

            self.is_visible = True
            self.current_source = 'no_hdmi_device'

            # Start the bouncing animation
            self._start_no_signal_animation()

            logger.info("[DRMAdBlocker] No-signal display started")
            return True
        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start no-signal mode: {e}")
            return False

    def _start_no_signal_animation(self):
        """Start the DVD-style bouncing animation thread."""
        self._stop_no_signal_anim = threading.Event()
        self._no_signal_anim_thread = threading.Thread(
            target=self._no_signal_animation_loop,
            daemon=True,
            name="NoSignalBounce"
        )
        self._no_signal_anim_thread.start()

    def _stop_no_signal_animation(self):
        """Stop the bouncing animation thread."""
        if hasattr(self, '_stop_no_signal_anim'):
            self._stop_no_signal_anim.set()
        if hasattr(self, '_no_signal_anim_thread') and self._no_signal_anim_thread:
            self._no_signal_anim_thread.join(timeout=1.0)
            self._no_signal_anim_thread = None
        self._no_signal_textoverlay = None

    def _no_signal_animation_loop(self):
        """Animate the NO SIGNAL text bouncing around like DVD screensaver."""
        import time

        # Position and velocity (0.0 to 1.0 range)
        x, y = 0.5, 0.5
        vx, vy = 0.008, 0.006  # Velocity per frame

        # Boundaries (leave margin for text size)
        min_x, max_x = 0.1, 0.9
        min_y, max_y = 0.1, 0.9

        # Corner hit celebration
        corner_hit_frames = 0
        spin_angle = 0

        while not self._stop_no_signal_anim.is_set():
            try:
                # Update position
                x += vx
                y += vy

                # Track if we hit edges
                hit_x = False
                hit_y = False

                # Bounce off edges
                if x <= min_x or x >= max_x:
                    vx = -vx
                    x = max(min_x, min(max_x, x))
                    hit_x = True
                if y <= min_y or y >= max_y:
                    vy = -vy
                    y = max(min_y, min(max_y, y))
                    hit_y = True

                # Corner hit! Start celebration spin
                if hit_x and hit_y:
                    corner_hit_frames = 30  # Celebrate for 30 frames (~1 second)
                    logger.info("[DRMAdBlocker] NO SIGNAL hit corner! 🎉")

                # Update textoverlay
                if self._no_signal_textoverlay:
                    self._no_signal_textoverlay.set_property('xpos', x)
                    self._no_signal_textoverlay.set_property('ypos', y)

                    # During corner celebration, cycle through spin text
                    if corner_hit_frames > 0:
                        spin_chars = ['*', '+', 'x', '+']
                        spin_idx = (30 - corner_hit_frames) % 4
                        spin_text = f"[{spin_chars[spin_idx]} NO SIGNAL {spin_chars[spin_idx]}]"
                        self._no_signal_textoverlay.set_property('text', spin_text)
                        corner_hit_frames -= 1
                    else:
                        self._no_signal_textoverlay.set_property('text', '[ NO SIGNAL ]')

                time.sleep(0.033)  # ~30fps animation
            except Exception:
                break

    def start_loading_mode(self):
        """Start a standalone display for 'Loading' with animated ellipses.

        This creates a pipeline using videotestsrc that shows "Loading" with
        animated dots (0-4 dots, increasing then decreasing).
        """
        try:
            # Stop any existing animations
            self._stop_loading_animation()
            self._stop_no_signal_animation()

            # Stop existing pipeline if any
            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self.pipeline = None

            # Create a standalone pipeline for loading display
            # Uses videotestsrc with named textoverlay for animation
            loading_pipeline = (
                f"videotestsrc pattern=black ! "
                f"video/x-raw,width=1920,height=1080,framerate=30/1 ! "
                f"textoverlay name=loading_text text=\"[ INITIALIZING ]\" "
                f"valignment=center halignment=center font-desc=\"Sans Bold 24\" ! "
                f"videoconvert ! video/x-raw,format=NV12 ! "
                f"kmssink plane-id={self.plane_id} connector-id={self.connector_id} sync=false"
            )

            logger.debug("[DRMAdBlocker] Creating loading pipeline...")
            self.pipeline = Gst.parse_launch(loading_pipeline)

            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)

            # Get the textoverlay element for animation
            self._loading_textoverlay = self.pipeline.get_by_name('loading_text')

            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("[DRMAdBlocker] Failed to start loading pipeline")
                return False

            self.is_visible = True
            self.current_source = 'loading'

            # Start the loading animation thread
            self._start_loading_animation()

            logger.info("[DRMAdBlocker] Loading display started")
            return True
        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start loading mode: {e}")
            return False

    def _start_loading_animation(self):
        """Start the loading dots animation thread."""
        self._stop_loading_anim = threading.Event()
        self._loading_anim_thread = threading.Thread(
            target=self._loading_animation_loop,
            daemon=True,
            name="LoadingAnimation"
        )
        self._loading_anim_thread.start()

    def _stop_loading_animation(self):
        """Stop the loading dots animation thread."""
        if hasattr(self, '_stop_loading_anim'):
            self._stop_loading_anim.set()
        if hasattr(self, '_loading_anim_thread') and self._loading_anim_thread:
            self._loading_anim_thread.join(timeout=1.0)
            self._loading_anim_thread = None
        self._loading_textoverlay = None

    def _loading_animation_loop(self):
        """Animate the loading text with ellipses (0-4 dots, increasing then decreasing)."""
        # Pattern: "", ".", "..", "...", "....", "...", "..", "."
        dot_counts = [0, 1, 2, 3, 4, 3, 2, 1]
        idx = 0
        interval = 0.3  # Update every 300ms

        while not self._stop_loading_anim.is_set():
            if hasattr(self, '_loading_textoverlay') and self._loading_textoverlay:
                dots = "." * dot_counts[idx]
                padding = " " * (4 - dot_counts[idx])  # Keep width consistent
                text = f"[ INITIALIZING{dots}{padding}]"
                try:
                    self._loading_textoverlay.set_property('text', text)
                except Exception:
                    pass  # Pipeline may have been destroyed

            idx = (idx + 1) % len(dot_counts)
            self._stop_loading_anim.wait(interval)

    def _on_error(self, bus, message):
        err, debug = message.parse_error()
        self._pipeline_errors += 1
        self._last_error_time = time.time()
        logger.error(f"[DRMAdBlocker] Pipeline error: {err.message}")
        error_msg = err.message.lower() if err.message else ""
        if any(kw in error_msg for kw in ['connection', 'refused', 'timeout', 'socket', 'http']):
            if not self.is_visible:
                threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def _on_eos(self, bus, message):
        logger.warning("[DRMAdBlocker] Unexpected EOS")
        if not self.is_visible:
            threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def _on_warning(self, bus, message):
        warn, debug = message.parse_warning()
        logger.warning(f"[DRMAdBlocker] Pipeline warning: {warn.message}")

    def get_pipeline_health(self):
        if not self.pipeline:
            return {'healthy': False, 'state': 'stopped', 'errors': self._pipeline_errors}
        state_ret, state, pending = self.pipeline.get_state(0)
        return {
            'healthy': state == Gst.State.PLAYING,
            'state': state.value_nick if state else 'unknown',
            'errors': self._pipeline_errors,
            'last_error': self._last_error_time
        }

    def _get_blocking_text(self, source='default'):
        if source == 'hdmi_lost':
            return "[ NO SIGNAL ]\n\nHDMI DISCONNECTED\n\nWaiting for signal..."
        if source == 'no_hdmi_device':
            return "[ NO SIGNAL ]\n\nWAITING FOR HDMI..."
        if source == 'ocr':
            header = "[ BLOCKING // OCR ]"
        elif source == 'vlm':
            header = "[ BLOCKING // VLM ]"
        elif source == 'both':
            header = "[ BLOCKING // OCR+VLM ]"
        else:
            header = "[ BLOCKING ]"
        vocab = random.choice(SPANISH_VOCABULARY)
        spanish, pronunciation, english, example = vocab
        self._current_vocab = vocab  # Track current word for API
        # Layout matching web UI vocabulary card:
        # - Header (small)
        # - Spanish word (prominent)
        # - (pronunciation) in parentheses
        # - = translation
        # - "Example sentence" in quotes
        return f"{header}\n\n{spanish}\n({pronunciation})\n\n= {english}\n\n\"{example}\""

    def _get_debug_text(self):
        uptime_str = "N/A"
        if self.minus and hasattr(self.minus, 'start_time'):
            uptime_secs = int(time.time() - self.minus.start_time)
            hours, remainder = divmod(uptime_secs, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"

        current_block_time = 0
        if self._current_block_start:
            current_block_time = time.time() - self._current_block_start

        total_block_secs = int(self._total_blocking_time + current_block_time)
        block_mins, block_secs = divmod(total_block_secs, 60)
        block_hours, block_mins = divmod(block_mins, 60)
        block_time_str = f"{block_hours}h {block_mins}m {block_secs}s" if block_hours > 0 else f"{block_mins}m {block_secs}s"

        # Format time saved
        time_saved_secs = int(self._total_time_saved)
        saved_mins, saved_secs = divmod(time_saved_secs, 60)
        saved_hours, saved_mins = divmod(saved_mins, 60)
        if saved_hours > 0:
            time_saved_str = f"{saved_hours}h {saved_mins}m {saved_secs}s"
        elif saved_mins > 0:
            time_saved_str = f"{saved_mins}m {saved_secs}s"
        else:
            time_saved_str = f"{saved_secs}s"

        debug_text = f"UPTIME    {uptime_str}\nBLOCKED   {self._total_ads_blocked}\nBLK TIME  {block_time_str}\nSAVED     {time_saved_str}"
        if self._skip_text:
            debug_text += f"\n> {self._skip_text}"
        return debug_text

    def _rotation_loop(self, source):
        while not self._stop_rotation.is_set():
            text = self._get_blocking_text(source)
            self._blocking_api_call('/blocking/set', {'text_vocab': text})
            self._stop_rotation.wait(random.uniform(11.0, 15.0))

    def _start_rotation(self, source):
        self._stop_rotation.clear()
        self._rotation_thread = threading.Thread(target=self._rotation_loop, args=(source,), daemon=True)
        self._rotation_thread.start()

    def _stop_rotation_thread(self):
        self._stop_rotation.set()
        if self._rotation_thread:
            self._rotation_thread.join(timeout=1.0)
            self._rotation_thread = None

    def _debug_loop(self):
        while not self._stop_debug.is_set():
            if self._debug_overlay_enabled:
                self._blocking_api_call('/blocking/set', {'text_stats': self._get_debug_text()})
            self._stop_debug.wait(self._debug_interval)

    def _start_debug(self):
        if not self._debug_overlay_enabled:
            self._blocking_api_call('/blocking/set', {'text_stats': ''})
            return
        self._stop_debug.clear()
        self._debug_thread = threading.Thread(target=self._debug_loop, daemon=True, name="DebugUpdate")
        self._debug_thread.start()

    def _stop_debug_thread(self):
        self._stop_debug.set()
        if self._debug_thread:
            self._debug_thread.join(timeout=2.0)
            self._debug_thread = None

    def _start_snapshot_buffer(self):
        self._stop_snapshot_buffer.clear()
        self._snapshot_buffer_thread = threading.Thread(target=self._snapshot_buffer_loop, daemon=True, name="SnapshotBuffer")
        self._snapshot_buffer_thread.start()

    def _stop_snapshot_buffer_thread(self):
        self._stop_snapshot_buffer.set()
        if self._snapshot_buffer_thread:
            self._snapshot_buffer_thread.join(timeout=2.0)
            self._snapshot_buffer_thread = None

    def _snapshot_buffer_loop(self):
        while not self._stop_snapshot_buffer.is_set():
            try:
                url = f"http://localhost:{self.ustreamer_port}/snapshot"
                with urllib.request.urlopen(url, timeout=1.0) as response:
                    self._snapshot_buffer.append({'data': response.read(), 'time': time.time()})
            except Exception:
                pass
            self._stop_snapshot_buffer.wait(self._snapshot_interval)

    def _upload_background(self):
        """Upload pixelated background. Thread-safe for async execution."""
        try:
            # Thread-safe: copy snapshot data atomically to avoid race conditions
            try:
                if not self._snapshot_buffer:
                    logger.warning("[DRMAdBlocker] No snapshots in buffer for background")
                    return False
                # Copy data immediately to avoid race with buffer updates
                snapshot_entry = self._snapshot_buffer[0]
                snapshot_data = bytes(snapshot_entry['data'])  # Make a copy
            except (IndexError, KeyError):
                logger.warning("[DRMAdBlocker] Snapshot buffer race condition - skipping background")
                return False

            logger.info(f"[DRMAdBlocker] Uploading background ({len(self._snapshot_buffer)} snapshots in buffer)")

            try:
                import cv2
                import numpy as np
                nparr = np.frombuffer(snapshot_data, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if img is not None:
                    h, w = img.shape[:2]
                    factor = 20
                    small = cv2.resize(img, (max(1, w // factor), max(1, h // factor)), interpolation=cv2.INTER_LINEAR)
                    pixelated = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
                    pixelated = (pixelated * 0.6).astype(np.uint8)
                    _, encoded = cv2.imencode('.jpg', pixelated, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    snapshot_data = encoded.tobytes()
                    logger.info(f"[DRMAdBlocker] Pixelated background: {w}x{h}, {len(snapshot_data)} bytes")
                else:
                    logger.warning("[DRMAdBlocker] Failed to decode snapshot for pixelation")
            except ImportError:
                logger.warning("[DRMAdBlocker] OpenCV not available for pixelation")
            except Exception as e:
                logger.warning(f"[DRMAdBlocker] Pixelation failed: {e}")

            result = self._blocking_api_call('/blocking/background', data=snapshot_data, method='POST', timeout=0.5)
            success = result is not None and result.get('ok', False)
            if success:
                logger.info(f"[DRMAdBlocker] Background uploaded successfully")
            else:
                logger.warning(f"[DRMAdBlocker] Background upload failed: {result}")
            return success

        except Exception as e:
            # Catch-all for thread safety - don't let exceptions crash the background thread
            logger.exception(f"[DRMAdBlocker] Background upload error: {e}")
            return False

    def _ease_out(self, t):
        return 1 - (1 - t) ** 2

    def _ease_in(self, t):
        return t ** 2

    def _stop_animation_thread(self):
        self._stop_animation.set()
        if self._animation_thread:
            self._animation_thread.join(timeout=2.0)
            self._animation_thread = None
        self._animating = False
        self._animation_direction = None

    def _start_animation(self, direction, source=None):
        self._stop_animation_thread()
        self._stop_animation.clear()
        self._animation_source = source
        self._animating = True
        self._animation_direction = direction
        self._animation_thread = threading.Thread(target=self._animation_loop, args=(direction,), daemon=True, name=f"Animation-{direction}")
        self._animation_thread.start()

    def _animation_loop(self, direction):
        start_time = time.time()
        duration = self._animation_duration_start if direction == 'start' else self._animation_duration_end

        full_x, full_y = 0, 0
        full_w, full_h = self._frame_width, self._frame_height
        corner_x = self._frame_width - self._preview_w - self._preview_padding
        corner_y = self._frame_height - self._preview_h - self._preview_padding
        corner_w, corner_h = self._preview_w, self._preview_h

        while not self._stop_animation.is_set():
            elapsed = time.time() - start_time
            progress = min(1.0, elapsed / duration)

            if direction == 'start':
                t = self._ease_out(progress)
                x = int(full_x + (corner_x - full_x) * t)
                y = int(full_y + (corner_y - full_y) * t)
                w = int(full_w + (corner_w - full_w) * t)
                h = int(full_h + (corner_h - full_h) * t)
            else:
                t = self._ease_in(progress)
                x = int(corner_x + (full_x - corner_x) * t)
                y = int(corner_y + (full_y - corner_y) * t)
                w = int(corner_w + (full_w - corner_w) * t)
                h = int(corner_h + (full_h - corner_h) * t)

            self._blocking_api_call('/blocking/set', {'preview_x': str(x), 'preview_y': str(y), 'preview_w': str(w), 'preview_h': str(h)})

            if progress >= 1.0:
                break
            time.sleep(0.016)

        # Set final position
        if direction == 'start':
            self._blocking_api_call('/blocking/set', {'preview_x': str(corner_x), 'preview_y': str(corner_y), 'preview_w': str(corner_w), 'preview_h': str(corner_h)})
        else:
            self._blocking_api_call('/blocking/set', {'preview_x': '0', 'preview_y': '0', 'preview_w': str(full_w), 'preview_h': str(full_h)})

        self._animating = False
        self._animation_direction = None
        if direction == 'start':
            self._on_start_animation_complete()
        else:
            self._on_end_animation_complete()

    def _on_start_animation_complete(self):
        logger.debug("[DRMAdBlocker] Start animation complete")
        source = self._animation_source or 'default'
        self._blocking_api_call('/blocking/set', {'text_vocab': self._get_blocking_text(source)})
        self._start_rotation(source)
        self._current_block_start = time.time()
        self._total_ads_blocked += 1
        self._start_debug()

    def _on_end_animation_complete(self):
        logger.debug("[DRMAdBlocker] End animation complete")
        self._blocking_api_call('/blocking/set', {'enabled': 'false'}, timeout=0.5)
        if self.audio:
            self.audio.unmute()

    def set_minus(self, minus_instance):
        self.minus = minus_instance

    def set_audio(self, audio):
        self.audio = audio

    def is_preview_enabled(self):
        return self._preview_enabled

    def set_preview_enabled(self, enabled):
        self._preview_enabled = enabled
        logger.info(f"[DRMAdBlocker] Preview {'enabled' if enabled else 'disabled'}")
        if self.is_visible:
            self._blocking_api_call('/blocking/set', {'preview_enabled': 'true' if enabled else 'false'})

    def is_debug_overlay_enabled(self):
        return self._debug_overlay_enabled

    def set_debug_overlay_enabled(self, enabled):
        self._debug_overlay_enabled = enabled
        logger.info(f"[DRMAdBlocker] Debug overlay {'enabled' if enabled else 'disabled'}")
        if self.is_visible:
            if enabled:
                if not self._debug_thread or not self._debug_thread.is_alive():
                    self._start_debug()
            else:
                self._stop_debug_thread()
                self._blocking_api_call('/blocking/set', {'text_stats': ''})

    def set_skip_status(self, available: bool, text: str = None):
        self._skip_available = available
        self._skip_text = text

    def get_skip_status(self) -> tuple:
        return (self._skip_available, self._skip_text)

    def add_time_saved(self, seconds: float):
        """Add to the total time saved by skipping ads."""
        self._total_time_saved += seconds
        logger.info(f"[DRMAdBlocker] Time saved: +{seconds:.0f}s (total: {self._total_time_saved:.0f}s)")

    def get_time_saved(self) -> float:
        """Get total time saved in seconds."""
        return self._total_time_saved

    def get_current_vocabulary(self) -> dict:
        """Get the current vocabulary word being displayed."""
        if self._current_vocab and self.is_visible:
            spanish, pronunciation, english, example = self._current_vocab
            return {
                'word': spanish,
                'pronunciation': pronunciation,
                'translation': english,
                'example': example,
            }
        return {'word': None, 'pronunciation': None, 'translation': None, 'example': None}

    def set_test_mode(self, duration_seconds: float):
        self._test_blocking_until = time.time() + duration_seconds
        logger.info(f"[DRMAdBlocker] Test mode enabled for {duration_seconds}s")

    def clear_test_mode(self):
        self._test_blocking_until = 0
        logger.info("[DRMAdBlocker] Test mode cleared")

    def is_test_mode_active(self) -> bool:
        return self._test_blocking_until > time.time()

    def show(self, source='default'):
        with self._lock:
            if not self.pipeline:
                logger.warning("[DRMAdBlocker] Pipeline not initialized")
                return

            if self.is_visible and self._animation_direction != 'end':
                if self.current_source != source:
                    self.current_source = source
                return

            if self._animating and self._animation_direction == 'start':
                if self.current_source != source:
                    self.current_source = source
                return

            if self._animating and self._animation_direction == 'end':
                logger.info(f"[DRMAdBlocker] Reversing end animation ({source})")
                self._stop_animation_thread()

            logger.info(f"[DRMAdBlocker] Starting blocking ({source})")

            # Mute audio immediately
            if self.audio:
                self.audio.mute()

            # Enable blocking immediately (background will upload async)
            self._blocking_api_call('/blocking/set', {
                'enabled': 'true',
                'preview_x': '0', 'preview_y': '0',
                'preview_w': str(self._frame_width), 'preview_h': str(self._frame_height),
                'preview_enabled': 'true' if self._preview_enabled else 'false',
                'text_vocab': '', 'text_stats': '',
                'box_alpha': str(self._box_alpha),
                'text_y': str(self._text_y),
                'text_u': str(self._text_u),
                'text_v': str(self._text_v)
            }, timeout=0.5)

            self.is_visible = True
            self.current_source = source

            if self.minus:
                self.minus.blocking_active = True

            # Upload background asynchronously (don't block animation start)
            threading.Thread(target=self._upload_background, daemon=True, name="BackgroundUpload").start()

            self._start_animation('start', source)

    def hide(self, force=False):
        if not force and self._test_blocking_until > time.time():
            return

        with self._lock:
            if self._animating and self._animation_direction == 'end':
                return

            was_visible = self.is_visible
            self.is_visible = False
            self.current_source = None

            if self.minus:
                self.minus.blocking_active = False

            self._stop_rotation_thread()
            self._stop_debug_thread()

            if self._current_block_start:
                self._total_blocking_time += time.time() - self._current_block_start
                self._current_block_start = None

            self._blocking_api_call('/blocking/set', {'text_vocab': '', 'text_stats': ''})

            if not self.pipeline:
                if was_visible:
                    logger.warning("[DRMAdBlocker] Pipeline not initialized")
                if self.audio:
                    self.audio.unmute()
                return

            if not was_visible and self._animation_direction != 'start':
                return

            if self._animating:
                self._stop_animation_thread()

            logger.info("[DRMAdBlocker] Starting end animation")
            self._start_animation('end', None)

    def update(self, ad_detected, is_skippable=False, skip_location=None, ocr_detected=False, vlm_detected=False):
        if ad_detected and not is_skippable:
            if ocr_detected and vlm_detected:
                source = 'both'
            elif ocr_detected:
                source = 'ocr'
            elif vlm_detected:
                source = 'vlm'
            else:
                source = 'default'
            self.show(source)
        else:
            self.hide()

    def destroy(self):
        with self._lock:
            self._stop_watchdog_thread()
            self._stop_rotation_thread()
            self._stop_debug_thread()
            self._stop_animation_thread()
            self._stop_snapshot_buffer_thread()
            self._stop_loading_animation()
            self._stop_no_signal_animation()

            self._blocking_api_call('/blocking/set', {'clear': 'true'}, timeout=0.5)

            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                    logger.info("[DRMAdBlocker] Pipeline stopped")
                except Exception as e:
                    logger.error(f"[DRMAdBlocker] Error stopping pipeline: {e}")
                self.pipeline = None

            self.is_visible = False


AdBlocker = DRMAdBlocker
