#!/usr/bin/env python3
"""
Comprehensive test suite for Minus.

Run with: python3 -m pytest tests/test_modules.py -v
Or:       python3 tests/test_modules.py
"""

import sys
import os
import tempfile
import shutil
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock
from dataclasses import dataclass

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

# Try to import numpy, skip image tests if not available
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    np = None


# ============================================================================
# Vocabulary Tests
# ============================================================================

class TestVocabulary:
    """Tests for vocabulary.py"""

    def test_vocabulary_imports(self):
        """Test that vocabulary module imports correctly."""
        from vocabulary import SPANISH_VOCABULARY
        assert SPANISH_VOCABULARY is not None

    def test_vocabulary_not_empty(self):
        """Test that vocabulary list is not empty."""
        from vocabulary import SPANISH_VOCABULARY
        assert len(SPANISH_VOCABULARY) > 0

    def test_vocabulary_has_expected_count(self):
        """Test that vocabulary has expected number of entries (500+)."""
        from vocabulary import SPANISH_VOCABULARY
        assert len(SPANISH_VOCABULARY) >= 500

    def test_vocabulary_tuple_format(self):
        """Test that each vocabulary entry is a 4-tuple."""
        from vocabulary import SPANISH_VOCABULARY
        for entry in SPANISH_VOCABULARY:
            assert isinstance(entry, tuple), f"Entry is not a tuple: {entry}"
            assert len(entry) == 4, f"Entry doesn't have 4 elements: {entry}"

    def test_vocabulary_tuple_contents(self):
        """Test that each tuple contains strings."""
        from vocabulary import SPANISH_VOCABULARY
        for spanish, pronunciation, english, example in SPANISH_VOCABULARY[:10]:
            assert isinstance(spanish, str), f"Spanish word is not string: {spanish}"
            assert isinstance(pronunciation, str), f"Pronunciation is not string: {pronunciation}"
            assert isinstance(english, str), f"English is not string: {english}"
            assert isinstance(example, str), f"Example is not string: {example}"
            assert len(spanish) > 0, "Spanish word is empty"
            assert len(english) > 0, "English translation is empty"

    def test_vocabulary_has_common_words(self):
        """Test that vocabulary contains expected common words."""
        from vocabulary import SPANISH_VOCABULARY
        spanish_words = [entry[0] for entry in SPANISH_VOCABULARY]
        assert "hablar" in spanish_words, "Missing 'hablar'"
        assert "comer" in spanish_words, "Missing 'comer'"
        assert "hola" in spanish_words, "Missing 'hola'"


# ============================================================================
# Config Tests
# ============================================================================

class TestConfig:
    """Tests for config.py"""

    def test_config_imports(self):
        """Test that config module imports correctly."""
        from config import MinusConfig
        assert MinusConfig is not None

    def test_config_defaults(self):
        """Test that MinusConfig has expected defaults."""
        from config import MinusConfig
        config = MinusConfig()
        assert config.device == "/dev/video0"
        assert config.screenshot_dir == "screenshots"
        assert config.ocr_timeout == 1.5
        assert config.ustreamer_port == 9090
        assert config.max_screenshots == 50
        assert config.webui_port == 8080

    def test_config_custom_values(self):
        """Test that MinusConfig accepts custom values."""
        from config import MinusConfig
        config = MinusConfig(
            device="/dev/video1",
            screenshot_dir="/tmp/screenshots",
            ocr_timeout=2.0,
            max_screenshots=100
        )
        assert config.device == "/dev/video1"
        assert config.screenshot_dir == "/tmp/screenshots"
        assert config.ocr_timeout == 2.0
        assert config.max_screenshots == 100


# ============================================================================
# Skip Detection Tests
# ============================================================================

