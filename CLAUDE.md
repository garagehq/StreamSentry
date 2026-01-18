# Minus - Development Notes

## Overview

HDMI passthrough with real-time ML-based ad detection and blocking using dual NPUs:
- **PaddleOCR** on RK3588 NPU (~400ms per frame)
- **Qwen3-VL-2B** on Axera LLM 8850 NPU (~1.5s per frame)
- **Spanish vocabulary practice** during ad blocks!

## Architecture

```
┌──────────────┐     ┌────────────────────┐     ┌─────────────────────┐
│   HDMI-RX    │────▶│     ustreamer      │────▶│  GStreamer Pipeline │
│ /dev/video0  │     │ (MJPEG encoding)   │     │  (input-selector)   │
│  4K@30fps    │     │                    │     │                     │
│              │     │   :9090/stream     │     │  ┌───────────────┐  │
│              │     │   :9090/snapshot   │     │  │ Video Input   │  │
└──────────────┘     └────────┬───────────┘     │  │ (souphttpsrc) │  │
                              │                 │  └───────┬───────┘  │
                              │                 │          │ INSTANT  │
                              │                 │          ▼ SWITCH   │
                              │                 │  ┌───────────────┐  │
                              │                 │  │Blocking Input │  │
                              │                 │  │ (videotestsrc │  │
                              │                 │  │  + textoverlay│  │
                              │                 │  │  + Spanish!)  │  │
                              │                 │  └───────────────┘  │
                              │                 │          │          │
                              │                 │          ▼          │
                              │                 │  ┌───────────────┐  │
                              │                 │  │    kmssink    │  │
                              │                 │  │ (auto-detect) │  │
                              │                 │  └───────────────┘  │
                              │                 └─────────────────────┘
                              │
                              ▼ HTTP snapshot (~150ms, non-blocking)
              ┌───────────────┴───────────────┐
              │                               │
     ┌────────┴────────┐           ┌──────────┴──────────┐
     │   OCR Worker    │           │    VLM Worker       │
     │  ┌───────────┐  │           │  ┌───────────────┐  │
     │  │ PaddleOCR │  │           │  │ Qwen3-VL-2B   │  │
     │  │ RK3588 NPU│  │           │  │ Axera LLM 8850│  │
     │  │ ~400ms    │  │           │  │ ~1.5s         │  │
     │  └───────────┘  │           │  └───────────────┘  │
     └────────┬────────┘           └──────────┬──────────┘
              │                               │
              └───────────────┬───────────────┘
                              │
                     ┌────────┴────────┐
                     │  input-selector │
                     │ INSTANT SWITCH! │
                     └─────────────────┘
```

