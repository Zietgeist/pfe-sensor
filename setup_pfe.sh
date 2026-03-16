#!/bin/bash
# =============================================================
# PFE Sensor — Fresh Install Setup Script
# Run once on a fresh Raspberry Pi OS Bookworm Lite
# Usage: sudo bash setup_pfe.sh
# =============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
info() { echo -e "${YELLOW}[..] $1${NC}"; }
err()  { echo -e "${RED}[ERR] $1${NC}"; exit 1; }

if [[ $EUID -ne 0 ]]; then
    err "Please run as root: sudo bash setup_pfe.sh"
fi

echo ""
echo "============================================="
echo "   PFE Sensor — Device Setup"
echo "============================================="
echo ""

read -p "Enter device number (e.g. 1 for PFE-1): " DEVICE_NUM

if ! [[ "$DEVICE_NUM" =~ ^[0-9]+$ ]]; then
    err "Device number must be a number (e.g. 1, 2, 3)"
fi

DEVICE_NAME="PFE-$DEVICE_NUM"
HOME_DIR="/home/pi"
REPO_DIR="$HOME_DIR/pfe-sensor"
REPO_URL="https://github.com/Zietgeist/pfe-sensor.git"

echo ""
info "Setting up device: $DEVICE_NAME"
info "Home directory: $HOME_DIR"
echo ""

# STEP 1 — Hostname
info "Setting hostname to $DEVICE_NAME..."
hostnamectl set-hostname "$DEVICE_NAME"
sed -i "s/127.0.1.1.*/127.0.1.1\t$DEVICE_NAME/" /etc/hosts
ok "Hostname set to $DEVICE_NAME"

# STEP 2 — System update
info "Updating system packages (this may take a few minutes)..."
apt-get update -y
apt-get upgrade -y
ok "System updated"

# STEP 3 — Dependencies
info "Installing system dependencies..."
apt-get install -y \
    git \
    python3-pip \
    python3-smbus2 \
    python3-pil \
    python3-spidev \
    python3-libgpiod \
    python3-pygame \
    i2c-tools \
    network-manager
ok "System dependencies installed"

# STEP 4 — I2C and SPI
info "Enabling I2C interface..."
raspi-config nonint do_i2c 0
ok "I2C enabled"

info "Enabling SPI interface..."
raspi-config nonint do_spi 0
ok "SPI enabled"

# STEP 5 — Whisplay HAT driver
info "Cloning Whisplay HAT driver..."
cd "$HOME_DIR"
if [ ! -d "$HOME_DIR/Whisplay" ]; then
    sudo -u pi git clone https://github.com/PiSugar/Whisplay.git --depth 1
    ok "Whisplay repo cloned"
else
    ok "Whisplay repo already exists, skipping"
fi

info "Installing Whisplay driver (screen + audio)..."
cd "$HOME_DIR/Whisplay/Driver"
bash install_wm8960_drive.sh
ok "Whisplay driver installed"

# STEP 6 — PiSugar 3 driver + auto-shutdown
info "Installing PiSugar 3 power manager..."
debconf-set-selections <<< "pisugar-poweroff pisugar-poweroff/model select PiSugar 3"
wget -q -O /tmp/pisugar-power-manager.sh https://cdn.pisugar.com/release/pisugar-power-manager.sh
DEBIAN_FRONTEND=noninteractive bash /tmp/pisugar-power-manager.sh -c release
ok "PiSugar driver installed"

info "Configuring auto-shutdown on low battery..."
CONFIG="/etc/pisugar-server/config.json"
for i in {1..10}; do
    [ -f "$CONFIG" ] && break
    sleep 2
done

if [ ! -f "$CONFIG" ]; then
    err "PiSugar config not found at $CONFIG — driver may not have installed correctly"
fi

python3 - <<EOF
import json
with open("$CONFIG", 'r') as f:
    config = json.load(f)
config['auto_shutdown_level'] = 15
config['auto_shutdown_delay'] = 60
with open("$CONFIG", 'w') as f:
    json.dump(config, f, indent=2)
print("Config updated")
EOF

systemctl restart pisugar-server
ok "Auto-shutdown set: triggers at 15% battery, 60s delay"

# STEP 7 — Clone PFE repo
info "Cloning PFE sensor repo..."
if [ ! -d "$REPO_DIR" ]; then
    sudo -u pi git clone "$REPO_URL" "$REPO_DIR"
    ok "PFE repo cloned to $REPO_DIR"
else
    info "PFE repo already exists, pulling latest..."
    cd "$REPO_DIR"
    sudo -u pi git pull
    ok "PFE repo updated"
fi
git config --global --add safe.directory /home/pi/pfe-sensor

# STEP 7b — Set PFE-home as preferred network (highest priority)
info "Setting PFE-home as preferred network..."
nmcli connection modify PFE-home connection.autoconnect-priority 100 \
    && ok "PFE-home priority set to 100" \
    || info "PFE-home connection not found — skipping (make sure it was set in RPi Imager)"

# STEP 8 — Systemd service
info "Installing systemd service..."
cat > /etc/systemd/system/pfe-sensor.service <<EOF
[Unit]
Description=PFE Sensor — auto-update and run ($DEVICE_NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/bin/bash $REPO_DIR/update_and_run.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pfe-sensor.service
ok "Service installed and enabled"

echo ""
echo "============================================="
echo -e " ${GREEN}$DEVICE_NAME setup complete!${NC}"
echo "============================================="
echo ""
echo " What happens on next reboot:"
echo "   1. Pi waits for network"
echo "   2. Git pulls latest code from GitHub"
echo "   3. Runs pressure_display.py automatically"
echo ""
echo " Useful commands:"
echo "   Watch logs:     tail -f $HOME_DIR/pfe_update.log"
echo "   Service status: systemctl status pfe-sensor"
echo "   Start manually: systemctl start pfe-sensor"
echo "   Battery level:  python3 -c \"import socket; s=socket.socket(); s.connect(('127.0.0.1',8423)); s.send(b'get battery\n'); print(s.recv(64).decode()); s.close()\""
echo ""
echo " Battery auto-shutdown: 15% (60 second delay)"
echo " PiSugar WebUI: http://<this-pi-ip>:8421"
echo ""
read -p " Reboot now? [y/N]: " DO_REBOOT
if [[ "$DO_REBOOT" =~ ^[Yy]$ ]]; then
    reboot
fi