class TestSkipDetection:
    """Tests for skip_detection.py"""

    def test_skip_detection_imports(self):
        """Test that skip_detection module imports correctly."""
        from skip_detection import check_skip_opportunity
        assert check_skip_opportunity is not None

    def test_skip_button_detected(self):
        """Test detection of 'Skip' button (skippable)."""
        from skip_detection import check_skip_opportunity
        is_skippable, text, countdown = check_skip_opportunity(["Skip"])
        assert is_skippable is True
        assert countdown == 0

    def test_skip_ad_button_detected(self):
        """Test detection of 'Skip Ad' button (skippable)."""
        from skip_detection import check_skip_opportunity
        is_skippable, text, countdown = check_skip_opportunity(["Skip Ad"])
        assert is_skippable is True
        assert countdown == 0

    def test_skip_ads_button_detected(self):
        """Test detection of 'Skip Ads' button (skippable)."""
        from skip_detection import check_skip_opportunity
        is_skippable, text, countdown = check_skip_opportunity(["Skip Ads"])
        assert is_skippable is True
        assert countdown == 0

    def test_skip_countdown_detected(self):
        """Test detection of skip with countdown (not skippable)."""
        from skip_detection import check_skip_opportunity
        is_skippable, text, countdown = check_skip_opportunity(["Skip 5"])
        assert is_skippable is False
        assert countdown == 5

    def test_skip_ad_in_countdown_detected(self):
        """Test detection of 'Skip Ad in 5' (not skippable)."""
        from skip_detection import check_skip_opportunity
        is_skippable, text, countdown = check_skip_opportunity(["Skip Ad in 5"])
        assert is_skippable is False
        assert countdown == 5

    def test_skip_in_countdown_with_s(self):
        """Test detection of 'Skip in 10s' (not skippable)."""
        from skip_detection import check_skip_opportunity
        is_skippable, text, countdown = check_skip_opportunity(["Skip in 10s"])
        assert is_skippable is False
        assert countdown == 10

    def test_no_skip_button(self):
        """Test no skip button detected."""
        from skip_detection import check_skip_opportunity
        is_skippable, text, countdown = check_skip_opportunity(["Hello World", "Some text"])
        assert is_skippable is False
        assert text is None
        assert countdown is None

    def test_empty_text_list(self):
        """Test empty text list."""
        from skip_detection import check_skip_opportunity
        is_skippable, text, countdown = check_skip_opportunity([])
        assert is_skippable is False
        assert text is None

    def test_case_insensitive(self):
        """Test case insensitivity."""
        from skip_detection import check_skip_opportunity
        is_skippable, _, _ = check_skip_opportunity(["SKIP AD"])
        assert is_skippable is True

        is_skippable, _, _ = check_skip_opportunity(["skip ad"])
        assert is_skippable is True

    def test_false_positive_skip_this_step(self):
        """Test that 'Skip this step' is NOT detected as skippable."""
        from skip_detection import check_skip_opportunity
        is_skippable, _, _ = check_skip_opportunity(["Skip this step"])
        assert is_skippable is False


# ============================================================================
# Screenshots Tests
# ============================================================================

