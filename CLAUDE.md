# Minus - Development Notes

## Overview

HDMI passthrough with real-time ML-based ad detection and blocking using dual NPUs:
- **PaddleOCR** on RK3588 NPU (~400ms per frame)
- **FastVLM-1.5B** on Axera LLM 8850 NPU (~0.9s per frame)
- **Spanish vocabulary practice** during ad blocks!

## Visual Design

See **[AESTHETICS.md](AESTHETICS.md)** for the complete visual design guide including:
- Color palette (black background, matrix green, danger red, purple accents)
- Typography (VT323 for display, IBM Plex Mono for body, DejaVu for TV overlays)
- Component styling and animations
- TV overlay layout specifications

## Architecture

```
┌──────────────┐     ┌────────────────────┐     ┌─────────────────────┐
│   HDMI-RX    │────▶│     ustreamer      │────▶│  GStreamer Pipeline │
│ /dev/video0  │     │ (MJPEG encoding)   │     │  (queue + kmssink)  │
│  4K@30fps    │     │                    │     │                     │
│              │     │   :9090/stream     │     │                     │
│              │     │   :9090/snapshot   │     │                     │
└──────────────┘     └────────┬───────────┘     └─────────────────────┘
                              │
                              ▼ HTTP snapshot (~150ms, non-blocking)
              ┌───────────────┴───────────────┐
              │                               │
     ┌────────┴────────┐           ┌──────────┴──────────┐
     │   OCR Worker    │           │    VLM Worker       │
     │  ┌───────────┐  │           │  ┌───────────────┐  │
     │  │ PaddleOCR │  │           │  │ FastVLM-1.5B  │  │
     │  │ RK3588 NPU│  │           │  │ Axera LLM 8850│  │
     │  │ ~400ms    │  │           │  │ ~0.9s         │  │
     │  └───────────┘  │           │  └───────────────┘  │
     └────────┬────────┘           └──────────┬──────────┘
              │                               │
              └───────────────┬───────────────┘
                              │
                     ┌────────┴────────┐
                     │ Blocking Mode   │
                     │ (ustreamer API) │
                     └─────────────────┘
```

**Key Architecture Points:**
- Simple GStreamer pipeline with `queue max-size-buffers=3 leaky=downstream`
- All blocking overlay rendering done in ustreamer's MPP encoder at 60fps
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
| `src/ad_blocker.py` | GStreamer video pipeline, blocking API client |
| `src/audio.py` | GStreamer audio passthrough with mute control |
| `src/ocr.py` | PaddleOCR on RKNN NPU, keyword detection |
| `src/vlm.py` | FastVLM-1.5B on Axera NPU |
| `src/health.py` | Unified health monitor for all subsystems |
| `src/webui.py` | Flask web UI for remote monitoring/control |
| `src/fire_tv.py` | Fire TV ADB remote control for ad skipping |
| `src/fire_tv_setup.py` | Fire TV auto-setup flow with overlay notifications |
| `src/overlay.py` | Notification overlay via ustreamer API |
| `src/vocabulary.py` | Spanish vocabulary list (120+ words) |
| `src/console.py` | Console blanking/restore functions |
| `src/drm.py` | DRM output probing (HDMI, resolution, plane) |
| `src/v4l2.py` | V4L2 device probing (format, resolution) |
| `src/config.py` | MinusConfig dataclass |
| `src/capture.py` | UstreamerCapture class for snapshot capture |
| `src/screenshots.py` | ScreenshotManager class with deduplication |
| `src/skip_detection.py` | Skip button detection (regex patterns) |
| `test_fire_tv.py` | Fire TV controller test and interactive remote |
| `tests/test_modules.py` | Unit tests for all extracted modules (106 tests) |
| `src/templates/index.html` | Web UI single-page app |
| `src/static/style.css` | Web UI dark theme styles |
| `install.sh` | Install as systemd service |
| `uninstall.sh` | Remove systemd service |
| `stop.sh` | Graceful shutdown script |
| `minus.service` | systemd service file |
| `screenshots/ads/` | OCR-detected ads (for training) |
| `screenshots/non_ads/` | User paused = false positives (for training) |
| `screenshots/vlm_spastic/` | VLM uncertainty cases (for analysis) |
| `screenshots/static/` | Static screen suppression (still frames) |

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

