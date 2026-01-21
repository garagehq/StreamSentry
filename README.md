# Minus

HDMI passthrough with real-time ML-based ad detection and blocking using dual NPUs:
- **PaddleOCR** on RK3588 NPU (~300ms per frame)
- **Qwen3-VL-2B** on Axera LLM 8850 NPU (~1.5s per frame)
- **Audio passthrough** with auto-mute during ads
- **Spanish vocabulary practice** during ad blocks!
- **Web UI** for remote monitoring and control via Tailscale

## Overview

Minus captures video from HDMI-RX, displays it via GStreamer kmssink at 30fps, while running two ML workers concurrently to detect ads. When ads are detected, **instantly** switches to a blocking overlay with Spanish vocabulary practice.

**Key features:**
- **Instant ad blocking** - GStreamer input-selector switches in ~1 frame (no black screen!)
- **Audio passthrough** - HDMI audio with instant mute during ads, silent keepalive prevents stalls
- **Dual NPU inference** - OCR and VLM run concurrently on separate NPUs
- **Web UI** - Remote monitoring/control via Tailscale (mobile-friendly)
- **MPP hardware encoding** - 60fps 4K streaming via RK3588 VPU
- **No X11 required** - Pure DRM/KMS display via kmssink
- **Spanish learning** - Practice vocabulary while ads are blocked
- **30fps display** - Smooth passthrough without stutter
- **Set and forget** - systemd service, health monitoring, automatic recovery
- **Fire TV control** - Auto-skip ads via ADB remote control (optional)
- **Text overlay API** - Dynamic on-screen notifications via ustreamer

```
┌──────────────┐     ┌────────────────────┐     ┌─────────────────────┐
│   HDMI-RX    │────▶│     ustreamer      │────▶│  GStreamer Pipeline │
│ /dev/video0  │     │ (MJPEG encoding)   │     │  (input-selector)   │
│  4K@30fps    │     │                    │     │                     │
│              │     │   :9090/stream     │     │  Video ◄──► Blocking│
│  Audio ──────┼─────┼────────────────────┼────▶│   INSTANT SWITCH!   │
│  hw:4,0      │     │   :9090/snapshot   │     │         │           │
└──────────────┘     └────────┬───────────┘     │    kmssink + audio  │
                              │                 │   (auto-mute on ad) │
                              │                 └─────────────────────┘
                              │
                              ▼ HTTP snapshot (~150ms)
              ┌───────────────┴───────────────┐
              │                               │
     ┌────────┴────────┐           ┌──────────┴──────────┐
     │   OCR Worker    │           │    VLM Worker       │
     │   PaddleOCR     │           │   Qwen3-VL-2B       │
     │   RK3588 NPU    │           │   Axera LLM 8850    │
     │   ~400ms        │           │   ~1.5s             │
     └─────────────────┘           └─────────────────────┘
```

## Hardware

- **Board:** Radxa with RK3588
- **HDMI-RX:** `/dev/video0` (rk_hdmirx driver)
- **HDMI-RX Audio:** `hw:4,0` (rockchip,hdmiin @ 48kHz)
- **HDMI-TX Audio:** `hw:0,0` (rockchip-hdmi0)
- **OCR NPU:** RK3588 6 TOPS NPU
- **VLM NPU:** Axera M5 LLM 8850 (AX650N) via M.2
- **Supported resolutions:** Up to 4K@30fps

## Quick Start

```bash
cd /home/radxa/Minus

# Install dependencies (first time only)
sudo apt install -y imagemagick ffmpeg curl

# Build ustreamer (first time only)
git clone https://github.com/pikvm/ustreamer.git
cd ustreamer && make -j$(nproc) && sudo cp ustreamer /usr/local/bin/

# Python packages
pip3 install pyclipper shapely numpy opencv-python pexpect PyGObject

# Run everything
python3 minus.py

# Check HDMI signal only
python3 minus.py --check-signal
```

## Command Line Options

