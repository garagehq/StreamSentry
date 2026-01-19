"""
Notification Overlay Module for Minus.

Provides small corner overlays for notifications and guidance
without blocking the main video content.

Unlike ad_blocker's full-screen blocking overlay, this module
shows small text notifications in a corner of the screen while
the video continues playing.
"""

import logging
import threading
import time
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class NotificationOverlay:
    """
    Manages small corner notification overlays.

    Shows guidance text in a corner of the screen without blocking
    the main video content. Uses GStreamer textoverlay element.

    Position: Top-right corner
    Size: ~380px wide at 1080p, scaled for higher resolutions
    """

    # Overlay positions
    POSITION_TOP_RIGHT = 'top-right'
    POSITION_TOP_LEFT = 'top-left'
    POSITION_BOTTOM_RIGHT = 'bottom-right'
    POSITION_BOTTOM_LEFT = 'bottom-left'

    def __init__(self, ad_blocker=None, position: str = POSITION_TOP_RIGHT):
        """
        Initialize notification overlay.

        Args:
            ad_blocker: DRMAdBlocker instance (has access to GStreamer pipeline)
            position: Corner position for overlay
        """
        self.ad_blocker = ad_blocker
        self.position = position

        # Overlay state
        self._visible = False
        self._current_text = None
        self._lock = threading.Lock()

        # Auto-hide timer
        self._auto_hide_timer: Optional[threading.Timer] = None
        self._default_duration = 10.0  # Default auto-hide after 10s

        # Get output resolution for scaling
        self._output_width = 1920
        self._output_height = 1080
        if ad_blocker:
            self._output_width = getattr(ad_blocker, 'output_width', 1920)
            self._output_height = getattr(ad_blocker, 'output_height', 1080)

        # Calculate overlay size based on resolution
        # Base: 380px at 1080p
        self._base_width = 380
        self._scale_factor = self._output_height / 1080.0
        self._overlay_width = int(self._base_width * self._scale_factor)

        logger.info(f"[Overlay] Initialized at {position}, "
                   f"width={self._overlay_width}px (scale={self._scale_factor:.2f}x)")

    def _get_halign_valign(self) -> tuple:
        """Get GStreamer halign/valign values for position."""
        if self.position == self.POSITION_TOP_RIGHT:
            return ('right', 'top')
        elif self.position == self.POSITION_TOP_LEFT:
            return ('left', 'top')
        elif self.position == self.POSITION_BOTTOM_RIGHT:
            return ('right', 'bottom')
        elif self.position == self.POSITION_BOTTOM_LEFT:
            return ('left', 'bottom')
        return ('right', 'top')  # Default

    def _get_font_size(self) -> int:
        """Get font size scaled for resolution."""
        # Base: 18px at 1080p
        base_size = 18
        return int(base_size * self._scale_factor)

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

            # Update the overlay via ad_blocker's notification textoverlay
            self._update_overlay(text, background)

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

            # Clear the overlay
            self._update_overlay('', False)

            logger.debug("[Overlay] Hidden")

    def update(self, text: str):
        """Update overlay text without resetting auto-hide timer."""
        with self._lock:
            if not self._visible:
                return

            self._current_text = text
            self._update_overlay(text, True)

    def _update_overlay(self, text: str, background: bool):
        """Update the GStreamer textoverlay elements (video and blocking paths)."""
        if not self.ad_blocker:
            logger.warning("[Overlay] No ad_blocker available")
            return

        try:
            text_to_set = text if text else ''

            # Set text on video path notification overlay
            overlay_video = getattr(self.ad_blocker, 'notificationoverlay', None)
            if overlay_video:
                overlay_video.set_property('text', text_to_set)

            # Set text on blocking path notification overlay
            overlay_block = getattr(self.ad_blocker, 'notificationoverlay_block', None)
            if overlay_block:
                overlay_block.set_property('text', text_to_set)

            if overlay_video or overlay_block:
                logger.debug(f"[Overlay] Set notification text: {text[:30] if text else 'cleared'}...")
            else:
                logger.warning("[Overlay] No notification overlay elements found in pipeline")

        except Exception as e:
            logger.error(f"[Overlay] Error updating: {e}")
            import traceback
            traceback.print_exc()

    def _show_via_textoverlay(self, text: str, background: bool):
        """
        Show notification using the pipeline's textoverlay.

        This is a fallback when no dedicated notification overlay exists.
        We position text in the corner using textoverlay properties.
        """
        if not self.ad_blocker:
            return

        # Check if we have a dedicated notification overlay in the pipeline
        # If not, we need to work with what we have

        # For now, we'll use a simple approach: store the notification
        # and let the debug overlay include it
        self.ad_blocker._notification_text = text if text else None

        # Force debug overlay update to show notification
        if hasattr(self.ad_blocker, '_notification_text'):
            logger.debug(f"[Overlay] Set notification text: {text[:30] if text else 'None'}...")

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


class FireTVNotification(NotificationOverlay):
    """
    Specialized notification overlay for Fire TV setup guidance.

    Shows compact setup instructions in the top-right corner
    while the user navigates their Fire TV to enable ADB or
    authorize the connection.
    """

    def __init__(self, ad_blocker=None):
        super().__init__(ad_blocker, position=NotificationOverlay.POSITION_TOP_RIGHT)

    def show_scanning(self):
        """Show 'Scanning for Fire TV...' notification."""
        text = "🔍 Scanning for Fire TV..."
        self.show(text, duration=None)

    def show_adb_enable_instructions(self, timeout_remaining: int = None):
        """Show instructions for enabling ADB debugging."""
        lines = [
            "📺 Fire TV Setup",
            "",
            "Enable ADB Debugging:",
            "1. Settings → My Fire TV",
            "2. Developer Options",
            "3. Turn ON 'ADB Debugging'",
        ]

        if timeout_remaining is not None:
            lines.append(f"")
            lines.append(f"Scanning... ({timeout_remaining}s)")

        text = "\n".join(lines)
        self.show(text, duration=None)

    def show_auth_instructions(self, ip_address: str = None, attempt: int = None, timeout_remaining: int = None):
        """Show instructions for authorizing ADB connection."""
        lines = [
            "📺 Fire TV Found!",
            "",
            "On your TV, press 'Allow'",
            "on the USB Debugging dialog.",
            "",
            "✓ Check 'Always allow'",
        ]

        if ip_address:
            lines.insert(1, f"IP: {ip_address}")

        if attempt is not None and timeout_remaining is not None:
            lines.append(f"")
            lines.append(f"Attempt {attempt}... ({timeout_remaining}s)")

        text = "\n".join(lines)
        self.show(text, duration=None)

    def show_connected(self, device_name: str = "Fire TV"):
        """Show connection success notification."""
        text = f"✓ {device_name} Connected!\n\nAd skipping enabled."
        self.show(text, duration=5.0)  # Auto-hide after 5s

    def show_failed(self, reason: str = "Connection failed"):
        """Show connection failure notification."""
        text = f"✗ {reason}\n\nFire TV skipping disabled."
        self.show(text, duration=10.0)  # Auto-hide after 10s

    def show_skipped(self):
        """Show setup skipped notification."""
        text = "Fire TV setup skipped.\n\nManual skip unavailable."
        self.show(text, duration=5.0)
