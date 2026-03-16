#!/usr/bin/env python3
"""
PFE Pressure Sensor
- At home: connects to PFE-home for updates/testing
- In field: auto-assigns as host (creates PFE-NET + runs dashboard)
            or client (joins PFE-NET + sends data)
Same code runs on every device.

Boot sequence:
  1. Zero both sensors (rapid averaging)
  2. Confirm outdoor temp from SDP800 thermometer
  3. Pick climate zone (1/2/3 clicks, default Moderate)
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

HOST_IP       = "192.168.4.1"
WEB_PORT      = 80

# Climate zones
ZONE_MILD     = "mild"
ZONE_MODERATE = "moderate"
ZONE_SEVERE   = "severe"
DEFAULT_ZONE  = ZONE_MODERATE   # Colorado default

# Button timing
HOLD_SECONDS  = 2.0   # hold duration to re-enter setup

# Zero-calibration
ZERO_SAMPLES  = 50    # number of readings to average for zero offset

# =============================================================
# Target pressure lookup tables
# Indexed by round(baseline * 10) so 0.5 Pa → key 5
# Four temp bands per zone: ">32", "32to14", "14to-4", "<-4"
# Baseline range: 0.0 to 6.0 Pa in 0.1 steps
# =============================================================

def _build_mild():
    # >32°F: target = -baseline (1:1)
    # 32–14°F: target = -baseline * 0.4 (approx, rounded to 0.1)
    # 14 to -4°F and below: 0.0
    data = {}
    for i in range(61):   # 0 to 6.0 in 0.1 steps
        b = round(i * 0.1, 1)
        data[i] = {
            ">32":    round(-b, 1),
            "32to14": round(-b * 0.4, 1),
            "14to-4": 0.0,
            "<-4":    0.0,
        }
    return data

def _build_moderate():
    # >32°F: target = -baseline * 1.2 (approx from table)
    # 32–14°F: target = -baseline * 0.5
    # 14 to -4°F: 0.0
    # <-4°F: 0.0
    # Using exact table values for key points, interpolating between
    # Exact values from PDF:
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
    # Exact values from PDF
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

def temp_band(temp_f):
    """Return the temperature band string for a given °F value."""
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
    Look up minimum target pressure from table.
    baseline_pa: the pre-fan reading (positive Pa, e.g. +0.5)
    Returns target Pa (negative) or None if inputs invalid.
    """
    try:
        table = TABLES[zone]
        band  = temp_band(temp_f)
        # Round baseline to nearest 0.1, clamp 0–6
        b = max(0.0, min(6.0, baseline_pa))
        key = round(round(b * 10) )   # integer key 0–60
        return table[key][band]
    except Exception:
        return None


# =============================================================
# Shared state
# =============================================================

lock = threading.Lock()

# Live sensor readings
current_pressure1 = None
current_temp1     = None
current_pressure2 = None
current_temp2     = None

# Zero offsets applied at boot
zero_offset1 = 0.0
zero_offset2 = 0.0

# Boot/calibration state
# Stages: "zeroing" | "confirm_temp" | "pick_zone" | "lock_baseline" | "running"
boot_stage     = "zeroing"
outdoor_temp_f = None       # confirmed outdoor temp in °F
climate_zone   = DEFAULT_ZONE
zone_clicks    = 0          # how many clicks received in pick_zone stage

# Per-sensor calibration
baseline1      = None       # locked baseline Pa for sensor 1
baseline2      = None       # locked baseline Pa for sensor 2
target1        = None       # calculated target Pa for sensor 1
target2        = None       # calculated target Pa for sensor 2

# WiFi / host state
wifi_mode  = "searching"
is_host    = False
sensor_data = {}   # host only: { "PFE-1": { s1, s2, t1, t2, tgt1, tgt2, time } }

# Screen on/off
active = True

# Battery
current_battery = None   # 0.0–100.0 or None if unreadable


# =============================================================
# Battery (PiSugar 3)
# =============================================================

def read_battery():
    """Read battery % from PiSugar 3 server. Returns float 0–100 or None."""
    try:
        s = socket.socket()
        s.settimeout(1)
        s.connect(('127.0.0.1', 8423))
        s.send(b'get battery\n')
        data = s.recv(64).decode().strip()
        s.close()
        # Response: "battery: 80.45"
        return float(data.split(':')[1].strip())
    except Exception:
        return None

def battery_poll_loop():
    """Update battery reading every 30 seconds."""
    global current_battery
    while True:
        val = read_battery()
        with lock:
            current_battery = val
        time.sleep(30)

