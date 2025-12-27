#!/bin/bash
# Stream Sentry Launcher
# Stops X11 (gdm3) and starts stream_sentry with DRM/KMS display

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/tmp/stream_sentry.log"

echo "=== Stream Sentry Launcher ==="

# Check if running as root (needed to stop gdm3)
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo $0"
    exit 1
fi

# Stop any existing stream_sentry
echo "[1/3] Stopping any existing stream_sentry..."
pkill -9 -f stream_sentry.py 2>/dev/null
pkill -9 ustreamer 2>/dev/null
fuser -k /dev/video0 2>/dev/null
sleep 1

# Stop X11/gdm3
echo "[2/3] Stopping X11 (gdm3)..."
systemctl stop gdm3 2>/dev/null
sleep 2

# Verify X11 is stopped
if pgrep -x Xorg > /dev/null; then
    echo "Warning: Xorg still running, killing..."
    pkill -9 Xorg
    sleep 1
fi

# Start stream_sentry
echo "[3/3] Starting stream_sentry..."
cd "$SCRIPT_DIR"
python3 stream_sentry.py > "$LOG_FILE" 2>&1 &
PID=$!

sleep 5

# Check if it started successfully
if ps -p $PID > /dev/null 2>&1; then
    echo ""
    echo "=== Stream Sentry Started ==="
    echo "PID: $PID"
    echo "Log: $LOG_FILE"
    echo ""
    echo "To monitor: tail -f $LOG_FILE"
    echo "To stop:    sudo pkill -f stream_sentry.py"
else
    echo "ERROR: stream_sentry failed to start"
    echo "Check log: cat $LOG_FILE"
    exit 1
fi