**Auto-detection at startup:**
- **Connected HDMI output** - Works with either HDMI-A-1 (connector 215) or HDMI-A-2 (connector 231)
- **Preferred resolution** - Reads EDID to get the display's preferred mode (e.g., 4K@60Hz or 1080p@60Hz)
- **NV12-capable overlay plane** - Finds a suitable DRM plane that supports NV12 format for video output
- **Audio output device** - Matches ALSA device to the connected HDMI output (hw:0,0 for HDMI-A-1, hw:1,0 for HDMI-A-2)

This allows Minus to work with different displays without manual configuration.

## Performance

| Metric | Value |
|--------|-------|
| Display (video) | **30fps** (GStreamer kmssink, MJPEG → NV12 → DRM plane) |
| Display (blocking) | **60fps** (ustreamer MPP blocking mode with FreeType) |
| Preview window | **60fps** (hardware-scaled in MPP encoder) |
| Blocking composite | **~0.5ms** per frame overhead |
| Audio mute/unmute | **INSTANT** (volume element mute property) |
| ustreamer MJPEG stream | **~60fps** (MPP hardware encoding at 4K) |
| OCR latency | **100-200ms** capture + **250-400ms** inference |
| VLM latency | **~0.9s per frame** (FastVLM-1.5B, smarter than 0.5B) |
| VLM model load | **~13s** (once at startup) |
| Snapshot capture | **~150ms** (4K JPEG download) |
| OCR image size | 960x540 (downscaled from 4K for speed) |
| ustreamer quality | 80% JPEG (MPP encoder) |
| Animation start | **0.3s** (fast blocking response) |
| Animation end | **0.25s** (fast unblocking) |

**FPS Tracking:**
- GStreamer identity element with pad probe counts frames
- FPS logged every 60 seconds via health monitor
- Warning logged if FPS drops below 25

## ustreamer-patched (NV12 + MPP Hardware Encoding)

We use a patched version of ustreamer from `garagehq/ustreamer` that adds:
- **NV12/NV16/NV24 format support** for RK3588 HDMI-RX devices
- **MPP hardware JPEG encoding** using RK3588 VPU (~60fps at 4K!)
- **Blocking mode system** with FreeType TrueType rendering for ad blocking overlays
- **Extended timeouts** for RK3588 HDMI-RX driver compatibility
- **Multi-worker MPP support** (4 parallel encoders optimal)
- **Cache sync fix** for DMA-related visual artifacts
- **Thread-safe FreeType** mutex for multi-worker encoding

**Why patched ustreamer?**
The stock PiKVM ustreamer doesn't support NV12 format or RK3588 hardware encoding.
Our fork adds NV12→JPEG encoding via Rockchip MPP (Media Process Platform) that
achieves ~60fps on 4K input with minimal CPU usage.

**Dynamic Format Detection:**
Minus automatically probes the V4L2 device to detect its current format and resolution. Supported formats:
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
- `src/ustreamer/encoders/mpp/encoder.c` - MPP hardware JPEG encoder with cache sync, blocking composite
- `src/libs/capture.c` - NV12/NV16/NV24 format support, extended timeouts
- `src/libs/blocking.c` - FreeType text rendering, NV12 compositing, thread-safe mutex
- `src/ustreamer/http/server.c` - Blocking API endpoints (`/blocking`, `/blocking/set`, `/blocking/background`)
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

The `audiotestsrc wave=silence` provides a silent keepalive that prevents pipeline stalls when the HDMI source has no audio (between songs, during video silence, etc.).

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

