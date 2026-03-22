#!/usr/bin/env python3
"""
PFE Pressure Sensor
- At home: connects to PFE-home for updates/testing
- In field: auto-assigns as host (creates PFE-NET + runs dashboard)
            or client (joins PFE-NET + sends data)
Same code runs on every device.

Boot sequence:
  1. Zero both sensors (rapid averaging)
  2. Pick outdoor temp:
       1 click + wait 3s → auto-read from thermometer
       2 clicks           → T < -4°F
       3 clicks           → -4°F to 14°F
       4 clicks           → 14°F to 32°F
       5 clicks           → > 32°F
  3. Pick climate zone (1/2/3 clicks + wait 3s, default Moderate)
  4. One click locks both sensor baselines
  5. Targets calculated per sensor from zone + temp + baseline
  6. Blue fields if target unknown, green/red once calibrated
"""

import sys
import os
import time
import random
import threading
import subprocess
import socket
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from smbus2 import SMBus, i2c_msg
from PIL import Image, ImageDraw, ImageFont

sys.path.append('/home/pi/Whisplay/Driver')
from WhisPlay import WhisPlayBoard

# =============================================================
# Constants
# =============================================================

DEVICE_NAME   = os.uname().nodename
SDP_ADDR_1    = 0x25
SDP_ADDR_2    = 0x26

HOME_SSID     = "PFE-home"
HOME_PASSWORD = "pferadon1"
SITE_SSID     = "PFE-NET"
SITE_PASSWORD = "pferadon1"

HOST_IP       = "10.42.0.1"
WEB_PORT      = 80

ZONE_MILD     = "mild"
ZONE_MODERATE = "moderate"
ZONE_SEVERE   = "severe"
DEFAULT_ZONE  = ZONE_MODERATE

HOLD_SECONDS  = 2.0
ZERO_SAMPLES  = 50
CLICK_TIMEOUT = 3.0   # seconds of silence before clicks are processed

# Temp band click map:
#   1 click + wait → auto (read thermometer)
#   2 clicks       → < -4°F
#   3 clicks       → -4 to 14°F
#   4 clicks       → 14 to 32°F
#   5 clicks       → > 32°F
TEMP_CLICK_BANDS = {
    1: "auto",
    2: "<-4",
    3: "14to-4",
    4: "32to14",
    5: ">32",
}

TEMP_BAND_LABELS = {
    "auto":   "AUTO (thermometer)",
    "<-4":    "< -4°F",
    "14to-4": "-4°F to 14°F",
    "32to14": "14°F to 32°F",
    ">32":    "> 32°F",
}

# Representative mid-point °F for each band (used for table lookup)
TEMP_BAND_MIDPOINT_F = {
    "<-4":    -10.0,
    "14to-4":   5.0,
    "32to14":  23.0,
    ">32":     50.0,
}

# =============================================================
# Target pressure lookup tables
# =============================================================

def _build_mild():
    gt32 = [0.0,-0.1,-0.2,-0.3,-0.4,-0.5,-0.6,-0.7,-0.8,-0.9,-1.0,
            -1.1,-1.2,-1.3,-1.4,-1.5,-1.6,-1.7,-1.8,-1.9,-2.0,-2.1,
            -2.2,-2.3,-2.4,-2.5,-2.6,-2.7,-2.8,-2.9,-3.0,-3.1,-3.2,
            -3.3,-3.4,-3.5,-3.6,-3.7,-3.8,-3.9,-4.0,-4.1,-4.2,-4.3,
            -4.4,-4.5,-4.6,-4.7,-4.8,-4.9,-5.0,-5.1,-5.2,-5.3,-5.4,
            -5.5,-5.6,-5.7,-5.8,-5.9,-6.0]
    t32_14 = [0.0,-0.1,-0.1,-0.2,-0.2,-0.2,-0.3,-0.3,-0.4,-0.4,-0.4,
              -0.5,-0.5,-0.6,-0.6,-0.6,-0.7,-0.7,-0.8,-0.8,-0.8,-0.9,
              -0.9,-1.0,-1.0,-1.0,-1.1,-1.1,-1.2,-1.2,-1.2,-1.3,-1.3,
              -1.4,-1.4,-1.4,-1.5,-1.5,-1.6,-1.6,-1.6,-1.7,-1.7,-1.8,
              -1.8,-1.8,-1.9,-1.9,-2.0,-2.0,-2.0,-2.1,-2.1,-2.2,-2.2,
              -2.2,-2.3,-2.3,-2.4,-2.4,-2.4]
    data = {}
    for i in range(61):
        data[i] = {
            ">32":    gt32[i],
            "32to14": t32_14[i],
            "14to-4": 0.0,
            "<-4":    0.0,
        }
    return data

