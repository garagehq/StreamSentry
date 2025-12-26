# Stream Sentry

HDMI passthrough with real-time ML-based ad detection and blocking using dual NPUs:
- **PaddleOCR** on RK3588 NPU (~400ms per frame)
- **Qwen3-VL-2B** on Axera LLM 8850 NPU (~1.5s per frame)
- **Audio passthrough** with auto-mute during ads
- **Spanish vocabulary practice** during ad blocks!

## Overview

Stream Sentry captures video from HDMI-RX, displays it via GStreamer kmssink at 30fps, while running two ML workers concurrently to detect ads. When ads are detected, **instantly** switches to a blocking overlay with Spanish vocabulary practice.

**Key features:**
- **Instant ad blocking** - GStreamer input-selector switches in ~1 frame (no black screen!)
- **Audio passthrough** - HDMI audio with instant mute during ads, silent keepalive prevents stalls
- **Dual NPU inference** - OCR and VLM run concurrently on separate NPUs
- **No X11 required** - Pure DRM/KMS display via kmssink
- **Spanish learning** - Practice vocabulary while ads are blocked
- **30fps display** - Smooth passthrough without stutter
- **Set and forget** - systemd service, health monitoring, automatic recovery

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
cd /home/radxa/StreamSentry

# Install dependencies (first time only)
sudo apt install -y imagemagick ffmpeg curl

# Build ustreamer (first time only)
git clone https://github.com/pikvm/ustreamer.git
cd ustreamer && make -j$(nproc) && sudo cp ustreamer /usr/local/bin/

# Python packages
pip3 install pyclipper shapely numpy opencv-python pexpect PyGObject

# Run everything
python3 stream_sentry.py

# Check HDMI signal only
python3 stream_sentry.py --check-signal
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

## Performance

| Metric | Value |
|--------|-------|
| Display framerate | ~25 fps (video), 2-3 fps (blocking overlay) |
| Ad blocking switch | **INSTANT** (~1 frame) |
| Snapshot capture | ~150ms (non-blocking, scaled to 720p) |
| OCR latency | 300-500ms per frame |
| VLM latency | 1.3-1.5s per frame |
| VLM model load | ~40s (once at startup) |
| JPEG quality | 75% (optimized for CPU) |

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
- **Header**: `BLOCKING (OCR)`, `BLOCKING (VLM)`, or `BLOCKING (OCR+VLM)`
- **Spanish word**: Random intermediate-level vocabulary
- **Translation**: English meaning
- **Example**: Sentence using the word
- **Rotation**: New vocabulary every 11-15 seconds

Example display:
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

## Ad Keywords Detected

**Exact phrases** (match anywhere):
- skip ad, skip ads, skipad, skipads
- sponsored, advertisement, ad break
- shop now, buy now, promoted

**Whole words:**
- skip, sponsor

## Log Output

```
2025-12-24 02:32:47 [I] Starting Stream Sentry...
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
stream-sentry/
├── stream_sentry.py      # Main entry point
├── stream_sentry.spec    # PyInstaller build spec
├── src/
│   ├── ocr.py            # PaddleOCR on RKNN NPU
│   ├── vlm.py            # Qwen3-VL-2B on Axera NPU
│   ├── ad_blocker.py     # GStreamer video pipeline with input-selector
│   ├── audio.py          # GStreamer audio passthrough with mute control
│   └── health.py         # Health monitor for all subsystems
├── install.sh            # Install as systemd service
├── uninstall.sh          # Remove systemd service
├── stop.sh               # Graceful shutdown script
├── stream-sentry.service # systemd service file
├── models/
│   └── paddleocr/        # RKNN models (or symlink)
├── screenshots/
│   └── ocr/              # Ad detection screenshots (auto-truncated)
├── README.md             # This file
├── CLAUDE.md             # Development notes
└── AUDIO.md              # Audio implementation details
```

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

## Dependencies

```bash
# System packages
sudo apt install -y imagemagick ffmpeg curl libevent-dev libjpeg-dev libbsd-dev

# Build ustreamer from source
git clone https://github.com/pikvm/ustreamer.git
cd ustreamer && make -j$(nproc) && sudo cp ustreamer /usr/local/bin/

# Python packages
pip3 install pyclipper shapely numpy opencv-python pexpect PyGObject
```

## Troubleshooting

**No HDMI signal:**
When started without HDMI input, Stream Sentry displays "NO HDMI INPUT" on screen
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

Stream Sentry can run as a systemd service for 24/7 unattended operation:

```bash
# Install as systemd service
sudo ./install.sh

# View logs
journalctl -u stream-sentry -f

# Stop service
sudo systemctl stop stream-sentry

# Uninstall
sudo ./uninstall.sh
```

The service automatically:
- Starts on boot
- Restarts on crashes (5 attempts per 5 minutes)
- Disables X11/display managers to avoid conflicts

## Health Monitoring

Stream Sentry includes a unified health monitor that runs in the background:

**What it monitors:**
- HDMI signal (detects unplug/replug, shows "NO SIGNAL" message)
- ustreamer health (HTTP health check, not just PID)
- Output FPS (logged every 60s, warning if < 25 fps)
- VLM/OCR health (consecutive timeout detection)
- Memory usage (warning at 80%, critical at 90%)
- Disk space (warning below 500MB)

**Automatic recovery:**
- HDMI signal lost → Shows placeholder, mutes audio
- HDMI signal restored → Restarts capture, unmutes audio
- ustreamer stall → Restarts ustreamer process
- VLM failure → Degrades to OCR-only mode, attempts VLM restart
- Critical memory → Triggers garbage collection, cleans old screenshots

**Graceful degradation:**
- If VLM fails repeatedly (5+ consecutive timeouts), switches to OCR-only mode
- VLM restart is attempted after 30 seconds
- OCR continues working even if VLM is disabled
- 30-second startup grace period before health checks begin

## Housekeeping

**Log File:**
- Location: `/tmp/stream_sentry.log`
- Max 5MB per log file
- Keeps 3 backup files (stream_sentry.log.1, .2, .3)

**Screenshot Truncation:**
- Keeps only last 50 screenshots by default
- Configurable via `--max-screenshots`

**Audio Error Recovery:**
- Watchdog checks every 3 seconds, restarts if stalled for 6+ seconds
- Exponential backoff for restart attempts (1s → 2s → 4s → ... → 60s max)
- No maximum restart limit - always tries to recover
- Backoff resets after 5 seconds of sustained audio flow

## Building Executable

Stream Sentry can be compiled into a standalone executable using PyInstaller:

```bash
# Install PyInstaller
pip3 install pyinstaller

# Build executable
pyinstaller stream_sentry.spec

# Output will be in dist/stream_sentry
```

**Note:** The executable still requires external model files at runtime:
- PaddleOCR models in standard location
- VLM models in `/home/radxa/axera_models/Qwen3-VL-2B/`

## License

MIT
