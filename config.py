#!/usr/bin/env python3
"""
PFE Configuration
All settings live here. Edit this file to change device behavior.
"""
import os

# --- Device identity ---
DEVICE_NAME = os.uname().nodename   # Reads hostname: PFE-1, PFE-2, etc.

# --- Sensor I2C addresses ---
SDP_ADDR_1 = 0x25   # Sensor 1 (inlet)
SDP_ADDR_2 = 0x26   # Sensor 2 (outlet)

# --- WiFi ---
HOME_SSID     = "PFE-home"
HOME_PASSWORD = "pferadon1"
SITE_SSID     = "PFE-NET"
SITE_PASSWORD = "pferadon1"

# --- Network ---
HOST_IP  = "10.42.0.1"
WEB_PORT = 80

# --- Pressure target ---
TARGET_PRESSURE = -12.5   # Pascals — adjust per job if needed

# --- File paths ---
REPO_DIR    = "/home/pi/pfe-sensor"
SPLASH_PATH = f"{REPO_DIR}/marten_screen.png"
FONT_BOLD   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG    = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