**OCR (Primary - Authoritative):**
- Triggers blocking immediately on 1 detection
- Stops blocking after 3 consecutive no-ads (`OCR_STOP_THRESHOLD`)
- **Authoritative for stopping** when OCR triggered the block
- Tracks `last_ocr_ad_time` for VLM context

**VLM (Secondary - Anti-Waffle Protected):**
- Uses sliding window of last 45 seconds of VLM decisions (`vlm_history_window`)
- Only triggers blocking alone if 80%+ of recent decisions are "ad" (`vlm_start_agreement`)
- Hysteresis: needs 90% agreement to START (80% + 10% boost for state change)
- Minimum 4 decisions in window before VLM can act (`vlm_min_decisions`)
- 8-second cooldown after state changes prevents rapid flip-flopping (`vlm_min_state_duration`)
- **Sliding window only for starting** - stopping uses simple consecutive count

**Sliding Window Parameters:**
| Parameter | Value | Purpose |
|-----------|-------|---------|
| `vlm_history_window` | 45s | How far back to look at VLM decisions |
| `vlm_min_decisions` | 4 | Minimum decisions needed before acting |
| `vlm_start_agreement` | 80% | Agreement threshold to start blocking |
| `vlm_hysteresis_boost` | 10% | Extra agreement needed to change state |
| `vlm_min_state_duration` | 8s | Cooldown after VLM state change |

**Transition Frame Detection:**
When blocking is active, black/solid-color frames are detected as transitions between ads and held in blocking state to prevent premature unblocking and re-blocking flicker. The `_is_transition_frame()` method analyzes:
- Mean brightness < 30 with low std deviation → black screen
- Low std deviation across frame → solid color
- >95% pixels within 20 values of median → uniform/static

**Starting Blocking:**
1. OCR detects ad → blocking starts immediately (unless home screen detected)
2. VLM detects ad (no OCR) → needs 80%+ agreement in sliding window (4+ decisions)
3. VLM with recent OCR → trusted, triggers blocking
4. Home screen detection suppresses both OCR and VLM blocking on streaming interfaces

**Stopping Blocking:**
1. **If OCR triggered** (source=ocr or both): OCR says stop (3 no-ads) → ends immediately (~2-3s)
2. **If VLM triggered alone** (source=vlm): VLM says stop (2 no-ads) → ends (~4s after ad ends)
3. VLM history cleared on stop → prevents immediate re-trigger
4. VLM stop uses simple consecutive count, NOT sliding window (for responsiveness)

**Why This Design:**
- VLM sliding window prevents erratic false-positive blocking when acting alone
- OCR is authoritative for stopping OCR-triggered blocks (fast unblock)
- VLM-triggered blocks require VLM to confirm ad ended (since OCR never saw it)
- Clearing VLM history on stop prevents "waffle memory" from causing re-triggers
- VLM stopping uses simple consecutive count (not sliding window) for responsiveness

**Anti-flicker:**
- Minimum 3s blocking duration (`MIN_BLOCKING_DURATION`)
- VLM history cleared on stop prevents false re-triggers
- Transition frame detection holds blocking through black screens between ads

## Blocking Overlay

When ads are detected, the screen shows a full blocking overlay **rendered at 60fps via ustreamer's native MPP blocking mode**:
- **Pixelated Background**: Blurred/pixelated version of the screen from ~6 seconds before the ad
- **Header**: `BLOCKING (OCR)`, `BLOCKING (VLM)`, or `BLOCKING (OCR+VLM)`
- **Spanish vocabulary**: Random intermediate-level word with translation
- **Example sentence**: Shows the word in context
- **Rotation**: New vocabulary every 11-15 seconds
- **Ad Preview Window**: Live preview of the blocked ad in bottom-right corner (60fps!)
- **Debug Dashboard**: Stats overlay in bottom-left corner