| Option | Description |
|--------|-------------|
| `--device PATH` | Video device (default: /dev/video0) |
| `--screenshot-dir DIR` | Screenshot directory (default: screenshots) |
| `--ocr-timeout SEC` | Skip OCR frames taking longer than this (default: 1.5s) |
| `--max-screenshots N` | Keep only N recent screenshots (default: 50, 0=unlimited) |
| `--check-signal` | Check HDMI signal and exit |
| `--connector-id N` | DRM connector ID (default: 215) |
| `--plane-id N` | DRM plane ID (default: 72) |
| `--webui-port N` | Web UI port (default: 8080) |

## Performance

| Metric | Value |
|--------|-------|
| Display framerate | **30fps** (video), 2-3fps (blocking overlay) |
| ustreamer stream | **~60fps** (MPP hardware encoding at 4K) |
| Ad blocking switch | **1.5s animated transition** |
| Preview window | **~4fps** (live ad preview in corner) |
| Animation framerate | **~30fps** (smooth ease-in/ease-out) |
| Snapshot capture | ~150ms (4K JPEG download) |
| OCR latency | 250-400ms per frame (960x540 input) |
| VLM latency | 1.3-1.5s per frame |
| VLM model load | ~40s (once at startup) |
| JPEG quality | 80% (MPP hardware encoder) |

**FPS Monitoring:** Output FPS is logged every 60 seconds via health monitor.

## Ad Detection Logic (Weighted Model)

**OCR is PRIMARY (high trust):**
- Triggers blocking immediately on 1 detection
- Needs 3 consecutive no-ads to stop blocking

**VLM is SECONDARY (contextual trust):**
- If OCR detected within last 5s: VLM is trusted
- If no recent OCR: VLM needs 5 consecutive detections to trigger alone
- Needs 2 consecutive no-ads to stop

**Anti-flicker protection:**
- Minimum 3 seconds blocking duration
- Both OCR and VLM must agree to stop (when VLM has context)

## Blocking Overlay

When ads are detected, the screen shows:
- **Pixelated Background**: Blurred/pixelated version of the screen from ~6 seconds before the ad
- **Header**: `BLOCKING (OCR)`, `BLOCKING (VLM)`, or `BLOCKING (OCR+VLM)`
- **Spanish word**: Random intermediate-level vocabulary
- **Translation**: English meaning
- **Example**: Sentence using the word
- **Rotation**: New vocabulary every 11-15 seconds
- **Ad Preview**: Live preview of blocked ad in bottom-right corner (~4fps)
- **Debug Dashboard**: Stats in bottom-left (uptime, ads blocked, block time)

**Pixelated Background:**
Instead of a plain black background, the blocking overlay shows a heavily pixelated and darkened version of what was on screen before the ad appeared. This provides visual context while clearly indicating blocking is active. The system maintains a rolling 6-second buffer of snapshots (captured every 2 seconds) and uses the oldest frame when blocking starts.

**Smooth Transitions:**
- **Start blocking**: 1.5s animation - ad shrinks from full-screen to corner preview
- **End blocking**: 1.5s animation - preview grows to full-screen, then switches to video
- Preview updates during animation for responsive feel

