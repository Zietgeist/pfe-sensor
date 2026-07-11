#!/usr/bin/env python3
"""
PFE Boot Splash
Puts the logo on screen as fast as possible after power-on — runs
BEFORE pfe-sensor.service, so it doesn't wait on networking, git pulls,
BLE, or sensor reads. Draws once and exits; the main app takes over
the screen once it gets that far.

Installed/enabled by setup_pfe.sh as pfe-boot-splash.service.
"""
import sys
sys.path.append('/home/pi/Whisplay/Driver')
from WhisPlay import WhisPlayBoard
from PIL import Image

SPLASH_PATH = "/home/pi/pfe-sensor/marten_screen.png"


def image_to_pixels(img):
    pixels = []
    for r, g, b in img.getdata():
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        pixels.extend([(rgb565 >> 8) & 0xFF, rgb565 & 0xFF])
    return pixels


try:
    board = WhisPlayBoard()
    board.set_backlight(80)
    img = Image.open(SPLASH_PATH).convert('RGB')
    board.draw_image(0, 0, 240, 280, image_to_pixels(img))
    print("Boot splash shown.")
except Exception as e:
    # Never block boot over a missing logo — just log it and move on.
    print(f"Boot splash error: {e}")