def _build_moderate():
    gt32 = [0.0,-0.2,-0.3,-0.4,-0.5,-0.6,-0.8,-0.9,-1.0,-1.1,-1.2,
            -1.4,-1.5,-1.6,-1.7,-1.8,-2.0,-2.1,-2.2,-2.3,-2.4,-2.6,
            -2.7,-2.8,-2.9,-3.0,-3.2,-3.3,-3.4,-3.5,-3.6,-3.8,-3.9,
            -4.0,-4.1,-4.2,-4.4,-4.5,-4.6,-4.7,-4.8,-5.0,-5.1,-5.2,
            -5.3,-5.4,-5.6,-5.7,-5.8,-5.9,-6.0,-6.2,-6.3,-6.4,-6.5,
            -6.6,-6.8,-6.9,-7.0,-7.1,-7.2]
    t32_14 = [0.0,-0.1,-0.1,-0.2,-0.2,-0.3,-0.3,-0.4,-0.4,-0.5,-0.5,
              -0.6,-0.6,-0.7,-0.7,-0.8,-0.8,-0.9,-0.9,-1.0,-1.0,-1.1,
              -1.1,-1.2,-1.2,-1.3,-1.3,-1.4,-1.4,-1.5,-1.5,-1.6,-1.6,
              -1.7,-1.7,-1.8,-1.8,-1.9,-1.9,-2.0,-2.0,-2.1,-2.1,-2.2,
              -2.2,-2.3,-2.3,-2.4,-2.4,-2.5,-2.5,-2.6,-2.6,-2.7,-2.7,
              -2.8,-2.8,-2.9,-2.9,-3.0,-3.0]
    data = {}
    for i in range(61):
        data[i] = {
            ">32":    gt32[i],
            "32to14": t32_14[i],
            "14to-4": 0.0,
            "<-4":    0.0,
        }
    return data

def _build_severe():
    gt32 = [0.0,-0.2,-0.3,-0.5,-0.6,-0.8,-0.9,-1.1,-1.2,-1.4,-1.5,
            -1.7,-1.8,-2.0,-2.1,-2.3,-2.4,-2.6,-2.7,-2.9,-3.0,-3.2,
            -3.3,-3.5,-3.6,-3.8,-3.9,-4.1,-4.2,-4.4,-4.5,-4.7,-4.8,
            -5.0,-5.1,-5.3,-5.4,-5.6,-5.7,-5.9,-6.0,-6.2,-6.3,-6.5,
            -6.6,-6.8,-6.9,-7.1,-7.2,-7.4,-7.5,-7.7,-7.8,-8.0,-8.1,
            -8.3,-8.4,-8.6,-8.7,-8.9,-9.0]
    t32_14 = [0.0,-0.1,-0.2,-0.2,-0.3,-0.3,-0.4,-0.5,-0.5,-0.6,-0.6,
              -0.7,-0.8,-0.8,-0.9,-0.9,-1.0,-1.1,-1.1,-1.2,-1.2,-1.3,
              -1.4,-1.4,-1.5,-1.5,-1.6,-1.7,-1.7,-1.8,-1.8,-1.9,-2.0,
              -2.0,-2.1,-2.1,-2.2,-2.3,-2.3,-2.4,-2.4,-2.5,-2.6,-2.6,
              -2.7,-2.7,-2.8,-2.9,-2.9,-3.0,-3.0,-3.1,-3.2,-3.2,-3.3,
              -3.3,-3.4,-3.5,-3.5,-3.6,-3.6]
    t14_n4 = [0.0,-0.1,-0.1,-0.1,-0.1,-0.1,-0.2,-0.2,-0.2,-0.2,-0.2,
              -0.3,-0.3,-0.3,-0.3,-0.3,-0.4,-0.4,-0.4,-0.4,-0.4,-0.5,
              -0.5,-0.5,-0.5,-0.5,-0.6,-0.6,-0.6,-0.6,-0.6,-0.7,-0.7,
              -0.7,-0.7,-0.7,-0.8,-0.8,-0.8,-0.8,-0.8,-0.9,-0.9,-0.9,
              -0.9,-0.9,-1.0,-1.0,-1.0,-1.0,-1.0,-1.1,-1.1,-1.1,-1.1,
              -1.1,-1.2,-1.2,-1.2,-1.2,-1.2]
    data = {}
    for i in range(61):
        data[i] = {
            ">32":    gt32[i],
            "32to14": t32_14[i],
            "14to-4": t14_n4[i],
            "<-4":    0.0,
        }
    return data

TABLES = {
    ZONE_MILD:     _build_mild(),
    ZONE_MODERATE: _build_moderate(),
    ZONE_SEVERE:   _build_severe(),
}

def temp_band_from_f(temp_f):
    """Return the table key for a given °F value."""
    if temp_f > 32:
        return ">32"
    elif temp_f >= 14:
        return "32to14"
    elif temp_f >= -4:
        return "14to-4"
    else:
        return "<-4"

def lookup_target(zone, temp_f, baseline_pa):
    """
    Look up minimum target pressure.
    baseline_pa: positive Pa reading pre-fan.
    Returns negative Pa target, or None.
    """
    try:
        table = TABLES[zone]
        band  = temp_band_from_f(temp_f)
        b     = max(0.0, min(6.0, baseline_pa))
        key   = round(round(b * 10))
        return table[key][band]
    except Exception:
        return None


# =============================================================
# Shared state
# =============================================================

lock = threading.Lock()

current_pressure1 = None
current_temp1     = None
current_pressure2 = None
current_temp2     = None

zero_offset1 = 0.0
zero_offset2 = 0.0

# Boot stages: "zeroing" | "pick_temp" | "pick_zone" | "lock_baseline" | "running"
boot_stage  = "zeroing"

# Temperature selection state
temp_clicks      = 0          # clicks received in pick_temp stage
temp_band_choice = None       # chosen band string e.g. ">32" or "auto"
outdoor_temp_f   = None       # resolved °F value used for table lookup

# Zone selection state
climate_zone = DEFAULT_ZONE
zone_clicks  = 0