**Multi-color Text Per Line:**
- **Purple** - Spanish word (IBM Plex Mono Bold font)
- **White** - Header and translation (DejaVu Sans Bold font)
- **Gray** - Pronunciation and example sentence (DejaVu Sans Bold font)

**Font Configuration:**
- `FONT_PATH_VOCAB_PRIMARY` = DejaVu Sans Bold (vocabulary text, centered)
- `FONT_PATH_WORD_PRIMARY` = IBM Plex Mono Bold (Spanish word, purple)
- `FONT_PATH_STATS_PRIMARY` = IBM Plex Mono Regular (debug stats, monospace)

**Rendering Pipeline:**
All overlay rendering is done inside ustreamer's MPP encoder, NOT GStreamer:
1. `ad_blocker.py` captures pre-ad frame and creates pixelated NV12 background
2. Background uploaded via `POST /blocking/background` (async, non-blocking)
3. Text and preview configured via `GET /blocking/set`
4. FreeType renders TrueType fonts directly to NV12 planes at encoder resolution
5. Composite runs at 60fps with ~0.5ms overhead per frame

**Pixelated Background:**
Instead of a plain black background, the blocking overlay shows a heavily pixelated (20x downscale) and darkened (60% brightness) version of what was on screen before the ad appeared. This provides visual context while clearly indicating blocking is active.

Implementation (`src/ad_blocker.py`):
- Rolling 6-second snapshot buffer (3 frames at 2-second intervals)
- Uses oldest frame when blocking starts (ensures pre-ad content)
- OpenCV pixelation: downscale by 20x, upscale with INTER_NEAREST
- Converted to NV12 and uploaded via `/blocking/background` POST API
- Upload runs in background thread for non-blocking operation

**Preview Window:**
Unlike the old GStreamer approach (limited to ~4fps), the ustreamer blocking mode provides:
- Full 60fps live preview of the blocked ad
- Hardware-accelerated scaling in the MPP encoder
- Automatic resolution handling (works at 1080p, 2K, 4K)

**Web UI Toggles:** Ad Preview Window and Debug Dashboard toggleable via Settings (both default ON)

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

**FastVLM-1.5B** on Axera LLM 8850 NPU:
- Smarter than 0.5B with fewer false positives on streaming interfaces
- **~0.9s** inference time
- **~13s** model load time (once at startup)
- Uses Python axengine + transformers tokenizer
- Home screen detection provides additional safety net

```
/home/radxa/axera_models/FastVLM-1.5B/
├── fastvlm_ax650_context_1k_prefill_640_int4/  # LLM decoder models
│   ├── image_encoder_512x512.axmodel           # Vision encoder
│   ├── llava_qwen2_p128_l*.axmodel             # 28 decoder layers
│   └── model.embed_tokens.weight.npy           # Embeddings (float32)
├── fastvlm_tokenizer/                           # Tokenizer files
└── utils/                                       # LlavaConfig and InferManager
```

**Why FastVLM-1.5B instead of 0.5B?**
| Aspect | FastVLM-0.5B | FastVLM-1.5B |
|--------|--------------|--------------|
| Inference Time | 0.7s | 0.9s |
| False Positive Rate | ~88% on home screens | ~36% on home screens |
| Intelligence | Basic | **Much smarter** |
| Parameters | 0.5B | **1.5B** |

## Dependencies

```bash
# System packages
sudo apt install -y imagemagick ffmpeg curl v4l-utils

# GStreamer and plugins for video pipeline
sudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-rockchip1 \
  gir1.2-gst-plugins-base-1.0 \
  libgstreamer1.0-dev

# Build ustreamer with MPP hardware encoding and FreeType fonts
sudo apt install -y librockchip-mpp-dev libfreetype-dev libjpeg-dev libevent-dev
git clone https://github.com/garagehq/ustreamer.git /home/radxa/ustreamer-garagehq
cd /home/radxa/ustreamer-garagehq && make WITH_MPP=1
cp ustreamer /home/radxa/ustreamer-patched

# Fonts for blocking overlay
sudo apt install -y fonts-dejavu-core fonts-ibm-plex

# Python dependencies
pip3 install --break-system-packages \
  pyclipper shapely numpy opencv-python \
  pexpect PyGObject flask requests androidtv \
  rknnlite  # RKNN NPU runtime for OCR (may need Rockchip's pip repo)
```