def draw_battery_bar(draw, pct):
    """
    Draw a 2px wide battery bar on the right edge of the screen (x=238–239).
    Full height = 280px. Green top → yellow middle → red bottom.
    Filled from top down based on charge level.
    If pct is None, draw a dim gray bar.
    """
    BAR_X     = 238
    BAR_W     = 2
    BAR_H     = 280

    if pct is None:
        draw.rectangle([BAR_X, 0, BAR_X + BAR_W - 1, BAR_H - 1], fill=(40, 40, 40))
        return

    pct = max(0.0, min(100.0, pct))
    filled = int(BAR_H * pct / 100.0)   # pixels filled from top

    # Draw filled portion with color gradient: green→yellow→red top to bottom
    for y in range(filled):
        ratio = y / BAR_H   # 0 at top, 1 at bottom
        if ratio < 0.5:
            # Green → Yellow
            r = int(255 * (ratio * 2))
            g = 200
        else:
            # Yellow → Red
            r = 255
            g = int(200 * (1 - (ratio - 0.5) * 2))
        draw.line([(BAR_X, y), (BAR_X + BAR_W - 1, y)], fill=(r, g, 0))

    # Draw empty portion (dim)
    if filled < BAR_H:
        draw.rectangle([BAR_X, filled, BAR_X + BAR_W - 1, BAR_H - 1], fill=(30, 30, 30))


# =============================================================
# WiFi (unchanged from original)
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
    """Return the SSID we are currently connected to, or None."""
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

    # Check if already connected before doing anything
    current = already_connected_to()
    if current == HOME_SSID:
        print(f"Already connected to {HOME_SSID} — staying home")
        wifi_mode = "home"
        return "home"
    if current == SITE_SSID:
        print(f"Already connected to {SITE_SSID} — joining as client")
        wifi_mode = "client"
        return "client"

    # Not connected — scan and decide
    print(f"Checking for {HOME_SSID}...")
    if scan_for(HOME_SSID):
        if connect_to(HOME_SSID, HOME_PASSWORD):
            wifi_mode = "home"
            return "home"

    delay = random.uniform(1, 5)
    print(f"No home network. Waiting {delay:.1f}s before checking for {SITE_SSID}...")
    time.sleep(delay)

    print(f"Checking for {SITE_SSID}...")
    if scan_for(SITE_SSID):
        if connect_to(SITE_SSID, SITE_PASSWORD):
            wifi_mode = "client"
            return "client"

    print("No networks found — becoming host")
    if create_hotspot():
        wifi_mode = "host"
        return "host"

    wifi_mode = "searching"
    return "searching"


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
    """Read raw pressure and temp. Returns (pressure_pa, temp_c) or (None, None)."""
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
    """Average ZERO_SAMPLES readings on each sensor to find offsets."""
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
        boot_stage   = "confirm_temp"
    print(f"Zero offsets — S1: {zero_offset1:.3f} Pa  S2: {zero_offset2:.3f} Pa")

def advance_boot_stage():
    """Called on button press during boot sequence."""
    global boot_stage, outdoor_temp_f, climate_zone, zone_clicks
    global baseline1, baseline2, target1, target2

    with lock:
        stage = boot_stage

    if stage == "confirm_temp":
        # Confirm the temp shown on screen
        with lock:
            # Use average of both sensor temps, converted to °F
            t1 = current_temp1
            t2 = current_temp2
            temps = [t for t in [t1, t2] if t is not None]
            if temps:
                avg_c = sum(temps) / len(temps)
                outdoor_temp_f = round(celsius_to_fahrenheit(avg_c), 1)
            else:
                outdoor_temp_f = 50.0   # safe fallback
            boot_stage = "pick_zone"
            zone_clicks = 0
        print(f"Outdoor temp confirmed: {outdoor_temp_f}°F")

    elif stage == "pick_zone":
        # Each click cycles through zones; after 3s with no click → advance
        with lock:
            zone_clicks += 1
            zones = [ZONE_MILD, ZONE_MODERATE, ZONE_SEVERE]
            climate_zone = zones[(zone_clicks - 1) % 3]
        print(f"Zone click {zone_clicks} → {climate_zone}")
        # Start a timer to auto-advance if no more clicks
        threading.Timer(3.0, _zone_timeout).start()

    elif stage == "lock_baseline":
        # Lock both sensor baselines right now
        with lock:
            p1 = current_pressure1
            p2 = current_pressure2
            tf = outdoor_temp_f if outdoor_temp_f is not None else 50.0
            z  = climate_zone

            baseline1 = p1
            baseline2 = p2
            target1   = lookup_target(z, tf, p1) if p1 is not None else None
            target2   = lookup_target(z, tf, p2) if p2 is not None else None
            boot_stage = "running"

        print(f"Baseline locked — S1: {baseline1} Pa → target {target1} Pa")
        print(f"Baseline locked — S2: {baseline2} Pa → target {target2} Pa")