class TestScreenshots:
    """Tests for screenshots.py"""

    def setup_method(self):
        """Set up test fixtures."""
        self.test_dir = tempfile.mkdtemp()
        self.screenshot_dir = Path(self.test_dir) / "ocr"
        self.non_ad_dir = Path(self.test_dir) / "non_ad"

    def teardown_method(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_screenshots_imports(self):
        """Test that screenshots module imports correctly."""
        from screenshots import ScreenshotManager
        assert ScreenshotManager is not None

    def test_screenshot_manager_init(self):
        """Test ScreenshotManager initialization."""
        from screenshots import ScreenshotManager
        manager = ScreenshotManager(
            screenshot_dir=self.screenshot_dir,
            non_ad_dir=self.non_ad_dir,
            max_screenshots=10
        )
        assert manager.screenshot_dir == self.screenshot_dir
        assert manager.non_ad_dir == self.non_ad_dir
        assert manager.max_screenshots == 10
        assert self.screenshot_dir.exists()
        assert self.non_ad_dir.exists()

    def test_compute_image_hash(self):
        """Test image hash computation."""
        if not HAS_NUMPY:
            return  # Skip if numpy not available

        from screenshots import ScreenshotManager
        manager = ScreenshotManager(
            screenshot_dir=self.screenshot_dir,
            non_ad_dir=self.non_ad_dir
        )

        # Create a test image
        test_image = np.zeros((100, 100, 3), dtype=np.uint8)
        hash1 = manager.compute_image_hash(test_image)
        assert hash1 is not None

        # Same image should have same hash
        hash2 = manager.compute_image_hash(test_image)
        assert hash1 == hash2

        # Different image should have different hash
        different_image = np.ones((100, 100, 3), dtype=np.uint8) * 255
        hash3 = manager.compute_image_hash(different_image)
        assert hash1 != hash3

    def test_save_ad_screenshot(self):
        """Test saving ad screenshot."""
        if not HAS_NUMPY:
            return

        from screenshots import ScreenshotManager
        manager = ScreenshotManager(
            screenshot_dir=self.screenshot_dir,
            non_ad_dir=self.non_ad_dir
        )

        test_image = np.zeros((100, 100, 3), dtype=np.uint8)
        manager.save_ad_screenshot(test_image, [("skip", "skip ad")], ["skip ad", "some text"])

        screenshots = list(self.screenshot_dir.glob("ad_*.png"))
        assert len(screenshots) == 1

    def test_save_ad_screenshot_deduplication(self):
        """Test that duplicate images are not saved."""
        if not HAS_NUMPY:
            return

        from screenshots import ScreenshotManager
        manager = ScreenshotManager(
            screenshot_dir=self.screenshot_dir,
            non_ad_dir=self.non_ad_dir
        )

        test_image = np.zeros((100, 100, 3), dtype=np.uint8)
        manager.save_ad_screenshot(test_image, [("skip", "skip")], ["skip"])
        manager.save_ad_screenshot(test_image, [("skip", "skip")], ["skip"])

        screenshots = list(self.screenshot_dir.glob("ad_*.png"))
        assert len(screenshots) == 1  # Only one saved due to deduplication

    def test_save_non_ad_screenshot(self):
        """Test saving non-ad screenshot."""
        if not HAS_NUMPY:
            return

        from screenshots import ScreenshotManager
        manager = ScreenshotManager(
            screenshot_dir=self.screenshot_dir,
            non_ad_dir=self.non_ad_dir
        )

        test_image = np.zeros((100, 100, 3), dtype=np.uint8)
        manager.save_non_ad_screenshot(test_image)

        screenshots = list(self.non_ad_dir.glob("non_ad_*.png"))
        assert len(screenshots) == 1

    def test_save_static_ad_screenshot(self):
        """Test saving static ad screenshot."""
        if not HAS_NUMPY:
            return

        from screenshots import ScreenshotManager
        manager = ScreenshotManager(
            screenshot_dir=self.screenshot_dir,
            non_ad_dir=self.non_ad_dir
        )

        test_image = np.zeros((100, 100, 3), dtype=np.uint8)
        manager.save_static_ad_screenshot(test_image)

        screenshots = list(self.non_ad_dir.glob("static_ad_*.png"))
        assert len(screenshots) == 1

    def test_save_vlm_spastic_screenshot(self):
        """Test saving VLM spastic screenshot."""
        if not HAS_NUMPY:
            return

        from screenshots import ScreenshotManager
        manager = ScreenshotManager(
            screenshot_dir=self.screenshot_dir,
            non_ad_dir=self.non_ad_dir
        )

        test_image = np.zeros((100, 100, 3), dtype=np.uint8)
        manager.save_vlm_spastic_screenshot(test_image, 3)

        screenshots = list(self.non_ad_dir.glob("vlm_spastic_3x_*.png"))
        assert len(screenshots) == 1

    def test_truncate_screenshots(self):
        """Test screenshot truncation when exceeding max."""
        if not HAS_NUMPY:
            return

        from screenshots import ScreenshotManager
        manager = ScreenshotManager(
            screenshot_dir=self.screenshot_dir,
            non_ad_dir=self.non_ad_dir,
            max_screenshots=3
        )

        # Save more than max screenshots
        for i in range(5):
            test_image = np.ones((100, 100, 3), dtype=np.uint8) * (i * 50)
            manager.save_ad_screenshot(test_image, [("skip", f"skip{i}")], [f"skip{i}"])
            time.sleep(0.01)  # Ensure different timestamps

        screenshots = list(self.screenshot_dir.glob("ad_*.png"))
        assert len(screenshots) == 3  # Truncated to max


# ============================================================================
# Console Tests
# ============================================================================

class TestConsole:
    """Tests for console.py"""

    def test_console_imports(self):
        """Test that console module imports correctly."""
        from console import blank_console, restore_console
        assert blank_console is not None
        assert restore_console is not None

    @patch('console.subprocess.run')
    @patch('console.os.system')
    def test_blank_console_calls_expected_commands(self, mock_system, mock_run):
        """Test that blank_console calls expected system commands."""
        from console import blank_console
        blank_console()
        # Should call os.system('clear')
        mock_system.assert_called()

    @patch('console.subprocess.run')
    def test_restore_console_calls_expected_commands(self, mock_run):
        """Test that restore_console calls expected system commands."""
        from console import restore_console
        restore_console()
        # Should have called subprocess.run at least once
        assert mock_run.called


# ============================================================================
# Capture Tests
# ============================================================================

class TestCapture:
    """Tests for capture.py"""

    def test_capture_imports(self):
        """Test that capture module imports correctly."""
        from capture import UstreamerCapture
        assert UstreamerCapture is not None

    def test_ustreamer_capture_init(self):
        """Test UstreamerCapture initialization."""
        from capture import UstreamerCapture
        capture = UstreamerCapture(port=9090)
        assert capture.port == 9090
        assert "9090" in capture.snapshot_url
        assert "/snapshot/raw" in capture.snapshot_url

    def test_ustreamer_capture_custom_port(self):
        """Test UstreamerCapture with custom port."""
        from capture import UstreamerCapture
        capture = UstreamerCapture(port=8888)
        assert capture.port == 8888
        assert "8888" in capture.snapshot_url

    def test_cleanup(self):
        """Test cleanup removes temp file."""
        from capture import UstreamerCapture
        capture = UstreamerCapture()
        # Create the temp file
        Path(capture.screenshot_path).touch()
        assert Path(capture.screenshot_path).exists()
        capture.cleanup()
        assert not Path(capture.screenshot_path).exists()


# ============================================================================
# DRM Tests
# ============================================================================

class TestDRM:
    """Tests for drm.py"""

    def test_drm_imports(self):
        """Test that drm module imports correctly."""
        from drm import probe_drm_output
        assert probe_drm_output is not None

    @patch('drm.subprocess.run')
    def test_probe_drm_output_returns_dict(self, mock_run):
        """Test that probe_drm_output returns expected dict structure."""
        from drm import probe_drm_output

        # Mock modetest output
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr=""
        )

        result = probe_drm_output()

        assert isinstance(result, dict)
        assert 'connector_id' in result
        assert 'connector_name' in result
        assert 'width' in result
        assert 'height' in result
        assert 'plane_id' in result
        assert 'audio_device' in result

    def test_probe_drm_output_fallback_values(self):
        """Test that probe_drm_output returns fallback values on failure."""
        from drm import probe_drm_output

        with patch('drm.subprocess.run') as mock_run:
            mock_run.side_effect = Exception("modetest not found")
            result = probe_drm_output()

        # Should return fallback values
        assert result['width'] == 1920
        assert result['height'] == 1080
        assert result['plane_id'] == 72