Example display:
```
┌─────────────────────────────────────────────────────────────────┐
│                        BLOCKING (OCR)                           │
│                                                                 │
│                         aprovechar                              │
│                    = to take advantage of                       │
│                                                                 │
│                 Hay que aprovechar el tiempo.                   │
│                                                     ┌─────────┐ │
│  Uptime: 2h 15m 30s                                 │   AD    │ │
│  Ads blocked: 47                                    │ PREVIEW │ │
│  Block time: 12m 45s                                └─────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

Both preview window and debug dashboard are toggleable via Web UI Settings.

## Spanish Vocabulary

120+ intermediate-level words and phrases including:
- **Common verbs**: aprovechar, lograr, desarrollar, destacar, enfrentar...
- **Reflexive verbs**: comprometerse, enterarse, arrepentirse, darse cuenta...
- **Adjectives**: disponible, imprescindible, agotado, capaz, dispuesto...
- **Nouns**: desarrollo, comportamiento, conocimiento, ambiente, herramienta...
- **Expressions**: sin embargo, a pesar de, de repente, hoy en dia, cada vez mas...
- **False friends**: embarazada, exito, sensible, libreria, asistir...
- **Subjunctive triggers**: es importante que, espero que, dudo que, ojala...
- **Time expressions**: hace poco, dentro de poco, a la larga, de antemano...

## Ad Keywords Detected

**Exact phrases** (match anywhere):
- skip ad, skip ads, skipad, skipads
- sponsored, advertisement, ad break
- shop now, buy now, promoted

**Whole words:**
- skip, sponsor

## Log Output

```
2025-12-24 02:32:47 [I] Starting Minus...
2025-12-24 02:32:47 [I] HDMI signal: 3840x2160 @ 30.0fps
2025-12-24 02:32:50 [I] ustreamer started on port 9090
2025-12-24 02:32:52 [I] Display pipeline started - 30 FPS with instant ad blocking
2025-12-24 02:32:52 [I] [AudioPassthrough] Audio passthrough started
2025-12-24 02:33:33 [I] VLM model loaded successfully
2025-12-24 02:33:45 [W] AD BLOCKING STARTED (OCR)
2025-12-24 02:33:45 [I] [DRMAdBlocker] Switching to blocking overlay (ocr)
2025-12-24 02:33:45 [I] [AudioPassthrough] Audio MUTED
2025-12-24 02:34:04 [W] AD BLOCKING ENDED after 19.3s
2025-12-24 02:34:04 [I] [DRMAdBlocker] Switching to video stream
2025-12-24 02:34:04 [I] [AudioPassthrough] Audio UNMUTED
```

## Project Structure

```
minus/
├── minus.py              # Main entry point - orchestrates everything
├── minus.spec            # PyInstaller build spec
├── test_fire_tv.py       # Fire TV controller test script
├── tests/
│   └── test_modules.py   # Unit tests for all modules
├── src/
│   ├── ocr.py            # PaddleOCR on RKNN NPU
│   ├── vlm.py            # Qwen3-VL-2B on Axera NPU
│   ├── ad_blocker.py     # GStreamer video pipeline with input-selector
│   ├── audio.py          # GStreamer audio passthrough with mute control
│   ├── health.py         # Health monitor for all subsystems
│   ├── webui.py          # Flask web UI server
│   ├── overlay.py        # Text overlay via ustreamer API
│   ├── fire_tv.py        # Fire TV ADB controller
│   ├── fire_tv_setup.py  # Fire TV setup flow with overlay notifications
│   ├── vocabulary.py     # Spanish vocabulary list (120+ words)
│   ├── console.py        # Console blanking/restore functions
│   ├── drm.py            # DRM output probing (HDMI, resolution, plane)
│   ├── v4l2.py           # V4L2 device probing (format, resolution)
│   ├── config.py         # MinusConfig dataclass
│   ├── capture.py        # UstreamerCapture class for snapshots
│   ├── screenshots.py    # ScreenshotManager with deduplication
│   ├── skip_detection.py # Skip button detection (regex patterns)
│   ├── templates/
│   │   └── index.html    # Web UI single-page app
│   └── static/
│       └── style.css     # Web UI dark theme styles
├── install.sh            # Install as systemd service
├── uninstall.sh          # Remove systemd service
├── stop.sh               # Graceful shutdown script
├── minus.service         # systemd service file
├── models/
│   └── paddleocr/        # RKNN models (or symlink)
├── screenshots/
│   ├── ocr/              # Ad detection screenshots (auto-truncated)
│   └── non_ad/           # Non-ad screenshots for VLM training
├── README.md             # This file
├── CLAUDE.md             # Development notes
└── AUDIO.md              # Audio implementation details
```

## Testing

Minus includes a comprehensive test suite covering all extracted modules.

```bash
# Run all tests (no dependencies required)
python3 tests/test_modules.py

# Or with pytest (if installed)
python3 -m pytest tests/test_modules.py -v
```

**Test Output:**
```
============================================================
Running TestVocabulary
============================================================
  PASS: test_vocabulary_has_common_words
  PASS: test_vocabulary_not_empty
  ...