# Per-sensor calibration
baseline1 = None
baseline2 = None
target1   = None
target2   = None

# WiFi
wifi_mode   = "searching"
is_host     = False
sensor_data = {}

active          = True
current_battery = None

# Timers for click-timeout detection
_temp_click_timer = None
_zone_click_timer = None


# =============================================================
# Battery
# =============================================================

def read_battery():
    try:
        s = socket.socket()
        s.settimeout(1)
        s.connect(('127.0.0.1', 8423))
        s.send(b'get battery\n')
        data = s.recv(64).decode().strip()
        s.close()
        return float(data.split(':')[1].strip())
    except Exception:
        return None

def battery_poll_loop():
    global current_battery
    while True:
        val = read_battery()
        with lock:
            current_battery = val
        time.sleep(30)

def draw_battery_bar(draw, pct):
    BAR_X = 238
    BAR_W = 2
    BAR_H = 280
    if pct is None:
        draw.rectangle([BAR_X, 0, BAR_X + BAR_W - 1, BAR_H - 1], fill=(40, 40, 40))
        return
    pct    = max(0.0, min(100.0, pct))
    filled = int(BAR_H * pct / 100.0)
    for y in range(filled):
        ratio = y / BAR_H
        if ratio < 0.5:
            r = int(255 * (ratio * 2)); g = 200
        else:
            r = 255; g = int(200 * (1 - (ratio - 0.5) * 2))
        draw.line([(BAR_X, y), (BAR_X + BAR_W - 1, y)], fill=(r, g, 0))
    if filled < BAR_H:
        draw.rectangle([BAR_X, filled, BAR_X + BAR_W - 1, BAR_H - 1], fill=(30, 30, 30))


# =============================================================
# WiFi
# =============================================================

def scan_for(ssid, retries=2):
    for _ in range(retries):
        try:
            result = subprocess.run(
                ['sudo', 'nmcli', '-t', '-f', 'SSID', 'dev', 'wifi',
                 'list', '--rescan', 'yes'],
                capture_output=True, text=True, timeout=20)
            if ssid in result.stdout:
                return True
        except Exception as e:
            print(f"Scan error: {e}")
        time.sleep(2)
    return False

def connect_to(ssid, password):
    try:
        subprocess.run(['sudo', 'nmcli', 'dev', 'wifi', 'connect',
                        ssid, 'password', password],
                       check=True, timeout=30)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"Failed to connect to {ssid}: {e}")
        return False

def create_hotspot():
    try:
        subprocess.run(['sudo', 'nmcli', 'dev', 'wifi', 'hotspot',
                        'ifname', 'wlan0', 'ssid', SITE_SSID,
                        'password', SITE_PASSWORD],
                       check=True, timeout=30)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"Hotspot error: {e}")
        return False

def get_host_ip():
    try:
        result = subprocess.run(['ip', '-4', 'addr', 'show', 'wlan0'],
                                capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if 'inet ' in line:
                return line.strip().split()[1].split('/')[0]
    except Exception:
        pass
    return None

def already_connected_to():
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'ACTIVE,SSID', 'dev', 'wifi'],
            capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            if line.startswith('yes:'):
                return line.split(':', 1)[1].strip()
    except Exception:
        pass
    return None

def setup_wifi():
    global wifi_mode
    current = already_connected_to()
    if current == HOME_SSID:
        wifi_mode = "home"; return "home"
    if current == SITE_SSID:
        wifi_mode = "client"; return "client"
    if scan_for(HOME_SSID):
        if connect_to(HOME_SSID, HOME_PASSWORD):
            wifi_mode = "home"; return "home"
    delay = random.uniform(1, 5)
    time.sleep(delay)
    if scan_for(SITE_SSID):
        if connect_to(SITE_SSID, SITE_PASSWORD):
            wifi_mode = "client"; return "client"
    if create_hotspot():
        wifi_mode = "host"; return "host"
    wifi_mode = "searching"; return "searching"


# =============================================================
# Sensor
# =============================================================

def init_sensor(bus):
    try:
        bus.i2c_rdwr(i2c_msg.write(0x00, [0x06]))
        time.sleep(0.05)
    except Exception:
        pass

def read_sdp_raw(bus, address):
    try:
        bus.i2c_rdwr(i2c_msg.write(address, [0x36, 0x2F]))
        time.sleep(0.05)
        read = i2c_msg.read(address, 9)
        bus.i2c_rdwr(read)
        data  = list(read)
        raw_p = (data[0] << 8) | data[1]
        if raw_p > 32767: raw_p -= 65536
        raw_t = (data[3] << 8) | data[4]
        if raw_t > 32767: raw_t -= 65536
        scale = (data[6] << 8) | data[7]
        if scale == 0: return None, None
        return raw_p / scale, raw_t / 200.0
    except Exception:
        return None, None

def celsius_to_fahrenheit(c):
    return c * 9 / 5 + 32 if c is not None else None


# =============================================================
# Boot sequence logic
# =============================================================

def do_zeroing(bus):
    global zero_offset1, zero_offset2, boot_stage
    print("Zeroing sensors...")
    samples1, samples2 = [], []
    for _ in range(ZERO_SAMPLES):
        p1, _ = read_sdp_raw(bus, SDP_ADDR_1)
        p2, _ = read_sdp_raw(bus, SDP_ADDR_2)
        if p1 is not None: samples1.append(p1)
        if p2 is not None: samples2.append(p2)
        time.sleep(0.05)
    with lock:
        zero_offset1 = sum(samples1) / len(samples1) if samples1 else 0.0
        zero_offset2 = sum(samples2) / len(samples2) if samples2 else 0.0
        boot_stage   = "pick_temp"
    print(f"Zero offsets — S1: {zero_offset1:.3f}  S2: {zero_offset2:.3f}")