# ============================================================================
# V4L2 Tests
# ============================================================================

class TestV4L2:
    """Tests for v4l2.py"""

    def test_v4l2_imports(self):
        """Test that v4l2 module imports correctly."""
        from v4l2 import probe_v4l2_device
        assert probe_v4l2_device is not None

    @patch('v4l2.subprocess.run')
    def test_probe_v4l2_device_parses_output(self, mock_run):
        """Test that probe_v4l2_device parses v4l2-ctl output."""
        from v4l2 import probe_v4l2_device

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="""Format Video Capture:
	Width/Height      : 3840/2160
	Pixel Format      : 'NV12' (Y/UV 4:2:0)
""",
            stderr=""
        )

        result = probe_v4l2_device("/dev/video0")

        assert result['width'] == 3840
        assert result['height'] == 2160
        assert result['format'] == 'NV12'
        assert result['ustreamer_format'] == 'NV12'

    @patch('v4l2.subprocess.run')
    def test_probe_v4l2_device_bgr_format(self, mock_run):
        """Test that probe_v4l2_device handles BGR3 format."""
        from v4l2 import probe_v4l2_device

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="""Format Video Capture:
	Width/Height      : 1920/1080
	Pixel Format      : 'BGR3' (24-bit BGR 8-8-8)
""",
            stderr=""
        )

        result = probe_v4l2_device("/dev/video0")

        assert result['width'] == 1920
        assert result['height'] == 1080
        assert result['format'] == 'BGR3'
        assert result['ustreamer_format'] == 'BGR24'

    def test_probe_v4l2_device_failure(self):
        """Test that probe_v4l2_device handles failure gracefully."""
        from v4l2 import probe_v4l2_device

        with patch('v4l2.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = probe_v4l2_device("/dev/video99")

        assert result['width'] == 0
        assert result['height'] == 0
        assert result['format'] is None


# ============================================================================
# Overlay Tests
# ============================================================================

class TestOverlay:
    """Tests for overlay.py"""

    def test_overlay_imports(self):
        """Test that overlay module imports correctly."""
        from overlay import NotificationOverlay, FireTVNotification, SystemNotification
        assert NotificationOverlay is not None
        assert FireTVNotification is not None
        assert SystemNotification is not None

    def test_notification_overlay_init(self):
        """Test NotificationOverlay initialization."""
        from overlay import NotificationOverlay
        overlay = NotificationOverlay(ustreamer_port=9090)
        assert overlay.ustreamer_port == 9090
        assert overlay.position == 'top-right'
        assert overlay._visible is False

    def test_notification_overlay_positions(self):
        """Test overlay position constants."""
        from overlay import NotificationOverlay
        assert NotificationOverlay.POSITION_TOP_LEFT == 0
        assert NotificationOverlay.POSITION_TOP_RIGHT == 1
        assert NotificationOverlay.POSITION_BOTTOM_LEFT == 2
        assert NotificationOverlay.POSITION_BOTTOM_RIGHT == 3
        assert NotificationOverlay.POSITION_CENTER == 4

    def test_notification_overlay_set_position(self):
        """Test changing overlay position."""
        from overlay import NotificationOverlay
        overlay = NotificationOverlay()
        overlay.set_position('bottom-left')
        assert overlay.position == 'bottom-left'
        assert overlay._api_position == 2

    def test_notification_overlay_set_scale(self):
        """Test setting text scale."""
        from overlay import NotificationOverlay
        overlay = NotificationOverlay()
        overlay.set_scale(5)
        assert overlay._scale == 5
        # Test clamping
        overlay.set_scale(15)
        assert overlay._scale == 10
        overlay.set_scale(-1)
        assert overlay._scale == 1

    def test_notification_overlay_set_background_alpha(self):
        """Test setting background alpha."""
        from overlay import NotificationOverlay
        overlay = NotificationOverlay()
        overlay.set_background_alpha(128)
        assert overlay._bg_alpha == 128
        # Test clamping
        overlay.set_background_alpha(300)
        assert overlay._bg_alpha == 255
        overlay.set_background_alpha(-10)
        assert overlay._bg_alpha == 0

    @patch('overlay.urllib.request.urlopen')
    def test_notification_overlay_show(self, mock_urlopen):
        """Test showing overlay."""
        from overlay import NotificationOverlay
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        overlay = NotificationOverlay()
        overlay.show("Test message")

        assert overlay._visible is True
        assert overlay._current_text == "Test message"
        mock_urlopen.assert_called()

    @patch('overlay.urllib.request.urlopen')
    def test_notification_overlay_hide(self, mock_urlopen):
        """Test hiding overlay."""
        from overlay import NotificationOverlay
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        overlay = NotificationOverlay()
        overlay._visible = True
        overlay._current_text = "Test"
        overlay.hide()

        assert overlay._visible is False
        assert overlay._current_text is None

    def test_fire_tv_notification_init(self):
        """Test FireTVNotification initialization."""
        from overlay import FireTVNotification
        overlay = FireTVNotification(ustreamer_port=9090)
        assert overlay.ustreamer_port == 9090
        assert overlay._scale == 3  # Smaller scale for Fire TV notifications

    def test_system_notification_init(self):
        """Test SystemNotification initialization."""
        from overlay import SystemNotification
        overlay = SystemNotification(ustreamer_port=9090)
        assert overlay.ustreamer_port == 9090
        assert overlay._scale == 3


# ============================================================================
# Health Monitor Tests
# ============================================================================

class TestHealth:
    """Tests for health.py"""

    def test_health_imports(self):
        """Test that health module imports correctly."""
        from health import HealthMonitor, HealthStatus
        assert HealthMonitor is not None
        assert HealthStatus is not None

    def test_health_status_dataclass(self):
        """Test HealthStatus dataclass defaults."""
        from health import HealthStatus
        status = HealthStatus()
        assert status.hdmi_signal is False
        assert status.hdmi_resolution == ""
        assert status.ustreamer_alive is False
        assert status.memory_percent == 0
        assert status.disk_free_mb == 0
        assert status.output_fps == 0.0

    def test_health_status_custom_values(self):
        """Test HealthStatus with custom values."""
        from health import HealthStatus
        status = HealthStatus(
            hdmi_signal=True,
            hdmi_resolution="1920x1080",
            memory_percent=45.5,
            output_fps=30.0
        )
        assert status.hdmi_signal is True
        assert status.hdmi_resolution == "1920x1080"
        assert status.memory_percent == 45.5
        assert status.output_fps == 30.0

    def test_health_monitor_init(self):
        """Test HealthMonitor initialization."""
        from health import HealthMonitor
        mock_minus = MagicMock()
        monitor = HealthMonitor(mock_minus, check_interval=10.0)
        assert monitor.minus == mock_minus
        assert monitor.check_interval == 10.0

    def test_health_monitor_thresholds(self):
        """Test HealthMonitor default thresholds."""
        from health import HealthMonitor
        mock_minus = MagicMock()
        monitor = HealthMonitor(mock_minus)
        assert monitor.memory_warning_percent == 80
        assert monitor.memory_critical_percent == 90
        assert monitor.disk_warning_mb == 500
        assert monitor.startup_grace_period == 30.0

    def test_health_monitor_callbacks(self):
        """Test setting recovery callbacks."""
        from health import HealthMonitor
        mock_minus = MagicMock()
        monitor = HealthMonitor(mock_minus)

        callback = MagicMock()
        monitor.on_hdmi_lost(callback)
        assert monitor._on_hdmi_lost == callback

        monitor.on_hdmi_restored(callback)
        assert monitor._on_hdmi_restored == callback

    @patch('health.subprocess.run')
    def test_check_hdmi_signal_present(self, mock_run):
        """Test HDMI signal detection when present."""
        from health import HealthMonitor
        mock_minus = MagicMock()
        mock_minus.ad_blocker = None
        mock_minus.audio = None
        mock_minus.vlm = None
        mock_minus.ocr = None

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Active width: 1920\nActive height: 1080\n"
        )

        monitor = HealthMonitor(mock_minus)
        signal, resolution = monitor._check_hdmi_signal()

        assert signal is True
        assert resolution == "1920x1080"

    @patch('health.subprocess.run')
    def test_check_hdmi_signal_absent(self, mock_run):
        """Test HDMI signal detection when absent."""
        from health import HealthMonitor
        mock_minus = MagicMock()
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        monitor = HealthMonitor(mock_minus)
        signal, resolution = monitor._check_hdmi_signal()

        assert signal is False
        assert resolution == ""