def _zone_timeout():
    """Auto-advance from pick_zone to lock_baseline after 3s silence."""
    global boot_stage
    with lock:
        if boot_stage == "pick_zone":
            boot_stage = "lock_baseline"
            print(f"Zone locked: {climate_zone} — waiting for baseline click")

def re_enter_setup():
    """Called on long press — restart the boot sequence."""
    global boot_stage, outdoor_temp_f, climate_zone, zone_clicks
    global baseline1, baseline2, target1, target2
    with lock:
        boot_stage     = "confirm_temp"
        outdoor_temp_f = None
        zone_clicks    = 0
        baseline1      = None
        baseline2      = None
        target1        = None
        target2        = None
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
        # Long press: re-enter setup OR toggle screen
        if stage == "running":
            re_enter_setup()
        return

    # Short press
    if stage == "running":
        # Toggle screen on/off
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
# Screen
# =============================================================

def load_splash():
    try:
        img = Image.open('/home/pi/pfe-sensor/marten_screen.png').convert('RGB')
        return image_to_pixels(img)
    except Exception:
        return None

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

def make_screen_boot(stage, temp_c, zone, zone_clicks_count):
    """Draw the boot/setup screens."""
    img  = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fp   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fr   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_big   = _font(fp, fr, 36, True)
    f_med   = _font(fp, fr, 20, True)
    f_small = _font(fp, fr, 15, False)
    f_tiny  = _font(fp, fr, 13, False)

    draw.text((6, 4), DEVICE_NAME, font=f_small, fill=(120, 120, 255))
    draw.text((160, 4), "SETUP", font=f_small, fill=(255, 200, 50))
    draw.line([(0, 24), (240, 24)], fill=(50, 50, 50), width=1)

    if stage == "zeroing":
        draw.text((20, 60),  "Zeroing sensors...", font=f_small, fill=(180, 180, 180))
        draw.text((20, 90),  "Please wait", font=f_tiny, fill=(120, 120, 120))

    elif stage == "confirm_temp":
        temp_f = celsius_to_fahrenheit(temp_c) if temp_c is not None else None
        draw.text((10, 40), "Outdoor temp?", font=f_med, fill=(200, 200, 255))
        if temp_f is not None:
            draw.text((10, 75),  f"{temp_f:.1f} °F", font=f_big, fill=(100, 220, 255))
            draw.text((10, 125), f"({temp_c:.1f} °C)", font=f_small, fill=(100, 150, 180))
        else:
            draw.text((10, 75), "-- °F", font=f_big, fill=(80, 80, 80))
        draw.text((10, 200), "Press button to confirm", font=f_tiny, fill=(180, 180, 80))

    elif stage == "pick_zone":
        zones = [ZONE_MILD, ZONE_MODERATE, ZONE_SEVERE]
        current = zones[(zone_clicks_count - 1) % 3] if zone_clicks_count > 0 else DEFAULT_ZONE
        draw.text((10, 40), "Climate zone?", font=f_med, fill=(200, 200, 255))
        colors = {ZONE_MILD: (100, 255, 100), ZONE_MODERATE: (255, 200, 50), ZONE_SEVERE: (255, 80, 80)}
        draw.text((10, 80), current.upper(), font=f_big, fill=colors.get(current, (200,200,200)))
        draw.text((10, 140), "1 click = Mild", font=f_tiny, fill=(150,150,150))
        draw.text((10, 158), "2 clicks = Moderate", font=f_tiny, fill=(150,150,150))
        draw.text((10, 176), "3 clicks = Severe", font=f_tiny, fill=(150,150,150))
        draw.text((10, 210), "Wait 3s to confirm", font=f_tiny, fill=(180, 180, 80))

    elif stage == "lock_baseline":
        draw.text((10, 40),  "Ready to lock", font=f_med, fill=(200, 200, 255))
        draw.text((10, 70),  "baseline pressure", font=f_med, fill=(200, 200, 255))
        draw.text((10, 120), "Ensure fan is OFF", font=f_small, fill=(255, 180, 50))
        draw.text((10, 145), "and tubes are placed", font=f_small, fill=(255, 180, 50))
        draw.text((10, 200), "Press button to lock", font=f_tiny, fill=(180, 180, 80))

    with lock:
        batt = current_battery
    draw_battery_bar(draw, batt)

    return image_to_pixels(img)

