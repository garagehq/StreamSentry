"""
Notification Overlay Module for Minus.

Provides small corner overlays for notifications and guidance
without blocking the main video content.

Uses ustreamer's overlay API to render text directly on the
video stream via the MPP hardware encoder - no GStreamer
pipeline modifications needed.
"""

import logging
import threading
import time
import urllib.request
import urllib.parse
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class NotificationOverlay:
    """
    Manages small corner notification overlays via ustreamer API.

    Shows guidance text in a corner of the screen without blocking
    the main video content. Uses ustreamer's /overlay/set HTTP API
    to render text directly in the MPP encoder.

    Position: Top-right corner by default
    """

    # Overlay positions (matching ustreamer API)
    POSITION_TOP_LEFT = 0
    POSITION_TOP_RIGHT = 1
    POSITION_BOTTOM_LEFT = 2
    POSITION_BOTTOM_RIGHT = 3
    POSITION_CENTER = 4

    # String position names for compatibility
    POS_TOP_RIGHT = 'top-right'
    POS_TOP_LEFT = 'top-left'
    POS_BOTTOM_RIGHT = 'bottom-right'
    POS_BOTTOM_LEFT = 'bottom-left'
    POS_CENTER = 'center'

    # Map string positions to API values
    _POS_MAP = {
        'top-left': 0,
        'top-right': 1,
        'bottom-left': 2,
        'bottom-right': 3,
        'center': 4,
    }

    def __init__(self, ustreamer_port: int = 9090, position: str = POS_TOP_RIGHT):
        """
        Initialize notification overlay.

        Args:
            ustreamer_port: Port where ustreamer is running (default: 9090)
            position: Corner position for overlay
        """
        self.ustreamer_port = ustreamer_port
        self.position = position
        self._api_position = self._POS_MAP.get(position, 1)  # Default top-right

        # Overlay state
        self._visible = False
        self._current_text = None
        self._lock = threading.Lock()

        # Auto-hide timer
        self._auto_hide_timer: Optional[threading.Timer] = None
        self._default_duration = 10.0  # Default auto-hide after 10s

        # Default styling
        self._scale = 3  # Text scale factor
        self._bg_alpha = 200  # Background transparency

        # API endpoint
        self._api_base = f"http://localhost:{ustreamer_port}/overlay/set"

        logger.info(f"[Overlay] Initialized with ustreamer API at port {ustreamer_port}, position={position}")

    def _call_api(self, params: dict) -> bool:
        """
        Call the ustreamer overlay API.

        Args:
            params: Query parameters for the API

        Returns:
            True if successful, False otherwise
        """
        try:
            # Build query string
            query = urllib.parse.urlencode(params)
            url = f"{self._api_base}?{query}"

            # Make request with short timeout
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2.0) as response:
                if response.status == 200:
                    return True
                else:
                    logger.warning(f"[Overlay] API returned status {response.status}")
                    return False

        except urllib.error.URLError as e:
            logger.error(f"[Overlay] API connection error: {e}")
            return False
        except Exception as e:
            logger.error(f"[Overlay] API error: {e}")
            return False

    def show(self, text: str, duration: float = None, background: bool = True):
        """
        Show notification overlay with text.

        Args:
            text: Text to display (supports newlines)
            duration: Auto-hide after this many seconds (None = stay visible)
            background: Show semi-transparent background behind text
        """
        with self._lock:
            # Cancel any pending auto-hide
            if self._auto_hide_timer:
                self._auto_hide_timer.cancel()
                self._auto_hide_timer = None

            self._current_text = text
            self._visible = True

            # Call ustreamer API
            params = {
                'text': text,
                'position': self._api_position,
                'scale': self._scale,
                'enabled': 'true',
            }

            if background:
                params['bg_enabled'] = 'true'
                params['bg_alpha'] = self._bg_alpha
            else:
                params['bg_enabled'] = 'false'

            self._call_api(params)

            # Set auto-hide timer if duration specified
            if duration is not None and duration > 0:
                self._auto_hide_timer = threading.Timer(duration, self.hide)
                self._auto_hide_timer.daemon = True
                self._auto_hide_timer.start()

            logger.debug(f"[Overlay] Showing: {text[:50]}...")

    def hide(self):
        """Hide the notification overlay."""
        with self._lock:
            # Cancel any pending auto-hide
            if self._auto_hide_timer:
                self._auto_hide_timer.cancel()
                self._auto_hide_timer = None

            self._visible = False
            self._current_text = None

            # Clear the overlay via API
            self._call_api({'clear': 'true', 'enabled': 'false'})

            logger.debug("[Overlay] Hidden")

    def update(self, text: str):
        """Update overlay text without resetting auto-hide timer."""
        with self._lock:
            if not self._visible:
                return

            self._current_text = text

            # Update via API
            params = {
                'text': text,
                'position': self._api_position,
                'scale': self._scale,
                'enabled': 'true',
                'bg_enabled': 'true',
                'bg_alpha': self._bg_alpha,
            }
            self._call_api(params)

    def set_position(self, position: str):
        """
        Change overlay position.

        Args:
            position: One of 'top-left', 'top-right', 'bottom-left', 'bottom-right', 'center'
        """
        self.position = position
        self._api_position = self._POS_MAP.get(position, 1)

        # Update position if currently visible
        if self._visible and self._current_text:
            self.show(self._current_text)

    def set_scale(self, scale: int):
        """Set text scale factor (1-10)."""
        self._scale = max(1, min(10, scale))

    def set_background_alpha(self, alpha: int):
        """Set background transparency (0-255, 255=opaque)."""
        self._bg_alpha = max(0, min(255, alpha))

    @property
    def is_visible(self) -> bool:
        """Check if overlay is currently visible."""
        return self._visible

    @property
    def current_text(self) -> Optional[str]:
        """Get current overlay text."""
        return self._current_text

    def destroy(self):
        """Clean up resources."""
        with self._lock:
            if self._auto_hide_timer:
                self._auto_hide_timer.cancel()
                self._auto_hide_timer = None
            self._visible = False
            self._current_text = None

        # Clear overlay on destroy
        try:
            self._call_api({'clear': 'true', 'enabled': 'false'})
        except:
            pass