# ============================================================================
# Fire TV Controller Tests
# ============================================================================

class TestFireTV:
    """Tests for fire_tv.py"""

    def test_fire_tv_imports(self):
        """Test that fire_tv module imports correctly."""
        from fire_tv import FireTVController, KEY_CODES, quick_connect
        assert FireTVController is not None
        assert KEY_CODES is not None
        assert quick_connect is not None

    def test_key_codes_exist(self):
        """Test that expected key codes are defined."""
        from fire_tv import KEY_CODES
        assert "up" in KEY_CODES
        assert "down" in KEY_CODES
        assert "left" in KEY_CODES
        assert "right" in KEY_CODES
        assert "select" in KEY_CODES
        assert "back" in KEY_CODES
        assert "home" in KEY_CODES
        assert "play" in KEY_CODES
        assert "pause" in KEY_CODES

    def test_key_codes_format(self):
        """Test that key codes have proper Android format."""
        from fire_tv import KEY_CODES
        for name, code in KEY_CODES.items():
            assert code.startswith("KEYCODE_"), f"{name} has invalid code: {code}"

    def test_is_fire_tv_device_amazon(self):
        """Test Fire TV detection for Amazon manufacturer."""
        from fire_tv import FireTVController
        assert FireTVController._is_fire_tv_device("Amazon", "AFTMM") is True
        assert FireTVController._is_fire_tv_device("amazon", "Something") is True
        assert FireTVController._is_fire_tv_device("AMAZON", "Fire TV") is True

    def test_is_fire_tv_device_model_patterns(self):
        """Test Fire TV detection by model patterns."""
        from fire_tv import FireTVController
        assert FireTVController._is_fire_tv_device("", "AFTMM") is True
        assert FireTVController._is_fire_tv_device("", "Fire TV Cube") is True
        assert FireTVController._is_fire_tv_device("Unknown", "AFTT") is True

    def test_is_fire_tv_device_negative(self):
        """Test that non-Fire TV devices are not detected."""
        from fire_tv import FireTVController
        assert FireTVController._is_fire_tv_device("Samsung", "Smart TV") is False
        assert FireTVController._is_fire_tv_device("Google", "Chromecast") is False
        assert FireTVController._is_fire_tv_device("Roku", "Ultra") is False

    def test_fire_tv_controller_init(self):
        """Test FireTVController initialization."""
        from fire_tv import FireTVController
        controller = FireTVController()
        assert controller._connected is False
        assert controller._ip_address is None
        assert controller._auto_reconnect is True

    def test_fire_tv_controller_get_status(self):
        """Test getting controller status."""
        from fire_tv import FireTVController
        controller = FireTVController()
        status = controller.get_status()
        assert "connected" in status
        assert "ip_address" in status
        assert "auto_reconnect" in status
        assert status["connected"] is False

    def test_fire_tv_send_command_unknown(self):
        """Test sending unknown command fails gracefully."""
        from fire_tv import FireTVController
        controller = FireTVController()
        result = controller.send_command("unknown_command")
        assert result is False

    def test_fire_tv_send_command_not_connected(self):
        """Test sending command when not connected."""
        from fire_tv import FireTVController
        controller = FireTVController()
        result = controller.send_command("select")
        assert result is False