**Key Architecture Points:**
- Single GStreamer pipeline with `input-selector` for instant video/blocking switching
- No process restart needed - just changes which input is active
- No X11 required - uses DRM/KMS directly via kmssink
- **Auto-detects HDMI output, resolution, and DRM plane** at startup
- Works with both 4K and 1080p displays (uses display's preferred resolution)
- Both ML workers run concurrently on separate NPUs
- Display runs independently at 30fps without any stutter

## Key Files

| File | Purpose |
|------|---------|
| `minus.py` | Main entry point - orchestrates everything |
| `minus.spec` | PyInstaller spec for building executable |
| `src/ad_blocker.py` | GStreamer video pipeline with input-selector, Spanish vocab |
| `src/audio.py` | GStreamer audio passthrough with mute control |
| `src/ocr.py` | PaddleOCR on RKNN NPU, keyword detection |
| `src/vlm.py` | Qwen3-VL-2B on Axera NPU |
| `src/health.py` | Unified health monitor for all subsystems |
| `src/webui.py` | Flask web UI for remote monitoring/control |
| `src/templates/index.html` | Web UI single-page app |
| `src/static/style.css` | Web UI dark theme styles |
| `install.sh` | Install as systemd service |
| `uninstall.sh` | Remove systemd service |
| `stop.sh` | Graceful shutdown script |
| `minus.service` | systemd service file |
| `screenshots/ocr/` | Ad detection screenshots (auto-truncated) |
| `screenshots/non_ad/` | Non-ad screenshots for VLM training |

## Running

```bash
python3 minus.py
```

**Command-line options:**
```bash
--device /dev/video1      # Custom capture device
--ocr-timeout 1.5         # OCR timeout in seconds (default: 1.5)
--max-screenshots 100     # Keep N recent screenshots (default: 50, 0=unlimited)
--check-signal            # Just check HDMI signal and exit
--connector-id 231        # DRM connector ID (auto-detected if not specified)
--plane-id 192            # DRM plane ID (auto-detected if not specified)
--webui-port 8080         # Web UI port (default: 8080)
```

**Auto-detection:**
At startup, Minus automatically probes the DRM subsystem to detect:
- **Connected HDMI output** - Works with either HDMI-A-1 (connector 215) or HDMI-A-2 (connector 231)
- **Preferred resolution** - Reads EDID to get the display's preferred mode (e.g., 4K@60Hz or 1080p@60Hz)
- **NV12-capable overlay plane** - Finds a suitable DRM plane that supports NV12 format for video output
- **Audio output device** - Matches ALSA device to the connected HDMI output (hw:0,0 for HDMI-A-1, hw:1,0 for HDMI-A-2)

This allows Minus to work with different displays without manual configuration.

## Performance

| Metric | Value |
|--------|-------|
| Display (video) | **30fps** (GStreamer kmssink, MJPEG → NV12 → plane 72) |
| Display (blocking) | 2-3fps (videotestsrc + textoverlay) |
| Ad blocking switch | **1.5s animation** (shrink/grow transition) |
| Audio mute/unmute | **INSTANT** (volume element mute property) |
| Preview window | **~4fps** (gdkpixbufoverlay, 20% of screen) |
| Animation framerate | **~30fps** (smooth ease-in/ease-out) |
| ustreamer MJPEG stream | **~60fps** (MPP hardware encoding at 4K) |
| OCR latency | **100-200ms** capture + **250-400ms** inference |
| VLM latency | 1.3-1.5s per frame |
| VLM model load | ~40s (once at startup) |
| Snapshot capture | **~150ms** (4K JPEG download) |
| OCR image size | 960x540 (downscaled from 4K for speed) |
| ustreamer quality | 80% JPEG (MPP encoder) |

**FPS Tracking:**
- GStreamer identity element with pad probe counts frames
- FPS logged every 60 seconds via health monitor
- Warning logged if FPS drops below 25

## ustreamer-patched (NV12 + MPP Hardware Encoding)

We use a patched version of ustreamer from `garagehq/ustreamer` that adds:
- **NV12/NV16/NV24 format support** for RK3588 HDMI-RX devices
- **MPP hardware JPEG encoding** using RK3588 VPU (~60fps at 4K!)
- **Extended timeouts** for RK3588 HDMI-RX driver compatibility
- **Multi-worker MPP support** (4 parallel encoders optimal)
- **Cache sync fix** for DMA-related visual artifacts

**Why patched ustreamer?**
The stock PiKVM ustreamer doesn't support NV12 format or RK3588 hardware encoding.
Our fork adds NV12→JPEG encoding via Rockchip MPP (Media Process Platform) that
achieves ~60fps on 4K input with minimal CPU usage.

**Dynamic Format Detection:**
Minus automatically probes the V4L2 device to detect its current format
and resolution. Supported formats:
- **NV12** - RK3588 HDMI-RX native (uses MPP hardware encoder)
- **BGR24/BGR3** - Some HDMI devices (uses standard ustreamer BGR24 support)
- **YUYV/UYVY** - Webcam-style devices
- **MJPEG** - Pre-compressed JPEG sources

**Performance comparison (4K HDMI input):**

| Mode | ustreamer FPS | CPU Usage | Notes |
|------|---------------|-----------|-------|
| CPU encoding | ~4 fps | ~100% | CPU can't keep up with 4K JPEG encoding |
| MPP hardware | **~60 fps** | **~5%** | `--encoder=mpp-jpeg` (default) |

**ustreamer command (used by Minus):**
```bash
/home/radxa/ustreamer-patched \
  --device=/dev/video0 \
  --format=NV12 \
  --resolution=3840x2160 \
  --persistent \
  --port=9090 \
  --host=0.0.0.0 \
  --encoder=mpp-jpeg \
  --encode-scale=4k \
  --quality=80 \
  --workers=4 \
  --buffers=5
```

**Installation:**
```bash
# Clone and build with MPP support
git clone https://github.com/garagehq/ustreamer.git /home/radxa/ustreamer-garagehq
cd /home/radxa/ustreamer-garagehq
make WITH_MPP=1
cp ustreamer /home/radxa/ustreamer-patched

# Minus uses /home/radxa/ustreamer-patched automatically
```

**Key changes in garagehq/ustreamer:**
- `src/ustreamer/encoders/mpp/encoder.c` - MPP hardware JPEG encoder with cache sync
- `src/libs/capture.c` - NV12/NV16/NV24 format support, extended timeouts
- `src/ustreamer/encoder.c` - MPP encoder integration, multi-worker support
- `src/ustreamer/options.c` - `--encoder=mpp-jpeg` CLI option

## Audio Passthrough

**Hardware:**
- Capture: `hw:4,0` (rockchip,hdmiin) - HDMI-RX audio input
- Playback: `hw:0,0` (rockchip-hdmi0) - HDMI-TX0 output
- Format: 48kHz, stereo, S16LE

**GStreamer Pipeline:**
```
alsasrc (HDMI) ──┐
                 ├──► audiomixer ──► volume ──► alsasink
audiotestsrc ────┘
(silent keepalive)
```

The `audiotestsrc wave=silence` provides a silent keepalive that prevents pipeline
stalls when the HDMI source has no audio (between songs, during video silence, etc.).

**Mute Control:**
- `ad_blocker.show()` calls `audio.mute()` - instant mute during ads
- `ad_blocker.hide()` calls `audio.unmute()` - restore audio after ads
- Uses GStreamer `volume` element's `mute` property (no pipeline restart)

**Why separate pipeline?**
- Audio runs independently from video - simpler debugging
- If audio fails, video continues unaffected
- No sync issues for live passthrough

**Error Recovery:**
- GStreamer bus monitors for pipeline errors and EOS
- Buffer probe tracks audio flow (detects stalls)
- Watchdog thread checks every 3s, restarts if no buffer for 6s
- Exponential backoff for restarts (1s → 2s → 4s → ... → 60s max)
- No maximum restart limit - always tries to recover
- Backoff resets after 5 seconds of sustained audio flow
- Mute state is preserved across restarts

**Testing:**
```bash
# Test passthrough manually
gst-launch-1.0 alsasrc device=hw:4,0 ! \
  "audio/x-raw,rate=48000,channels=2,format=S16LE" ! \
  audioconvert ! audioresample ! \
  alsasink device=hw:0,0 sync=false

# Check if HDMI source has audio
v4l2-ctl -d /dev/video0 --get-ctrl audio_present
```

## Ad Detection Logic (Weighted Model)

**OCR (Primary - High Trust):**
- Triggers blocking immediately on 1 detection
- Needs 3 consecutive no-ads to stop (`OCR_STOP_THRESHOLD`)
- Tracks `last_ocr_ad_time` for VLM context

**VLM (Secondary - Contextual Trust):**
- If OCR detected within 5s (`OCR_TRUST_WINDOW`): VLM is trusted
- If no recent OCR: needs 5 consecutive detections (`VLM_ALONE_THRESHOLD`)
- Needs 2 consecutive no-ads to stop (`VLM_STOP_THRESHOLD`)

**Anti-flicker:**
- Minimum 3s blocking duration (`MIN_BLOCKING_DURATION`)
- Both must agree to stop when VLM has OCR context

## Blocking Overlay

When ads are detected, the screen shows:
- **Header**: `BLOCKING (OCR)`, `BLOCKING (VLM)`, or `BLOCKING (OCR+VLM)`
- **Spanish vocabulary**: Random intermediate-level word with translation
- **Example sentence**: Shows the word in context
- **Rotation**: New vocabulary every 11-15 seconds
- **Ad Preview Window**: Live preview of the blocked ad in bottom-right corner (~4fps)
- **Debug Dashboard**: Stats overlay in bottom-left corner (uptime, ads blocked, block time)

**Transition Animations (1.5s):**
- **Start blocking**: Ad video shrinks from full-screen to corner preview (ease-out)
- **End blocking**: Preview grows from corner to full-screen, then switches to video (ease-in)
- Preview updates at ~4fps during animation for responsive feel

Example display:
```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│                        BLOCKING (OCR)                           │
│                                                                 │
│                         aprovechar                              │
│                    = to take advantage of                       │
│                                                                 │
│                 Hay que aprovechar el tiempo.                   │
│                                                                 │
│                                                     ┌─────────┐ │
│  Uptime: 2h 15m 30s                                 │ [AD     │ │
│  Ads blocked: 47                                    │ PREVIEW]│ │
│  Block time: 12m 45s                                └─────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

**Web UI Toggles:**
- Ad Preview Window: toggleable via Settings (default: ON)
- Debug Dashboard: toggleable via Settings (default: ON)

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

## Housekeeping

**Log File:**
- Location: `/tmp/minus.log`
- Max 5MB per log file
- Keeps 3 backup files (minus.log.1, .2, .3)

**Screenshot Truncation:**
- Keeps only last 50 screenshots by default
- Configurable via `--max-screenshots`

## VLM Model

**Qwen3-VL-2B-INT4** on Axera LLM 8850 NPU:
- 96% accuracy on ad detection benchmark
- 1.3-1.7s inference time
- ~40s model load time (once)
- No tokenizer service needed (uses local file)

```
/home/radxa/axera_models/Qwen3-VL-2B/
├── main_axcl_aarch64_rebuilt
├── qwen3_tokenizer.txt
└── Qwen3-VL-2B-Instruct-AX650-c128_p1152-int4/
```

## Dependencies

```bash
# System
sudo apt install -y imagemagick ffmpeg curl

# Build ustreamer with MPP hardware encoding
git clone https://github.com/garagehq/ustreamer.git /home/radxa/ustreamer-garagehq
cd /home/radxa/ustreamer-garagehq && make WITH_MPP=1
cp ustreamer /home/radxa/ustreamer-patched

# Python
pip3 install --break-system-packages pyclipper shapely numpy opencv-python pexpect PyGObject flask requests
```

## Troubleshooting

**ustreamer fails to start:**
```bash
fuser -k /dev/video0  # Kill processes using device
pkill -9 ustreamer    # Kill orphaned ustreamer
```

**VLM not loading:**
- Check Axera card: `axcl_smi`
- Verify model files exist in `/home/radxa/axera_models/Qwen3-VL-2B/`

**OCR not detecting:**
- Test snapshot: `curl http://localhost:9090/snapshot -o test.jpg`
- Check HDMI: `v4l2-ctl -d /dev/video0 --query-dv-timings`

**Display issues:**
- Check DRM plane: `modetest -M rockchip -p | grep -A5 "plane\[72\]"`
- Verify connector: `modetest -M rockchip -c | grep HDMI`

## Color Correction

Color correction is done via GStreamer's `videobalance` element in the pipeline.

**Why not ustreamer/V4L2?**
The HDMI-RX device doesn't support V4L2 image controls (saturation, contrast, brightness).
Only read-only controls are available: `audio_sampling_rate`, `audio_present`, `power_present`.

**Current settings (in `src/ad_blocker.py`):**
```
videobalance saturation=0.85  # Reduce oversaturation (default 1.0, range 0-2)
```

**To adjust colors:**
Edit the `videobalance` element in `_init_pipeline()` in `src/ad_blocker.py`:
- `saturation`: 0.0-2.0 (default 1.0, lower = less saturated)
- `contrast`: 0.0-2.0 (default 1.0)
- `brightness`: -1.0 to 1.0 (default 0.0)

## Health Monitoring

The health monitor (`src/health.py`) runs in a background thread and checks:

| Subsystem | Check | Recovery |
|-----------|-------|----------|
| HDMI signal | v4l2-ctl --query-dv-timings | Show "NO SIGNAL" overlay, mute audio |
| No HDMI at startup | check_hdmi_signal() | Show "NO HDMI INPUT" and wait |
| ustreamer | HTTP HEAD to :9090/snapshot | Restart ustreamer + video pipeline |
| Video pipeline | Buffer flow + FPS monitoring | Restart pipeline with exponential backoff |
| Output FPS | GStreamer pad probe | Log warning if < 25fps |
| VLM | Consecutive timeouts < 5 | Degrade to OCR-only, retry VLM after 30s |
| Memory | Usage < 90% | Force GC, clean old screenshots |
| Disk | Free > 500MB | Log warning |

**HDMI Disconnect/Reconnect Recovery:**
- Detects HDMI signal loss via v4l2-ctl
- Shows "NO SIGNAL" overlay and mutes audio immediately
- On signal restoration: restarts ustreamer → restarts video pipeline → restores display
- Full recovery typically completes in ~7 seconds

**Video Pipeline Watchdog:**
- Buffer watchdog detects stalls (10 seconds without buffer)
- Monitors GStreamer pipeline state (must be PLAYING)
- Handles HTTP connection errors from souphttpsrc
- Handles unexpected EOS (end-of-stream) events
- Exponential backoff for restarts (1s → 2s → 4s → ... → 30s max)
- Backoff resets after 10 seconds of sustained buffer flow

**Startup grace period:**
- 30-second grace period before ustreamer health checks begin
- Prevents false positives during VLM model loading

**Graceful degradation:**
- If VLM fails 5+ times consecutively, switches to OCR-only mode
- VLM restart is attempted after 30 seconds in background
- OCR continues working independently

**Scene skip cap:**
- OCR: Force run after 30 consecutive skips
- VLM: Force run after 10 consecutive skips
- Prevents missing ads that appear without scene change

**Periodic logging:**
- FPS logged every 60 seconds
- Full status logged every 5 minutes (uptime, fps, hdmi, video, audio, vlm, mem, disk)

## Web UI

Minus includes a lightweight Flask-based web UI for remote monitoring and control,
accessible via Tailscale from desktop or mobile devices.

**Features:**
- **Live video feed** - MJPEG stream proxied from ustreamer (CORS bypass)
- **Status display** - Blocking state, FPS, HDMI info, uptime
- **Pause controls** - 1/2/5/10 minute presets to pause ad blocking
- **Detection history** - Recent OCR/VLM detections with timestamps
- **Log viewer** - Collapsible log output for debugging

**Architecture:**
```
┌─────────────────────────────────────────────────────────────┐
│                      Web Browser                             │
│  ┌─────────────────┐  ┌──────────────────────────────────┐  │
│  │   Live View     │  │         Control Panel            │  │
│  │ (ustreamer:9090)│  │  - Status (blocking, FPS, etc)   │  │
│  │                 │  │  - Pause button (1/2/5/10 min)   │  │
│  │   <img src=     │  │  - Recent detections             │  │
│  │   /stream>      │  │  - Settings (preview, debug)     │  │
│  └─────────────────┘  └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │ HTTP :8080
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    WebUI Server (Flask)                      │
│  GET /              → Single-page app (index.html)          │
│  GET /api/status    → JSON status                           │
│  POST /api/pause/N  → Pause blocking for N minutes          │
│  POST /api/resume   → Resume blocking immediately           │
│  GET /api/detections→ Recent OCR/VLM detections             │
│  GET /api/logs      → Last 100 log lines                    │
│  GET /api/preview   → Get preview window state              │
│  POST /api/preview/enable  → Enable ad preview window       │
│  POST /api/preview/disable → Disable ad preview window      │
│  GET /api/debug-overlay    → Get debug overlay state        │
│  POST /api/debug-overlay/enable  → Enable debug dashboard   │
│  POST /api/debug-overlay/disable → Disable debug dashboard  │
│  GET /stream        → Proxy to ustreamer:9090/stream        │
│  GET /snapshot      → Proxy to ustreamer:9090/snapshot      │
└─────────────────────────────────────────────────────────────┘
```

**Access URLs:**
- Local: `http://localhost:8080`
- Tailscale: `http://<tailscale-hostname>:8080`
- Direct stream: `http://<hostname>:9090/stream`

**Security:**
- No authentication (relies on Tailscale network security)
- Read-mostly API with minimal attack surface
- Binds to 0.0.0.0 for remote access

## VLM Training Data Collection

Minus automatically collects training data for future VLM improvements:

**Ad screenshots** (`screenshots/ocr/`):
- Saved when OCR detects ad keywords
- Includes matched keywords and all detected text in logs
- Auto-truncated to keep last 50 (configurable via `--max-screenshots`)
- Filename format: `ad_YYYYMMDD_HHMMSS_mmm_NNNN.png`

**Non-ad screenshots** (`screenshots/non_ad/`):
- Saved when user pauses blocking via WebUI
- Represents content that should NOT be classified as ads
- User pausing = "this is a false positive, save for training"
- Filename format: `non_ad_YYYYMMDD_HHMMSS_mmm_NNNN.png`

**Training workflow:**
1. Run Minus normally
2. When you see a false positive (blocking non-ad content), pause via WebUI
3. Non-ad screenshot is automatically saved
4. Use collected screenshots to fine-tune VLM:
   - `screenshots/ocr/*.png` → label as "ad"
   - `screenshots/non_ad/*.png` → label as "not_ad"

## Running as a Service

```bash
# Install
sudo ./install.sh

# View logs
journalctl -u minus -f

# Stop
sudo systemctl stop minus
./stop.sh  # Alternative with optional X11 restart

# Uninstall
sudo ./uninstall.sh
```

The service:
- Starts on boot (`multi-user.target`)
- Conflicts with display managers (gdm, lightdm, sddm)
- Restarts on crash (5 attempts per 5 minutes)
- Runs as root for DRM/device access

## Development Notes

- Do NOT create v2, v3, v4 files - update existing files directly
- VLM binary runs continuously via pexpect (not subprocess per frame)
- Both NPUs run in parallel without resource contention
- No X11 required - pure DRM/KMS display
- Single GStreamer pipeline with input-selector for instant switching
- Color correction via GStreamer videobalance (not V4L2 controls)
- Health monitor runs every 5 seconds in background thread
- VLM frame files use PID-based naming to avoid permission conflicts
- Snapshots scaled to 960x540 before OCR (model uses 960x960 anyway, smaller = faster)
- ustreamer quality set to 75% to reduce CPU load
- FPS tracked via GStreamer identity element with pad probe
- Startup cleanup removes stale frame files and kills orphaned processes

## Building Executable

```bash
# Install PyInstaller
pip3 install pyinstaller

# Build standalone executable
pyinstaller minus.spec

# Output: dist/minus
```

Note: Models are external and must be present at runtime.
