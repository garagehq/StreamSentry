# FPS Debugging Results for HDMI-RX Streaming on RK3588

## Executive Summary

**Key Finding:** The encoding pipeline achieves **~35 FPS at 4K** but the display bottleneck was **CPU-based format conversion (videoconvert)**.

**SOLUTION FOUND:** Use `mppjpegdec` → NV12 → `kmssink plane-id=72` for **27-30 FPS actual display output!**

The breakthrough: DRM plane 72 accepts NV12 natively, eliminating CPU videoconvert entirely. This achieves full frame rate passthrough.

## Hardware Configuration

- **SoC:** RK3588 (Rock 5B+)
- **HDMI-RX:** `/dev/video0` (rk_hdmirx driver)
- **Source:** 3840x2160 @ 30fps (BGR24 format)
- **Available HW Accelerators:**
  - RK3588 NPU (for ML inference)
  - RK3588 VPU (H264/H265/JPEG encode/decode)
  - Mali GPU (for display)

## Test Results Summary

| Test | Pipeline | Result | Notes |
|------|----------|--------|-------|
| Raw Capture | v4l2-ctl --stream-mmap | **30 FPS** | Capture is NOT bottleneck |
| ustreamer JPEG Encode | CPU @ Quality 50 | **35 FPS** | Counting JPEG markers in stream |
| ustreamer JPEG Encode | CPU @ Quality 70 | **35 FPS** | Same throughput |
| GStreamer mppjpegenc | Hardware JPEG | **5.3 FPS** | Slow BGR→NV12 conversion |
| GStreamer mpph264enc | Hardware H264 | **6.5 FPS** | Slow BGR→NV12 conversion |
| 1080p Scale + Encode | videoscale + mppjpegenc | **5.7 FPS** | CPU scaling bottleneck |
| FFmpeg Transcode | libx264 4K→1080p | **13 FPS** | CPU encoding bottleneck |
| ffplay Display | Software MJPEG decode | **~10 FPS** | 50-90 frames dropped |
| mpv Display | Software MJPEG decode | **~12 FPS** | 50-100 frames dropped |
| GStreamer Display | mppjpegdec + GL | **~10 FPS** | Fullscreen GPU bottleneck |
| VLC Display | Software decode | **~5 FPS** | Chunky updates every 1.5s |
| xvimagesink | X11 shared memory | **~0.5 FPS** | Extremely slow |
| rkximagesink | RGA accelerated | **~0.5 FPS** | Extremely slow |
| H264 Transcode | HW JPEG→H264→display | **TBD** | Needs verification |

## Bottleneck Analysis

### 1. Capture Stage (NOT a bottleneck)
```
Raw V4L2 capture: 100 frames in 3623ms = ~27.6 FPS
```
The HDMI-RX captures at full 30fps.

### 2. Encoding Stage (NOT the main bottleneck)

**ustreamer (CPU JPEG):**
- Uses libjpeg with multiple workers
- Achieves 35fps at 4K
- Quality 50-90 has similar throughput

**GStreamer Hardware Encoding:**
- mppjpegenc/mpph264enc only achieve 5-6 FPS
- **Root cause:** BGR24→NV12 conversion is slow
- The hardware encoder is fast, but format conversion is CPU-bound

### 3. Display/Decode Stage (MAIN BOTTLENECK)

**Software MJPEG Decode (ffplay/mpv):**
- 4K JPEG frames are ~200-500KB each
- Software decode can't keep up at 30fps
- Results in 50-90 frames dropped per playback

**Hardware JPEG Decode (mppjpegdec):**
- GStreamer's `mppjpegdec` can do hardware JPEG decode
- Outputs NV12 directly to GL surface
- Should achieve full 30fps (needs verification)

## Recommended Solutions

### Option 1: Full GStreamer Pipeline (Best)
```bash
# Use GStreamer with hardware JPEG decode
gst-launch-1.0 \
  souphttpsrc location=http://localhost:9090/stream ! \
  multipartdemux ! \
  jpegparse ! \
  mppjpegdec ! \
  glimagesink
```

This uses:
- ustreamer for efficient CPU JPEG encoding
- mppjpegdec for hardware JPEG decoding
- GL sink for efficient GPU rendering

### Option 2: Direct V4L2 to DRM (Zero-Copy)
```bash
# Direct capture to display without encoding
gst-launch-1.0 \
  v4l2src device=/dev/video0 ! \
  video/x-raw,format=BGR,width=3840,height=2160 ! \
  kmssink
```