# ============================================================================
# VLM Tests
# ============================================================================

class TestVLM:
    """Tests for vlm.py"""

    def test_vlm_imports(self):
        """Test that vlm module imports correctly."""
        from vlm import VLMManager, QWEN3_MODEL_DIR
        assert VLMManager is not None
        assert QWEN3_MODEL_DIR is not None

    def test_vlm_manager_init(self):
        """Test VLMManager initialization."""
        from vlm import VLMManager
        manager = VLMManager()
        assert manager.is_ready is False
        assert manager.process is None

    def test_vlm_ad_prompt(self):
        """Test VLM ad detection prompt."""
        from vlm import VLMManager
        assert "advertisement" in VLMManager.AD_PROMPT.lower()
        assert "yes" in VLMManager.AD_PROMPT.lower() or "no" in VLMManager.AD_PROMPT.lower()

    def test_vlm_is_ad_response_yes(self):
        """Test parsing 'yes' responses."""
        from vlm import VLMManager
        manager = VLMManager()
        assert manager._is_ad_response("Yes") is True
        assert manager._is_ad_response("yes") is True
        assert manager._is_ad_response("Yes, this is an ad") is True
        assert manager._is_ad_response("Y") is True

    def test_vlm_is_ad_response_no(self):
        """Test parsing 'no' responses."""
        from vlm import VLMManager
        manager = VLMManager()
        assert manager._is_ad_response("No") is False
        assert manager._is_ad_response("no") is False
        assert manager._is_ad_response("No, this is not an ad") is False
        assert manager._is_ad_response("N") is False

    def test_vlm_detect_ad_not_ready(self):
        """Test detect_ad when VLM not ready."""
        from vlm import VLMManager
        manager = VLMManager()
        manager.is_ready = False
        is_ad, response, elapsed = manager.detect_ad("/tmp/test.jpg")
        assert is_ad is False
        assert "not ready" in response.lower()

    def test_vlm_detect_ad_file_not_found(self):
        """Test detect_ad with non-existent file."""
        from vlm import VLMManager
        manager = VLMManager()
        manager.is_ready = True
        manager.process = MagicMock()
        is_ad, response, elapsed = manager.detect_ad("/nonexistent/path.jpg")
        assert is_ad is False
        assert "not found" in response.lower()