def _resolve_temp_band(clicks):
    """
    Given click count, return the band string.
    1 click → "auto" (will be resolved from thermometer).
    2–5 clicks → fixed band.
    Out of range → nearest valid.
    """
    c = max(1, min(5, clicks))
    return TEMP_CLICK_BANDS[c]

def _temp_timeout():
    """Called 3s after last click in pick_temp stage — commit the choice."""
    global boot_stage, temp_band_choice, outdoor_temp_f, _temp_click_timer
    with lock:
        if boot_stage != "pick_temp":
            return
        clicks = temp_clicks
        t1     = current_temp1
        t2     = current_temp2

    band = _resolve_temp_band(clicks)

    if band == "auto":
        # Use thermometer average
        temps = [t for t in [t1, t2] if t is not None]
        if temps:
            avg_c  = sum(temps) / len(temps)
            temp_f = celsius_to_fahrenheit(avg_c)
        else:
            temp_f = 50.0   # safe fallback if no sensor reads
        resolved_band = temp_band_from_f(temp_f)
        print(f"Auto temp: {temp_f:.1f}°F → band {resolved_band}")
    else:
        temp_f        = TEMP_BAND_MIDPOINT_F[band]
        resolved_band = band
        print(f"Manual temp: {clicks} clicks → band {resolved_band}")

    with lock:
        temp_band_choice = resolved_band
        outdoor_temp_f   = temp_f
        boot_stage       = "pick_zone"
        _temp_click_timer = None

def _zone_timeout():
    """Called 3s after last click in pick_zone stage — commit zone."""
    global boot_stage, _zone_click_timer
    with lock:
        if boot_stage == "pick_zone":
            boot_stage = "lock_baseline"
            _zone_click_timer = None
            print(f"Zone locked: {climate_zone}")

def advance_boot_stage():
    """Called on every short button press during boot."""
    global boot_stage, temp_clicks, zone_clicks, climate_zone
    global baseline1, baseline2, target1, target2
    global _temp_click_timer, _zone_click_timer

    with lock:
        stage = boot_stage

    if stage == "pick_temp":
        # Cancel existing timer, increment clicks, restart timer
        with lock:
            temp_clicks += 1
            clicks = temp_clicks
        if _temp_click_timer is not None:
            _temp_click_timer.cancel()
        _temp_click_timer = threading.Timer(CLICK_TIMEOUT, _temp_timeout)
        _temp_click_timer.start()
        print(f"Temp click {clicks} → {TEMP_CLICK_BANDS.get(min(5,clicks), '?')}")

    elif stage == "pick_zone":
        with lock:
            zone_clicks += 1
            zc = zone_clicks
            zones = [ZONE_MILD, ZONE_MODERATE, ZONE_SEVERE]
            climate_zone = zones[(zc - 1) % 3]
        if _zone_click_timer is not None:
            _zone_click_timer.cancel()
        _zone_click_timer = threading.Timer(CLICK_TIMEOUT, _zone_timeout)
        _zone_click_timer.start()
        print(f"Zone click {zc} → {climate_zone}")

    elif stage == "lock_baseline":
        with lock:
            p1 = current_pressure1
            p2 = current_pressure2
            tf = outdoor_temp_f if outdoor_temp_f is not None else 50.0
            z  = climate_zone
            baseline1  = p1
            baseline2  = p2
            target1    = lookup_target(z, tf, p1) if p1 is not None else None
            target2    = lookup_target(z, tf, p2) if p2 is not None else None
            boot_stage = "running"
        print(f"Baseline S1: {baseline1} → target {target1} Pa")
        print(f"Baseline S2: {baseline2} → target {target2} Pa")

def re_enter_setup():
    global boot_stage, temp_clicks, temp_band_choice, outdoor_temp_f
    global zone_clicks, climate_zone, baseline1, baseline2, target1, target2
    global _temp_click_timer, _zone_click_timer
    if _temp_click_timer: _temp_click_timer.cancel()
    if _zone_click_timer: _zone_click_timer.cancel()
    with lock:
        boot_stage       = "pick_temp"
        temp_clicks      = 0
        temp_band_choice = None
        outdoor_temp_f   = None
        zone_clicks      = 0
        climate_zone     = DEFAULT_ZONE
        baseline1        = None
        baseline2        = None
        target1          = None
        target2          = None
    print("Setup restarted by long press")


# =============================================================
# Button handler
# =============================================================

_button_press_time = None

def button_down():
    global _button_press_time
    _button_press_time = time.time()

def button_up():
    global active, _button_press_time
    if _button_press_time is None:
        return
    held = time.time() - _button_press_time
    _button_press_time = None

    with lock:
        stage = boot_stage

    if held >= HOLD_SECONDS:
        if stage == "running":
            re_enter_setup()
        return

    # Short press
    if stage == "running":
        with lock:
            active = not active
        if active:
            board.set_backlight(80)
        else:
            board.set_backlight(0)
            board.fill_screen(0)
    else:
        advance_boot_stage()


# =============================================================
# Screen drawing
# =============================================================