**Note:** The `rknnlite` package is provided by Rockchip and may need to be installed from their SDK or a custom repository. On the Radxa board with NPU support, it may already be pre-installed.

**Axera NPU (for VLM):**
The FastVLM-1.5B model runs on the Axera LLM 8850 NPU. Required Python packages:
```bash
pip3 install --break-system-packages axengine transformers ml_dtypes
```
The `axengine` package requires the Axera AXCL runtime to be installed - see the Axera documentation.

## Troubleshooting

**ustreamer fails to start:**
```bash
fuser -k /dev/video0  # Kill processes using device
pkill -9 ustreamer    # Kill orphaned ustreamer
```

**VLM not loading:**
- Check Axera card: `axcl_smi`
- Verify model files exist in `/home/radxa/axera_models/FastVLM-1.5B/`
- Ensure Python dependencies: `pip3 show axengine transformers ml_dtypes`

**OCR not detecting:**
- Test snapshot: `curl http://localhost:9090/snapshot -o test.jpg`
- Check HDMI: `v4l2-ctl -d /dev/video0 --query-dv-timings`

**Display issues:**
- Check DRM plane: `modetest -M rockchip -p | grep -A5 "plane\[72\]"`
- Verify connector: `modetest -M rockchip -c | grep HDMI`

## CRITICAL: Blocking Mode Architecture

**NEVER REVERT TO GSTREAMER TEXTOVERLAY FOR BLOCKING OVERLAYS.**

The blocking overlay system uses ustreamer's native MPP blocking mode (`/blocking/*` API), NOT GStreamer's input-selector or textoverlay. This is a one-way migration - we only move forward.

**Current Architecture:**
- Simple GStreamer pipeline with `queue max-size-buffers=3 leaky=downstream` for smooth video
- All blocking compositing (background, preview, text) done in ustreamer's MPP encoder at 60fps
- Control via HTTP API: `/blocking/set`, `/blocking/background`
- FreeType TrueType font rendering:
  - **IBM Plex Mono Bold** for Spanish word (purple, centered)
  - **DejaVu Sans Bold** for vocabulary text (white/gray, centered)
  - **IBM Plex Mono Regular** for stats dashboard (bottom-left, monospace)
- Per-line multi-color text matching web UI aesthetic (see AESTHETICS.md)
- Thread-safe with mutex protection for 4 parallel MPP encoder workers

**Resolution Flexibility:**
The blocking system automatically handles resolution mismatches:
- API calls may specify 4K dimensions (3840x2160)
- Encoder may output at 1080p due to `--encode-scale native`
- Preview dimensions are scaled proportionally to fit
- Positions are clamped to valid ranges
- All coordinates aligned to even values for NV12

**Thread Safety:**
FreeType is NOT thread-safe. With 4 parallel MPP encoder workers, a `pthread_mutex_t _ft_mutex` serializes all FreeType calls in the composite function to prevent crashes. Without this, concurrent FT_Set_Pixel_Sizes/FT_Load_Glyph calls corrupt FreeType's internal state.

**Why NOT GStreamer textoverlay:**
- Caused pipeline stalls every ~12 seconds
- NV12 format incompatibility issues
- 4K→1080p resolution mismatch problems
- gdkpixbufoverlay limited to ~4fps for preview updates
- Complex input-selector switching logic

**Key files:**
- `ustreamer-garagehq/src/libs/blocking.c` - NV12 compositing with FreeType, mutex protection
- `ustreamer-garagehq/src/libs/blocking.h` - Blocking mode API
- `src/ad_blocker.py` - Python client using blocking API