============================================================
RESULTS: 93/93 passed
============================================================
```

**What's Tested:**
- **Vocabulary** - Format validation, content structure, common words
- **Config** - Dataclass defaults and custom values
- **Skip Detection** - Button pattern matching, countdown parsing, edge cases
- **Screenshots** - Deduplication, file saving, hash computation, truncation
- **Console** - Blanking/restore command generation
- **Capture** - Snapshot URL handling, cleanup
- **DRM** - Output probing, fallback values
- **V4L2** - Format detection, error handling
- **Overlay** - NotificationOverlay, positions, show/hide
- **Health** - HealthMonitor, HealthStatus, HDMI detection
- **Fire TV** - Controller, key codes, device detection
- **VLM** - VLMManager, response parsing
- **OCR** - Keywords, exclusions, terminal detection
- **WebUI** - Flask routes, API endpoints

## VLM Model

The VLM uses **Qwen3-VL-2B-INT4** on the Axera LLM 8850 NPU:

| Metric | Value |
|--------|-------|
| Accuracy | 96% on ad detection benchmark |
| Inference | 1.3-1.7s per frame |
| Model load | ~40s (once at startup) |
| Prompt | "Is this an advertisement? Answer Yes or No." |

Model location:
```
/home/radxa/axera_models/Qwen3-VL-2B/
├── main_axcl_aarch64_rebuilt
├── qwen3_tokenizer.txt
└── Qwen3-VL-2B-Instruct-AX650-c128_p1152-int4/
```

## Fire TV Control (Optional)

Minus can control Fire TV devices via ADB over WiFi to automatically skip ads.

**Auto-Setup:** When Minus starts, it automatically scans for Fire TV devices and guides you through setup with on-screen overlay notifications. First-time connection requires approving the ADB authorization dialog on your TV.

**Requirements:**
- Fire TV on the same WiFi network
- ADB debugging enabled on Fire TV

**Enable ADB Debugging on Fire TV:**
1. Go to **Settings** (gear icon)
2. Select **My Fire TV**
3. Select **Developer Options** (if not visible: go to About → click device name 7 times)
4. Turn ON **ADB Debugging**

**Test Fire TV Connection:**
```bash
# Auto-discover and connect
python3 test_fire_tv.py

# Guided setup with instructions
python3 test_fire_tv.py --setup

# Interactive remote control
python3 test_fire_tv.py --interactive
```

**First Connection:**
When connecting for the first time, your Fire TV will show an "Allow USB Debugging?" dialog.
Look at your TV and press **Allow** (check "Always allow" for permanent authorization).

**Available Commands:**
- Navigation: up, down, left, right, select, back, home
- Media: play, pause, play_pause, fast_forward, rewind
- Volume: volume_up, volume_down, mute
- Power: power, sleep, wakeup

## Dependencies

```bash
# System packages
sudo apt install -y imagemagick ffmpeg curl libevent-dev libjpeg-dev libbsd-dev

# Build ustreamer with MPP hardware encoding
git clone https://github.com/garagehq/ustreamer.git /home/radxa/ustreamer-garagehq
cd /home/radxa/ustreamer-garagehq && make WITH_MPP=1
cp ustreamer /home/radxa/ustreamer-patched

# Python packages
pip3 install --break-system-packages pyclipper shapely numpy opencv-python pexpect PyGObject flask requests androidtv
```

## Troubleshooting

**No HDMI signal:**
When started without HDMI input, Minus displays "NO HDMI INPUT" on screen
and waits for user to connect a source. To check signal manually:
```bash
v4l2-ctl -d /dev/video0 --query-dv-timings
```

**ustreamer fails to start:**
```bash
fuser -k /dev/video0  # Kill processes using device
pkill -9 ustreamer    # Kill orphaned ustreamer
```

**VLM not loading:**
```bash
axcl_smi              # Check Axera card status
ls /home/radxa/axera_models/Qwen3-VL-2B/  # Verify model files
```

**Display issues:**
```bash
modetest -M rockchip -p | grep -A5 "plane\[72\]"  # Check DRM plane
modetest -M rockchip -c | grep HDMI               # Check connector
```

**OCR not detecting:**
```bash
curl http://localhost:9090/snapshot -o test.jpg  # Test snapshot
```

**Audio issues:**
```bash
# Check audio devices
arecord -l                                        # List capture devices
aplay -l                                          # List playback devices
v4l2-ctl -d /dev/video0 --get-ctrl audio_present # Check if HDMI has audio