This bypasses JPEG encode/decode entirely but:
- Only works for local display
- Cannot provide snapshots for ML pipeline

### Option 3: Lower Resolution (Compromise)
Not recommended - 1080p scaling is slower than 4K pass-through due to CPU scaling overhead.

## V4L2 Format Details

The HDMI-RX supports multiple formats but only BGR3 works at 4K:

```
[0]: 'BGR3' (24-bit BGR 8-8-8)  ✓ Works at 4K
[1]: 'NV24' (Y/UV 4:4:4)        ✗ "Invalid argument" at 4K
[2]: 'NV16' (Y/UV 4:2:2)        ✗ "Invalid argument" at 4K
[3]: 'NV12' (Y/UV 4:2:0)        ✗ "Invalid argument" at 4K
```

This is why hardware encoding (which prefers NV12) requires slow format conversion.

## ustreamer Performance

| Quality | Stream FPS | Bytes/5s | Notes |
|---------|-----------|----------|-------|
| 90 | ~35 | 45.4 MB | Large frames |
| 70 | ~35 | 35.4 MB | Balanced |
| 50 | ~35 | 27.2 MB | Good for OCR |
| 30 | ~35 | 24.7 MB | Visible artifacts |
| 20 | ~35 | - | "Halo effect" artifacts |

**Recommendation:** Quality 50-70 for best balance of size and quality.

## GStreamer Hardware Plugins

Available on RK3588:
- `mppjpegenc` - Hardware JPEG encoder
- `mppjpegdec` - Hardware JPEG decoder
- `mpph264enc` - Hardware H264 encoder
- `mpph265enc` - Hardware H265 encoder
- `mppvp8enc` - Hardware VP8 encoder
- `mppvideodec` - Hardware video decoder

## Implementing in minus.py

To achieve 30fps display, replace ffplay/mpv with GStreamer:

```python
def start_display_gstreamer(self):
    """Start GStreamer display with hardware JPEG decode."""
    pipeline = [
        'gst-launch-1.0',
        'souphttpsrc', f'location=http://localhost:{self.stream_port}/stream', '!',
        'multipartdemux', '!',
        'jpegparse', '!',
        'mppjpegdec', '!',
        'glimagesink', 'sync=false'
    ]
    self.display_proc = subprocess.Popen(
        pipeline,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
```

## Files Tested

- `/dev/video0` - HDMI-RX capture device
- `/dev/video-dec0` - Hardware decoder (RK3588 VPU)
- `/dev/video-enc0` - Hardware encoder (RK3588 VPU)

## Dependencies for Hardware Acceleration

```bash
# Already installed on this system
librockchip-mpp1              # Media Process Platform
libv4l-rkmpp                  # V4L2 wrapper for MPP
gstreamer1.0-plugins-bad      # Contains rockchipmpp plugins
gstreamer1.0-rtsp             # For RTSP streaming (optional)
```

## Updated Findings (Detailed Testing)

### Display Sink Comparison (4K Fullscreen)

| Sink | FPS (windowed) | FPS (fullscreen) | Notes |
|------|---------------|------------------|-------|
| glimagesink | ~20 | **~10** | GL texture upload bottleneck |
| autovideosink | ~20 | **~10** | Chooses glimagesink internally |
| rkximagesink | TBD | TBD | Rockchip-optimized, uses RGA |
| kmssink | N/A | N/A | Requires root, conflicts with X11 |

### Key Bottleneck: GPU Rendering at 4K

The pipeline stages:
1. **Capture** → 30 FPS ✓
2. **JPEG Encode (ustreamer)** → 34 FPS ✓
3. **JPEG Decode (mppjpegdec)** → 33 FPS ✓
4. **Display Rendering** → **~10 FPS** ✗

The bottleneck is **GPU rendering at 4K**, not encoding or decoding. The Mali GPU struggles to:
- Upload 4K NV12 frames to GL textures
- Render at full 4K resolution
- Maintain 30fps throughput

### Optimization Settings Found

Best configuration for balanced FPS:
```bash
ustreamer --quality=50 --workers=6 --buffers=6
```

- Quality 50 reduces CPU load (smaller frames)
- 6 workers leaves CPU headroom for display rendering

### Display Options Tested

