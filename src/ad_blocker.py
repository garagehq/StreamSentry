"""
Ad Blocker Overlay for Stream Sentry.

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

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

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

    def __init__(self, connector_id=215, plane_id=72, stream_sentry=None, ustreamer_port=9090):
        self.is_visible = False
        self.current_source = None
        self.connector_id = connector_id
        self.plane_id = plane_id
        self.ustreamer_port = ustreamer_port
        self.stream_sentry = stream_sentry
        self._lock = threading.Lock()

        # GStreamer pipeline and elements
        self.pipeline = None
        self.selector = None
        self.video_pad = None
        self.blocking_pad = None
        self.textoverlay = None
        self.bus = None

        # Audio passthrough reference (set by stream_sentry)
        self.audio = None

        # Pipeline health tracking
        self._pipeline_errors = 0
        self._last_error_time = 0

        # FPS tracking
        self._frame_count = 0
        self._fps_start_time = time.time()
        self._current_fps = 0.0
        self._fps_lock = threading.Lock()

        # Text rotation for Spanish vocabulary
        self._rotation_thread = None
        self._stop_rotation = threading.Event()
        self._current_vocab_index = 0

        # Initialize GStreamer
        Gst.init(None)
        self._init_pipeline()

    def _init_pipeline(self):
        """Initialize GStreamer pipeline with input-selector for instant switching."""
        try:
            # Build pipeline with input-selector
            # sink_0 = video stream, sink_1 = blocking pattern
            #
            # Color correction via videobalance (HDMI-RX doesn't support V4L2 image controls):
            # - saturation=0.85: reduce oversaturation (default 1.0, range 0-2)
            # - contrast=1.0: keep default
            # - brightness=0.0: keep default
            pipeline_str = (
                f"input-selector name=sel ! "
                f"identity name=fpsprobe ! "
                f"kmssink plane-id={self.plane_id} connector-id={self.connector_id} sync=false "

                # Video input (sink_0) with color correction
                f"souphttpsrc location=http://localhost:{self.ustreamer_port}/stream blocksize=524288 ! "
                f"multipartdemux ! jpegparse ! mppjpegdec ! video/x-raw,format=NV12 ! "
                f"videobalance saturation=0.85 name=colorbalance ! "
                f"queue max-size-buffers=3 leaky=downstream name=videoqueue ! sel.sink_0 "

                # Blocking input (sink_1) - black screen with text
                f"videotestsrc pattern=2 is-live=true ! "
                f"video/x-raw,format=NV12,width=3840,height=2160,framerate=30/1 ! "
                f"textoverlay name=blocktext text='BLOCKING AD' font-desc='Sans Bold 48' "
                f"valignment=center halignment=center shaded-background=true ! "
                f"queue name=blockqueue ! sel.sink_1"
            )

            logger.debug(f"[DRMAdBlocker] Creating pipeline...")
            self.pipeline = Gst.parse_launch(pipeline_str)

            # Get references to key elements
            self.selector = self.pipeline.get_by_name('sel')
            self.textoverlay = self.pipeline.get_by_name('blocktext')

            # Get the pads for switching
            self.video_pad = self.selector.get_static_pad('sink_0')
            self.blocking_pad = self.selector.get_static_pad('sink_1')

            # Start with video input active
            self.selector.set_property('active-pad', self.video_pad)

            # Set up bus message handling for error detection
            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)
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
        with self._fps_lock:
            self._frame_count += 1
            current_time = time.time()
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
            return True

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start pipeline: {e}")
            return False

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

        # Notify health monitor if available
        if self.stream_sentry and hasattr(self.stream_sentry, 'health_monitor'):
            # The health monitor will handle recovery
            pass

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

    def set_stream_sentry(self, stream_sentry):
        """Set reference to StreamSentry."""
        self.stream_sentry = stream_sentry

    def set_audio(self, audio):
        """Set reference to AudioPassthrough for mute control."""
        self.audio = audio

    def show(self, source='default'):
        """Switch to blocking overlay - INSTANT, no pipeline restart.

        Args:
            source: Detection source - 'ocr', 'vlm', 'both', or 'default'
        """
        with self._lock:
            if not self.pipeline or not self.selector:
                logger.warning("[DRMAdBlocker] Pipeline not initialized")
                return

            # If already blocking, just update the source for rotation
            if self.is_visible:
                if self.current_source != source:
                    self.current_source = source
                    # Rotation thread will pick up the new source
                return

            # Set initial text
            text = self._get_blocking_text(source)
            if self.textoverlay:
                self.textoverlay.set_property('text', text)

            # Switch to blocking input - INSTANT!
            logger.info(f"[DRMAdBlocker] Switching to blocking overlay ({source})")
            self.selector.set_property('active-pad', self.blocking_pad)

            # Mute audio during ad blocking
            if self.audio:
                self.audio.mute()

            self.is_visible = True
            self.current_source = source

            # Start vocabulary rotation
            self._start_rotation(source)

            # Set flag to prevent external restarts
            if self.stream_sentry:
                self.stream_sentry.blocking_active = True

    def hide(self):
        """Switch back to video stream - INSTANT, no pipeline restart."""
        with self._lock:
            if not self.pipeline or not self.selector:
                logger.warning("[DRMAdBlocker] Pipeline not initialized")
                return

            if not self.is_visible:
                return

            # Stop vocabulary rotation
            self._stop_rotation_thread()

            # Switch to video input - INSTANT!
            logger.info("[DRMAdBlocker] Switching to video stream")
            self.selector.set_property('active-pad', self.video_pad)

            # Unmute audio after ad ends
            if self.audio:
                self.audio.unmute()

            self.is_visible = False
            self.current_source = None

            # Clear blocking flag
            if self.stream_sentry:
                self.stream_sentry.blocking_active = False

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
            # Stop rotation thread
            self._stop_rotation_thread()

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