# Test audio passthrough with silent keepalive (prevents stalls)
gst-launch-1.0 \
  alsasrc device=hw:4,0 ! "audio/x-raw,rate=48000,channels=2,format=S16LE" ! \
  queue ! audioconvert ! "audio/x-raw,rate=48000,channels=2,format=F32LE" ! mix. \
  audiotestsrc wave=silence is-live=true ! "audio/x-raw,rate=48000,channels=2,format=F32LE" ! mix. \
  audiomixer name=mix ! alsasink device=hw:0,0 sync=false
```

The `audiomixer` with `audiotestsrc wave=silence` keeps the pipeline alive even when
the HDMI source has no audio (between songs, during silence, etc.).

**Color correction:**
Adjust saturation/contrast/brightness in `src/ad_blocker.py` via the `videobalance` element:
```python
# In _init_pipeline(), modify:
videobalance saturation=0.85  # Range 0-2, default 1.0
```

## Running as a Service

Minus can run as a systemd service for 24/7 unattended operation:

```bash
# Install as systemd service
sudo ./install.sh

# View logs
journalctl -u minus -f

# Stop service
sudo systemctl stop minus

# Uninstall
sudo ./uninstall.sh
```

The service automatically:
- Starts on boot
- Restarts on crashes (5 attempts per 5 minutes)
- Disables X11/display managers to avoid conflicts

## Health Monitoring

Minus includes a unified health monitor that runs in the background:

**What it monitors:**
- HDMI signal (detects unplug/replug, shows "NO SIGNAL" message)
- ustreamer health (HTTP health check, not just PID)
- Video pipeline health (buffer flow, pipeline state)
- Output FPS (logged every 60s, warning if < 25 fps)
- VLM/OCR health (consecutive timeout detection)
- Memory usage (warning at 80%, critical at 90%)
- Disk space (warning below 500MB)

**Automatic recovery:**
- HDMI signal lost → Shows "NO SIGNAL" overlay, mutes audio
- HDMI signal restored → Restarts ustreamer + video pipeline, unmutes audio (~7s recovery)
- ustreamer stall → Restarts ustreamer + video pipeline
- Video pipeline stall → Restarts pipeline with exponential backoff (1s-30s)
- VLM failure → Degrades to OCR-only mode, attempts VLM restart
- Critical memory → Triggers garbage collection, cleans old screenshots

**HDMI Cable Robustness:**
- Jiggling or unplugging HDMI cables triggers automatic recovery
- No manual restart required - system recovers automatically
- Video pipeline watchdog detects stalls (10s threshold)
- Exponential backoff prevents restart storms

**Graceful degradation:**
- If VLM fails repeatedly (5+ consecutive timeouts), switches to OCR-only mode
- VLM restart is attempted after 30 seconds
- OCR continues working even if VLM is disabled
- 30-second startup grace period before health checks begin

## Web UI

Minus includes a mobile-friendly web UI for remote monitoring and control:

**Access:**
- Local: `http://localhost:8080`
- Tailscale: `http://<tailscale-hostname>:8080`
- Direct video stream: `http://<hostname>:9090/stream`

**Features:**
- **Live video feed** - Real-time MJPEG stream from ustreamer
- **Status display** - Blocking state, FPS, HDMI resolution, uptime
- **Pause controls** - 1/2/5/10 minute presets to pause ad blocking
- **Settings** - Toggle ad preview window and debug dashboard
- **Detection history** - Recent OCR/VLM detections with timestamps
- **Log viewer** - Collapsible log output for debugging

**Pause & Training Data:**
When you pause blocking via the WebUI, Minus automatically saves a screenshot
to `screenshots/non_ad/`. This creates training data for improving the VLM:
- **Pausing = "this is NOT an ad"** (false positive correction)
- Screenshots saved with `non_ad_` prefix for easy labeling
- Use these to fine-tune the VLM and reduce false positives

