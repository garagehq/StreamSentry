# Stream Sentry - Development Notes

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
                              │                 │  │  plane-id=72  │  │
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
- Both ML workers run concurrently on separate NPUs
- Display runs independently at 30fps without any stutter

## Key Files

| File | Purpose |
|------|---------|
| `stream_sentry.py` | Main entry point - orchestrates everything |
| `stream_sentry.spec` | PyInstaller spec for building executable |
| `src/ad_blocker.py` | GStreamer video pipeline with input-selector, Spanish vocab |
| `src/audio.py` | GStreamer audio passthrough with mute control |
| `src/ocr.py` | PaddleOCR on RKNN NPU, keyword detection |
| `src/vlm.py` | Qwen3-VL-2B on Axera NPU |
| `src/health.py` | Unified health monitor for all subsystems |
| `install.sh` | Install as systemd service |
| `uninstall.sh` | Remove systemd service |
| `stop.sh` | Graceful shutdown script |
| `stream-sentry.service` | systemd service file |
| `screenshots/ocr/` | Ad detection screenshots (auto-truncated) |

## Running

```bash
python3 stream_sentry.py
```

**Command-line options:**
```bash
--device /dev/video1      # Custom capture device
--ocr-timeout 1.5         # OCR timeout in seconds (default: 1.5)
--max-screenshots 100     # Keep N recent screenshots (default: 50, 0=unlimited)
--check-signal            # Just check HDMI signal and exit
--connector-id 215        # DRM connector ID (default: 215)
--plane-id 72             # DRM plane ID (default: 72)
```

## Performance

| Metric | Value |
|--------|-------|
| Display (video) | ~25fps (GStreamer kmssink, NV12 → plane 72) |
| Display (blocking) | 2-3fps (videotestsrc + textoverlay) |
| Ad blocking switch | **INSTANT** (input-selector, no restart) |
| Audio mute/unmute | **INSTANT** (volume element mute property) |
| OCR latency | 300-500ms per frame |
| VLM latency | 1.3-1.5s per frame |
| VLM model load | ~40s (once at startup) |
| Snapshot capture | ~150ms (non-blocking, scaled to 720p) |
| ustreamer quality | 75% JPEG |

**FPS Tracking:**
- GStreamer identity element with pad probe counts frames
- FPS logged every 60 seconds via health monitor
- Warning logged if FPS drops below 25

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

Example:
```
BLOCKING (OCR)

aprovechar
= to take advantage of

Hay que aprovechar el tiempo.
```

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
- Location: `/tmp/stream_sentry.log`
- Max 5MB per log file
- Keeps 3 backup files (stream_sentry.log.1, .2, .3)

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

# Build ustreamer
git clone https://github.com/pikvm/ustreamer.git
cd ustreamer && make -j$(nproc) && sudo cp ustreamer /usr/local/bin/

# Python
pip3 install pyclipper shapely numpy opencv-python pexpect PyGObject
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
| ustreamer | HTTP HEAD to :9090/snapshot | Restart ustreamer process |
| Output FPS | GStreamer pad probe | Log warning if < 25fps |
| VLM | Consecutive timeouts < 5 | Degrade to OCR-only, retry VLM after 30s |
| Memory | Usage < 90% | Force GC, clean old screenshots |
| Disk | Free > 500MB | Log warning |

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

## Running as a Service

```bash
# Install
sudo ./install.sh

# View logs
journalctl -u stream-sentry -f

# Stop
sudo systemctl stop stream-sentry
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
- Snapshots scaled to 720p before OCR (model uses 960x960 anyway)
- ustreamer quality set to 75% to reduce CPU load
- FPS tracked via GStreamer identity element with pad probe
- Startup cleanup removes stale frame files and kills orphaned processes

## Building Executable

```bash
# Install PyInstaller
pip3 install pyinstaller

# Build standalone executable
pyinstaller stream_sentry.spec

# Output: dist/stream_sentry
```

Note: Models are external and must be present at runtime.
