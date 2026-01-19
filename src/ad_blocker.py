"""
Ad Blocker Overlay for Minus.

Displays a blocking overlay when ads are detected on screen.
Uses GStreamer with input-selector for instant switching between video and blocking.

Features:
- Instant switching (no process restart, no black screen gap)
- Spanish vocabulary practice during ad blocks
- Rotates vocabulary every 11-15 seconds
"""

import os
import threading
import time
import random
import logging
import urllib.request
import shutil
from collections import deque

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

# Try to import OpenCV for pixelation (fallback to basic method if not available)
try:
    import cv2
    import numpy as np
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False

# Set up logging
logger = logging.getLogger(__name__)

# Spanish vocabulary - intermediate level
# Format: (spanish, english, example_sentence)
SPANISH_VOCABULARY = [
    # Common verbs
    ("aprovechar", "to take advantage of", "Hay que aprovechar el tiempo."),
    ("lograr", "to achieve/manage", "Logre terminar el proyecto."),
    ("desarrollar", "to develop", "Vamos a desarrollar una app."),
    ("destacar", "to stand out", "Su trabajo destaca por su calidad."),
    ("enfrentar", "to face/confront", "Debemos enfrentar los problemas."),
    ("realizar", "to carry out/accomplish", "Voy a realizar mi sueno."),
    ("averiguar", "to find out", "Necesito averiguar la verdad."),
    ("pertenecer", "to belong", "Este libro pertenece a Maria."),
    ("alcanzar", "to reach/achieve", "Quiero alcanzar mis metas."),
    ("surgir", "to arise/emerge", "Surgio un problema inesperado."),
    ("contar con", "to count on/rely on", "Puedes contar conmigo."),
    ("tardar", "to take time/be late", "Cuanto tardas en llegar?"),
    ("soler", "to usually do", "Suelo desayunar temprano."),
    ("fingir", "to pretend", "No finjas que no lo sabes."),
    ("invertir", "to invest/reverse", "Quiero invertir en mi futuro."),
    ("sostener", "to sustain/hold", "Sostengo que es verdad."),
    ("agregar", "to add", "Agrega un poco de sal."),
    ("advertir", "to warn/notice", "Te advierto que es peligroso."),
    ("exigir", "to demand", "El trabajo exige dedicacion."),
    ("proponer", "to propose", "Propongo una solucion."),

    # Useful adjectives
    ("disponible", "available", "El doctor esta disponible manana."),
    ("imprescindible", "essential/indispensable", "El agua es imprescindible."),
    ("agotado", "exhausted/sold out", "Estoy agotado despues del trabajo."),
    ("capaz", "capable", "Eres capaz de hacerlo."),
    ("dispuesto", "willing/ready", "Estoy dispuesto a ayudar."),
    ("actual", "current (not actual!)", "La situacion actual es dificil."),
    ("cotidiano", "daily/everyday", "Es parte de la vida cotidiana."),
    ("propio", "own/proper", "Tengo mi propio carro."),
    ("debido", "due/proper", "Debido al clima, cancelamos."),
    ("cercano", "close/nearby", "Vive en un pueblo cercano."),
    ("lejano", "distant/far", "Es un recuerdo lejano."),
    ("gracioso", "funny (not gracious!)", "El chiste fue muy gracioso."),
    ("largo", "long (not large!)", "El camino es muy largo."),
    ("ancho", "wide", "El rio es muy ancho."),
    ("estrecho", "narrow/tight", "El pasillo es muy estrecho."),
    ("sencillo", "simple/easy", "Es un problema sencillo."),
    ("complejo", "complex", "Es un tema muy complejo."),
    ("valioso", "valuable", "Es un consejo muy valioso."),
    ("profundo", "deep/profound", "Tiene un significado profundo."),
    ("sorprendente", "surprising", "Fue un resultado sorprendente."),

    # Common nouns
    ("desarrollo", "development", "El desarrollo del proyecto va bien."),
    ("comportamiento", "behavior", "Su comportamiento es extrano."),
    ("conocimiento", "knowledge", "El conocimiento es poder."),
    ("ambiente", "environment/atmosphere", "El ambiente es muy agradable."),
    ("herramienta", "tool", "Necesito una herramienta."),
    ("recurso", "resource", "Tenemos pocos recursos."),
    ("acontecimiento", "event/happening", "Fue un gran acontecimiento."),
    ("requisito", "requirement", "Es un requisito obligatorio."),
    ("plazo", "deadline/term", "El plazo termina manana."),
    ("ubicacion", "location", "La ubicacion es perfecta."),
    ("esfuerzo", "effort", "Hizo un gran esfuerzo."),
    ("resultado", "result", "El resultado fue positivo."),
    ("propuesta", "proposal", "Tengo una propuesta interesante."),
    ("desempleo", "unemployment", "El desempleo ha bajado."),
    ("prueba", "test/proof", "Necesito una prueba."),
    ("ventaja", "advantage", "Tenemos una gran ventaja."),
    ("desventaja", "disadvantage", "No veo ninguna desventaja."),
    ("meta", "goal", "Mi meta es aprender espanol."),
    ("reto", "challenge", "Es un gran reto personal."),
    ("logro", "achievement", "Fue un gran logro."),

    # Expressions
    ("sin embargo", "however/nevertheless", "Es dificil, sin embargo posible."),
    ("a pesar de", "despite/in spite of", "A pesar de todo, sigo adelante."),
    ("en cuanto a", "as for/regarding", "En cuanto a tu pregunta..."),
    ("a partir de", "starting from", "A partir de hoy, todo cambia."),
    ("de repente", "suddenly", "De repente, empezo a llover."),
    ("al fin y al cabo", "after all", "Al fin y al cabo, lo logramos."),
    ("hoy en dia", "nowadays", "Hoy en dia todo es digital."),
    ("cada vez mas", "more and more", "Es cada vez mas dificil."),
    ("por lo tanto", "therefore", "Por lo tanto, debemos actuar."),
    ("mientras tanto", "meanwhile", "Mientras tanto, esperamos."),
    ("en cambio", "on the other hand", "A el le gusta; en cambio, a mi no."),
    ("de todos modos", "anyway/regardless", "De todos modos, gracias."),
    ("en realidad", "actually/in reality", "En realidad, no es tan dificil."),
    ("por cierto", "by the way", "Por cierto, te llamo tu madre."),
    ("a menudo", "often", "A menudo voy al parque."),
    ("de vez en cuando", "from time to time", "De vez en cuando como pizza."),
    ("en seguida", "right away", "Vengo en seguida."),
    ("poco a poco", "little by little", "Poco a poco se aprende."),
    ("tal vez", "maybe/perhaps", "Tal vez llueva manana."),
    ("a lo mejor", "maybe/probably", "A lo mejor viene mas tarde."),

    # More verbs (reflexive and common)
    ("comprometerse", "to commit oneself", "Me comprometo a estudiar."),
    ("enterarse", "to find out", "Me entere de la noticia."),
    ("arrepentirse", "to regret", "No te vas a arrepentir."),
    ("darse cuenta", "to realize", "Me di cuenta del error."),
    ("tratarse de", "to be about", "Se trata de un tema importante."),
    ("encargarse", "to take charge of", "Yo me encargo de eso."),
    ("aprovecharse", "to take advantage (negative)", "No te aproveches de el."),
    ("equivocarse", "to be wrong/make mistake", "Todos nos equivocamos."),
    ("atreverse", "to dare", "No me atrevo a decirlo."),
    ("quejarse", "to complain", "Siempre se queja de todo."),
    ("preocuparse", "to worry", "No te preocupes por eso."),
    ("olvidarse", "to forget", "Me olvide de llamarte."),
    ("acordarse", "to remember", "No me acuerdo de su nombre."),
    ("acostumbrarse", "to get used to", "Me acostumbre al frio."),
    ("burlarse", "to mock/make fun of", "No te burles de el."),

    # False friends (tricky words)
    ("embarazada", "pregnant (not embarrassed!)", "Mi hermana esta embarazada."),
    ("exito", "success (not exit!)", "El proyecto fue un exito."),
    ("sensible", "sensitive (not sensible!)", "Es una persona muy sensible."),
    ("libreria", "bookstore (not library!)", "Compre un libro en la libreria."),
    ("recordar", "to remember (not record!)", "Recuerdo ese dia."),
    ("asistir", "to attend (not assist!)", "Voy a asistir a la reunion."),
    ("realizar", "to accomplish (not realize!)", "Realize mi sueno."),
    ("soportar", "to tolerate (not support!)", "No soporto el ruido."),
    ("pretender", "to try/intend (not pretend!)", "Pretendo terminar hoy."),
    ("introducir", "to insert (not introduce!)", "Introduce la moneda aqui."),

    # Subjunctive triggers
    ("es importante que", "it's important that", "Es importante que estudies."),
    ("espero que", "I hope that", "Espero que todo salga bien."),
    ("dudo que", "I doubt that", "Dudo que venga hoy."),
    ("quiero que", "I want (someone) to", "Quiero que me ayudes."),
    ("me alegra que", "I'm glad that", "Me alegra que estes aqui."),
    ("es necesario que", "it's necessary that", "Es necesario que lo hagas."),
    ("ojala", "hopefully/I wish", "Ojala puedas venir."),
    ("antes de que", "before (something happens)", "Antes de que llueva, vamos."),
    ("para que", "so that/in order to", "Te lo digo para que sepas."),
    ("aunque", "although/even if", "Voy aunque llueva."),

    # Time expressions
    ("hace poco", "a little while ago", "Llegue hace poco."),
    ("dentro de poco", "in a little while", "Salgo dentro de poco."),
    ("a la larga", "in the long run", "A la larga, vale la pena."),
    ("a corto plazo", "short term", "Es una solucion a corto plazo."),
    ("a largo plazo", "long term", "Pienso a largo plazo."),
    ("de antemano", "beforehand", "Gracias de antemano."),
    ("en aquel entonces", "back then", "En aquel entonces era joven."),
    ("a primera vista", "at first sight", "A primera vista parece facil."),
]


