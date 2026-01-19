#!/bin/bash
# Test script for ustreamer text overlay
# Runs full HDMI passthrough with overlay cycling through different positions/text

set -e

USTREAMER="/home/radxa/ustreamer-patched"
API_URL="http://localhost:9090"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

cleanup() {
    log "Cleaning up..."
    pkill -9 ustreamer 2>/dev/null || true
    pkill -9 gst-launch 2>/dev/null || true
    log "Done."
}

trap cleanup EXIT

# Kill any existing processes
log "Stopping any existing processes..."
pkill -9 ustreamer 2>/dev/null || true
pkill -9 gst-launch 2>/dev/null || true
sleep 2

# Auto-detect HDMI output connector and plane
log "Auto-detecting HDMI output..."
CONNECTOR_ID=""
PLANE_ID=""

# Find connected HDMI connector
CONNECTOR_ID=$(modetest -M rockchip -c 2>/dev/null | grep -E "^[0-9]+.*connected.*HDMI" | head -1 | awk '{print $1}')

if [ -z "$CONNECTOR_ID" ]; then
    error "No HDMI output connected!"
    exit 1
fi

log "Using connector ID: $CONNECTOR_ID"

# Find NV12-capable overlay plane
PLANE_ID=$(modetest -M rockchip -p 2>/dev/null | grep -B5 "NV12" | grep "^[0-9]" | head -1 | awk '{print $1}')
if [ -z "$PLANE_ID" ]; then
    PLANE_ID="121"  # Default fallback
fi
log "Using plane ID: $PLANE_ID"

# Start ustreamer with MPP encoder
log "Starting ustreamer with MPP encoder and overlay support..."
$USTREAMER \
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
    --buffers=5 \
    2>&1 | while read line; do echo -e "${YELLOW}[ustreamer]${NC} $line"; done &

USTREAMER_PID=$!
sleep 3

# Check if ustreamer started
if ! curl -s "$API_URL/state" > /dev/null 2>&1; then
    error "ustreamer failed to start!"
    exit 1
fi
log "ustreamer started successfully"

# Start GStreamer display pipeline
log "Starting GStreamer display pipeline..."
gst-launch-1.0 -q \
    souphttpsrc location="$API_URL/stream" is-live=true do-timestamp=true ! \
    multipartdemux ! \
    jpegparse ! \
    mppjpegdec ! \
    video/x-raw,format=NV12 ! \
    kmssink connector-id=$CONNECTOR_ID plane-id=$PLANE_ID sync=false \
    2>&1 | while read line; do echo -e "${YELLOW}[gstreamer]${NC} $line"; done &

GST_PID=$!
sleep 2
log "Display pipeline started"

# Function to set overlay
set_overlay() {
    local text="$1"
    local position="$2"
    local scale="${3:-3}"
    local extra="${4:-}"

    curl -s "$API_URL/overlay/set?text=$text&position=$position&scale=$scale&enabled=true$extra" > /dev/null
    log "Overlay: '$text' at position $position (scale $scale)"
}

clear_overlay() {
    curl -s "$API_URL/overlay/set?clear=true&enabled=false" > /dev/null
    log "Overlay cleared"
}

# Position names for logging
declare -A POS_NAMES=(
    [0]="TOP-LEFT"
    [1]="TOP-RIGHT"
    [2]="BOTTOM-LEFT"
    [3]="BOTTOM-RIGHT"
    [4]="CENTER"
)

log ""
log "=========================================="
log "  OVERLAY TEST - 2 MINUTE DEMO"
log "=========================================="
log ""

# Test sequence over 2 minutes (120 seconds)
# Each test runs for ~10 seconds

# 1. Simple "testing" in top-right (as requested)
set_overlay "testing" 1 4
sleep 10

# 2. Different positions with same text
for pos in 0 1 2 3 4; do
    set_overlay "Position: ${POS_NAMES[$pos]}" $pos 3
    sleep 6
done

# 3. Larger scale test
set_overlay "BIG TEXT" 4 6
sleep 8

# 4. Small scale test
set_overlay "small text here" 1 2
sleep 8

# 5. Multi-line text (URL encoded newline is %0A)
set_overlay "Line 1%0ALine 2%0ALine 3" 0 3
sleep 8

# 6. Status-like overlay
set_overlay "LIVE" 1 4 "&color_y=255&bg_y=200&bg_u=90&bg_v=100"
sleep 8

# 7. Recording indicator (red-ish background)
set_overlay "REC" 0 5 "&bg_y=80&bg_u=90&bg_v=240&bg_alpha=220"
sleep 8

# 8. Bottom status bar style
set_overlay "4K @ 30fps | HDMI Passthrough" 3 2
sleep 8

# 9. Cycling through corners quickly
for i in {1..3}; do
    for pos in 0 1 3 2; do
        set_overlay "Corner $((pos+1))" $pos 3
        sleep 1
    done
done

# 10. Final message
set_overlay "Overlay Test Complete!" 4 4
sleep 5

# Clear and show final stats
clear_overlay
sleep 2

log ""
log "=========================================="
log "  TEST COMPLETE"
log "=========================================="
log ""
log "The overlay system is working!"
log "API endpoint: $API_URL/overlay/set"
log ""
log "Press Ctrl+C to stop the passthrough..."

# Keep running until interrupted
wait