## ustreamer Overlay and Blocking API

**Notification Overlay** (for Fire TV setup messages, etc.):
- `GET /overlay` - Get current overlay configuration
- `GET /overlay/set?params` - Set overlay configuration

| Parameter | Description |
|-----------|-------------|
| `text` | Text to display (URL-encoded, supports newlines) |
| `enabled` | `true` or `1` to enable overlay |
| `position` | 0=top-left, 1=top-right, 2=bottom-left, 3=bottom-right, 4=center |
| `scale` | Text scale factor (1-10) |
| `color_y`, `color_u`, `color_v` | Text color in YUV |
| `bg_enabled` | Enable background box |
| `bg_alpha` | Background transparency (0-255) |
| `clear` | Clear overlay |

**Example:**
```bash
curl "http://localhost:9090/overlay/set?text=LIVE&position=1&scale=3&enabled=true"
curl "http://localhost:9090/overlay/set?clear=true"
```

**Blocking Mode** (for ad blocking overlays):

**Blocking Mode Endpoints:**
- `GET /blocking` - Get current config (enabled, preview, colors, etc.)
- `GET /blocking/set?enabled=true&text_vocab=...&preview_enabled=true` - Configure
- `POST /blocking/background` - Upload pixelated NV12 background (width*height*1.5 bytes)

**Multi-color text auto-detection:** Lines starting with `[` → white (header), `(` → gray (pronunciation), `=` → white (translation), `"` → gray (example), other → purple (Spanish word)

## Health Monitoring

The health monitor (`src/health.py`) runs in a background thread and checks:

| Subsystem | Check | Recovery |
|-----------|-------|----------|
| HDMI signal | v4l2-ctl --query-dv-timings | Show "NO SIGNAL" overlay, mute audio |
| No HDMI at startup | check_hdmi_signal() | Show bouncing "NO SIGNAL" screensaver |
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

Minus includes a lightweight Flask-based web UI for remote monitoring and control, accessible via Tailscale from desktop or mobile devices.

**Features:**
- **Live video feed** - MJPEG stream proxied from ustreamer (CORS bypass)
- **Status display** - Blocking state, FPS, HDMI info, uptime
- **Pause controls** - 1/2/5/10 minute presets to pause ad blocking
- **Detection history** - Recent OCR/VLM detections with timestamps
- **Settings** - Toggle preview window and debug dashboard
- **Log viewer** - Collapsible log output for debugging

**Key API Routes:**
- `GET /`, `/api/status`, `/api/detections`, `/api/logs`
- `POST /api/pause/N`, `/api/resume`
- `GET/POST /api/preview/*`, `/api/debug-overlay/*`
- `POST /api/test/trigger-block`, `/api/test/stop-block`
- `GET /stream`, `/snapshot` - Proxy to ustreamer

**Test API Endpoints:**
For development and testing ad blocking without waiting for real ads:
```bash
# Trigger blocking for 20 seconds (max 60)
curl -X POST -H "Content-Type: application/json" \
  -d '{"duration": 20, "source": "ocr"}' \
  http://localhost:8080/api/test/trigger-block

# Stop blocking immediately
curl -X POST http://localhost:8080/api/test/stop-block
```

Parameters for trigger-block:
- `duration`: seconds to block (default: 10, max: 60)
- `source`: detection source - 'ocr', 'vlm', 'both', or 'default'

Test mode prevents the detection loop from canceling the blocking, allowing full testing of pixelated background, animations, and audio muting.

**Access URLs:**
- Local: `http://localhost:8080`
- Tailscale: `http://<tailscale-hostname>:8080`
- Direct stream: `http://<hostname>:9090/stream`

**Security:**
- No authentication (relies on Tailscale network security)
- Read-mostly API with minimal attack surface
- Binds to 0.0.0.0 for remote access