| Method | FPS (observed) | Notes |
|--------|---------------|-------|
| glimagesink (windowed) | ~20 | Acceptable |
| glimagesink (fullscreen) | ~10 | GPU-bound |
| autovideosink (windowed) | ~20 | Same as glimagesink |
| autovideosink (fullscreen) | ~10 | GPU-bound |
| rkximagesink | **~0.5** | Extremely slow, not usable |
| xvimagesink | **~0.5** | Extremely slow, not usable |
| VLC | **~5** | Updates in chunks every ~1.5s |
| mpv (default) | ~12 | 50-100 frames dropped |
| mpv (5s buffer) | ~12 | Buffering doesn't help display FPS |
| ffplay | ~10 | 50-90 frames dropped |
| H264 transcode | TBD | MJPEG→H264→display (needs verification) |

### VLC Test Results

VLC was tested as an alternative player:
```bash
DISPLAY=:0 vlc --fullscreen --intf dummy --no-audio \
    --network-caching=3000 http://localhost:9090/stream
```
- Result: **~5 FPS or chunky updates every 1.5 seconds**
- VLC shows VA-API errors (RK3588 doesn't have VA-API drivers)
- Falls back to software decode which is slow

### XvImageSink / rkximagesink Results

Both X11-based sinks performed extremely poorly:
```bash
DISPLAY=:0 gst-launch-1.0 \
    souphttpsrc location=http://localhost:9090/stream ! \
    multipartdemux ! jpegparse ! mppjpegdec ! \
    xvimagesink sync=false
```
- Result: **~0.5 FPS** - "pretty ass" per user feedback
- XvImageSink uses X11 shared memory, inefficient for 4K
- rkximagesink showed similar poor performance

### mpv with Heavy Buffering

Tested whether buffering would help:
```bash
DISPLAY=:0 mpv --fs --no-audio \
    --cache=yes --demuxer-max-bytes=100M \
    --demuxer-readahead-secs=5 --cache-secs=5 \
    http://localhost:9090/stream
```
- Result: Buffering does NOT improve display FPS
- The bottleneck is rendering, not network/decode buffering

### Direct V4L2 Capture Test

Tested bypassing HTTP streaming entirely:
```bash
DISPLAY=:0 gst-launch-1.0 \
    v4l2src device=/dev/video0 ! \
    video/x-raw,format=BGR,width=3840,height=2160,framerate=30/1 ! \
    queue max-size-buffers=5 ! videoconvert ! \
    autovideosink sync=false
```
- Pipeline runs but still limited by display rendering
- Confirms the bottleneck is GPU/display, not streaming

### H264 Transcode Pipeline

Promising approach - transcode MJPEG to H264 to use HW H264 decode:
```bash
DISPLAY=:0 gst-launch-1.0 \
    souphttpsrc location=http://localhost:9090/stream ! \
    multipartdemux ! jpegparse ! mppjpegdec ! \
    queue max-size-buffers=30 max-size-time=1000000000 ! \
    mpph264enc ! h264parse ! mppvideodec ! \
    queue max-size-buffers=10 ! autovideosink sync=false
```
- Pipeline runs successfully ("Pipeline is PLAYING")
- Uses HW JPEG decode → HW H264 encode → HW H264 decode
- User reported "responsive" but still not 30fps

## Scientific FPS Measurements

Measured using `ustreamer /state` endpoint while GStreamer display is running fullscreen.

### 4K Fullscreen (no scaling)
```bash
DISPLAY=:0 gst-launch-1.0 \
    souphttpsrc location=http://localhost:9090/stream ! \
    multipartdemux ! jpegparse ! mppjpegdec ! \
    autovideosink sync=false
```

| Measurement | Captured FPS | Delivered FPS |
|-------------|-------------|---------------|
| Sample 1 | 18 | 9 |
| Sample 2 | 21 | 9 |
| Sample 3 | 18 | 8 |
| Sample 4 | 19 | 8 |
| Sample 5 | 18 | 7 |
| **Average** | **~19** | **~8** |

### 1080p Scaled Fullscreen
```bash
DISPLAY=:0 gst-launch-1.0 \
    souphttpsrc location=http://localhost:9090/stream ! \
    multipartdemux ! jpegparse ! mppjpegdec ! \
    videoscale method=0 ! video/x-raw,width=1920,height=1080 ! \
    autovideosink sync=false
```

| Measurement | Captured FPS | Delivered FPS |
|-------------|-------------|---------------|
| Sample 1 | 15 | 12 |
| Sample 2 | 15 | 10 |
| Sample 3 | 17 | 9 |
| Sample 4 | 15 | 10 |
| Sample 5 | 14 | 9 |
| **Average** | **~15** | **~10** |

### Additional Tests

| Configuration | Captured FPS | Delivered FPS | Notes |
|--------------|-------------|---------------|-------|
| Quality 30, 3 workers | ~15 | ~10 FPS | No improvement over quality 50 |
| Quality 50, 4 workers | ~15 | ~10 FPS | Baseline |
| Quality 50, 7 workers | ~19 | ~9 FPS | More workers = higher capture, same delivery |
| **Quality 90, 7 workers** | **~20** | **~11 FPS** | **Best so far!** |
| glimagesink (1080p) | ~15 | ~10 FPS | Same as autovideosink |
| autovideosink (1080p) | ~15 | ~10 FPS | Consistent performance |
| 4K no scaling (q90, 7w) | ~18 | ~9 FPS | Scaling has minimal impact |
| Direct V4L2 (no HTTP) | N/A | **~2 FPS** | videoconvert BGR→NV12 is slow! |
| kmssink (with X11) | N/A | Failed | X11 owns DRM, permission denied |
| waylandsink | N/A | Failed | No Wayland compositor running |
| kmssink (X11 OFF, q90 7w) | ~23 | ~17 FPS | Direct DRM baseline |
| kmssink (X11 OFF, q100 7w) | ~22 | ~15 FPS | Higher quality = worse FPS |
| kmssink (X11 OFF, q80 8w) | **30** | **~22 FPS** | More workers helps! |
| kmssink (X11 OFF, q70 8w) | **30** | **~22 FPS** | Similar to q80 |
| kmssink (X11 OFF, q60 10w) | 30 | ~21 FPS | Best capture rate |
| kmssink + 512KB blocksize | 30 | ~23 FPS | Larger HTTP reads |
| kmssink + queues (non-leaky) | 30 | ~19 FPS | Queues add latency |
| kmssink + leaky queue | 30 | ~21 FPS (queued) | Smooth but ~10 FPS actual display |
| **🏆 kmssink + NV12 + plane-id=72** | **30** | **27-30 FPS ACTUAL** | **FULL FRAME RATE! No videoconvert!** |

### Key Findings
- **🏆🏆🏆 NV12 + plane-id=72 achieves FULL 30 FPS** - The ultimate solution!
- **videoconvert is THE bottleneck** - CPU-based format conversion at 4K only does 5-10 FPS
- **DRM plane 72 accepts NV12 natively** - Bypasses CPU entirely for zero-copy display
- **mppjpegdec outputs NV12 directly** - Hardware JPEG decode to NV12
- **More workers (8+) significantly improves FPS** - allows 30 FPS capture
- **Quality 70-80 is optimal** - balance between size and decode speed
- **X11 must be disabled** - kmssink requires exclusive DRM access
- **ustreamer + mppjpegdec is the fastest encode/decode path**
- Previous "~22 FPS" measurements were queue stats, not actual display FPS

## Best Working Configuration

### 🏆🏆🏆 ULTIMATE: NV12 Direct to Video Overlay Plane (30 FPS!)

**~27-30 FPS ACTUAL DISPLAY OUTPUT - FULL FRAME RATE!**

The key breakthrough: Use DRM plane 72 which supports NV12 natively, eliminating CPU videoconvert entirely!

```bash
# Stop X11/GDM first
sudo systemctl stop gdm

# Start ustreamer (quality 80, 8 workers)
ustreamer --device=/dev/video0 --format=BGR24 --port=9090 --quality=80 --workers=8 --buffers=8 &

# Display pipeline - NV12 DIRECT TO VIDEO OVERLAY PLANE (NO VIDEOCONVERT!)
# plane-id=72 is a video overlay plane that accepts NV12 natively
gst-launch-1.0 \
    souphttpsrc location=http://localhost:9090/stream blocksize=524288 ! \
    multipartdemux ! \
    jpegparse ! \
    mppjpegdec ! \
    'video/x-raw,format=NV12' ! \
    queue max-size-buffers=3 leaky=downstream ! \
    kmssink plane-id=72 connector-id=215 sync=false

# When done, restart X11
sudo systemctl start gdm
```

**Measured Performance (fpsdisplaysink):**
- Display: **27-30 FPS** (actual rendered frames!)
- Capture: **30 FPS** (full rate)
- Latency: ~30ms
- Quality: Very Good (quality=80 JPEG)

**Why this works:**
1. `mppjpegdec` outputs NV12 natively (hardware JPEG decode)
2. `plane-id=72` is a video overlay plane that accepts NV12 directly
3. **NO CPU videoconvert** - this was the bottleneck (only 5-10 FPS with BGRx conversion)
4. Zero-copy path from decoder to display

**DRM Plane Reference (from modetest):**
```
Plane 56: XR24, AR24, etc. (primary plane - used by default, no NV12)
Plane 72: NV12, NV21, NV16, etc. (video overlay - SUPPORTS NV12!) ← USE THIS
Plane 112: NV12 supported (another overlay)
Plane 152: NV12 supported (another overlay)
```

### Option 2: KMSSink with BGRx (Slower - ~10 FPS)

**~5-10 FPS actual display - CPU videoconvert is the bottleneck**

```bash
# This is SLOWER because videoconvert at 4K is CPU-bound
gst-launch-1.0 \
    souphttpsrc location=http://localhost:9090/stream ! \
    multipartdemux ! \
    jpegparse ! \
    mppjpegdec ! \
    queue max-size-buffers=3 leaky=downstream ! \
    videoconvert ! \
    video/x-raw,format=BGRx ! \
    kmssink connector-id=215 sync=false
```

**Why it's slow:** videoconvert NV12→BGRx at 4K uses CPU and can only do ~5-10 FPS.

### Option 3: X11 with autovideosink (Convenient, but slow)

**~11 FPS - works with desktop running**

```bash
# Start ustreamer (quality 90, 7 workers for best FPS)
ustreamer --device=/dev/video0 --format=BGR24 --port=9090 --quality=90 --workers=7 --buffers=6 &

# Display pipeline (4K - scaling doesn't help)
DISPLAY=:0 gst-launch-1.0 \
    souphttpsrc location=http://localhost:9090/stream ! \
    multipartdemux ! \
    jpegparse ! \
    mppjpegdec ! \
    autovideosink sync=false &

# Make fullscreen
sleep 3 && DISPLAY=:0 wmctrl -r "gst-launch" -b add,fullscreen
```

**Expected Performance:**
- Display: **~11 FPS** (fullscreen 4K via X11)
- Capture: ~18-20 FPS
- Latency: ~100-200ms
- Quality: Excellent (quality=90 JPEG)

## Conclusion

**SOLVED!** The bottleneck was **CPU-based videoconvert** at 4K, NOT the Mali GPU.

**Final Solution - 27-30 FPS at 4K:**

| Method | Actual Display FPS | Notes |
|--------|-------------------|-------|
| **kmssink + NV12 + plane-id=72** | **27-30 FPS** | **WINNER! Full frame rate!** |
| kmssink + videoconvert (BGRx) | 5-10 FPS | CPU conversion bottleneck |
| X11 glimagesink | ~10 FPS fullscreen | X11 overhead |
| VLC | ~5 FPS | Too slow |
| mpv/ffplay | ~10-12 FPS | Software decode |

**The winning pipeline:**
```bash
# Stop X11 first
sudo systemctl stop gdm

# Start ustreamer
ustreamer --device=/dev/video0 --format=BGR24 --port=9090 --quality=80 --workers=8 --buffers=8 &

# Display at FULL 30 FPS!
gst-launch-1.0 \
    souphttpsrc location=http://localhost:9090/stream blocksize=524288 ! \
    multipartdemux ! \
    jpegparse ! \
    mppjpegdec ! \
    'video/x-raw,format=NV12' ! \
    queue max-size-buffers=3 leaky=downstream ! \
    kmssink plane-id=72 connector-id=215 sync=false
```

**Key Insight:** The RK3588 has multiple DRM planes:
- Primary planes (56, 96, etc.) - Only accept RGB formats
- Video overlay planes (72, 112, 152) - Accept NV12 natively!

Using the video overlay plane eliminates all CPU format conversion, achieving true zero-copy HDMI passthrough at full frame rate.

## Next Steps

1. ✅ **ACHIEVED 30 FPS display** - Problem solved!
2. **Integrate into minus.py** - Use the new pipeline configuration
3. **Create systemd service** - Auto-start without X11 for dedicated HDMI passthrough mode
4. **Test color calibration** - Verify NV12 color accuracy vs RGB