class FireTVNotification(NotificationOverlay):
    """
    Specialized notification overlay for Fire TV setup guidance.

    Shows compact setup instructions in the top-right corner
    while the user navigates their Fire TV to enable ADB or
    authorize the connection.
    """

    def __init__(self, ustreamer_port: int = 9090):
        super().__init__(ustreamer_port=ustreamer_port, position=NotificationOverlay.POS_TOP_RIGHT)
        # Use slightly smaller scale for Fire TV notifications
        self._scale = 2

    def show_scanning(self):
        """Show 'Scanning for Fire TV...' notification."""
        text = "Scanning for Fire TV..."
        self.show(text, duration=None)

    def show_adb_enable_instructions(self, timeout_remaining: int = None):
        """Show instructions for enabling ADB debugging."""
        lines = [
            "Fire TV Setup",
            "",
            "Enable ADB Debugging:",
            "1. Settings > My Fire TV",
            "2. Developer Options",
            "3. Turn ON ADB Debugging",
        ]

        if timeout_remaining is not None:
            lines.append("")
            lines.append(f"Scanning... ({timeout_remaining}s)")

        text = "\n".join(lines)
        self.show(text, duration=None)

    def show_auth_instructions(self, ip_address: str = None, attempt: int = None, timeout_remaining: int = None):
        """Show instructions for authorizing ADB connection."""
        lines = [
            "Fire TV Found!",
            "",
            "On your TV, press Allow",
            "on the USB Debugging dialog.",
            "",
            "Check: Always allow",
        ]

        if ip_address:
            lines.insert(1, f"IP: {ip_address}")

        if attempt is not None and timeout_remaining is not None:
            lines.append("")
            lines.append(f"Attempt {attempt}... ({timeout_remaining}s)")

        text = "\n".join(lines)
        self.show(text, duration=None)

    def show_connected(self, device_name: str = "Fire TV"):
        """Show connection success notification."""
        text = f"{device_name} Connected!\n\nAd skipping enabled."
        self.show(text, duration=5.0)  # Auto-hide after 5s

    def show_failed(self, reason: str = "Connection failed"):
        """Show connection failure notification."""
        text = f"{reason}\n\nFire TV skipping disabled."
        self.show(text, duration=10.0)  # Auto-hide after 10s

    def show_skipped(self):
        """Show setup skipped notification."""
        text = "Fire TV setup skipped.\n\nManual skip unavailable."
        self.show(text, duration=5.0)