class DRMAdBlocker:
    """
    DRM-based ad blocker using a SINGLE GStreamer pipeline with input-selector.

    Uses Python GStreamer bindings to create a pipeline with two inputs:
    1. Video stream from ustreamer
    2. Blocking pattern with Spanish vocabulary practice

    Switching between video and blocking is INSTANT - just changes which
    input is active, no process restart needed.
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

        # GStreamer pipeline and elements
        self.pipeline = None
        self.selector = None
        self.video_pad = None
        self.blocking_pad = None
        self.textoverlay = None
        self.bgoverlay = None
        self.bus = None

        # Audio passthrough reference (set by minus)
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

        # Video buffer watchdog (like audio has)
        self._last_buffer_time = 0
        self._watchdog_thread = None
        self._stop_watchdog = threading.Event()
        self._watchdog_interval = 3.0  # Check every 3 seconds
        self._stall_threshold = 10.0   # 10 seconds without buffer = stall
        self._restart_count = 0
        self._last_restart_time = 0
        self._consecutive_failures = 0
        self._base_restart_delay = 1.0
        self._max_restart_delay = 30.0
        self._success_reset_time = 10.0  # Reset backoff after 10s of success

        # Text rotation for Spanish vocabulary
        self._rotation_thread = None
        self._stop_rotation = threading.Event()
        self._current_vocab_index = 0

        # Preview image update thread
        self._preview_thread = None
        self._stop_preview = threading.Event()
        self._preview_path = "/tmp/minus_preview.jpg"
        self._preview_interval = 0.25  # Update every 0.25 seconds (~4fps)
        self._preview_enabled = True  # Preview window enabled by default

        # Debug overlay settings
        self._debug_overlay_enabled = True  # Debug dashboard enabled by default
        self._debug_thread = None
        self._stop_debug = threading.Event()
        self._debug_interval = 2.0  # Update every 2 seconds
        self._total_blocking_time = 0.0  # Total seconds spent blocking ads
        self._current_block_start = None  # When current blocking session started
        self._total_ads_blocked = 0  # Number of ad blocking sessions
        self.debugoverlay = None  # GStreamer text overlay element

        # Skip status (for Fire TV integration)
        self._skip_available = False  # True when skip button ready
        self._skip_text = None  # Current skip status text

        # Animation settings
        self._animation_thread = None
        self._stop_animation = threading.Event()
        self._animation_duration = 1.5  # seconds
        self._animating = False
        self._animation_source = None  # Store source during animation

        # Test mode - when set, hide() is ignored until this timestamp
        self._test_blocking_until = 0

        # Preview corner position/size (set in _init_pipeline, reused for animation)
        self._preview_corner_x = 0
        self._preview_corner_y = 0
        self._preview_corner_w = 0
        self._preview_corner_h = 0

        # Create initial placeholder preview image
        self._create_placeholder_preview()

        # Pixelated background settings
        self._pixelated_bg_path = "/tmp/minus_bg_pixelated.jpg"
        self._snapshot_buffer = deque(maxlen=3)  # Keep 6 seconds of snapshots (at 2s intervals)
        self._snapshot_buffer_thread = None
        self._stop_snapshot_buffer = threading.Event()
        self._snapshot_interval = 2.0  # Capture every 2 seconds
        self._pixelation_factor = 20  # Downscale factor (higher = more pixelated)

        # Create initial placeholder for pixelated background
        self._create_placeholder_pixelated_bg()

        # Initialize GStreamer
        Gst.init(None)
        self._init_pipeline()

        # Start the rolling snapshot buffer
        self._start_snapshot_buffer()

    def _init_pipeline(self):
        """Initialize GStreamer pipeline with input-selector for instant switching."""
        try:
            # Build pipeline with input-selector
            # sink_0 = video stream, sink_1 = blocking pattern with live preview
            #
            # Color correction via videobalance (HDMI-RX doesn't support V4L2 image controls):
            # - saturation=0.85: reduce oversaturation (default 1.0, range 0-2)
            # - contrast=1.0: keep default
            # - brightness=0.0: keep default
            # Calculate font size based on output resolution (48 at 4K, scaled down for smaller displays)
            font_size = max(24, int(48 * self.output_height / 2160))

            # Preview window size and position (corner of blocking overlay)
            # Scale with output resolution: 20% of output size
            # At 1080p: 384x216, at 4K: 768x432
            preview_w = int(self.output_width * 0.20)
            preview_h = int(self.output_height * 0.20)
            preview_padding = int(self.output_height * 0.02)  # 2% padding
            preview_x = self.output_width - preview_w - preview_padding
            preview_y = self.output_height - preview_h - preview_padding

            # Store corner position/size for animation
            self._preview_corner_x = preview_x
            self._preview_corner_y = preview_y
            self._preview_corner_w = preview_w
            self._preview_corner_h = preview_h

            # Debug overlay font size - scales with resolution
            # 12pt at 1080p, 24pt at 4K (proportional scaling)
            debug_font_size = max(12, int(24 * self.output_height / 2160))
            # Debug overlay padding (scaled with resolution)
            debug_padding = int(self.output_height * 0.01)  # 1% padding

            logger.info(f"[DRMAdBlocker] Main font size: {font_size}, Debug font size: {debug_font_size}")

            pipeline_str = (
                f"input-selector name=sel ! "
                f"identity name=fpsprobe ! "
                f"kmssink plane-id={self.plane_id} connector-id={self.connector_id} sync=false "

                # Video input (sink_0) with color correction
                f"souphttpsrc location=http://localhost:{self.ustreamer_port}/stream blocksize=524288 ! "
                f"multipartdemux ! jpegparse ! mppjpegdec ! video/x-raw,format=NV12 ! "
                f"videobalance saturation=0.85 name=colorbalance ! "
                f"queue max-size-buffers=3 leaky=downstream name=videoqueue ! sel.sink_0 "

                # Blocking input (sink_1) - pixelated background + live preview + text overlays
                # Layer order (bottom to top):
                # 1. videotestsrc (black base)
                # 2. bgoverlay (pixelated background from ~2s before ad - full screen)
                # 3. previewoverlay (live preview - corner)
                # 4. blocktext (Spanish vocabulary - center)
                # 5. debugtext (stats - bottom-left)
                f"videotestsrc pattern=2 is-live=true ! "
                f"video/x-raw,format=NV12,width={self.output_width},height={self.output_height},framerate=30/1 ! "
                f"gdkpixbufoverlay name=bgoverlay location={self._pixelated_bg_path} "
                f"offset-x=0 offset-y=0 overlay-width={self.output_width} overlay-height={self.output_height} ! "
                f"gdkpixbufoverlay name=previewoverlay location=/tmp/minus_preview.jpg "
                f"offset-x={preview_x} offset-y={preview_y} overlay-width={preview_w} overlay-height={preview_h} ! "
                f"textoverlay name=blocktext text='BLOCKING AD' font-desc='Sans Bold {font_size}' "
                f"valignment=center halignment=center shaded-background=true ! "
                # Debug dashboard overlay (bottom-left corner, very small text)
                # Use explicit Pango font string with auto-resize disabled
                f"textoverlay name=debugtext text='' font-desc='Monospace {debug_font_size}' "
                f"valignment=bottom halignment=left xpad={debug_padding} ypad={debug_padding} "
                f"shaded-background=true auto-resize=false ! "
                f"queue name=blockqueue ! sel.sink_1"
            )

            logger.debug(f"[DRMAdBlocker] Creating pipeline...")
            self.pipeline = Gst.parse_launch(pipeline_str)

            # Get references to key elements
            self.selector = self.pipeline.get_by_name('sel')
            self.textoverlay = self.pipeline.get_by_name('blocktext')
            self.previewoverlay = self.pipeline.get_by_name('previewoverlay')
            self.debugoverlay = self.pipeline.get_by_name('debugtext')
            self.bgoverlay = self.pipeline.get_by_name('bgoverlay')

            # Set debug overlay font size explicitly (must be done after element creation)
            if self.debugoverlay:
                debug_font = f'Monospace {debug_font_size}'
                self.debugoverlay.set_property('font-desc', debug_font)
                self.debugoverlay.set_property('auto-resize', False)
                logger.info(f"[DRMAdBlocker] Debug overlay font set to: {debug_font} (main font: Sans Bold {font_size})")

            # Get the pads for switching
            self.video_pad = self.selector.get_static_pad('sink_0')
            self.blocking_pad = self.selector.get_static_pad('sink_1')

            # Start with video input active
            self.selector.set_property('active-pad', self.video_pad)

            # Set up bus message handling for error detection
            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)
            self.bus.connect('message::eos', self._on_eos)
            self.bus.connect('message::warning', self._on_warning)

            # Set up FPS probe
            fpsprobe = self.pipeline.get_by_name('fpsprobe')
            if fpsprobe:
                srcpad = fpsprobe.get_static_pad('src')
                srcpad.add_probe(Gst.PadProbeType.BUFFER, self._fps_probe_callback, None)

            logger.info("[DRMAdBlocker] Pipeline created with input-selector")

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to initialize GStreamer: {e}")
            import traceback
            traceback.print_exc()
            self.pipeline = None

    def _fps_probe_callback(self, pad, info, user_data):
        """Callback for counting frames passing through the pipeline."""
        current_time = time.time()

        # Track last buffer time for watchdog
        self._last_buffer_time = current_time

        # Reset consecutive failures after sustained success
        if self._consecutive_failures > 0:
            if current_time - self._last_restart_time > self._success_reset_time:
                self._consecutive_failures = 0
                logger.debug("[DRMAdBlocker] Buffer flow restored, reset backoff")

        with self._fps_lock:
            self._frame_count += 1
            elapsed = current_time - self._fps_start_time

            # Calculate FPS every second
            if elapsed >= 1.0:
                self._current_fps = self._frame_count / elapsed
                self._frame_count = 0
                self._fps_start_time = current_time

        return Gst.PadProbeReturn.OK

    def get_fps(self):
        """Get current output FPS."""
        with self._fps_lock:
            return self._current_fps

    def start(self):
        """Start the GStreamer pipeline."""
        if not self.pipeline:
            logger.error("[DRMAdBlocker] No pipeline to start")
            return False

        try:
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("[DRMAdBlocker] Failed to start pipeline")
                return False

            logger.info("[DRMAdBlocker] Pipeline started (video active)")

            # Start watchdog thread
            self._start_watchdog()

            return True

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start pipeline: {e}")
            return False

    def _start_watchdog(self):
        """Start the video buffer watchdog thread."""
        self._stop_watchdog.clear()
        self._last_buffer_time = time.time()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="VideoWatchdog"
        )
        self._watchdog_thread.start()
        logger.debug("[DRMAdBlocker] Watchdog started")

    def _stop_watchdog_thread(self):
        """Stop the watchdog thread."""
        self._stop_watchdog.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None

    def _watchdog_loop(self):
        """Watchdog thread to detect video pipeline stalls."""
        logger.debug("[DRMAdBlocker] Watchdog loop started")

        while not self._stop_watchdog.is_set():
            self._stop_watchdog.wait(self._watchdog_interval)

            if self._stop_watchdog.is_set():
                break

            # Skip check if we're showing blocking screen (using videotestsrc, not souphttpsrc)
            if self.is_visible:
                continue

            # Skip check if pipeline is restarting
            if self._pipeline_restarting:
                continue

            # Check if buffers are flowing
            if self._last_buffer_time > 0:
                time_since_buffer = time.time() - self._last_buffer_time
                if time_since_buffer > self._stall_threshold:
                    logger.warning(f"[DRMAdBlocker] Video pipeline stalled ({time_since_buffer:.1f}s without buffer)")
                    self._restart_pipeline()

            # Check pipeline state
            if self.pipeline and not self.is_visible:
                try:
                    state_ret, state, pending = self.pipeline.get_state(0)
                    if state not in (Gst.State.PLAYING, Gst.State.PAUSED):
                        logger.warning(f"[DRMAdBlocker] Pipeline not in PLAYING state: {state.value_nick}")
                        self._restart_pipeline()
                except Exception as e:
                    logger.debug(f"[DRMAdBlocker] Error checking pipeline state: {e}")

        logger.debug("[DRMAdBlocker] Watchdog loop stopped")

    def _restart_pipeline(self):
        """Restart the video pipeline after a stall with exponential backoff."""
        with self._restart_lock:
            if self._pipeline_restarting:
                return

            self._pipeline_restarting = True

        try:
            self._restart_count += 1
            self._consecutive_failures += 1

            # Calculate backoff delay: 1s, 2s, 4s, 8s, ... up to 30s
            delay = min(
                self._base_restart_delay * (2 ** (self._consecutive_failures - 1)),
                self._max_restart_delay
            )

            logger.warning(
                f"[DRMAdBlocker] Restarting pipeline (attempt {self._restart_count}, "
                f"delay {delay:.1f}s, {self._consecutive_failures} consecutive failures)"
            )

            # Stop current pipeline
            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception as e:
                    logger.debug(f"[DRMAdBlocker] Error stopping old pipeline: {e}")
                self.pipeline = None
                self.selector = None
                self.video_pad = None
                self.blocking_pad = None
                self.textoverlay = None
                self.bgoverlay = None

            # Wait with exponential backoff
            time.sleep(delay)

            # Recreate pipeline
            self._init_pipeline()

            if self.pipeline:
                # Check CURRENT state to decide whether to show blocking
                # Don't just restore old state - respect pause, hdmi recovery, etc.
                should_show_blocking = False
                blocking_source = None

                if self.minus:
                    # Only restore blocking if:
                    # 1. Ad is actually detected (not just hdmi_lost/no_signal)
                    # 2. Blocking is not paused
                    # 3. Not suppressed due to static screen
                    if (self.minus.ad_detected and
                        self.minus.blocking_source and
                        self.minus.blocking_source not in ('hdmi_lost', 'no_hdmi_device') and
                        not self.minus.is_blocking_paused() and
                        not self.minus.static_blocking_suppressed):
                        should_show_blocking = True
                        blocking_source = self.minus.blocking_source

                if should_show_blocking and self.selector and self.blocking_pad:
                    self.selector.set_property('active-pad', self.blocking_pad)
                    if self.textoverlay and blocking_source:
                        text = self._get_blocking_text(blocking_source)
                        self.textoverlay.set_property('text', text)
                    self.is_visible = True
                    self.current_source = blocking_source
                    logger.info(f"[DRMAdBlocker] Restored blocking state ({blocking_source})")
                else:
                    # Start with video visible
                    self.is_visible = False
                    self.current_source = None

                ret = self.pipeline.set_state(Gst.State.PLAYING)
                if ret != Gst.StateChangeReturn.FAILURE:
                    logger.info("[DRMAdBlocker] Pipeline restarted successfully")
                    self._last_buffer_time = time.time()
                    self._last_restart_time = time.time()
                else:
                    logger.error("[DRMAdBlocker] Pipeline restart failed - state change returned FAILURE")
            else:
                logger.error("[DRMAdBlocker] Pipeline restart failed - could not create pipeline")

        finally:
            self._pipeline_restarting = False

    def restart(self):
        """Public method to restart the video pipeline (called by minus)."""
        logger.info("[DRMAdBlocker] External restart requested")
        threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def start_no_signal_mode(self):
        """Start the pipeline in no-signal mode (blocking display only).

        Used when no HDMI input is detected at startup. Starts the pipeline
        but immediately switches to blocking input with "NO HDMI INPUT" message.
        The video input will be unused/erroring but that's fine since we
        use the blocking input exclusively.
        """
        if not self.pipeline:
            logger.error("[DRMAdBlocker] No pipeline to start")
            return False

        try:
            # Switch to blocking input BEFORE starting so video errors don't matter
            if self.selector and self.blocking_pad:
                self.selector.set_property('active-pad', self.blocking_pad)

            # Set the no-signal text
            if self.textoverlay:
                text = self._get_blocking_text('no_hdmi_device')
                self.textoverlay.set_property('text', text)

            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("[DRMAdBlocker] Failed to start pipeline in no-signal mode")
                return False

            self.is_visible = True
            self.current_source = 'no_hdmi_device'
            logger.info("[DRMAdBlocker] Pipeline started in no-signal mode (showing NO HDMI INPUT)")
            return True

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start pipeline in no-signal mode: {e}")
            return False

    def _on_error(self, bus, message):
        """Handle GStreamer pipeline errors."""
        err, debug = message.parse_error()
        self._pipeline_errors += 1
        self._last_error_time = time.time()

        logger.error(f"[DRMAdBlocker] Pipeline error: {err.message}")
        logger.debug(f"[DRMAdBlocker] Debug info: {debug}")

        # Check for recoverable errors (connection errors from souphttpsrc)
        error_msg = err.message.lower() if err.message else ""
        if any(keyword in error_msg for keyword in ['connection', 'refused', 'timeout', 'socket', 'http', 'network']):
            logger.warning("[DRMAdBlocker] HTTP connection error detected - triggering restart")
            # Don't restart immediately if we're already in blocking mode
            if not self.is_visible:
                threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def _on_eos(self, bus, message):
        """Handle end-of-stream (shouldn't happen for live source)."""
        logger.warning("[DRMAdBlocker] Unexpected EOS received - triggering restart")
        if not self.is_visible:
            threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def _on_warning(self, bus, message):
        """Handle GStreamer pipeline warnings."""
        warn, debug = message.parse_warning()
        logger.warning(f"[DRMAdBlocker] Pipeline warning: {warn.message}")

    def get_pipeline_health(self):
        """Get pipeline health status for health monitor."""
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
        """Generate blocking text with Spanish vocabulary."""
        # Special case: HDMI signal lost
        if source == 'hdmi_lost':
            return (
                "NO SIGNAL\n\n"
                "HDMI input disconnected\n\n"
                "Waiting for signal..."
            )

        # Special case: No HDMI device at startup
        if source == 'no_hdmi_device':
            return (
                "NO HDMI INPUT\n\n"
                "No HDMI input recognized\n\n"
                "Waiting for HDMI signal..."
            )

        # Header based on detection source
        if source == 'ocr':
            header = "BLOCKING (OCR)"
        elif source == 'vlm':
            header = "BLOCKING (VLM)"
        elif source == 'both':
            header = "BLOCKING (OCR+VLM)"
        else:
            header = "BLOCKING AD"

        # Get random Spanish vocabulary
        vocab = random.choice(SPANISH_VOCABULARY)
        spanish, english, example = vocab

        # Format the display text
        text = (
            f"{header}\n\n"
            f"{spanish}\n"
            f"= {english}\n\n"
            f"{example}"
        )
        return text

    def _rotation_loop(self, source):
        """Background thread to rotate vocabulary every 3-5 seconds."""
        while not self._stop_rotation.is_set():
            # Update text - GStreamer handles thread safety for property updates
            text = self._get_blocking_text(source)
            if self.textoverlay:
                try:
                    self.textoverlay.set_property('text', text)
                except Exception as e:
                    logger.debug(f"[DRMAdBlocker] Text update error: {e}")

            # Wait 11-15 seconds before next rotation (more reading time)
            wait_time = random.uniform(11.0, 15.0)
            self._stop_rotation.wait(wait_time)

    def _start_rotation(self, source):
        """Start the vocabulary rotation thread."""
        self._stop_rotation.clear()
        self._rotation_thread = threading.Thread(
            target=self._rotation_loop,
            args=(source,),
            daemon=True
        )
        self._rotation_thread.start()

    def _stop_rotation_thread(self):
        """Stop the vocabulary rotation thread."""
        self._stop_rotation.set()
        if self._rotation_thread:
            self._rotation_thread.join(timeout=1.0)
            self._rotation_thread = None

    def _create_placeholder_preview(self):
        """Create a placeholder preview image (small black rectangle)."""
        try:
            # Create a minimal valid JPEG (1x1 black pixel)
            # This is just a placeholder until the first real snapshot
            placeholder_data = bytes([
                0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
                0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
                0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
                0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
                0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
                0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
                0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
                0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
                0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
                0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
                0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
                0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
                0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
                0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
                0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
                0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
                0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
                0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
                0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
                0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
                0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
                0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
                0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
                0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
                0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
                0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
                0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD5, 0xDB, 0x20, 0xA8, 0xF1, 0x9E, 0xDF,
                0xFF, 0xD9
            ])
            with open(self._preview_path, 'wb') as f:
                f.write(placeholder_data)
            logger.debug(f"[DRMAdBlocker] Created placeholder preview at {self._preview_path}")
        except Exception as e:
            logger.warning(f"[DRMAdBlocker] Failed to create placeholder preview: {e}")

    def _create_placeholder_pixelated_bg(self):
        """Create a placeholder pixelated background image (dark gray)."""
        try:
            if HAS_OPENCV:
                # Create a dark gray image matching output resolution
                img = np.full((self.output_height, self.output_width, 3), 30, dtype=np.uint8)
                cv2.imwrite(self._pixelated_bg_path, img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                logger.debug(f"[DRMAdBlocker] Created placeholder pixelated background at {self._pixelated_bg_path}")
            else:
                # Copy the placeholder preview as fallback
                shutil.copy(self._preview_path, self._pixelated_bg_path)
        except Exception as e:
            logger.warning(f"[DRMAdBlocker] Failed to create placeholder pixelated background: {e}")
            # Copy the placeholder preview as fallback
            try:
                shutil.copy(self._preview_path, self._pixelated_bg_path)
            except:
                pass

    def _start_snapshot_buffer(self):
        """Start the rolling snapshot buffer thread."""
        self._stop_snapshot_buffer.clear()
        self._snapshot_buffer_thread = threading.Thread(
            target=self._snapshot_buffer_loop,
            daemon=True,
            name="SnapshotBuffer"
        )
        self._snapshot_buffer_thread.start()
        logger.debug("[DRMAdBlocker] Snapshot buffer thread started")

    def _stop_snapshot_buffer_thread(self):
        """Stop the snapshot buffer thread."""
        self._stop_snapshot_buffer.set()
        if self._snapshot_buffer_thread:
            self._snapshot_buffer_thread.join(timeout=2.0)
            self._snapshot_buffer_thread = None

    def _snapshot_buffer_loop(self):
        """Background thread to continuously capture snapshots into rolling buffer."""
        logger.debug("[DRMAdBlocker] Snapshot buffer loop started")

        while not self._stop_snapshot_buffer.is_set():
            try:
                # Fetch snapshot from ustreamer
                url = f"http://localhost:{self.ustreamer_port}/snapshot"
                with urllib.request.urlopen(url, timeout=2) as response:
                    snapshot_data = response.read()

                # Store in rolling buffer with timestamp
                self._snapshot_buffer.append({
                    'data': snapshot_data,
                    'time': time.time()
                })

            except Exception as e:
                logger.debug(f"[DRMAdBlocker] Snapshot buffer capture error: {e}")

            # Wait before next capture
            self._stop_snapshot_buffer.wait(self._snapshot_interval)

        logger.debug("[DRMAdBlocker] Snapshot buffer loop stopped")

    def _get_snapshot_from_buffer(self, seconds_ago=2.0):
        """Get a snapshot from the buffer that's approximately N seconds old.

        Args:
            seconds_ago: How many seconds ago the snapshot should be from

        Returns:
            JPEG bytes or None if no suitable snapshot found
        """
        if not self._snapshot_buffer:
            return None

        target_time = time.time() - seconds_ago

        # Find the snapshot closest to the target time
        best_snapshot = None
        best_diff = float('inf')

        for snapshot in self._snapshot_buffer:
            diff = abs(snapshot['time'] - target_time)
            if diff < best_diff:
                best_diff = diff
                best_snapshot = snapshot

        if best_snapshot:
            return best_snapshot['data']

        return None

    def _pixelate_image(self, jpeg_data, factor=None):
        """Pixelate a JPEG image by downscaling and upscaling.

        Args:
            jpeg_data: JPEG bytes
            factor: Pixelation factor (higher = more pixelated). Default uses instance setting.

        Returns:
            Pixelated JPEG bytes or None on error
        """
        if not HAS_OPENCV:
            logger.warning("[DRMAdBlocker] OpenCV not available for pixelation")
            return None

        factor = factor or self._pixelation_factor

        try:
            # Decode JPEG
            nparr = np.frombuffer(jpeg_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if img is None:
                return None

            h, w = img.shape[:2]

            # Downscale
            small_w = max(1, w // factor)
            small_h = max(1, h // factor)
            small = cv2.resize(img, (small_w, small_h), interpolation=cv2.INTER_LINEAR)

            # Upscale back to original size (nearest neighbor for blocky look)
            pixelated = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)

            # Darken the image slightly to make text more readable (multiply by 0.6)
            pixelated = (pixelated * 0.6).astype(np.uint8)

            # Encode back to JPEG
            _, encoded = cv2.imencode('.jpg', pixelated, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return encoded.tobytes()

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Pixelation error: {e}")
            return None

    def _prepare_pixelated_background(self):
        """Prepare the pixelated background image from the rolling buffer.

        Gets the oldest snapshot from the buffer (~6 seconds ago), pixelates it, and saves to disk.
        This ensures the background shows content from before the ad appeared.
        """
        # Get the oldest snapshot from buffer (first item = oldest)
        snapshot_data = None
        if self._snapshot_buffer:
            snapshot_data = self._snapshot_buffer[0]['data']  # Oldest snapshot

        if not snapshot_data:
            logger.debug("[DRMAdBlocker] No snapshot available for pixelated background")
            return False

        # Pixelate the image
        pixelated_data = self._pixelate_image(snapshot_data)

        if not pixelated_data:
            logger.debug("[DRMAdBlocker] Failed to pixelate snapshot")
            return False

        # Save to disk
        try:
            temp_path = self._pixelated_bg_path + ".tmp"
            with open(temp_path, 'wb') as f:
                f.write(pixelated_data)
            os.rename(temp_path, self._pixelated_bg_path)
            logger.debug("[DRMAdBlocker] Prepared pixelated background")
            return True
        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to save pixelated background: {e}")
            return False

    def _preview_loop(self):
        """Background thread to periodically update the preview image from ustreamer."""
        logger.debug("[DRMAdBlocker] Preview update thread started")
        while not self._stop_preview.is_set():
            try:
                # Fetch snapshot from ustreamer
                url = f"http://localhost:{self.ustreamer_port}/snapshot"
                temp_path = self._preview_path + ".tmp"

                with urllib.request.urlopen(url, timeout=2) as response:
                    with open(temp_path, 'wb') as f:
                        shutil.copyfileobj(response, f)

                # Atomic rename to avoid partial reads
                os.rename(temp_path, self._preview_path)

                # Trigger gdkpixbufoverlay to reload the image by re-setting location
                if self.previewoverlay:
                    try:
                        self.previewoverlay.set_property('location', self._preview_path)
                    except Exception as e:
                        logger.debug(f"[DRMAdBlocker] Preview overlay update error: {e}")

            except Exception as e:
                logger.debug(f"[DRMAdBlocker] Preview update error: {e}")

            # Wait before next update
            self._stop_preview.wait(self._preview_interval)

        logger.debug("[DRMAdBlocker] Preview update thread stopped")

    def _start_preview(self):
        """Start the preview update thread (if preview is enabled)."""
        if not self._preview_enabled:
            return

        self._stop_preview.clear()
        self._preview_thread = threading.Thread(
            target=self._preview_loop,
            daemon=True,
            name="PreviewUpdate"
        )
        self._preview_thread.start()

    def _stop_preview_thread(self):
        """Stop the preview update thread."""
        self._stop_preview.set()
        if self._preview_thread:
            self._preview_thread.join(timeout=2.0)
            self._preview_thread = None

    def is_preview_enabled(self):
        """Check if preview window is enabled."""
        return self._preview_enabled

    def set_preview_enabled(self, enabled):
        """Enable or disable the preview window.

        Args:
            enabled: True to enable preview, False to disable
        """
        self._preview_enabled = enabled
        logger.info(f"[DRMAdBlocker] Preview window {'enabled' if enabled else 'disabled'}")

        if self.is_visible:
            # Currently blocking - apply change immediately
            if enabled:
                # Start preview thread if not running
                if not self._preview_thread or not self._preview_thread.is_alive():
                    self._start_preview()
                # Show the overlay
                if self.previewoverlay:
                    self.previewoverlay.set_property('alpha', 1.0)
            else:
                # Stop preview thread
                self._stop_preview_thread()
                # Hide the overlay by setting alpha to 0
                if self.previewoverlay:
                    self.previewoverlay.set_property('alpha', 0.0)

    def _get_debug_text(self):
        """Generate debug dashboard text with current stats."""
        # Get uptime from minus instance
        uptime_str = "N/A"
        if self.minus:
            uptime_secs = int(time.time() - self.minus.start_time) if hasattr(self.minus, 'start_time') else 0
            hours, remainder = divmod(uptime_secs, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"

        # Calculate current blocking session time
        current_block_time = 0
        if self._current_block_start:
            current_block_time = time.time() - self._current_block_start

        # Total blocking time (including current session)
        total_block_secs = int(self._total_blocking_time + current_block_time)
        block_mins, block_secs = divmod(total_block_secs, 60)
        block_hours, block_mins = divmod(block_mins, 60)
        if block_hours > 0:
            block_time_str = f"{block_hours}h {block_mins}m {block_secs}s"
        else:
            block_time_str = f"{block_mins}m {block_secs}s"

        # Format debug text (compact multi-line format to fit in corner)
        debug_text = (
            f"Uptime: {uptime_str}\n"
            f"Ads blocked: {self._total_ads_blocked}\n"
            f"Block time: {block_time_str}"
        )

        # Add skip status if available
        if self._skip_available:
            debug_text += f"\n>>> SKIP NOW! <<<"
        elif self._skip_text:
            debug_text += f"\n{self._skip_text}"

        return debug_text

    def _debug_loop(self):
        """Background thread to periodically update the debug overlay."""
        logger.debug("[DRMAdBlocker] Debug update thread started")
        while not self._stop_debug.is_set():
            try:
                if self.debugoverlay and self._debug_overlay_enabled:
                    text = self._get_debug_text()
                    self.debugoverlay.set_property('text', text)
            except Exception as e:
                logger.debug(f"[DRMAdBlocker] Debug update error: {e}")

            # Wait before next update
            self._stop_debug.wait(self._debug_interval)

        logger.debug("[DRMAdBlocker] Debug update thread stopped")

    def _start_debug(self):
        """Start the debug update thread (if debug overlay is enabled)."""
        if not self._debug_overlay_enabled:
            # Still need to hide the overlay if disabled
            if self.debugoverlay:
                self.debugoverlay.set_property('text', '')
            return

        self._stop_debug.clear()
        self._debug_thread = threading.Thread(
            target=self._debug_loop,
            daemon=True,
            name="DebugUpdate"
        )
        self._debug_thread.start()

    def _stop_debug_thread(self):
        """Stop the debug update thread."""
        self._stop_debug.set()
        if self._debug_thread:
            self._debug_thread.join(timeout=2.0)
            self._debug_thread = None

    def is_debug_overlay_enabled(self):
        """Check if debug overlay is enabled."""
        return self._debug_overlay_enabled

    def set_debug_overlay_enabled(self, enabled):
        """Enable or disable the debug overlay.

        Args:
            enabled: True to enable debug overlay, False to disable
        """
        self._debug_overlay_enabled = enabled
        logger.info(f"[DRMAdBlocker] Debug overlay {'enabled' if enabled else 'disabled'}")

        if self.is_visible:
            # Currently blocking - apply change immediately
            if enabled:
                # Start debug thread if not running
                if not self._debug_thread or not self._debug_thread.is_alive():
                    self._start_debug()
            else:
                # Stop debug thread and clear text
                self._stop_debug_thread()
                if self.debugoverlay:
                    self.debugoverlay.set_property('text', '')

    # Skip status methods (for Fire TV integration)
    def set_skip_status(self, available: bool, text: str = None):
        """Update skip button status for display.

        Args:
            available: True if skip button is ready (no countdown)
            text: Skip status text (e.g., "Skip", "Skip in 5s")
        """
        self._skip_available = available
        self._skip_text = text

    def get_skip_status(self) -> tuple:
        """Get current skip status.

        Returns:
            Tuple of (is_available, text)
        """
        return (self._skip_available, self._skip_text)

    def set_test_mode(self, duration_seconds: float):
        """Enable test mode - blocks hide() for specified duration.

        Args:
            duration_seconds: How long to ignore hide() calls
        """
        self._test_blocking_until = time.time() + duration_seconds
        logger.info(f"[DRMAdBlocker] Test mode enabled for {duration_seconds}s")

    def clear_test_mode(self):
        """Clear test mode, allowing normal hide() behavior."""
        self._test_blocking_until = 0
        logger.info("[DRMAdBlocker] Test mode cleared")

    def is_test_mode_active(self) -> bool:
        """Check if test mode is currently active."""
        return self._test_blocking_until > time.time()

    # Animation methods
    def _ease_out(self, t):
        """Ease-out function: fast start, slow finish (quadratic)."""
        return 1 - (1 - t) ** 2

    def _ease_in(self, t):
        """Ease-in function: slow start, fast finish (quadratic)."""
        return t ** 2

    def _stop_animation_thread(self):
        """Stop any running animation thread."""
        self._stop_animation.set()
        if self._animation_thread:
            self._animation_thread.join(timeout=2.0)
            self._animation_thread = None
        self._animating = False

    def _start_animation(self, direction, source=None):
        """Start animation thread.

        Args:
            direction: 'start' (full-screen to corner) or 'end' (corner to full-screen)
            source: Detection source (only used for 'start' direction)
        """
        self._stop_animation_thread()
        self._stop_animation.clear()
        self._animation_source = source
        self._animating = True

        self._animation_thread = threading.Thread(
            target=self._animation_loop,
            args=(direction,),
            daemon=True,
            name=f"Animation-{direction}"
        )
        self._animation_thread.start()

    def _animation_loop(self, direction):
        """Animation loop that interpolates preview position/size.

        Args:
            direction: 'start' (shrink to corner) or 'end' (grow to full-screen)
        """
        start_time = time.time()

        # Full-screen position
        full_x, full_y = 0, 0
        full_w, full_h = self.output_width, self.output_height

        # Corner position
        corner_x = self._preview_corner_x
        corner_y = self._preview_corner_y
        corner_w = self._preview_corner_w
        corner_h = self._preview_corner_h

        logger.debug(f"[DRMAdBlocker] Animation '{direction}' starting")

        # Track when we last updated the preview image (for live feel during animation)
        last_preview_update = start_time
        preview_update_interval = self._preview_interval  # ~4fps

        while not self._stop_animation.is_set():
            current_time = time.time()
            elapsed = current_time - start_time
            progress = min(1.0, elapsed / self._animation_duration)

            if direction == 'start':
                t = self._ease_out(progress)
                # Interpolate from full to corner
                x = int(full_x + (corner_x - full_x) * t)
                y = int(full_y + (corner_y - full_y) * t)
                w = int(full_w + (corner_w - full_w) * t)
                h = int(full_h + (corner_h - full_h) * t)
            else:  # 'end'
                t = self._ease_in(progress)
                # Interpolate from corner to full
                x = int(corner_x + (full_x - corner_x) * t)
                y = int(corner_y + (full_y - corner_y) * t)
                w = int(corner_w + (full_w - corner_w) * t)
                h = int(corner_h + (full_h - corner_h) * t)

            # Update preview image periodically for live feel during animation
            if current_time - last_preview_update >= preview_update_interval:
                try:
                    url = f"http://localhost:{self.ustreamer_port}/snapshot"
                    temp_path = self._preview_path + ".tmp"
                    with urllib.request.urlopen(url, timeout=1) as response:
                        with open(temp_path, 'wb') as f:
                            shutil.copyfileobj(response, f)
                    os.rename(temp_path, self._preview_path)
                    if self.previewoverlay:
                        self.previewoverlay.set_property('location', self._preview_path)
                    last_preview_update = current_time
                except Exception:
                    pass  # Don't let preview update failures interrupt animation

            # Update preview overlay properties
            try:
                if self.previewoverlay:
                    self.previewoverlay.set_property('offset-x', x)
                    self.previewoverlay.set_property('offset-y', y)
                    self.previewoverlay.set_property('overlay-width', w)
                    self.previewoverlay.set_property('overlay-height', h)
            except Exception as e:
                logger.debug(f"[DRMAdBlocker] Animation property update error: {e}")

            if progress >= 1.0:
                break

            time.sleep(0.033)  # ~30fps animation

        # Animation complete - trigger final state
        self._animating = False
        if direction == 'start':
            self._on_start_animation_complete()
        else:
            self._on_end_animation_complete()

    def _on_start_animation_complete(self):
        """Called when start animation (shrink to corner) completes."""
        logger.debug("[DRMAdBlocker] Start animation complete")

        source = self._animation_source or 'default'

        # Show language learning text
        text = self._get_blocking_text(source)
        if self.textoverlay:
            self.textoverlay.set_property('text', text)

        # Start vocabulary rotation
        self._start_rotation(source)

        # Start live preview updates
        self._start_preview()

        # Start debug overlay
        self._current_block_start = time.time()
        self._total_ads_blocked += 1
        self._start_debug()

    def _on_end_animation_complete(self):
        """Called when end animation (grow to full-screen) completes."""
        logger.debug("[DRMAdBlocker] End animation complete")

        # Switch to video input
        if self.selector and self.video_pad:
            logger.info("[DRMAdBlocker] Switching to video stream")
            self.selector.set_property('active-pad', self.video_pad)

        # Unmute audio
        if self.audio:
            self.audio.unmute()

    def set_minus(self, minus_instance):
        """Set reference to Minus instance."""
        self.minus = minus_instance

    def set_audio(self, audio):
        """Set reference to AudioPassthrough for mute control."""
        self.audio = audio

    def show(self, source='default'):
        """Switch to blocking overlay with animation.

        Args:
            source: Detection source - 'ocr', 'vlm', 'both', or 'default'
        """
        with self._lock:
            if not self.pipeline or not self.selector:
                logger.warning("[DRMAdBlocker] Pipeline not initialized")
                return

            # If already blocking (or animating to block), just update the source
            if self.is_visible or self._animating:
                if self.current_source != source:
                    self.current_source = source
                return

            logger.info(f"[DRMAdBlocker] Starting blocking animation ({source})")

            # Prepare pixelated background from snapshot buffer (~2 seconds ago)
            if self._prepare_pixelated_background():
                logger.debug("[DRMAdBlocker] Pixelated background prepared")
                # Trigger bgoverlay to reload the new pixelated background
                if self.bgoverlay:
                    self.bgoverlay.set_property('location', self._pixelated_bg_path)
            else:
                logger.debug("[DRMAdBlocker] Using placeholder pixelated background")

            # Capture current frame for animation (update preview image first)
            try:
                url = f"http://localhost:{self.ustreamer_port}/snapshot"
                temp_path = self._preview_path + ".tmp"
                with urllib.request.urlopen(url, timeout=2) as response:
                    with open(temp_path, 'wb') as f:
                        shutil.copyfileobj(response, f)
                os.rename(temp_path, self._preview_path)
            except Exception as e:
                logger.debug(f"[DRMAdBlocker] Failed to capture animation frame: {e}")

            # Switch to blocking input
            self.selector.set_property('active-pad', self.blocking_pad)

            # Mute audio immediately
            if self.audio:
                self.audio.mute()

            # Hide text overlays during animation
            if self.textoverlay:
                self.textoverlay.set_property('text', '')
            if self.debugoverlay:
                self.debugoverlay.set_property('text', '')

            # Set preview to full-screen for animation start
            if self.previewoverlay:
                self.previewoverlay.set_property('offset-x', 0)
                self.previewoverlay.set_property('offset-y', 0)
                self.previewoverlay.set_property('overlay-width', self.output_width)
                self.previewoverlay.set_property('overlay-height', self.output_height)
                self.previewoverlay.set_property('alpha', 1.0)
                # Trigger reload of the captured frame
                self.previewoverlay.set_property('location', self._preview_path)

            self.is_visible = True
            self.current_source = source

            # Set flag to prevent external restarts
            if self.minus:
                self.minus.blocking_active = True

            # Start animation (will trigger _on_start_animation_complete when done)
            self._start_animation('start', source)

    def hide(self, force=False):
        """Switch back to video stream with animation.

        Args:
            force: If True, ignore test mode and hide anyway
        """
        # Check test mode BEFORE acquiring lock to avoid any race conditions
        if not force and self._test_blocking_until > time.time():
            return  # Silently ignore - test mode active

        with self._lock:

            # Always update visibility state first, even if pipeline is unavailable
            # This ensures _restart_pipeline() doesn't restore blocking when it shouldn't
            was_visible = self.is_visible
            self.is_visible = False
            self.current_source = None

            # Clear blocking flag
            if self.minus:
                self.minus.blocking_active = False

            # Stop vocabulary rotation (safe to call even if not running)
            self._stop_rotation_thread()

            # Stop preview update thread (freeze current frame for animation)
            self._stop_preview_thread()

            # Stop debug overlay thread and accumulate blocking time
            self._stop_debug_thread()
            if self._current_block_start:
                self._total_blocking_time += time.time() - self._current_block_start
                self._current_block_start = None

            # Hide text overlays immediately for animation
            if self.textoverlay:
                self.textoverlay.set_property('text', '')
            if self.debugoverlay:
                self.debugoverlay.set_property('text', '')

            if not self.pipeline or not self.selector:
                if was_visible:
                    logger.warning("[DRMAdBlocker] Pipeline not initialized, but marking as hidden")
                # Still unmute even without pipeline
                if self.audio:
                    self.audio.unmute()
                return

            if not was_visible and not self._animating:
                return  # Was already hidden, nothing to do with pipeline

            # If currently animating (e.g., start animation), cancel it
            if self._animating:
                self._stop_animation_thread()

            logger.info("[DRMAdBlocker] Starting end blocking animation")

            # Start end animation (will switch to video and unmute when complete)
            self._start_animation('end', None)

    def update(self, ad_detected, is_skippable=False, skip_location=None,
               ocr_detected=False, vlm_detected=False):
        """Update overlay based on ad detection."""
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
        """Clean up the pipeline."""
        with self._lock:
            # Stop watchdog thread
            self._stop_watchdog_thread()

            # Stop rotation thread
            self._stop_rotation_thread()

            # Stop preview thread
            self._stop_preview_thread()

            # Stop debug thread
            self._stop_debug_thread()

            # Stop animation thread
            self._stop_animation_thread()

            # Stop snapshot buffer thread
            self._stop_snapshot_buffer_thread()

            if self.pipeline:
                try:
                    # Remove bus watch
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None

                    self.pipeline.set_state(Gst.State.NULL)
                    logger.info("[DRMAdBlocker] Pipeline stopped")
                except Exception as e:
                    logger.error(f"[DRMAdBlocker] Error stopping pipeline: {e}")

                self.pipeline = None
                self.selector = None

            self.is_visible = False


# Export the ad blocker class
AdBlocker = DRMAdBlocker