## VLM Training Data Collection

Minus automatically collects training data for future VLM improvements, organized by type:

**Screenshot directories:**
- `screenshots/ads/` - OCR-detected ads (rate limited, hash dedup)
- `screenshots/non_ads/` - User paused = false positives
- `screenshots/vlm_spastic/` - VLM uncertainty cases (detected 2-5x then changed)
- `screenshots/static/` - Static screen suppression

## Fire TV Remote Control

Minus can control Fire TV devices over WiFi via ADB for ad skipping and playback control.

**Auto-setup:** Fire TV is automatically discovered and connected 5 seconds after Minus starts. First-time connection requires approving the ADB authorization dialog on the TV screen (OCR detects when it appears). ADB keys are saved for future connections.

**Features:**
- Auto-discovery of Fire TV devices on local network
- Verification that discovered device is actually a Fire TV
- ADB key generation and persistent storage for pairing
- Auto-reconnect on connection drops
- Full remote control: play, pause, select, back, d-pad, etc.
- Async-compatible interface

**Requirements:**
- Fire TV must have ADB debugging enabled
- First connection requires approving RSA key on TV screen
- Both devices must be on the same WiFi network

**Enabling ADB on Fire TV:** Settings > My Fire TV > Developer Options > ADB Debugging ON (enable Dev Options first via About > click device name 7x)

**Testing:** `python3 test_fire_tv.py [--setup|--interactive|--scan|IP]`

**Commands:** Navigation (up/down/left/right/select/back/home), Media (play/pause), Volume, Power

**Usage:** `quick_connect()` → `skip_ad()` / `go_back()` → `disconnect()`

**Setup States:** `idle` → `scanning` → `waiting_adb_enable` → `waiting_auth` → `connected`

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
- VLM uses Python axengine for inference (not pexpect/C++ binary)
- Both NPUs run in parallel without resource contention
- No X11 required - pure DRM/KMS display
- Color correction via GStreamer videobalance (not V4L2 controls)
- Health monitor runs every 5 seconds in background thread
- VLM frame files use PID-based naming to avoid permission conflicts
- Snapshots scaled to 960x540 before OCR (model uses 960x960 anyway, smaller = faster)
- ustreamer quality set to 80% for balance of quality and CPU load
- FPS tracked via GStreamer identity element with pad probe
- Startup cleanup removes stale frame files and kills orphaned processes
- Background upload is async to prevent blocking main thread
- Animation times optimized: 0.3s start, 0.25s end for fast response
- DYNAMIC_COOLDOWN reduced to 0.5s for faster ad detection

## Building Executable

```bash
pip3 install pyinstaller
pyinstaller minus.spec
# Output: dist/minus
```

Note: Models are external and must be present at runtime.

## Testing

The project includes a comprehensive test suite for all extracted modules.

**Running Tests:**
```bash
python3 tests/test_modules.py  # 106 tests

# Or with pytest (if installed)
python3 -m pytest tests/test_modules.py -v
```

**Test Coverage:**

| Module | Test Class | Tests |
|--------|------------|-------|
| `src/vocabulary.py` | TestVocabulary | Format validation, content checks, common words |
| `src/config.py` | TestConfig | Dataclass defaults, custom values |
| `src/skip_detection.py` | TestSkipDetection | Pattern matching, countdown parsing, edge cases |
| `src/screenshots.py` | TestScreenshots | Deduplication, file saving, truncation |
| `src/console.py` | TestConsole | Console blanking/restore commands |
| `src/capture.py` | TestCapture | Snapshot capture, cleanup |
| `src/drm.py` | TestDRM | DRM probing, fallback values |
| `src/v4l2.py` | TestV4L2 | V4L2 format detection, error handling |
| `src/overlay.py` | TestOverlay | NotificationOverlay, positions, show/hide |
| `src/health.py` | TestHealth | HealthMonitor, HealthStatus, HDMI detection |
| `src/fire_tv.py` | TestFireTV | Controller, key codes, device detection |
| `src/vlm.py` | TestVLM | VLMManager, response parsing, 4-tuple returns |
| `src/ocr.py` | TestOCR | Keywords, exclusions, terminal detection |
| `src/webui.py` | TestWebUI | Flask routes, API endpoints |
| Integration | TestIntegration | Cross-module tests |

