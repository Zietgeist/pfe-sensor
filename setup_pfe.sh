#!/bin/bash
# PFE Sensor Setup Script — Revised
# Run on a fresh Raspberry Pi OS Bookworm Lite install
# Username: pi
# Don't abort on error — let it log and continue
set +e
# --- Ask for device number ---
read -p "Enter device number (e.g. 1 for PFE-1): " DEVICE_NUM
DEVICE_NAME="PFE-$DEVICE_NUM"
# --- Ask for GitHub PAT ---
read -p "Enter GitHub PAT (for auto-updates): " GITHUB_PAT
echo ""
echo "Setting up $DEVICE_NAME..."
echo ""
# --- Set hostname ---
echo "[1/8] Setting hostname to $DEVICE_NAME..."
sudo hostnamectl set-hostname "$DEVICE_NAME" || echo "hostnamectl failed, using fallback..."
echo "$DEVICE_NAME" | sudo tee /etc/hostname > /dev/null
sudo sed -i "s/127\.0\.1\.1.*/127.0.1.1\t$DEVICE_NAME/" /etc/hosts
echo "Hostname set to $DEVICE_NAME."
# --- Update system ---
echo "[2/8] Updating system..."
sudo apt update && sudo apt upgrade -y
# --- Install dependencies ---
echo "[3/8] Installing dependencies..."
sudo apt install -y git python3-pip python3-pil python3-pygame python3-smbus2 \
  python3-flask network-manager
# --- Install Whisplay driver ---
echo "[4/8] Installing Whisplay HAT driver..."
cd /home/pi
git clone https://github.com/PiSugar/Whisplay.git --depth 1
cd /home/pi/Whisplay/Driver
sudo bash install_wm8960_drive.sh
echo "Whisplay install done. Continuing (reboot comes at the end)..."
# --- Enable I2C and SPI ---
echo "[5/8] Enabling I2C and SPI..."
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0
echo "I2C and SPI enabled."
# --- Clone PFE repo ---
echo "[6/8] Cloning PFE repo..."
REPO_DIR="/home/pi/pfe-sensor"
# Store PAT in git credentials
sudo git config --global credential.helper store
echo "https://Zietgeist:${GITHUB_PAT}@github.com" | sudo tee /root/.git-credentials > /dev/null
if [ ! -d "$REPO_DIR" ]; then
  sudo git clone "https://Zietgeist:${GITHUB_PAT}@github.com/Zietgeist/pfe-sensor.git" "$REPO_DIR"
fi
sudo chown -R pi:pi "$REPO_DIR"
sudo git config --global --add safe.directory "$REPO_DIR"
# --- Clear stale nmcli connections ---
echo "[7/8] Clearing stale WiFi connections..."
sudo nmcli connection delete PFE-NET 2>/dev/null || true
sudo nmcli connection delete PFE-home 2>/dev/null || true
echo "Stale connections cleared."
# --- Install systemd service ---
echo "[8/8] Installing autostart service..."
sudo tee /etc/systemd/system/pfe-sensor.service > /dev/null <<EOF
[Unit]
Description=PFE Sensor — auto-update and run
After=time-sync.target
Wants=time-sync.target
[Service]
Type=simple
User=root
ExecStart=/bin/bash /home/pi/pfe-sensor/update_and_run.sh
WorkingDirectory=/home/pi/pfe-sensor
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable pfe-sensor.service
echo ""
echo "================================================"
echo " $DEVICE_NAME setup complete!"
echo ""
echo " Next steps:"
echo "   1. Install PiSugar driver (interactive — select PiSugar 3):"
echo "      curl http://cdn.pisugar.com/release/pisugar-power-manager.sh | sudo bash"
echo "   2. After PiSugar install, re-enable SPI/I2C:"
echo "      sudo raspi-config nonint do_spi 0 && sudo raspi-config nonint do_i2c 0"
echo "   3. Verify auto-shutdown: 15% battery, 60s delay"
echo "      (visit http://$(hostname -I | awk '{print $1}'):8421)"
echo "   4. Reboot: sudo reboot"
echo "   5. After reboot, check: cat /home/pi/pfe_update.log"
echo "================================================"
