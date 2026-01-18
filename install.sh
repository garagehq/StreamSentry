#!/bin/bash
# Minus Installation Script
# Sets up systemd service for auto-start on boot

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="minus"
SERVICE_FILE="${SCRIPT_DIR}/minus.service"

echo "=== Minus Installation ==="
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo $0"
    exit 1
fi

# Check if service file exists
if [ ! -f "$SERVICE_FILE" ]; then
    echo "ERROR: Service file not found: $SERVICE_FILE"
    exit 1
fi

# Stop existing service if running
echo "[1/5] Stopping existing service..."
systemctl stop ${SERVICE_NAME} 2>/dev/null || true
systemctl disable ${SERVICE_NAME} 2>/dev/null || true

# Stop X11 to free up display
echo "[2/5] Stopping X11 (gdm3)..."
systemctl stop gdm3 2>/dev/null || true
systemctl disable gdm3 2>/dev/null || true

# Copy service file
echo "[3/5] Installing systemd service..."
cp "$SERVICE_FILE" /etc/systemd/system/${SERVICE_NAME}.service
chmod 644 /etc/systemd/system/${SERVICE_NAME}.service

# Reload systemd
echo "[4/5] Reloading systemd..."
systemctl daemon-reload

# Enable and start service
echo "[5/5] Enabling service..."
systemctl enable ${SERVICE_NAME}

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Commands:"
echo "  Start:   sudo systemctl start ${SERVICE_NAME}"
echo "  Stop:    sudo systemctl stop ${SERVICE_NAME}"
echo "  Status:  sudo systemctl status ${SERVICE_NAME}"
echo "  Logs:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Disable: sudo systemctl disable ${SERVICE_NAME}"
echo ""
echo "The service will auto-start on boot."
echo "To start now: sudo systemctl start ${SERVICE_NAME}"