**Test API Endpoints:**
For development and testing, you can manually trigger ad blocking:
```bash
# Trigger blocking for 20 seconds (source: ocr, vlm, both, or default)
curl -X POST -H "Content-Type: application/json" \
  -d '{"duration": 20, "source": "ocr"}' \
  http://localhost:8080/api/test/trigger-block

# Stop blocking immediately
curl -X POST http://localhost:8080/api/test/stop-block
```

Test mode prevents the detection loop from canceling the blocking, allowing you to test the full blocking experience including pixelated background and animations.

## Text Overlay API

Minus includes a text overlay system that renders text directly on the video stream via ustreamer's MPP hardware encoder. This is used for Fire TV setup guidance and can be used for custom notifications.

**API Endpoints:**
- `GET http://localhost:9090/overlay` - Get current overlay configuration
- `GET http://localhost:9090/overlay/set?params` - Set overlay configuration

**Parameters:**
| Parameter | Description |
|-----------|-------------|
| `text` | Text to display (URL-encoded, supports newlines with `%0A`) |
| `enabled` | `true` to enable, `false` to disable |
| `position` | 0=top-left, 1=top-right, 2=bottom-left, 3=bottom-right, 4=center |
| `scale` | Text scale factor (1-10, default: 3) |
| `color_y`, `color_u`, `color_v` | Text color in YUV (default: white) |
| `bg_enabled` | Enable background box (default: true) |
| `bg_alpha` | Background transparency 0-255 (default: 180) |
| `clear` | Set to `true` to clear overlay |

**Example Usage:**
```bash
# Show "LIVE" in top-right corner
curl "http://localhost:9090/overlay/set?text=LIVE&position=1&scale=4&enabled=true"

# Show multi-line text
curl "http://localhost:9090/overlay/set?text=Line%201%0ALine%202&position=0&enabled=true"

# Clear overlay
curl "http://localhost:9090/overlay/set?clear=true"
```

**Python Usage:**
```python
from src.overlay import NotificationOverlay

overlay = NotificationOverlay(ustreamer_port=9090)
overlay.show("Hello World", duration=5.0)  # Auto-hides after 5 seconds
overlay.hide()  # Manual hide
```

**Performance:**
- ~0.5ms overhead per frame
- Rendered directly on NV12 frames before JPEG encoding
- No GStreamer pipeline modifications needed

## Housekeeping

**Log File:**
- Location: `/tmp/minus.log`
- Max 5MB per log file
- Keeps 3 backup files (minus.log.1, .2, .3)

**Screenshot Management:**
- Ad screenshots: `screenshots/ocr/` (auto-truncated to last 50)
- Non-ad screenshots: `screenshots/non_ad/` (saved when pausing via WebUI)
- Configurable via `--max-screenshots` (0 = unlimited)

**Audio Error Recovery:**
- Watchdog checks every 3 seconds, restarts if stalled for 6+ seconds
- Exponential backoff for restart attempts (1s → 2s → 4s → ... → 60s max)
- No maximum restart limit - always tries to recover
- Backoff resets after 5 seconds of sustained audio flow

**Video Pipeline Recovery:**
- Watchdog checks every 3 seconds, restarts if stalled for 10+ seconds
- Monitors GStreamer pipeline state and buffer flow
- Handles HTTP connection errors (ustreamer restart)
- Handles unexpected EOS events
- Exponential backoff for restart attempts (1s → 2s → 4s → ... → 30s max)
- Backoff resets after 10 seconds of sustained buffer flow
- Preserves blocking overlay state across restarts

## Building Executable

Minus can be compiled into a standalone executable using PyInstaller:

```bash
# Install PyInstaller
pip3 install pyinstaller

# Build executable
pyinstaller minus.spec

# Output will be in dist/minus
```

**Note:** The executable still requires external model files at runtime:
- PaddleOCR models in standard location
- VLM models in `/home/radxa/axera_models/Qwen3-VL-2B/`

## License

MIT
