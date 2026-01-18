#!/bin/bash
# Stop Minus and optionally restart X11

echo "=== Stopping Minus ==="

# Check if running as systemd service
if systemctl is-active --quiet minus 2>/dev/null; then
    echo "[1/2] Stopping systemd service..."
    sudo systemctl stop minus
else
    # Graceful shutdown: SIGTERM first, then SIGKILL
    echo "[1/2] Stopping minus (graceful)..."

    # Send SIGTERM for graceful shutdown
    pkill -TERM -f minus.py 2>/dev/null

    # Wait up to 5 seconds for graceful exit
    for i in {1..5}; do
        if ! pgrep -f minus.py > /dev/null 2>&1; then
            break
        fi
        sleep 1
    done

    # Force kill if still running
    if pgrep -f minus.py > /dev/null 2>&1; then
        echo "    Forcing shutdown..."
        pkill -9 -f minus.py 2>/dev/null
    fi

    # Clean up child processes
    pkill -9 ustreamer 2>/dev/null
    pkill -9 gst-launch 2>/dev/null
    fuser -k /dev/video0 2>/dev/null
fi

sleep 1
echo "[2/2] Minus stopped."

# Ask about restarting X11
if [ "$1" = "--restart-x11" ] || [ "$1" = "-x" ]; then
    echo ""
    echo "Restarting X11 (gdm3)..."
    sudo systemctl start gdm3
    echo "X11 started."
else
    echo ""
    echo "To restart X11: sudo systemctl start gdm3"
    echo "Or run: $0 --restart-x11"
fi