def load_splash():
    try:
        img = Image.open('/home/pi/pfe-sensor/marten_screen.png').convert('RGB')
        return image_to_pixels(img)
    except Exception:
        return None
def make_error_screen(errors):
    img  = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fp   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fr   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_med   = ImageFont.truetype(fp, 18) if os.path.exists(fp) else ImageFont.load_default()
    f_small = ImageFont.truetype(fr, 14) if os.path.exists(fr) else ImageFont.load_default()

    # Red header bar
    draw.rectangle([(0, 0), (240, 36)], fill=(180, 0, 0))
    draw.text((8, 8), "HARDWARE ERROR", font=f_med, fill=(255, 255, 255))

    draw.text((8, 48), DEVICE_NAME, font=f_small, fill=(120, 120, 255))

    y = 80
    for err in errors:
        draw.text((8, y), err, font=f_small, fill=(255, 80, 80))
        y += 28

    draw.text((8, 200), "Fix then reboot:", font=f_small, fill=(180, 180, 180))
    draw.text((8, 222), "raspi-config nonint", font=f_small, fill=(255, 200, 0))
    draw.text((8, 244), "do_spi 0  /  do_i2c 0", font=f_small, fill=(255, 200, 0))
    draw.text((8, 266), "[ btn ] continue anyway", font=f_small, fill=(100, 180, 255))

    return image_to_pixels(img)

def self_test(board):
    """Check SPI (Whisplay) and I2C sensors. Returns list of error strings."""
    errors = []

    # SPI — if we got here, board init succeeded (SPI works)
    # I2C — try to ping 0x25 and 0x26
    try:
        with SMBus(1) as bus:
            try:
                bus.read_byte(SDP_ADDR_1)
            except Exception:
                errors.append("NO SENSOR at 0x25 (S1)")
            try:
                bus.read_byte(SDP_ADDR_2)
            except Exception:
                errors.append("NO SENSOR at 0x26 (S2)")
    except Exception:
        errors.append("I2C BUS FAILED")

    return errors
def image_to_pixels(img):
    pixels = []
    for r, g, b in img.getdata():
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        pixels.extend([(rgb565 >> 8) & 0xFF, rgb565 & 0xFF])
    return pixels

def _font(fp, fr, size, bold=False):
    try:
        return ImageFont.truetype(fp if bold else fr, size)
    except Exception:
        return ImageFont.load_default()

def make_screen_boot(stage, temp_c, zone, temp_clicks_count, zone_clicks_count, temp_band_chosen):
    img  = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fp   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fr   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_big   = _font(fp, fr, 34, True)
    f_med   = _font(fp, fr, 18, True)
    f_small = _font(fp, fr, 14, False)
    f_tiny  = _font(fp, fr, 12, False)

    draw.text((6, 4), DEVICE_NAME, font=f_small, fill=(120, 120, 255))
    draw.text((160, 4), "SETUP", font=f_small, fill=(255, 200, 50))
    draw.line([(0, 24), (240, 24)], fill=(50, 50, 50), width=1)

    if stage == "zeroing":
        draw.text((20, 60), "Zeroing sensors...", font=f_small, fill=(180, 180, 180))
        draw.text((20, 90), "Please wait", font=f_tiny, fill=(120, 120, 120))

    elif stage == "pick_temp":
        draw.text((6, 32), "Outdoor temp?", font=f_med, fill=(200, 200, 255))

        # Show current selection based on clicks so far
        clicks = temp_clicks_count
        if clicks == 0:
            # Nothing clicked yet — show the options
            draw.text((6,  68), "1 click  = AUTO (thermometer)", font=f_tiny, fill=(100, 220, 255))
            draw.text((6,  86), "2 clicks = below -4\u00b0F",          font=f_tiny, fill=(150, 150, 200))
            draw.text((6, 104), "3 clicks = -4\u00b0F to 14\u00b0F",    font=f_tiny, fill=(150, 150, 200))
            draw.text((6, 122), "4 clicks = 14\u00b0F to 32\u00b0F",    font=f_tiny, fill=(150, 150, 200))
            draw.text((6, 140), "5 clicks = above 32\u00b0F",           font=f_tiny, fill=(150, 150, 200))
            draw.text((6, 200), "Press to begin", font=f_tiny, fill=(180, 180, 80))
        else:
            # Show what's selected so far
            c = min(5, clicks)
            band = TEMP_CLICK_BANDS[c]
            if band == "auto":
                temp_f = celsius_to_fahrenheit(temp_c) if temp_c is not None else None
                if temp_f is not None:
                    label = f"AUTO: {temp_f:.0f}\u00b0F"
                else:
                    label = "AUTO: --\u00b0F"
                color = (100, 220, 255)
            else:
                label = TEMP_BAND_LABELS[band]
                color = (200, 220, 100)

            # Click counter dots
            dot_x = 6
            for i in range(5):
                col = (255, 200, 50) if i < c else (50, 50, 50)
                draw.ellipse([dot_x + i*22, 58, dot_x + i*22 + 14, 72], fill=col)

            draw.text((6, 82), label, font=f_med, fill=color)
            draw.text((6, 200), "Wait 3s to confirm", font=f_tiny, fill=(180, 180, 80))

    elif stage == "pick_zone":
        zones  = [ZONE_MILD, ZONE_MODERATE, ZONE_SEVERE]
        zc     = zone_clicks_count
        current = zones[(zc - 1) % 3] if zc > 0 else DEFAULT_ZONE
        draw.text((6, 32), "Climate zone?", font=f_med, fill=(200, 200, 255))
        colors = {ZONE_MILD: (100,255,100), ZONE_MODERATE: (255,200,50), ZONE_SEVERE: (255,80,80)}
        draw.text((6, 68), current.upper(), font=f_big, fill=colors.get(current, (200,200,200)))
        draw.text((6, 130), "1 click  = Mild",     font=f_tiny, fill=(150,150,150))
        draw.text((6, 148), "2 clicks = Moderate",  font=f_tiny, fill=(150,150,150))
        draw.text((6, 166), "3 clicks = Severe",    font=f_tiny, fill=(150,150,150))
        draw.text((6, 210), "Wait 3s to confirm",   font=f_tiny, fill=(180,180,80))

        # Show selected temp band as reminder
        if temp_band_chosen:
            draw.text((6, 254), f"Temp: {TEMP_BAND_LABELS.get(temp_band_chosen,'?')}",
                      font=f_tiny, fill=(80,80,160))

    elif stage == "lock_baseline":
        draw.text((6,  32), "Ready to lock",       font=f_med, fill=(200, 200, 255))
        draw.text((6,  58), "baseline pressure",   font=f_med, fill=(200, 200, 255))
        draw.text((6, 110), "Fan must be OFF",     font=f_small, fill=(255, 180, 50))
        draw.text((6, 132), "Tubes in test holes", font=f_small, fill=(255, 180, 50))
        if temp_band_chosen:
            draw.text((6, 175), f"Temp: {TEMP_BAND_LABELS.get(temp_band_chosen,'?')}",
                      font=f_tiny, fill=(80, 120, 200))
        draw.text((6, 195), f"Zone: {zone.upper()}", font=f_tiny, fill=(80, 120, 200))
        draw.text((6, 230), "Press to lock baseline", font=f_tiny, fill=(180, 180, 80))

    with lock:
        batt = current_battery
    draw_battery_bar(draw, batt)
    return image_to_pixels(img)

