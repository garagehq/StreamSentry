"""
Minus Web UI

Lightweight Flask-based web interface for monitoring and controlling Minus.
Accessible via Tailscale for remote debugging and control.

Features:
- Live video feed (proxied from ustreamer)
- Status display (blocking state, FPS, HDMI, etc.)
- Pause/resume blocking (1/2/5/10 min presets)
- Recent detection history
- Log viewer
"""

import logging
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, Response, send_from_directory
import requests

logger = logging.getLogger('Minus.WebUI')


class WebUI:
    """Web UI server for Minus."""

    def __init__(self, minus_instance, port: int = 8080, ustreamer_port: int = 9090):
        """
        Initialize web UI.

        Args:
            minus_instance: Minus instance to control
            port: Port to run web server on
            ustreamer_port: Port where ustreamer is running (for stream proxy)
        """
        self.minus = minus_instance
        self.port = port
        self.ustreamer_port = ustreamer_port
        self.server_thread = None
        self.running = False

        # Create Flask app
        self.app = Flask(
            __name__,
            template_folder=str(Path(__file__).parent / 'templates'),
            static_folder=str(Path(__file__).parent / 'static'),
        )

        # Disable Flask's default logging (we use our own)
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.WARNING)

        # Register routes
        self._register_routes()

    def _register_routes(self):
        """Register all Flask routes."""

        @self.app.route('/')
        def index():
            """Serve the main UI page."""
            return send_from_directory(
                self.app.template_folder,
                'index.html'
            )

        @self.app.route('/api/status')
        def api_status():
            """Get current status."""
            try:
                status = self.minus.get_status_dict()
                return jsonify(status)
            except Exception as e:
                logger.error(f"Error getting status: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/pause/<int:minutes>', methods=['POST'])
        def api_pause(minutes):
            """Pause blocking for specified minutes."""
            if minutes not in [1, 2, 5, 10]:
                return jsonify({'error': 'Invalid duration. Use 1, 2, 5, or 10 minutes.'}), 400

            try:
                self.minus.pause_blocking(minutes * 60)
                return jsonify({
                    'success': True,
                    'paused_until': self.minus.blocking_paused_until,
                    'duration_minutes': minutes,
                })
            except Exception as e:
                logger.error(f"Error pausing: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/resume', methods=['POST'])
        def api_resume():
            """Resume blocking immediately."""
            try:
                self.minus.resume_blocking()
                return jsonify({'success': True})
            except Exception as e:
                logger.error(f"Error resuming: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/detections')
        def api_detections():
            """Get recent detection history."""
            try:
                detections = list(self.minus.detection_history)
                # Return in reverse order (newest first)
                return jsonify({'detections': detections[::-1]})
            except Exception as e:
                logger.error(f"Error getting detections: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/logs')
        def api_logs():
            """Get recent log lines."""
            try:
                log_file = Path('/tmp/minus.log')
                if log_file.exists():
                    # Read last 100 lines
                    with open(log_file, 'r') as f:
                        lines = f.readlines()[-100:]
                    return jsonify({'lines': [line.rstrip() for line in lines]})
                return jsonify({'lines': []})
            except Exception as e:
                logger.error(f"Error reading logs: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/stream')
        def stream_proxy():
            """Proxy the MJPEG stream from ustreamer (for CORS bypass)."""
            try:
                # Stream from ustreamer
                url = f'http://localhost:{self.ustreamer_port}/stream'
                req = requests.get(url, stream=True, timeout=10)

                def generate():
                    for chunk in req.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk

                # Pass through the Content-Type from ustreamer (includes correct boundary)
                content_type = req.headers.get('Content-Type', 'multipart/x-mixed-replace;boundary=boundarydonotcross')

                return Response(
                    generate(),
                    mimetype=content_type,
                    headers={
                        'Cache-Control': 'no-cache, no-store, must-revalidate',
                        'Pragma': 'no-cache',
                        'Expires': '0',
                    }
                )
            except Exception as e:
                logger.error(f"Stream proxy error: {e}")
                return Response(status=503)

        @self.app.route('/snapshot')
        def snapshot_proxy():
            """Proxy a single snapshot from ustreamer."""
            try:
                url = f'http://localhost:{self.ustreamer_port}/snapshot'
                req = requests.get(url, timeout=5)
                return Response(
                    req.content,
                    mimetype='image/jpeg',
                    headers={
                        'Cache-Control': 'no-cache, no-store, must-revalidate',
                    }
                )
            except Exception as e:
                logger.error(f"Snapshot proxy error: {e}")
                return Response(status=503)

    def start(self):
        """Start the web server in a background thread."""
        if self.running:
            return

        self.running = True

        def run_server():
            logger.info(f"[WebUI] Starting on http://0.0.0.0:{self.port}")
            try:
                # Use threaded=True for concurrent requests
                self.app.run(
                    host='0.0.0.0',
                    port=self.port,
                    threaded=True,
                    use_reloader=False,
                    debug=False,
                )
            except Exception as e:
                logger.error(f"[WebUI] Server error: {e}")
            finally:
                self.running = False

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()

        # Give it a moment to start
        time.sleep(0.5)
        logger.info(f"[WebUI] Server started on port {self.port}")

    def stop(self):
        """Stop the web server."""
        self.running = False
        logger.info("[WebUI] Server stopping...")
        # Flask doesn't have a clean shutdown in this mode,
        # but since it's a daemon thread, it will stop when the process exits