def make_screen_running(p1, p2, t1, tgt1, tgt2, mode):
    """Draw the main running screen with per-sensor pass/fail."""
    img  = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fp   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fr   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_big   = _font(fp, fr, 38, True)
    f_med   = _font(fp, fr, 18, True)
    f_small = _font(fp, fr, 14, False)
    f_tiny  = _font(fp, fr, 12, False)

    mode_colors = {"home": (100,200,100), "host": (200,150,50),
                   "client": (100,180,255), "searching": (160,160,160)}
    mode_labels = {"home":"HOME","host":"HOST","client":"CLIENT","searching":"..."}
    draw.text((6, 4), DEVICE_NAME, font=f_small, fill=(120,120,255))
    draw.text((160,4), mode_labels.get(mode,"?"), font=f_small,
              fill=mode_colors.get(mode,(160,160,160)))
    draw.line([(0,24),(240,24)], fill=(50,50,50), width=1)

    def draw_sensor(label, pressure, target, y_top):
        draw.text((6, y_top), label, font=f_tiny, fill=(150,150,150))

        if pressure is None:
            draw.text((6, y_top+16), "--", font=f_big, fill=(80,80,80))
            draw.line([(0, y_top+85),(240, y_top+85)], fill=(40,40,40), width=1)
            return

        if target is None:
            # Blue — not calibrated yet, no target line
            color = (80, 160, 255)
            draw.text((6,   y_top+16), f"{pressure:.2f}", font=f_big, fill=color)
            draw.text((148, y_top+16), "Pa", font=f_med, fill=(100,140,200))
        else:
            passed = pressure <= target
            color  = (0, 230, 0) if passed else (255, 60, 60)
            label2 = "PASS" if passed else "FAIL"
            draw.text((180, y_top+16), label2, font=f_med, fill=color)
            draw.text((6,   y_top+16), f"{pressure:.2f}", font=f_big, fill=color)
            draw.text((148, y_top+16), "Pa", font=f_med, fill=color)
            draw.text((6,   y_top+65), f"Target: {target:.2f} Pa", font=f_small, fill=(180,180,180))

        draw.line([(0, y_top+85),(240, y_top+85)], fill=(40,40,40), width=1)

    draw_sensor("SENSOR 1", p1, tgt1, 28)
    draw_sensor("SENSOR 2", p2, tgt2, 118)

    # Footer
    with lock:
        tf  = outdoor_temp_f
        z   = climate_zone
    if tf is not None:
        draw.text((6, 215), f"{tf:.0f}°F  {z[:3].upper()}", font=f_tiny, fill=(100,100,200))
    else:
        draw.text((6, 215), "Temp not set", font=f_tiny, fill=(160,80,80))

    if mode == "host":
        with lock:
            count = len(sensor_data)
        draw.text((6, 234), f"Devices: {count}", font=f_tiny, fill=(0,180,80))
    elif mode == "client":
        draw.text((6, 234), "Reporting to host", font=f_tiny, fill=(100,180,255))

    ip = get_host_ip()
    draw.text((6, 254), f"http://{ip}" if ip else "...",
              font=f_tiny, fill=(100,180,255) if ip else (160,160,160))

    with lock:
        batt = current_battery
    draw_battery_bar(draw, batt)

    return image_to_pixels(img)

def screen_thread(board, splash):
    if splash:
        board.draw_image(0, 0, 240, 280, splash)
    last_state = None

    while True:
        with lock:
            is_active = active
            stage  = boot_stage
            p1     = current_pressure1
            p2     = current_pressure2
            t1     = current_temp1
            tgt1   = target1
            tgt2   = target2
            mode   = wifi_mode
            zc     = zone_clicks
            batt   = current_battery

        if not is_active:
            time.sleep(0.5)
            continue

        # Only redraw when something meaningful actually changed
        p1r = round(p1, 1) if p1 is not None else None
        p2r = round(p2, 1) if p2 is not None else None
        t1r = round(t1, 1) if t1 is not None else None
        br  = round(batt)  if batt is not None else None
        state = (stage, p1r, p2r, t1r, tgt1, tgt2, mode, zc, br)

        if state != last_state:
            if stage == "running":
                pixels = make_screen_running(p1, p2, t1, tgt1, tgt2, mode)
            else:
                pixels = make_screen_boot(stage, t1, climate_zone, zc)
            board.draw_image(0, 0, 240, 280, pixels)
            last_state = state

        time.sleep(0.5)



