#!/bin/bash
# PFE Sensor Setup Script — Revised (auto-numbering)
# Run on a fresh Raspberry Pi OS Bookworm Lite install
# Username: pi
#
# INSTALL COMMAND:
#   curl -sSL https://raw.githubusercontent.com/Zietgeist/pfe-sensor/main/setup_pfe.sh -o setup_pfe.sh && bash setup_pfe.sh
#
# PROJECT NOTES (see README.md for the full version):
#   - Built by Ivan, who has limited programming experience. If you're
#     editing this script, keep every step as a literal command that can
#     be copy-pasted and run — don't assume familiarity with git/linux.
#   - Whenever this script changes, update README.md in the same pass so
#     the docs and the actual install process don't drift apart.
#   - Target hardware is the Raspberry Pi Zero family generally (Zero W,
#     Zero 2 W, etc.) — this script should stay board-version agnostic.
#     Known gap: the PiSugar driver install below currently segfaults on
#     Zero W v1 (works fine on Zero 2 W) — see README.md Known Issues.
#
# Don't abort on error — let it log and continue
set +e

REPO_DIR="/home/pi/pfe-sensor"
NUMBER_FILE="next_device_number.txt"

# --- Ask for GitHub PAT ---
read -p "Enter GitHub PAT (for auto-updates and numbering): " GITHUB_PAT
echo ""

# --- Set up git identity (needed to commit) ---
git config --global user.email "pfe-device@local"
git config --global user.name "PFE Device"
git config --global credential.helper store
echo "https://Zietgeist:${GITHUB_PAT}@github.com" | sudo tee /root/.git-credentials > /dev/null
echo "https://Zietgeist:${GITHUB_PAT}@github.com" > /home/pi/.git-credentials
git config --global --add safe.directory "$REPO_DIR"

# --- Clone the repo now (early) so we can claim a device number ---
echo "Cloning PFE repo to claim a device number..."
if [ ! -d "$REPO_DIR" ]; then
  git clone "https://Zietgeist:${GITHUB_PAT}@github.com/Zietgeist/pfe-sensor.git" "$REPO_DIR"
fi
sudo chown -R pi:pi "$REPO_DIR"

# --- Claim the next device number ---
echo "Claiming a device number..."
cd "$REPO_DIR"
DEVICE_NUM=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    git fetch origin main > /dev/null 2>&1
    git reset --hard origin/main > /dev/null 2>&1

    if [ ! -f "$NUMBER_FILE" ]; then
        echo "6" > "$NUMBER_FILE"   # fallback starting point if file is missing
    fi

    CANDIDATE=$(cat "$NUMBER_FILE" | tr -d '[:space:]')
    NEXT=$((CANDIDATE + 1))
    echo "$NEXT" > "$NUMBER_FILE"

    git add "$NUMBER_FILE"
    git commit -m "Claim device number $CANDIDATE" > /dev/null 2>&1
    if git push origin main > /dev/null 2>&1; then
        DEVICE_NUM=$CANDIDATE
        echo "Claimed device number: $DEVICE_NUM"
        break
    else
        echo "Number was taken by another device booting at the same time — retrying..."
        sleep $((RANDOM % 5 + 1))
    fi
done

if [ -z "$DEVICE_NUM" ]; then
    echo "Could not claim a device number after 10 tries. Check your internet connection and re-run this script."
    exit 1
fi

DEVICE_NAME="PFE-$DEVICE_NUM"
echo ""
echo "This device will be named: $DEVICE_NAME"
echo ""

# --- Set hostname ---
echo "[1/10] Setting hostname to $DEVICE_NAME..."
sudo hostnamectl set-hostname "$DEVICE_NAME" || echo "hostnamectl failed, using fallback..."
echo "$DEVICE_NAME" | sudo tee /etc/hostname > /dev/null
sudo sed -i "s/127\.0\.1\.1.*/127.0.1.1\t$DEVICE_NAME/" /etc/hosts
echo "Hostname set to $DEVICE_NAME."

# --- Update package list only (no full upgrade) ---
echo "[2/10] Updating package list..."
sudo apt update

# --- Install dependencies ---
# NOTE: python3-spidev, python3-libgpiod, and i2c-tools were added here.
# WhisPlay.py (the screen driver) imports spidev + gpiod directly, and
# report_status.sh calls i2cdetect — none of that worked without these.
echo "[3/10] Installing dependencies..."
sudo apt install -y git python3-pip python3-pil python3-smbus2 python3-spidev python3-libgpiod i2c-tools network-manager bluetooth bluez

