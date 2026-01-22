#!/bin/bash
# Minus Installation Script
# Sets up systemd service for auto-start on boot
# Configures hostname and mDNS for easy network access

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="minus"
SERVICE_FILE="${SCRIPT_DIR}/minus.service"
HOSTNAME="minus"

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

# Setup hostname and mDNS
echo "[1/7] Setting up hostname and mDNS..."
hostnamectl set-hostname ${HOSTNAME}
sed -i "s/127.0.1.1.*/127.0.1.1\t${HOSTNAME}/" /etc/hosts 2>/dev/null || echo "127.0.1.1	${HOSTNAME}" >> /etc/hosts

# Install and enable avahi for mDNS (.local resolution)
if ! dpkg -l | grep -q avahi-daemon; then
    echo "    Installing avahi-daemon for mDNS..."
    apt-get update -qq && apt-get install -y -qq avahi-daemon
fi
systemctl enable avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true
echo "    Hostname set to: ${HOSTNAME}"
echo "    Access via: http://${HOSTNAME}.local:8080"

# Stop existing service if running
echo "[2/7] Stopping existing service..."
systemctl stop ${SERVICE_NAME} 2>/dev/null || true
systemctl disable ${SERVICE_NAME} 2>/dev/null || true

# Stop X11 to free up display
echo "[3/7] Stopping X11 (gdm3)..."
systemctl stop gdm3 2>/dev/null || true
systemctl disable gdm3 2>/dev/null || true

# Copy service file
echo "[4/7] Installing systemd service..."
cp "$SERVICE_FILE" /etc/systemd/system/${SERVICE_NAME}.service
chmod 644 /etc/systemd/system/${SERVICE_NAME}.service

# Reload systemd
echo "[5/7] Reloading systemd..."
systemctl daemon-reload

# Enable and start service
echo "[6/7] Enabling service..."
systemctl enable ${SERVICE_NAME}

# Create screenshot directories
echo "[7/7] Creating screenshot directories..."
mkdir -p "${SCRIPT_DIR}/screenshots/ads"
mkdir -p "${SCRIPT_DIR}/screenshots/non_ads"
mkdir -p "${SCRIPT_DIR}/screenshots/vlm_spastic"
mkdir -p "${SCRIPT_DIR}/screenshots/static"
chown -R radxa:radxa "${SCRIPT_DIR}/screenshots" 2>/dev/null || true

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Access:"
echo "  Web UI:  http://${HOSTNAME}.local:8080"
echo "  Stream:  http://${HOSTNAME}.local:9090/stream"
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
