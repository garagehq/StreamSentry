#!/bin/bash
# Minus Uninstall Script

set -e

SERVICE_NAME="minus"

echo "=== Minus Uninstall ==="
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo $0"
    exit 1
fi

# Stop and disable service
echo "[1/3] Stopping service..."
systemctl stop ${SERVICE_NAME} 2>/dev/null || true
systemctl disable ${SERVICE_NAME} 2>/dev/null || true

# Remove service file
echo "[2/3] Removing service file..."
rm -f /etc/systemd/system/${SERVICE_NAME}.service

# Reload systemd
echo "[3/3] Reloading systemd..."
systemctl daemon-reload

echo ""
echo "=== Uninstall Complete ==="
echo ""
echo "Minus service has been removed."
echo "To restart X11: sudo systemctl start gdm3"