# =============================================================
# Web dashboard (host only)
# =============================================================

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        if self.path == '/data':
            with lock:
                data = dict(sensor_data)
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
.subtitle { text-align: center; color: #7a8aaa; font-size: 0.85em; margin-bottom: 24px; letter-spacing: 1px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; max-width: 1100px; margin: 0 auto; }
.card { background: #131c2e; border-radius: 10px; padding: 18px; border: 1.5px solid #2a3a5c; }
.card-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.card-name { font-size: 1em; font-weight: 700; color: #c8d8f8; letter-spacing: 1px; }
.badge { font-size: 0.7em; padding: 3px 10px; border-radius: 4px; font-weight: 700; letter-spacing: 1px; }
.badge-offline { background: #1e2535; color: #7a8aaa; border: 1px solid #3a4460; }
.sensors { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.sensor-box { background: #0d1525; border-radius: 8px; padding: 12px; text-align: center; border: 1px solid #1e2e4a; }
.sensor-box.pass { background: #0d1f17; border-color: #00c060; }
.sensor-box.fail { background: #1f0d0d; border-color: #e03030; }
.sensor-box.blue { background: #0d1530; border-color: #3060c0; }
.sensor-label { font-size: 0.65em; color: #5a7aaa; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 700; }
.sensor-box.pass .sensor-label { color: #00a050; }
.sensor-box.fail .sensor-label { color: #c03030; }
.sensor-box.blue .sensor-label { color: #4080d0; }
.s-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.s-badge { font-size: 0.65em; padding: 2px 7px; border-radius: 3px; font-weight: 700; letter-spacing: 1px; }
.s-badge-pass { background: #003d20; color: #00e874; border: 1px solid #00c060; }
.s-badge-fail { background: #3d0000; color: #ff6060; border: 1px solid #e03030; }
.sensor-value { font-size: 1.8em; font-weight: 700; }
.sensor-unit  { font-size: 0.75em; font-weight: 400; margin-left: 3px; }
.pass-val { color: #00e874; }
.fail-val { color: #ff6060; }
.blue-val { color: #5090ff; }
.na-val   { color: #3a4a6a; font-size: 1.3em; }
.sensor-sub { font-size: 0.72em; margin-top: 4px; }
.pass-val-dim { color: #00a050; }
.fail-val-dim { color: #a03030; }
.blue-val-dim { color: #3060a0; }
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
    // Blue — uncalibrated, no target line
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
    const now  = Date.now() / 1000;
    const names = Object.keys(data).sort();
    const grid = document.getElementById('grid');
    if (!names.length) {
      grid.innerHTML = '<div class="no-sensors">Waiting for sensors...</div>';
      return;
    }
    grid.innerHTML = names.map(name => {
      const s     = data[name];
      const stale = (now - s.time) > 30;
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
                p1   = current_pressure1
                p2   = current_pressure2
                t1   = current_temp1
                tg1  = target1
                tg2  = target2
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

# Wire up button — detect press/release for hold detection
board.on_button_press(button_down)
board.on_button_release(button_up)

splash = load_splash()
if splash:
    board.draw_image(0, 0, 240, 280, splash)
    time.sleep(1.5)

# Start screen thread immediately
threading.Thread(target=screen_thread, args=(board, None), daemon=True).start()

# Start battery polling
threading.Thread(target=battery_poll_loop, daemon=True).start()

# WiFi in background so boot sequence can proceed in parallel
threading.Thread(target=lambda: (
    setup_wifi(),
    run_web_server() if wifi_mode == "host" else None
), daemon=True).start()

# Sensor loop — also runs the boot zeroing first
with SMBus(1) as bus:
    init_sensor(bus)

    # Step 1: zero calibration (blocks ~3 seconds)
    do_zeroing(bus)

    while True:
        p1_raw, t1 = read_sdp_raw(bus, SDP_ADDR_1)
        p2_raw, t2 = read_sdp_raw(bus, SDP_ADDR_2)

        # Apply zero offsets
        p1 = (p1_raw - zero_offset1) if p1_raw is not None else None
        p2 = (p2_raw - zero_offset2) if p2_raw is not None else None

        with lock:
            current_pressure1 = p1
            current_temp1     = t1
            current_pressure2 = p2
            current_temp2     = t2

        # Host logs its own data
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

        # Start client reporter once running and connected
        if wifi_mode == "client" and boot_stage == "running":
            host_ip = get_host_ip()
            if host_ip:
                threading.Thread(target=report_data_loop,
                                 args=(host_ip,), daemon=True).start()
                wifi_mode = "client_reporting"   # prevent re-spawning

        s1_str = f"{p1:.2f} Pa" if p1 is not None else "--"
        s2_str = f"{p2:.2f} Pa" if p2 is not None else "--"
        print(f"S1: {s1_str}  S2: {s2_str}  Stage: {boot_stage}")
        time.sleep(1)