# --- Install BLE election packages (for host self-organizing) ---
echo "[4/10] Installing Bluetooth packages for device election..."
sudo apt install -y libcairo2-dev libgirepository1.0-dev pkg-config python3-dev python3-dbus
sudo pip3 install --break-system-packages bleak bluezero
sudo rfkill unblock bluetooth
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
echo "Bluetooth packages installed and service running."

# --- Install Whisplay driver ---
# NOTE: PiSugar restructured the Whisplay repo. The old path
# (Whisplay/Driver/install_wm8960_drive.sh) no longer exists and this step
# was silently failing. The new entry point is install_driver.sh at the repo
# root, which auto-detects the board and installs the right driver.
echo "[5/10] Installing Whisplay HAT driver..."
cd /home/pi
git clone https://github.com/PiSugar/Whisplay.git --depth 1
cd /home/pi/Whisplay
echo "y" | sudo bash install_driver.sh
echo "Whisplay install done. Continuing (reboot comes at the end)..."

# --- Enable I2C and SPI ---
echo "[6/10] Enabling I2C and SPI..."
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0
echo "I2C and SPI enabled."

# --- Repo already cloned above — just make sure it's current ---
echo "[7/10] Confirming PFE repo is up to date..."
cd "$REPO_DIR"
git fetch origin main > /dev/null 2>&1
git reset --hard origin/main > /dev/null 2>&1
sudo chown -R pi:pi "$REPO_DIR"

# --- Clear stale nmcli connections ---
echo "[8/10] Clearing stale WiFi connections..."
sudo nmcli connection delete PFE-NET 2>/dev/null || true
sudo nmcli connection delete PFE-home 2>/dev/null || true
sudo nmcli connection delete Hotspot 2>/dev/null || true
echo "Stale connections cleared."

# --- Install systemd service ---
echo "[9/10] Installing autostart service..."
sudo tee /etc/systemd/system/pfe-sensor.service > /dev/null <<EOF
[Unit]
Description=PFE Sensor — auto-update and run
After=time-sync.target bluetooth.target
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

# --- Install boot splash service ---
# NOTE: Shows the logo on screen right after power-on, before networking,
# git pulls, or Bluetooth get a chance to slow things down. Runs once via
# boot_splash.py (in the repo) and exits; pfe-sensor.service takes over
# the screen once the main app gets that far. Without this, the screen
# stays blank for however long WiFi/git/network takes to settle.
echo "[10/10] Installing boot splash service..."
sudo tee /etc/systemd/system/pfe-boot-splash.service > /dev/null <<EOF
[Unit]
Description=PFE Boot Splash — show logo before networking/git/sensors start
DefaultDependencies=no
After=local-fs.target
Before=pfe-sensor.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/pi/pfe-sensor/boot_splash.py
RemainAfterExit=no
TimeoutStartSec=10
[Install]
WantedBy=sysinit.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable pfe-boot-splash.service

echo ""
echo "================================================"
echo " $DEVICE_NAME setup complete!"
echo ""
echo " Next steps:"
echo "   1. Install PiSugar driver (interactive — select PiSugar 3):"
echo "      curl http://cdn.pisugar.com/release/pisugar-power-manager.sh | sudo bash"
echo "      NOTE: If it freezes, press Ctrl+C, then run:"
echo "      sudo dpkg --configure -a"
echo "   2. After PiSugar install, re-enable SPI/I2C:"
echo "      sudo raspi-config nonint do_spi 0 && sudo raspi-config nonint do_i2c 0"
echo "   3. Set up safe power-off (field units only have the physical button,"
echo "      no SSH access to run a clean shutdown — this makes a long button"
echo "      press do a clean shutdown before power actually cuts, and makes"
echo "      the device shut down cleanly on its own at 5% battery instead of"
echo "      browning out):"
echo "      echo -e 'set_button_enable long 1\\nset_button_shell long \"sudo shutdown now\"' | nc -U -q1 /tmp/pisugar-server.sock"
echo "      echo -e 'set_safe_shutdown_level 5\\nset_safe_shutdown_delay 30' | nc -U -q1 /tmp/pisugar-server.sock"
echo "   4. Check /boot/firmware/config.txt looks correct before rebooting"
echo "   5. Reboot: sudo reboot"
echo "   6. After reboot, check: cat /home/pi/pfe_update.log"
echo "================================================"