# ============================================================================
# OCR Tests
# ============================================================================

class TestOCR:
    """Tests for ocr.py - using mocks since RKNN isn't available in tests."""

    def test_ocr_imports(self):
        """Test that ocr module can be imported (may fail without rknnlite)."""
        try:
            from ocr import PaddleOCR, DBPostProcessor, CTCLabelDecode
            assert PaddleOCR is not None
        except ImportError as e:
            # Expected if rknnlite not installed
            assert "rknnlite" in str(e).lower()

    def test_ad_keywords_exist(self):
        """Test that ad keywords are defined."""
        try:
            from ocr import PaddleOCR
            assert len(PaddleOCR.AD_KEYWORDS_EXACT) > 0
            assert len(PaddleOCR.AD_KEYWORDS_WORD) > 0
        except ImportError:
            pass  # Skip if rknnlite not available

    def test_ad_keywords_content(self):
        """Test that expected keywords are in the lists."""
        try:
            from ocr import PaddleOCR
            assert "skip ad" in PaddleOCR.AD_KEYWORDS_EXACT
            assert "sponsored" in PaddleOCR.AD_KEYWORDS_EXACT
            assert "skip" in PaddleOCR.AD_KEYWORDS_WORD
        except ImportError:
            pass

    def test_ad_exclusions_exist(self):
        """Test that exclusion patterns are defined."""
        try:
            from ocr import PaddleOCR
            assert len(PaddleOCR.AD_EXCLUSIONS) > 0
            assert "skip recap" in PaddleOCR.AD_EXCLUSIONS
            assert "skip intro" in PaddleOCR.AD_EXCLUSIONS
        except ImportError:
            pass

    def test_terminal_indicators_exist(self):
        """Test that terminal content indicators are defined."""
        try:
            from ocr import PaddleOCR
            assert len(PaddleOCR.TERMINAL_INDICATORS) > 0
        except ImportError:
            pass


# ============================================================================
# WebUI Tests
# ============================================================================