def make_screen_running(p1, p2, t1, tgt1, tgt2, mode):
    img  = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fp   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fr   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_big   = _font(fp, fr, 38, True)
    f_med   = _font(fp, fr, 18, True)
    f_small = _font(fp, fr, 14, False)
    f_tiny  = _font(fp, fr, 12, False)

    mode_colors = {"home":(100,200,100), "host":(200,150,50),
                   "client":(100,180,255), "client_reporting":(100,180,255),  # ← add this
                   "searching":(160,160,160)}
    mode_labels = {"home":"HOME", "host":"HOST",
                   "client":"CLIENT", "client_reporting":"CLIENT",  # ← and this
                   "searching":"..."}
    draw.text((6, 4), DEVICE_NAME, font=f_small, fill=(120,120,255))
    draw.text((160,4), mode_labels.get(mode,"?"), font=f_small,
              fill=mode_colors.get(mode,(160,160,160)))
    draw.line([(0,24),(240,24)], fill=(50,50,50), width=1)


    def draw_sensor(label, pressure, target, y_top):
        draw.text((6, y_top), label, font=f_tiny, fill=(150,150,150))
        if pressure is None:
            draw.text((6, y_top+16), "--", font=f_big, fill=(80,80,80))
            draw.line([(0,y_top+85),(240,y_top+85)], fill=(40,40,40), width=1)
            return
        if target is None:
            color = (80, 160, 255)
            draw.text((6,   y_top+16), f"{pressure:.2f}", font=f_big, fill=color)
            draw.text((148, y_top+16), "Pa", font=f_med, fill=(100,140,200))
        else:
            passed = pressure <= target
            color  = (0,230,0) if passed else (255,60,60)
            draw.text((180, y_top+16), "PASS" if passed else "FAIL", font=f_med, fill=color)
            draw.text((6,   y_top+16), f"{pressure:.2f}", font=f_big, fill=color)
            draw.text((148, y_top+16), "Pa", font=f_med, fill=color)
            draw.text((6,   y_top+65), f"Target: {target:.2f} Pa", font=f_small, fill=(180,180,180))
        draw.line([(0,y_top+85),(240,y_top+85)], fill=(40,40,40), width=1)

    draw_sensor("SENSOR 1", p1, tgt1, 28)
    draw_sensor("SENSOR 2", p2, tgt2, 118)

    with lock:
        tf  = outdoor_temp_f
        tbc = temp_band_choice
        z   = climate_zone
    band_label = TEMP_BAND_LABELS.get(tbc, "?") if tbc else "Temp not set"
    draw.text((6, 215), band_label,   font=f_tiny, fill=(100,100,200))
    draw.text((6, 230), z.upper(),    font=f_tiny, fill=(100,100,200))

    if mode == "host":
        with lock:
            count = len(sensor_data)
        draw.text((6, 245), f"Devices: {count}", font=f_tiny, fill=(0,180,80))
    elif mode == "client":
        draw.text((6, 245), "Reporting to host", font=f_tiny, fill=(100,180,255))

    ip = get_host_ip()
    draw.text((6, 260), f"http://{ip}" if ip else "...",
              font=f_tiny, fill=(100,180,255) if ip else (160,160,160))

    with lock:
        batt = current_battery
    draw_battery_bar(draw, batt)
    return image_to_pixels(img)

