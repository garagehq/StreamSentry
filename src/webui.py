"""
Minus Web UI

Lightweight Flask-based web interface for monitoring and controlling Minus.
Accessible via Tailscale for remote debugging and control.

Features:
- Live video feed (proxied from ustreamer)
- Status display (blocking state, FPS, HDMI, etc.)
- Pause/resume blocking (custom duration support)
- Recent detection history
- Log viewer
- Fire TV remote control
- WiFi network management (nmcli)
- ADB RSA key management
- Screenshot gallery
- Configuration management
"""

import logging
import os
import re
import subprocess
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
            """Pause blocking for specified minutes (1-60)."""
            if minutes < 1 or minutes > 60:
                return jsonify({'error': 'Invalid duration. Use 1-60 minutes.'}), 400

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

        @self.app.route('/api/preview')
        def api_preview_status():
            """Get preview window status."""
            try:
                enabled = False
                if self.minus.ad_blocker:
                    enabled = self.minus.ad_blocker.is_preview_enabled()
                return jsonify({'preview_enabled': enabled})
            except Exception as e:
                logger.error(f"Error getting preview status: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/preview/enable', methods=['POST'])
        def api_preview_enable():
            """Enable the preview window."""
            try:
                if self.minus.ad_blocker:
                    self.minus.ad_blocker.set_preview_enabled(True)
                return jsonify({'success': True, 'preview_enabled': True})
            except Exception as e:
                logger.error(f"Error enabling preview: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/preview/disable', methods=['POST'])
        def api_preview_disable():
            """Disable the preview window."""
            try:
                if self.minus.ad_blocker:
                    self.minus.ad_blocker.set_preview_enabled(False)
                return jsonify({'success': True, 'preview_enabled': False})
            except Exception as e:
                logger.error(f"Error disabling preview: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/debug-overlay')
        def api_debug_overlay_status():
            """Get debug overlay status."""
            try:
                enabled = False
                if self.minus.ad_blocker:
                    enabled = self.minus.ad_blocker.is_debug_overlay_enabled()
                return jsonify({'debug_overlay_enabled': enabled})
            except Exception as e:
                logger.error(f"Error getting debug overlay status: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/debug-overlay/enable', methods=['POST'])
        def api_debug_overlay_enable():
            """Enable the debug overlay."""
            try:
                if self.minus.ad_blocker:
                    self.minus.ad_blocker.set_debug_overlay_enabled(True)
                return jsonify({'success': True, 'debug_overlay_enabled': True})
            except Exception as e:
                logger.error(f"Error enabling debug overlay: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/debug-overlay/disable', methods=['POST'])
        def api_debug_overlay_disable():
            """Disable the debug overlay."""
            try:
                if self.minus.ad_blocker:
                    self.minus.ad_blocker.set_debug_overlay_enabled(False)
                return jsonify({'success': True, 'debug_overlay_enabled': False})
            except Exception as e:
                logger.error(f"Error disabling debug overlay: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/firetv-keepalive')
        def api_firetv_keepalive_status():
            """Get Fire TV keep-alive status."""
            try:
                enabled = False
                if self.minus.fire_tv_controller:
                    enabled = self.minus.fire_tv_controller.is_keepalive_enabled()
                return jsonify({'keepalive_enabled': enabled})
            except Exception as e:
                logger.error(f"Error getting Fire TV keep-alive status: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/firetv-keepalive/enable', methods=['POST'])
        def api_firetv_keepalive_enable():
            """Enable Fire TV keep-alive pings."""
            try:
                if self.minus.fire_tv_controller:
                    self.minus.fire_tv_controller.set_keepalive_enabled(True)
                return jsonify({'success': True, 'keepalive_enabled': True})
            except Exception as e:
                logger.error(f"Error enabling Fire TV keep-alive: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/firetv-keepalive/disable', methods=['POST'])
        def api_firetv_keepalive_disable():
            """Disable Fire TV keep-alive pings."""
            try:
                if self.minus.fire_tv_controller:
                    self.minus.fire_tv_controller.set_keepalive_enabled(False)
                return jsonify({'success': True, 'keepalive_enabled': False})
            except Exception as e:
                logger.error(f"Error disabling Fire TV keep-alive: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/test/trigger-block', methods=['POST'])
        def api_test_trigger_block():
            """Trigger ad blocking for testing.

            Optional JSON body:
            - duration: seconds to block (default: 10, max: 60)
            - source: detection source ('ocr', 'vlm', 'both', 'default')
            """
            try:
                data = request.get_json() or {}
                duration = min(60, max(1, data.get('duration', 10)))
                source = data.get('source', 'ocr')

                if source not in ('ocr', 'vlm', 'both', 'default'):
                    source = 'ocr'

                if self.minus.ad_blocker:
                    # Enable test mode to prevent detection loop from hiding
                    self.minus.ad_blocker.set_test_mode(duration)

                    # Show blocking overlay
                    self.minus.ad_blocker.show(source)

                    # Schedule auto-hide after duration
                    def auto_hide():
                        time.sleep(duration)
                        if self.minus.ad_blocker:
                            self.minus.ad_blocker.clear_test_mode()
                            self.minus.ad_blocker.hide(force=True)
                            logger.info(f"[WebUI] Test blocking ended after {duration}s")

                    threading.Thread(target=auto_hide, daemon=True).start()

                    logger.info(f"[WebUI] Test blocking triggered: source={source}, duration={duration}s")
                    return jsonify({
                        'success': True,
                        'source': source,
                        'duration': duration,
                        'message': f'Blocking for {duration} seconds'
                    })
                else:
                    return jsonify({'error': 'Ad blocker not initialized'}), 500
            except Exception as e:
                logger.error(f"Error triggering test block: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/test/stop-block', methods=['POST'])
        def api_test_stop_block():
            """Stop ad blocking (for testing)."""
            try:
                if self.minus.ad_blocker:
                    self.minus.ad_blocker.clear_test_mode()
                    self.minus.ad_blocker.hide(force=True)
                    logger.info("[WebUI] Test blocking stopped")
                    return jsonify({'success': True})
                else:
                    return jsonify({'error': 'Ad blocker not initialized'}), 500
            except Exception as e:
                logger.error(f"Error stopping test block: {e}")
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

        # =========================================================================
        # Fire TV Remote Control
        # =========================================================================

        @self.app.route('/api/firetv/status')
        def api_firetv_status():
            """Get Fire TV connection status."""
            try:
                if hasattr(self.minus, 'fire_tv_setup') and self.minus.fire_tv_setup:
                    setup = self.minus.fire_tv_setup
                    return jsonify({
                        'connected': setup.is_connected(),
                        'state': setup.state,
                        'device_ip': setup.device_ip if hasattr(setup, 'device_ip') else None,
                    })
                return jsonify({'connected': False, 'state': 'not_initialized'})
            except Exception as e:
                logger.error(f"Error getting Fire TV status: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/firetv/command', methods=['POST'])
        def api_firetv_command():
            """Send a command to Fire TV."""
            try:
                data = request.get_json() or {}
                command = data.get('command')

                valid_commands = [
                    'up', 'down', 'left', 'right', 'select', 'back', 'home', 'menu',
                    'play', 'pause', 'play_pause', 'fast_forward', 'rewind',
                    'volume_up', 'volume_down', 'mute'
                ]

                if command not in valid_commands:
                    return jsonify({'error': f'Invalid command. Valid: {valid_commands}'}), 400

                if hasattr(self.minus, 'fire_tv_setup') and self.minus.fire_tv_setup:
                    controller = self.minus.fire_tv_setup.get_controller()
                    if controller and controller.is_connected:
                        controller.send_command(command)
                        return jsonify({'success': True, 'command': command})
                    return jsonify({'error': 'Fire TV not connected'}), 503

                return jsonify({'error': 'Fire TV not initialized'}), 500
            except Exception as e:
                logger.error(f"Error sending Fire TV command: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # Current Vocabulary Word
        # =========================================================================

        @self.app.route('/api/vocabulary')
        def api_vocabulary():
            """Get current vocabulary word being displayed."""
            try:
                if self.minus.ad_blocker:
                    word_info = self.minus.ad_blocker.get_current_vocabulary()
                    return jsonify(word_info)
                return jsonify({'word': None, 'translation': None, 'example': None})
            except Exception as e:
                logger.error(f"Error getting vocabulary: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # Screenshot Gallery
        # =========================================================================

        @self.app.route('/api/screenshots')
        def api_screenshots():
            """Get list of screenshots with pagination.

            Query params:
            - type: 'ads', 'non_ads', 'vlm_spastic', 'static' (default: 'ads')
            - page: page number starting from 1 (default: 1)
            - limit: items per page (default: 5, max: 20)
            """
            try:
                screenshot_type = request.args.get('type', 'ads')
                page = max(1, int(request.args.get('page', 1)))
                limit = min(20, max(1, int(request.args.get('limit', 5))))

                valid_types = ['ads', 'non_ads', 'vlm_spastic', 'static']
                if screenshot_type not in valid_types:
                    screenshot_type = 'ads'

                screenshots_dir = Path(__file__).parent.parent / 'screenshots' / screenshot_type

                if not screenshots_dir.exists():
                    return jsonify({
                        'screenshots': [],
                        'total': 0,
                        'page': page,
                        'pages': 0,
                        'has_more': False
                    })

                # Get all files sorted by modification time
                all_files = sorted(screenshots_dir.glob('*.png'), key=lambda x: x.stat().st_mtime, reverse=True)
                total = len(all_files)
                pages = (total + limit - 1) // limit  # Ceiling division

                # Paginate
                start = (page - 1) * limit
                end = start + limit
                files = all_files[start:end]

                screenshots = [{'name': f.name, 'path': f'/api/screenshots/{screenshot_type}/{f.name}'} for f in files]

                return jsonify({
                    'screenshots': screenshots,
                    'total': total,
                    'page': page,
                    'pages': pages,
                    'has_more': page < pages
                })
            except Exception as e:
                logger.error(f"Error listing screenshots: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/screenshots/<subdir>/<filename>')
        def api_screenshot_file(subdir, filename):
            """Serve a screenshot file."""
            try:
                valid_subdirs = ['ads', 'non_ads', 'vlm_spastic', 'static']
                if subdir not in valid_subdirs:
                    return Response(status=404)
                # Sanitize filename
                if '..' in filename or '/' in filename:
                    return Response(status=400)
                screenshots_dir = Path(__file__).parent.parent / 'screenshots' / subdir
                return send_from_directory(screenshots_dir, filename)
            except Exception as e:
                logger.error(f"Error serving screenshot: {e}")
                return Response(status=404)

        # =========================================================================
        # WiFi Management (nmcli)
        # =========================================================================

        @self.app.route('/api/wifi/connections')
        def api_wifi_connections():
            """Get saved WiFi connections with priorities."""
            try:
                result = subprocess.run(
                    ['nmcli', '-t', '-f', 'NAME,UUID,TYPE,DEVICE,AUTOCONNECT,AUTOCONNECT-PRIORITY', 'connection', 'show'],
                    capture_output=True, text=True, timeout=10
                )
                connections = []
                for line in result.stdout.strip().split('\n'):
                    if line:
                        parts = line.split(':')
                        # Filter for wifi connections (802-11-wireless)
                        if len(parts) >= 4 and parts[2] == '802-11-wireless':
                            connections.append({
                                'name': parts[0],
                                'uuid': parts[1],
                                'type': 'wifi',
                                'device': parts[3] if parts[3] else None,
                                'autoconnect': parts[4] == 'yes' if len(parts) > 4 else True,
                                'priority': int(parts[5]) if len(parts) > 5 and parts[5] else 0,
                            })
                return jsonify({'connections': connections})
            except Exception as e:
                logger.error(f"Error getting WiFi connections: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/wifi/scan')
        def api_wifi_scan():
            """Scan for available WiFi networks."""
            try:
                # Trigger a rescan
                subprocess.run(['nmcli', 'device', 'wifi', 'rescan'], capture_output=True, timeout=15)
                time.sleep(2)  # Wait for scan to complete

                result = subprocess.run(
                    ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,BSSID', 'device', 'wifi', 'list'],
                    capture_output=True, text=True, timeout=10
                )
                networks = []
                seen_ssids = set()
                for line in result.stdout.strip().split('\n'):
                    if line:
                        parts = line.split(':')
                        if len(parts) >= 3 and parts[0] and parts[0] not in seen_ssids:
                            seen_ssids.add(parts[0])
                            networks.append({
                                'ssid': parts[0],
                                'signal': int(parts[1]) if parts[1] else 0,
                                'security': parts[2] if len(parts) > 2 else 'Open',
                            })
                # Sort by signal strength
                networks.sort(key=lambda x: x['signal'], reverse=True)
                return jsonify({'networks': networks})
            except Exception as e:
                logger.error(f"Error scanning WiFi: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/wifi/connect', methods=['POST'])
        def api_wifi_connect():
            """Connect to a WiFi network."""
            try:
                data = request.get_json() or {}
                ssid = data.get('ssid')
                password = data.get('password')
                priority = data.get('priority', 0)

                if not ssid:
                    return jsonify({'error': 'SSID is required'}), 400

                # Check if connection already exists
                check = subprocess.run(
                    ['nmcli', 'connection', 'show', ssid],
                    capture_output=True, timeout=5
                )

                if check.returncode == 0:
                    # Connection exists, just activate it
                    result = subprocess.run(
                        ['nmcli', 'connection', 'up', ssid],
                        capture_output=True, text=True, timeout=30
                    )
                else:
                    # Create new connection
                    cmd = ['nmcli', 'device', 'wifi', 'connect', ssid]
                    if password:
                        cmd.extend(['password', password])
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                    # Set priority if specified
                    if result.returncode == 0 and priority:
                        subprocess.run(
                            ['nmcli', 'connection', 'modify', ssid, 'connection.autoconnect-priority', str(priority)],
                            capture_output=True, timeout=5
                        )

                if result.returncode == 0:
                    return jsonify({'success': True, 'message': f'Connected to {ssid}'})
                else:
                    return jsonify({'error': result.stderr or 'Connection failed'}), 500
            except Exception as e:
                logger.error(f"Error connecting to WiFi: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/wifi/delete', methods=['POST'])
        def api_wifi_delete():
            """Delete a saved WiFi connection."""
            try:
                data = request.get_json() or {}
                name = data.get('name')

                if not name:
                    return jsonify({'error': 'Connection name is required'}), 400

                result = subprocess.run(
                    ['nmcli', 'connection', 'delete', name],
                    capture_output=True, text=True, timeout=10
                )

                if result.returncode == 0:
                    return jsonify({'success': True, 'message': f'Deleted {name}'})
                else:
                    return jsonify({'error': result.stderr or 'Delete failed'}), 500
            except Exception as e:
                logger.error(f"Error deleting WiFi connection: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/wifi/priority', methods=['POST'])
        def api_wifi_priority():
            """Update WiFi connection priority."""
            try:
                data = request.get_json() or {}
                name = data.get('name')
                priority = data.get('priority', 0)

                if not name:
                    return jsonify({'error': 'Connection name is required'}), 400

                result = subprocess.run(
                    ['nmcli', 'connection', 'modify', name, 'connection.autoconnect-priority', str(priority)],
                    capture_output=True, text=True, timeout=10
                )

                if result.returncode == 0:
                    return jsonify({'success': True, 'message': f'Updated priority for {name}'})
                else:
                    return jsonify({'error': result.stderr or 'Update failed'}), 500
            except Exception as e:
                logger.error(f"Error updating WiFi priority: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # ADB RSA Key Management
        # =========================================================================

        @self.app.route('/api/adb/keys')
        def api_adb_keys():
            """Get ADB RSA key info."""
            try:
                adbkey_path = Path.home() / '.android' / 'adbkey'
                adbkey_pub_path = Path.home() / '.android' / 'adbkey.pub'

                result = {'exists': False, 'public_key': None, 'fingerprint': None}

                if adbkey_pub_path.exists():
                    result['exists'] = True
                    pub_key = adbkey_pub_path.read_text().strip()
                    result['public_key'] = pub_key[:50] + '...' if len(pub_key) > 50 else pub_key

                    # Generate fingerprint (MD5 of public key)
                    import hashlib
                    fingerprint = hashlib.md5(pub_key.encode()).hexdigest()
                    result['fingerprint'] = ':'.join(fingerprint[i:i+2] for i in range(0, 32, 2))

                return jsonify(result)
            except Exception as e:
                logger.error(f"Error getting ADB keys: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/adb/keys/revoke', methods=['POST'])
        def api_adb_keys_revoke():
            """Revoke (delete) ADB RSA keys."""
            try:
                adbkey_path = Path.home() / '.android' / 'adbkey'
                adbkey_pub_path = Path.home() / '.android' / 'adbkey.pub'

                deleted = []
                if adbkey_path.exists():
                    adbkey_path.unlink()
                    deleted.append('adbkey')
                if adbkey_pub_path.exists():
                    adbkey_pub_path.unlink()
                    deleted.append('adbkey.pub')

                # Disconnect Fire TV if connected
                if hasattr(self.minus, 'fire_tv_setup') and self.minus.fire_tv_setup:
                    controller = self.minus.fire_tv_setup.get_controller()
                    if controller:
                        controller.disconnect()

                if deleted:
                    logger.info(f"[WebUI] Revoked ADB keys: {deleted}")
                    return jsonify({'success': True, 'deleted': deleted, 'message': 'ADB keys revoked. You will need to re-authorize on the TV.'})
                else:
                    return jsonify({'success': True, 'deleted': [], 'message': 'No keys found to revoke.'})
            except Exception as e:
                logger.error(f"Error revoking ADB keys: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # Stats (Ads Blocked, Time Saved)
        # =========================================================================

        @self.app.route('/api/stats')
        def api_stats():
            """Get blocking statistics."""
            try:
                stats = {
                    'ads_blocked_today': 0,
                    'total_blocking_time': 0,
                    'time_saved': 0,
                    'blocking_start_time': None,
                    'current_blocking_duration': 0,
                }

                if self.minus.ad_blocker:
                    stats['ads_blocked_today'] = getattr(self.minus.ad_blocker, '_total_ads_blocked', 0)
                    stats['total_blocking_time'] = getattr(self.minus.ad_blocker, '_total_blocking_time', 0)
                    stats['time_saved'] = getattr(self.minus.ad_blocker, '_total_time_saved', 0)

                    if self.minus.ad_blocker.is_visible:
                        start_time = getattr(self.minus.ad_blocker, '_current_block_start', 0)
                        if start_time:
                            stats['blocking_start_time'] = start_time
                            stats['current_blocking_duration'] = time.time() - start_time

                return jsonify(stats)
            except Exception as e:
                logger.error(f"Error getting stats: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # Audio Mute Status
        # =========================================================================

        @self.app.route('/api/audio/status')
        def api_audio_status():
            """Get audio mute status."""
            try:
                muted = False
                if hasattr(self.minus, 'audio') and self.minus.audio:
                    muted = getattr(self.minus.audio, '_muted', False)
                return jsonify({'muted': muted})
            except Exception as e:
                logger.error(f"Error getting audio status: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # Network Info
        # =========================================================================

        @self.app.route('/api/network')
        def api_network():
            """Get network information (IP addresses)."""
            try:
                result = subprocess.run(
                    ['ip', '-4', '-o', 'addr', 'show'],
                    capture_output=True, text=True, timeout=5
                )
                interfaces = []
                for line in result.stdout.strip().split('\n'):
                    if line:
                        parts = line.split()
                        if len(parts) >= 4:
                            iface = parts[1]
                            # Extract IP from "inet x.x.x.x/xx" format
                            ip_part = parts[3].split('/')[0]
                            if iface != 'lo':  # Skip loopback
                                interfaces.append({'interface': iface, 'ip': ip_part})

                # Get hostname
                hostname = subprocess.run(['hostname'], capture_output=True, text=True, timeout=5).stdout.strip()

                return jsonify({
                    'hostname': hostname,
                    'interfaces': interfaces
                })
            except Exception as e:
                logger.error(f"Error getting network info: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # Clear Detections
        # =========================================================================

        @self.app.route('/api/detections/clear', methods=['POST'])
        def api_detections_clear():
            """Clear detection history."""
            try:
                if hasattr(self.minus, 'detection_history'):
                    self.minus.detection_history.clear()
                    logger.info("[WebUI] Detection history cleared")
                    return jsonify({'success': True, 'message': 'Detection history cleared'})
                return jsonify({'error': 'Detection history not available'}), 500
            except Exception as e:
                logger.error(f"Error clearing detections: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # Service Control
        # =========================================================================

        @self.app.route('/api/service/restart', methods=['POST'])
        def api_service_restart():
            """Schedule service restart."""
            try:
                logger.info("[WebUI] Service restart requested")
                # Schedule restart in background thread to allow response to be sent
                def restart():
                    time.sleep(1)
                    subprocess.run(['systemctl', 'restart', 'minus'], timeout=30)
                threading.Thread(target=restart, daemon=True).start()
                return jsonify({'success': True, 'message': 'Service restart scheduled'})
            except Exception as e:
                logger.error(f"Error restarting service: {e}")
                return jsonify({'error': str(e)}), 500

        # =========================================================================
        # Fire TV App Launch
        # =========================================================================

        @self.app.route('/api/firetv/launch/<app>', methods=['POST'])
        def api_firetv_launch(app):
            """Launch an app on Fire TV."""
            # App package mappings
            apps = {
                'youtube': 'com.amazon.firetv.youtube',
                'netflix': 'com.netflix.ninja',
                'prime': 'com.amazon.avod',
                'hulu': 'com.hulu.plus',
                'disney': 'com.disney.disneyplus',
                'hbomax': 'com.hbo.hbonow',
                'peacock': 'com.peacocktv.peacockandroid',
                'plex': 'com.plexapp.android',
                'kodi': 'org.xbmc.kodi',
                'spotify': 'com.spotify.tv.android',
                'twitch': 'tv.twitch.android.app',
                'home': 'com.amazon.tv.launcher',
            }

            try:
                if app.lower() not in apps:
                    return jsonify({'error': f'Unknown app: {app}. Available: {list(apps.keys())}'}), 400

                package = apps[app.lower()]

                if hasattr(self.minus, 'fire_tv_setup') and self.minus.fire_tv_setup:
                    controller = self.minus.fire_tv_setup.get_controller()
                    if controller and controller.is_connected and hasattr(controller, '_device') and controller._device:
                        # Use monkey to launch the app
                        controller._device.adb_shell(f'monkey -p {package} -c android.intent.category.LAUNCHER 1')
                        logger.info(f"[WebUI] Launched {app} on Fire TV")
                        return jsonify({'success': True, 'app': app, 'package': package})
                    return jsonify({'error': 'Fire TV not connected'}), 503

                return jsonify({'error': 'Fire TV not initialized'}), 500
            except Exception as e:
                logger.error(f"Error launching app: {e}")
                return jsonify({'error': str(e)}), 500

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