class TestWebUI:
    """Tests for webui.py"""

    def test_webui_imports(self):
        """Test that webui module imports correctly."""
        from webui import WebUI
        assert WebUI is not None

    def test_webui_init(self):
        """Test WebUI initialization."""
        from webui import WebUI
        mock_minus = MagicMock()
        ui = WebUI(mock_minus, port=8080, ustreamer_port=9090)
        assert ui.port == 8080
        assert ui.ustreamer_port == 9090
        assert ui.running is False

    def test_webui_flask_app_created(self):
        """Test that Flask app is created."""
        from webui import WebUI
        mock_minus = MagicMock()
        ui = WebUI(mock_minus)
        assert ui.app is not None

    def test_webui_routes_registered(self):
        """Test that expected routes are registered."""
        from webui import WebUI
        mock_minus = MagicMock()
        ui = WebUI(mock_minus)

        # Check that key routes exist
        routes = [rule.rule for rule in ui.app.url_map.iter_rules()]
        assert '/' in routes
        assert '/api/status' in routes
        assert '/api/logs' in routes
        assert '/stream' in routes
        assert '/snapshot' in routes

    def test_webui_api_status_route(self):
        """Test the /api/status endpoint."""
        from webui import WebUI
        mock_minus = MagicMock()
        mock_minus.get_status_dict.return_value = {
            "blocking": False,
            "fps": 30.0,
            "uptime": 100
        }
        ui = WebUI(mock_minus)

        with ui.app.test_client() as client:
            response = client.get('/api/status')
            assert response.status_code == 200
            data = response.get_json()
            assert "blocking" in data
            assert "fps" in data

    def test_webui_pause_invalid_duration(self):
        """Test pause endpoint with invalid duration."""
        from webui import WebUI
        mock_minus = MagicMock()
        ui = WebUI(mock_minus)

        with ui.app.test_client() as client:
            response = client.post('/api/pause/3')  # 3 is not in [1,2,5,10]
            assert response.status_code == 400


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests that verify modules work together."""

    def test_vocabulary_used_by_ad_blocker_import(self):
        """Test that vocabulary can be imported where ad_blocker would use it."""
        from vocabulary import SPANISH_VOCABULARY
        # Simulate what ad_blocker does
        import random
        word = random.choice(SPANISH_VOCABULARY)
        assert len(word) == 4  # (spanish, pronunciation, english, example)

    def test_config_serializable(self):
        """Test that MinusConfig can be converted to dict."""
        from config import MinusConfig
        from dataclasses import asdict
        config = MinusConfig()
        config_dict = asdict(config)
        assert "device" in config_dict
        assert "screenshot_dir" in config_dict

    def test_skip_detection_with_ocr_like_output(self):
        """Test skip detection with OCR-like text output."""
        from skip_detection import check_skip_opportunity
        # Simulate OCR output from a YouTube ad
        ocr_texts = [
            "Video will play after ad",
            "Skip Ad",
            "0:15",
            "Learn more"
        ]
        is_skippable, text, countdown = check_skip_opportunity(ocr_texts)
        assert is_skippable is True

    def test_overlay_destroy_cleanup(self):
        """Test that overlay cleanup works properly."""
        from overlay import NotificationOverlay

        with patch('overlay.urllib.request.urlopen'):
            overlay = NotificationOverlay()
            overlay._visible = True
            overlay._current_text = "Test"
            overlay.destroy()

            assert overlay._visible is False
            assert overlay._current_text is None


# ============================================================================
# Test Runner
# ============================================================================

def run_tests():
    """Run all tests manually (without pytest)."""
    import traceback

    test_classes = [
        TestVocabulary,
        TestConfig,
        TestSkipDetection,
        TestScreenshots,
        TestConsole,
        TestCapture,
        TestDRM,
        TestV4L2,
        TestOverlay,
        TestHealth,
        TestFireTV,
        TestVLM,
        TestOCR,
        TestWebUI,
        TestIntegration,
    ]

    total_tests = 0
    passed_tests = 0
    skipped_tests = 0
    failed_tests = []

    for test_class in test_classes:
        print(f"\n{'='*60}")
        print(f"Running {test_class.__name__}")
        print('='*60)

        instance = test_class()

        # Get all test methods
        test_methods = [m for m in dir(instance) if m.startswith('test_')]

        for method_name in test_methods:
            total_tests += 1
            method = getattr(instance, method_name)

            # Run setup if it exists
            if hasattr(instance, 'setup_method'):
                try:
                    instance.setup_method()
                except Exception as e:
                    print(f"  SETUP FAILED: {method_name}")
                    failed_tests.append((test_class.__name__, method_name, str(e)))
                    continue

            try:
                method()
                print(f"  PASS: {method_name}")
                passed_tests += 1
            except AssertionError as e:
                print(f"  FAIL: {method_name}")
                print(f"        {e}")
                failed_tests.append((test_class.__name__, method_name, str(e)))
            except ImportError as e:
                print(f"  SKIP: {method_name} (missing dependency: {e})")
                skipped_tests += 1
            except Exception as e:
                print(f"  ERROR: {method_name}")
                print(f"         {e}")
                failed_tests.append((test_class.__name__, method_name, traceback.format_exc()))

            # Run teardown if it exists
            if hasattr(instance, 'teardown_method'):
                try:
                    instance.teardown_method()
                except Exception:
                    pass

    print(f"\n{'='*60}")
    print(f"RESULTS: {passed_tests}/{total_tests} passed, {skipped_tests} skipped")
    print('='*60)

    if failed_tests:
        print("\nFailed tests:")
        for class_name, method_name, error in failed_tests:
            print(f"  - {class_name}.{method_name}")
            if len(error) < 100:
                print(f"    {error}")

    return len(failed_tests) == 0


if __name__ == '__main__':
    # Check if pytest is available
    try:
        import pytest
        sys.exit(pytest.main([__file__, '-v']))
    except ImportError:
        print("pytest not installed, running tests manually...")
        success = run_tests()
        sys.exit(0 if success else 1)