def screen_thread(board, splash):
    last = None
    last_draw_time = 0
    while True:
        with lock:
            is_active = active
            p1   = current_pressure1
            p2   = current_pressure2
            t1   = current_temp1
            tg1  = target1
            tg2  = target2
            mode = wifi_mode
            stage = boot_stage
            tc   = temp_clicks
            zc   = zone_clicks
            tbc  = temp_band_choice
            z    = climate_zone
        now = time.time()
        current = (stage, round(p1, 2) if p1 is not None else None,
                   round(p2, 2) if p2 is not None else None,
                   tg1, tg2, mode, tc, zc, tbc, z)
        if is_active and current != last and (now - last_draw_time) >= 2:
            if stage == "running":
                screen_data = make_screen_running(p1, p2, t1, tg1, tg2, mode)
            else:
                screen_data = make_screen_boot(stage, t1, z, tc, zc, tbc)
            board.draw_image(0, 0, 240, 280, screen_data)
            last = current
            last_draw_time = now
        time.sleep(0.5)


# =============================================================
# Web dashboard (unchanged)
# =============================================================

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        if self.path == '/data':
            with lock:
                now = time.time()
                data = {
                    k: {**v, 'age': now - v['time']}
                    for k, v in sensor_data.items()
                }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif self.path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(build_dashboard_html().encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/report':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            try:
                payload = json.loads(body)
                name = payload.get('name')
                if name:
                    with lock:
                        sensor_data[name] = {
                            's1':   payload.get('s1'),
                            's2':   payload.get('s2'),
                            'tgt1': payload.get('tgt1'),
                            'tgt2': payload.get('tgt2'),
                            'temp1':payload.get('temp1'),
                            'time': time.time(),
                        }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'OK')
            except Exception:
                self.send_response(400)
                self.end_headers()

def build_dashboard_html():
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PFE Sensor Dashboard</title>
  <style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Courier New', monospace; background: #0a0f1a; color: #e8edf5; padding: 20px; }
