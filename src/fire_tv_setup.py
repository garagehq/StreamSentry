"""
Fire TV Setup Manager for Minus.

Handles the Fire TV connection flow with visual guidance on the HDMI output:
1. Scans for Fire TV devices
2. Shows guidance overlay if ADB debugging needs to be enabled
3. Detects ADB authorization dialog via OCR
4. Shows guidance overlay for authorization
5. Auto-retries connection after user approves

Integrates with:
- src/fire_tv.py - Fire TV controller
- src/ad_blocker.py - Display overlay system
- src/ocr.py - Text detection for dialog detection
"""

import logging
import threading
import time
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# Keywords for detecting ADB authorization dialog
ADB_AUTH_KEYWORDS = [
    "allow usb debugging",
    "usb debugging",
    "rsa key fingerprint",
    "always allow from this computer",
    "always allow",
]

# Keywords for detecting ADB debug enable warning
ADB_ENABLE_KEYWORDS = [
    "adb debugging",
    "developer options",
    "usb debugging allows",
]


class FireTVSetupManager:
    """
    Manages Fire TV setup flow with visual guidance.

    States:
    - IDLE: Not doing anything
    - SCANNING: Scanning for Fire TV devices
    - WAITING_ADB_ENABLE: Showing instructions to enable ADB debugging
    - WAITING_AUTH: Waiting for user to authorize connection
    - CONNECTED: Successfully connected
    - SKIPPED: User skipped Fire TV setup
    """

    STATE_IDLE = "idle"
    STATE_SCANNING = "scanning"
    STATE_WAITING_ADB_ENABLE = "waiting_adb_enable"
    STATE_WAITING_AUTH = "waiting_auth"
    STATE_CONNECTED = "connected"
    STATE_SKIPPED = "skipped"

    def __init__(self, ad_blocker=None, ocr_worker=None):
        """
        Initialize Fire TV setup manager.

        Args:
            ad_blocker: DRMAdBlocker instance for showing overlays
            ocr_worker: OCR worker for detecting dialogs (optional)
        """
        self.ad_blocker = ad_blocker
        self.ocr_worker = ocr_worker
        self.controller = None

        self._state = self.STATE_IDLE
        self._lock = threading.Lock()

        # Setup thread
        self._setup_thread: Optional[threading.Thread] = None
        self._stop_setup = threading.Event()

        # Callbacks
        self._on_state_change: Optional[Callable[[str], None]] = None
        self._on_connected: Optional[Callable[[dict], None]] = None

        # Retry settings
        self._scan_interval = 10.0  # Seconds between network scans
        # IMPORTANT: Auth retry must be longer than AUTH_TIMEOUT (30s) in fire_tv.py
        # to avoid triggering multiple auth dialogs on the TV
        self._auth_retry_interval = 35.0  # Seconds between auth retries (longer than auth timeout)
        self._max_scan_time = 300.0  # 5 minutes max waiting for ADB enable
        self._max_auth_time = 180.0  # 3 minutes max waiting for auth (allows ~5 retries)

        # Fire TV info (once connected)
        self._fire_tv_info: Optional[dict] = None

        # Initialize notification overlay (small corner notification)
        # DISABLED: Notification overlay on video path causes pipeline stalls
        # TODO: Re-enable after fixing GStreamer pipeline overlay issue
        self._notification = None
        logger.info("[FireTVSetup] Notification overlay disabled (pipeline fix pending)")

    @property
    def state(self) -> str:
        """Get current state."""
        with self._lock:
            return self._state

    @state.setter
    def state(self, new_state: str):
        """Set state and notify callback."""
        with self._lock:
            if self._state != new_state:
                old_state = self._state
                self._state = new_state
                logger.info(f"[FireTVSetup] State: {old_state} -> {new_state}")

                if self._on_state_change:
                    try:
                        self._on_state_change(new_state)
                    except Exception as e:
                        logger.error(f"[FireTVSetup] State change callback error: {e}")

    def set_callbacks(self, on_state_change: Callable[[str], None] = None,
                      on_connected: Callable[[dict], None] = None):
        """Set callbacks for state changes and connection success."""
        self._on_state_change = on_state_change
        self._on_connected = on_connected

    def start_setup(self, blocking: bool = False) -> bool:
        """
        Start the Fire TV setup flow.

        Args:
            blocking: If True, blocks until setup complete or skipped

        Returns:
            True if setup started, False if already running
        """
        with self._lock:
            if self._setup_thread and self._setup_thread.is_alive():
                logger.warning("[FireTVSetup] Setup already in progress")
                return False

        self._stop_setup.clear()
        self._setup_thread = threading.Thread(
            target=self._setup_flow,
            daemon=True,
            name="FireTVSetup"
        )
        self._setup_thread.start()

        if blocking:
            self._setup_thread.join()

        return True

    def stop_setup(self):
        """Stop the setup flow."""
        self._stop_setup.set()
        if self._setup_thread:
            self._setup_thread.join(timeout=5.0)
            self._setup_thread = None

        # Clear overlay if showing
        self._hide_guidance()
        self.state = self.STATE_IDLE

    def skip_setup(self):
        """Skip Fire TV setup."""
        logger.info("[FireTVSetup] User skipped Fire TV setup")
        self._stop_setup.set()
        self._hide_guidance()
        self.state = self.STATE_SKIPPED

    def is_connected(self) -> bool:
        """Check if Fire TV is connected."""
        return self.state == self.STATE_CONNECTED and self.controller and self.controller.is_connected()

    def get_controller(self):
        """Get the Fire TV controller (if connected)."""
        if self.is_connected():
            return self.controller
        return None

    def _setup_flow(self):
        """Main setup flow thread."""
        try:
            from src.fire_tv import FireTVController

            logger.info("[FireTVSetup] Starting Fire TV setup flow...")
            self.controller = FireTVController()

            # Phase 1: Scan for devices
            self.state = self.STATE_SCANNING
            devices = self._scan_for_devices()

            if self._stop_setup.is_set():
                return

            if not devices:
                # No devices found - show ADB enable instructions
                self.state = self.STATE_WAITING_ADB_ENABLE
                devices = self._wait_for_adb_enable()

                if self._stop_setup.is_set():
                    return

                if not devices:
                    logger.warning("[FireTVSetup] Timeout waiting for ADB enable")
                    self._hide_guidance()
                    self.state = self.STATE_SKIPPED
                    return

            # Phase 2: Connect and authorize
            self.state = self.STATE_WAITING_AUTH
            success = self._connect_with_retry(devices[0]["ip"])

            if self._stop_setup.is_set():
                return

            if success:
                self._hide_guidance()
                self.state = self.STATE_CONNECTED
                self._show_success_message()

                if self._on_connected and self._fire_tv_info:
                    self._on_connected(self._fire_tv_info)
            else:
                logger.warning("[FireTVSetup] Failed to connect to Fire TV")
                self._hide_guidance()
                self.state = self.STATE_SKIPPED

        except Exception as e:
            logger.error(f"[FireTVSetup] Setup error: {e}")
            import traceback
            traceback.print_exc()
            self._hide_guidance()
            self.state = self.STATE_IDLE

    def _scan_for_devices(self) -> list:
        """Scan network for devices with ADB port open."""
        logger.info("[FireTVSetup] Scanning network for Fire TV devices...")

        from src.fire_tv import FireTVController
        devices = FireTVController.discover_devices(timeout=10.0)

        if devices:
            logger.info(f"[FireTVSetup] Found {len(devices)} device(s) with ADB enabled")
        else:
            logger.info("[FireTVSetup] No devices with ADB enabled found")

        return devices

    def _wait_for_adb_enable(self) -> list:
        """
        Show guidance for enabling ADB and wait for device to appear.

        Returns:
            List of devices if found, empty list if timeout
        """
        logger.info("[FireTVSetup] Showing ADB enable instructions...")
        self._show_adb_enable_guidance()

        start_time = time.time()

        while not self._stop_setup.is_set():
            # Check timeout
            if time.time() - start_time > self._max_scan_time:
                return []

            # Wait before scanning again
            self._stop_setup.wait(self._scan_interval)
            if self._stop_setup.is_set():
                return []

            # Scan for devices
            devices = self._scan_for_devices()
            if devices:
                logger.info("[FireTVSetup] Device found after ADB enable!")
                return devices

            # Update countdown on overlay
            remaining = int(self._max_scan_time - (time.time() - start_time))
            self._update_adb_enable_countdown(remaining)

        return []

    def _connect_with_retry(self, ip_address: str) -> bool:
        """
        Try to connect to Fire TV with retries for authorization.

        Returns:
            True if connected, False if failed
        """
        logger.info(f"[FireTVSetup] Connecting to {ip_address}...")
        self._show_auth_guidance(ip_address)

        start_time = time.time()
        attempt = 0

        while not self._stop_setup.is_set():
            attempt += 1

            # Check timeout
            if time.time() - start_time > self._max_auth_time:
                logger.warning("[FireTVSetup] Authorization timeout")
                return False

            logger.info(f"[FireTVSetup] Connection attempt {attempt}...")

            # Try to connect
            if self.controller.connect(ip_address, wait_for_auth=True):
                # Get device info
                info = self.controller.get_device_info()
                if info:
                    self._fire_tv_info = info
                    logger.info(f"[FireTVSetup] Connected to {info.get('manufacturer')} {info.get('model')}")
                else:
                    self._fire_tv_info = {"ip": ip_address}
                    logger.info(f"[FireTVSetup] Connected to {ip_address}")

                return True

            # Update overlay with retry message
            remaining = int(self._max_auth_time - (time.time() - start_time))
            self._update_auth_countdown(remaining, attempt)

            # Wait before retrying
            self._stop_setup.wait(self._auth_retry_interval)

        return False

    def _show_adb_enable_guidance(self):
        """Show small corner notification with ADB enable instructions."""
        if self._notification:
            self._notification.show_adb_enable_instructions()

    def _update_adb_enable_countdown(self, seconds_remaining: int):
        """Update the ADB enable notification with countdown."""
        if self._notification:
            self._notification.show_adb_enable_instructions(timeout_remaining=seconds_remaining)

    def _show_auth_guidance(self, ip_address: str):
        """Show small corner notification with authorization instructions."""
        if self._notification:
            self._notification.show_auth_instructions(ip_address=ip_address)

    def _update_auth_countdown(self, seconds_remaining: int, attempt: int):
        """Update the auth notification with countdown."""
        if self._notification:
            self._notification.show_auth_instructions(
                attempt=attempt,
                timeout_remaining=seconds_remaining
            )

    def _show_success_message(self):
        """Show success notification (auto-hides)."""
        if self._notification:
            manufacturer = self._fire_tv_info.get("manufacturer", "Fire TV") if self._fire_tv_info else "Fire TV"
            model = self._fire_tv_info.get("model", "") if self._fire_tv_info else ""
            device_name = f"{manufacturer} {model}".strip()
            self._notification.show_connected(device_name=device_name)

    def _hide_guidance(self):
        """Hide the notification overlay."""
        if self._notification:
            self._notification.hide()

    def check_for_auth_dialog(self, ocr_results: list) -> bool:
        """
        Check OCR results for ADB authorization dialog.

        Args:
            ocr_results: List of OCR result dicts with 'text' keys

        Returns:
            True if auth dialog detected
        """
        if not ocr_results:
            return False

        # Combine all text
        all_text = " ".join(r.get('text', '') for r in ocr_results).lower()

        # Check for auth dialog keywords
        matches = sum(1 for kw in ADB_AUTH_KEYWORDS if kw in all_text)

        # Need at least 2 keyword matches to be confident
        return matches >= 2

    def destroy(self):
        """Clean up resources."""
        self.stop_setup()
        if self._notification:
            self._notification.destroy()
            self._notification = None
        if self.controller:
            self.controller.destroy()
            self.controller = None
