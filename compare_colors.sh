#!/bin/bash
# Compare HDMI-RX input vs display output colors
# Pauses minus, stops mpv, captures raw HDMI-RX, then resumes

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="${1:-.}"

HDMI_RX="$OUTPUT_DIR/hdmi_rx_$TIMESTAMP.png"
HDMI_OUT="$OUTPUT_DIR/hdmi_out_$TIMESTAMP.png"

echo "Capturing display output..."
DISPLAY=:0 scrot "$HDMI_OUT"

echo "Pausing minus and stopping mpv..."

# Pause the minus python process so it doesn't restart mpv
pkill -STOP -f minus.py

# Now kill mpv
pkill -9 mpv
sleep 0.3

# Capture HDMI-RX with ffmpeg
echo "Capturing HDMI-RX input..."
ffmpeg -y -f v4l2 -input_format nv12 -video_size 1920x1080 -i /dev/video0 -frames:v 1 "$HDMI_RX" 2>/dev/null
RESULT=$?

# Resume minus (it will restart mpv)
pkill -CONT -f minus.py

if [ $RESULT -eq 0 ] && [ -f "$HDMI_RX" ]; then
    echo ""
    echo "Screenshots saved:"
    echo "  HDMI-RX input:  $HDMI_RX"
    echo "  Display output: $HDMI_OUT"
    echo ""
    echo "Compare side by side with:"
    echo "  feh -F $HDMI_RX $HDMI_OUT"
else
    echo "HDMI-RX capture failed"
    # Resume anyway
    pkill -CONT -f minus.py
fi
