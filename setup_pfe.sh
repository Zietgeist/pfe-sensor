#!/bin/bash
# PFE Sensor Setup Script
# Run this on a fresh Raspberry Pi OS Bookworm Lite install

set -e

# --- Ask for device number ---
read -p "Enter device number (e.g. 1 for PFE-1): " DEVICE_NUM
DEVICE_NAME="PFE-$DEVICE_NUM"

echo ""
echo "Setting up $DEVICE_NAME..."
echo ""

# --- Set hostname ---
echo "Setting hostname to $DEVICE_NAME..."
sudo hostnamectl set-hostname $DEVICE_NAME

# --- Update system ---
echo "Updating system..."
sudo apt update && sudo apt upgrade -y

# --- Install dependencies ---
echo "Installing dependencies..."
sudo apt install -y git python3-pip python3-pil python3-pygame python3-smbus2 bluetooth bluez

# --- Install Python packages ---
echo "Installing Python packages..."
sudo pip3 install bless --break-system-packages

# --- Install Whisplay driver ---
echo "Installing Whisplay HAT driver..."
cd ~
git clone https://github.com/PiSugar/Whisplay.git --depth 1
cd ~/Whisplay/Driver
sudo bash install_wm8960_drive.sh

# --- Install PiSugar driver ---
echo "Installing PiSugar 3 driver..."
curl http://cdn.pisugar.com/release/pisugar-power-manager.sh | sudo bash

# --- Enable interfaces ---
echo "Enabling I2C and SPI..."
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0

# --- Copy app files ---
echo "Copying application files..."
# Update BLE name and hostname in pressure_display.py
sed "s/PFE Sensor/$DEVICE_NAME/g" ~/pressure_display.py > ~/pressure_display_new.py
mv ~/pressure_display_new.py ~/pressure_display.py

# --- Install systemd service ---
echo "Installing autostart service..."
sudo tee /etc/systemd/system/pfe.service > /dev/null <<EOF
[Unit]
Description=PFE Pressure Sensor $DEVICE_NAME
After=bluetooth.target
Wants=bluetooth.target

[Service]
ExecStartPre=/usr/sbin/rfkill unblock bluetooth
ExecStartPre=/bin/sleep 5
ExecStart=/usr/bin/python3 /home/ivan/pfe-sensor/pressure_display.py
WorkingDirectory=/home/ivan/pfe-sensor
User=root
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pfe.service

echo ""
echo "================================================"
echo " $DEVICE_NAME setup complete!"
echo " Still needed:"
echo "   1. Copy pressure_display.py to this Pi"
echo "   2. Copy marten_screen.png to this Pi"
echo "   3. Then reboot"
echo "================================================"