h1 { color: #e8edf5; text-align: center; margin-bottom: 4px; font-size: 1.5em; letter-spacing: 2px; }
.subtitle { text-align: center; color: #c8d8f8; font-size: 0.85em; margin-bottom: 24px; letter-spacing: 1px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; max-width: 1100px; margin: 0 auto; }
.card { background: #131c2e; border-radius: 10px; padding: 18px; border: 1.5px solid #2a3a5c; }
.card-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.card-name { font-size: 1em; font-weight: 700; color: #c8d8f8; letter-spacing: 1px; }
.badge { font-size: 0.7em; padding: 3px 10px; border-radius: 4px; font-weight: 700; letter-spacing: 1px; }
.badge-offline { background: #1e2535; color: #a0b0cc; border: 1px solid #3a4460; }
.sensors { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.sensor-box { background: #0d1525; border-radius: 8px; padding: 12px; text-align: center; border: 1px solid #1e2e4a; }
.sensor-box.pass { background: #0d1f17; border-color: #00c060; }
.sensor-box.fail { background: #1f0d0d; border-color: #e03030; }
.sensor-box.blue { background: #0d1530; border-color: #3060c0; }
.sensor-label { font-size: 0.65em; color: #a0b8d8; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 700; }
.sensor-box.pass .sensor-label { color: #00c060; }
.sensor-box.fail .sensor-label { color: #e03030; }
.sensor-box.blue .sensor-label { color: #5090ff; }
.s-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.s-badge { font-size: 0.65em; padding: 2px 7px; border-radius: 3px; font-weight: 700; letter-spacing: 1px; }
.s-badge-pass { background: #003d20; color: #00e874; border: 1px solid #00c060; }
.s-badge-fail { background: #3d0000; color: #ff6060; border: 1px solid #e03030; }
.sensor-value { font-size: 1.8em; font-weight: 700; }
.sensor-unit  { font-size: 0.75em; font-weight: 400; margin-left: 3px; }
.pass-val { color: #00e874; }
.fail-val { color: #ff6060; }
.blue-val { color: #5090ff; }
.na-val   { color: #8899bb; font-size: 1.3em; }
.sensor-sub { font-size: 0.72em; margin-top: 4px; }
.pass-val-dim { color: #00a050; }
.fail-val-dim { color: #c03030; }
.blue-val-dim { color: #4070b0; }
#footer { text-align: center; color: #8899bb; font-size: 0.78em; margin-top: 24px; }
.no-sensors { text-align: center; color: #8899bb; margin-top: 60px; grid-column: 1/-1; }
#footer { text-align: center; color: #4a5a7a; font-size: 0.78em; margin-top: 24px; }
.no-sensors { text-align: center; color: #4a5a7a; margin-top: 60px; grid-column: 1/-1; }
  </style>
</head>
<body>
  <h1>PFE RADON SENSOR DASHBOARD</h1>
  <p class="subtitle" id="sub">Loading...</p>
  <div class="grid" id="grid"><div class="no-sensors">Waiting for sensors...</div></div>
  <div id="footer"></div>
  <script>
function fmtPa(v)   { return (v!=null) ? v.toFixed(2) : '--'; }
function fmtInWC(v) { return (v!=null) ? (Math.abs(v)/249.0).toFixed(4) : '--'; }

function sensorBox(label, val, tgt, stale) {
  if (stale || val === null || val === undefined) {
    return `<div class="sensor-box">
      <div class="s-top"><span class="sensor-label">${label}</span></div>
      <div class="sensor-value na-val">--</div>
      <div class="sensor-sub">&nbsp;</div>
    </div>`;
  }
  if (tgt === null || tgt === undefined) {
    return `<div class="sensor-box blue">
      <div class="s-top"><span class="sensor-label">${label}</span></div>
      <div class="sensor-value blue-val">${fmtPa(val)}<span class="sensor-unit">Pa</span></div>
      <div class="sensor-sub">&nbsp;</div>
    </div>`;
  }
  const pass = val <= tgt;
  const cls  = pass ? 'pass' : 'fail';
  const vc   = pass ? 'pass-val' : 'fail-val';
  const dc   = pass ? 'pass-val-dim' : 'fail-val-dim';
  const bc   = pass ? 's-badge-pass' : 's-badge-fail';
  const bt   = pass ? 'PASS' : 'FAIL';
  return `<div class="sensor-box ${cls}">
    <div class="s-top">
      <span class="sensor-label">${label}</span>
      <span class="s-badge ${bc}">${bt}</span>
    </div>
    <div class="sensor-value ${vc}">${fmtPa(val)}<span class="sensor-unit">Pa</span></div>
    <div class="sensor-sub ${dc}">Target: ${fmtPa(tgt)} Pa</div>
  </div>`;
}

async function refresh() {
  try {
    const res  = await fetch('/data');
    const data = await res.json();
    const names = Object.keys(data).sort();
    const grid = document.getElementById('grid');
    if (!names.length) {
      grid.innerHTML = '<div class="no-sensors">Waiting for sensors...</div>';
      return;
    }
    grid.innerHTML = names.map(name => {
      const s     = data[name];
      const stale = s.age > 30;
      const badge = stale ? '<span class="badge badge-offline">OFFLINE</span>' : '';
      return `<div class="card">
        <div class="card-top"><span class="card-name">${name}</span>${badge}</div>
        <div class="sensors">
          ${sensorBox('Sensor 1', s.s1, s.tgt1, stale)}
          ${sensorBox('Sensor 2', s.s2, s.tgt2, stale)}
        </div>
      </div>`;
    }).join('');
    document.getElementById('footer').textContent = 'Updated: ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('footer').textContent = 'Connection lost...';
  }
}
refresh();
setInterval(refresh, 1000);
  </script>
</body>
</html>"""

def run_web_server():
    try:
        server = HTTPServer(('0.0.0.0', WEB_PORT), DashboardHandler)
        print(f"Web server on port {WEB_PORT}")
        server.serve_forever()
    except Exception as e:
        print(f"Web server error: {e}")


# =============================================================
# Data reporter (client only)
# =============================================================

def report_data_loop(host_ip):
    url = f"http://{host_ip}/report"
    while True:
        try:
            with lock:
                p1  = current_pressure1
                p2  = current_pressure2
                t1  = current_temp1
                tg1 = target1
                tg2 = target2
            payload = json.dumps({
                'name':  DEVICE_NAME,
                's1':    p1,
                's2':    p2,
                'tgt1':  tg1,
                'tgt2':  tg2,
                'temp1': t1,
            }).encode()
            req = Request(url, data=payload,
                          headers={'Content-Type': 'application/json'})
            urlopen(req, timeout=3)
        except Exception as e:
            print(f"Report error: {e}")
        time.sleep(1)


# =============================================================
# Main
# =============================================================

print(f"Starting PFE Sensor — {DEVICE_NAME}")
board = WhisPlayBoard()
board.set_backlight(80)

board.on_button_press(button_down)
board.on_button_release(button_up)

splash = load_splash()
if splash:
    board.draw_image(0, 0, 240, 280, splash)
    time.sleep(2)



threading.Thread(target=screen_thread, args=(board, None), daemon=True).start()
threading.Thread(target=battery_poll_loop, daemon=True).start()

threading.Thread(target=lambda: (
    setup_wifi(),
    run_web_server() if wifi_mode == "host" else None
), daemon=True).start()

with SMBus(1) as bus:
    init_sensor(bus)
    do_zeroing(bus)
    time.sleep(2) 
    errors = self_test(board)

    while True:
        p1_raw, t1 = read_sdp_raw(bus, SDP_ADDR_1)
        p2_raw, t2 = read_sdp_raw(bus, SDP_ADDR_2)

        p1 = (p1_raw - zero_offset1) if p1_raw is not None else None
        p2 = (p2_raw - zero_offset2) if p2_raw is not None else None

        with lock:
            current_pressure1 = p1
            current_temp1     = t1
            current_pressure2 = p2
            current_temp2     = t2

        print(f"DEBUG: wifi_mode={wifi_mode} boot_stage={boot_stage}")
        if wifi_mode == "host" and boot_stage == "running":
            with lock:
                sensor_data[DEVICE_NAME] = {
                    's1':   p1,
                    's2':   p2,
                    'tgt1': target1,
                    'tgt2': target2,
                    'temp1':t1,
                    'time': time.time(),
                }

        if wifi_mode == "client" and boot_stage == "running":
            host_ip = get_host_ip()
            if host_ip:
                threading.Thread(target=report_data_loop,
                                 args=(host_ip,), daemon=True).start()
                wifi_mode = "client_reporting"

        s1_str = f"{p1:.2f} Pa" if p1 is not None else "--"
        s2_str = f"{p2:.2f} Pa" if p2 is not None else "--"
        print(f"S1: {s1_str}  S2: {s2_str}  Stage: {boot_stage}")
        time.sleep(1)
                