**Test Design:**
- Tests are self-contained with temporary directories
- Mock subprocess calls to avoid system dependencies
- Fallback to manual test runner if pytest not installed
- All 106 tests should pass on a clean system

## Module Structure

The codebase has been refactored from monolithic files into smaller, focused modules:

**Extracted from `minus.py`:**
- `src/console.py` - Console blanking functions (`blank_console`, `restore_console`)
- `src/drm.py` - DRM probing (`probe_drm_output`)
- `src/v4l2.py` - V4L2 probing (`probe_v4l2_device`)
- `src/config.py` - Configuration dataclass (`MinusConfig`)
- `src/capture.py` - Snapshot capture (`UstreamerCapture`)
- `src/screenshots.py` - Screenshot management (`ScreenshotManager`)
- `src/skip_detection.py` - Skip button detection (`check_skip_opportunity`)

**Extracted from `ad_blocker.py`:**
- `src/vocabulary.py` - Spanish vocabulary list (`SPANISH_VOCABULARY`)

**Benefits:**
- Easier to test individual components
- Better code organization and discoverability
- Reduced file sizes (minus.py ~1700 lines, ad_blocker.py ~950 lines)
- Clear separation of concerns

## Known Issues / Fixed

### GStreamer Video Path Overlay (Historical - FIXED)

**Previous problem:** Adding a `textoverlay` element to the GStreamer video path caused pipeline stalls every ~12 seconds due to NV12 format incompatibility and 4K→1080p resolution mismatch.

**Solution implemented:** Text overlay is now rendered directly in ustreamer's MPP encoder via the blocking mode API. This:
- Composites directly on NV12 frames in the encoder
- Has minimal CPU impact (~0.5ms per frame)
- Works at any resolution without GStreamer pipeline changes
- Supports pixelated background, live preview window, and text overlays
- Uses FreeType for proper TrueType font rendering

### Memory Management (Fixed)

**Issue:** Long-running sessions (several hours) could accumulate memory due to RKNN inference output buffers not being explicitly released.

**Solution implemented:**
- RKNN inference outputs are now explicitly copied and dereferenced in `src/ocr.py`
- Periodic `gc.collect()` runs every 100 OCR frames and every 50 VLM frames
- Health monitor triggers emergency cleanup at 90% memory usage
- Frame buffers (`prev_frame`, `vlm_prev_frame`) are cleared during memory critical events

**ThreadPoolExecutor fix (Jan 2026):**
- **CRITICAL:** The OCR worker was creating a new `ThreadPoolExecutor` on every iteration, causing massive file descriptor and memory leaks (~12GB after 12 hours)
- Fixed by creating a single `ocr_executor` before the loop and reusing it
- Symptom: "Too many open files" errors, display goes blank, memory exhaustion

**Memory monitoring:**
- Health monitor checks memory every 5 seconds
- Warning logged at 80% usage
- Critical cleanup triggered at 90% usage

### Fire TV Setup (Fixed)

**Status:** Fire TV auto-setup is ENABLED with notification overlays working via ustreamer API.

**Startup timing:**
- Fire TV setup starts 5 seconds after service start (runs in parallel with VLM loading)
- Total time from start to connection: ~13 seconds (5s delay + ~8s scan/connect)

**Bug fixed:** Auth retry interval was 3 seconds, causing multiple auth dialogs on the TV before user could respond. Fixed to 35 seconds (longer than AUTH_TIMEOUT of 30s) in `fire_tv_setup.py`.
