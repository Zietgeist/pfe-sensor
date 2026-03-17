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
       Saved default exists → 3s countdown, auto-uses default
                              Any click → override with manual pick
       No default saved    → pick manually (1–5 clicks + 3s confirm)
                              Then prompted to save as default
  3. Pick climate zone (1/2/3 clicks + wait 3s, default Moderate)
  4. One click locks both sensor baselines
  5. Targets calculated per sensor from zone + temp + baseline
  6. Blue fields if target unknown, green/red once calibrated
"""

import sys
import os
import time
import json
import random
import threading
import subprocess
import socket
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

ZONE_MILD     = "mild"
ZONE_MODERATE = "moderate"
ZONE_SEVERE   = "severe"
DEFAULT_ZONE  = ZONE_MODERATE

HOLD_SECONDS  = 2.0
ZERO_SAMPLES  = 50
CLICK_TIMEOUT = 3.0

CONFIG_FILE   = "/home/pi/pfe-sensor/pfe_config.json"

# Temp band click map:
#   1 click → auto (read thermometer)
#   2 clicks → < -4°F
#   3 clicks → -4 to 14°F
#   4 clicks → 14 to 32°F
#   5 clicks → > 32°F
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

TEMP_BAND_MIDPOINT_F = {
    "<-4":    -10.0,
    "14to-4":   5.0,
    "32to14":  23.0,
    ">32":     50.0,
}

# =============================================================
# Config file (saves default temp band)
# =============================================================

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(data):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Config save error: {e}")

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
        data[i] = {">32": gt32[i], "32to14": t32_14[i], "14to-4": 0.0, "<-4": 0.0}
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
        data[i] = {">32": gt32[i], "32to14": t32_14[i], "14to-4": 0.0, "<-4": 0.0}
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
        data[i] = {">32": gt32[i], "32to14": t32_14[i], "14to-4": t14_n4[i], "<-4": 0.0}
    return data

TABLES = {
    ZONE_MILD:     _build_mild(),
    ZONE_MODERATE: _build_moderate(),
    ZONE_SEVERE:   _build_severe(),
}

def temp_band_from_f(temp_f):
    if temp_f > 32:   return ">32"
    elif temp_f >= 14: return "32to14"
    elif temp_f >= -4: return "14to-4"
    else:              return "<-4"

def lookup_target(zone, temp_f, baseline_pa):
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

# Boot stages:
# "zeroing" | "pick_temp" | "save_default" | "pick_zone" | "lock_baseline" | "running"
boot_stage  = "zeroing"

# Temperature selection
temp_clicks        = 0
temp_band_choice   = None
outdoor_temp_f     = None
default_temp_band  = None   # loaded from config, None if never saved

# Countdown state (for default temp auto-select)
countdown_active   = False
countdown_value    = 3      # 3→2→1→0 then auto-select
_countdown_timer   = None

# Zone selection
climate_zone = DEFAULT_ZONE
zone_clicks  = 0

# Per-sensor calibration
baseline1 = None
baseline2 = None
target1   = None
target2   = None

# WiFi
wifi_mode   = "searching"
sensor_data = {}

active          = True
current_battery = None

# Click timers
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
    BAR_X = 238; BAR_W = 2; BAR_H = 280
    if pct is None:
        draw.rectangle([BAR_X, 0, BAR_X+BAR_W-1, BAR_H-1], fill=(40,40,40))
        return
    pct    = max(0.0, min(100.0, pct))
    filled = int(BAR_H * pct / 100.0)
    for y in range(filled):
        ratio = y / BAR_H
        if ratio < 0.5:
            r = int(255*(ratio*2)); g = 200
        else:
            r = 255; g = int(200*(1-(ratio-0.5)*2))
        draw.line([(BAR_X,y),(BAR_X+BAR_W-1,y)], fill=(r,g,0))
    if filled < BAR_H:
        draw.rectangle([BAR_X,filled,BAR_X+BAR_W-1,BAR_H-1], fill=(30,30,30))


# =============================================================
# WiFi
# =============================================================

def scan_for(ssid, retries=2):
    for _ in range(retries):
        try:
            result = subprocess.run(
                ['sudo','nmcli','-t','-f','SSID','dev','wifi','list','--rescan','yes'],
                capture_output=True, text=True, timeout=20)
            if ssid in result.stdout: return True
        except Exception as e:
            print(f"Scan error: {e}")
        time.sleep(2)
    return False

def connect_to(ssid, password):
    try:
        subprocess.run(['sudo','nmcli','dev','wifi','connect',ssid,'password',password],
                       check=True, timeout=30)
        time.sleep(3); return True
    except Exception as e:
        print(f"Failed to connect to {ssid}: {e}"); return False

def create_hotspot():
    try:
        subprocess.run(['sudo','nmcli','dev','wifi','hotspot','ifname','wlan0',
                        'ssid',SITE_SSID,'password',SITE_PASSWORD],
                       check=True, timeout=30)
        time.sleep(3); return True
    except Exception as e:
        print(f"Hotspot error: {e}"); return False

def get_host_ip():
    try:
        result = subprocess.run(['ip','-4','addr','show','wlan0'],
                                capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if 'inet ' in line:
                return line.strip().split()[1].split('/')[0]
    except Exception:
        pass
    return None

def already_connected_to():
    try:
        result = subprocess.run(['nmcli','-t','-f','ACTIVE,SSID','dev','wifi'],
                                capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            if line.startswith('yes:'):
                return line.split(':',1)[1].strip()
    except Exception:
        pass
    return None

def setup_wifi():
    global wifi_mode
    current = already_connected_to()
    if current == HOME_SSID:  wifi_mode = "home";   return "home"
    if current == SITE_SSID:  wifi_mode = "client"; return "client"
    if scan_for(HOME_SSID):
        if connect_to(HOME_SSID, HOME_PASSWORD):
            wifi_mode = "home"; return "home"
    time.sleep(random.uniform(1,5))
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
        raw_p = (data[0]<<8)|data[1]
        if raw_p > 32767: raw_p -= 65536
        raw_t = (data[3]<<8)|data[4]
        if raw_t > 32767: raw_t -= 65536
        scale = (data[6]<<8)|data[7]
        if scale == 0: return None, None
        return raw_p/scale, raw_t/200.0
    except Exception:
        return None, None

def celsius_to_fahrenheit(c):
    return c*9/5+32 if c is not None else None


# =============================================================
# Boot sequence logic
# =============================================================

def do_zeroing(bus):
    global zero_offset1, zero_offset2, boot_stage
    print("Zeroing sensors...")
    samples1, samples2 = [], []
    for _ in range(ZERO_SAMPLES):
        p1,_ = read_sdp_raw(bus, SDP_ADDR_1)
        p2,_ = read_sdp_raw(bus, SDP_ADDR_2)
        if p1 is not None: samples1.append(p1)
        if p2 is not None: samples2.append(p2)
        time.sleep(0.05)
    with lock:
        zero_offset1 = sum(samples1)/len(samples1) if samples1 else 0.0
        zero_offset2 = sum(samples2)/len(samples2) if samples2 else 0.0
        boot_stage   = "pick_temp"
    print(f"Zero offsets — S1: {zero_offset1:.3f}  S2: {zero_offset2:.3f}")

    # Load saved default and start countdown if one exists
    cfg = load_config()
    saved = cfg.get("default_temp_band")
    if saved:
        with lock:
            global default_temp_band
            default_temp_band = saved
        print(f"Saved default temp band: {saved}")
        _start_countdown()
    else:
        print("No saved default — waiting for manual temp selection")

def _start_countdown():
    """Begin 3-second auto-select countdown for saved default."""
    global countdown_active, countdown_value, _countdown_timer
    with lock:
        countdown_active = True
        countdown_value  = 3
    _schedule_countdown_tick()

def _schedule_countdown_tick():
    global _countdown_timer
    _countdown_timer = threading.Timer(1.0, _countdown_tick)
    _countdown_timer.start()

def _countdown_tick():
    global countdown_value, countdown_active, _countdown_timer
    with lock:
        if not countdown_active:
            return   # was cancelled by a button press
        countdown_value -= 1
        val = countdown_value

    if val <= 0:
        # Time's up — apply the saved default
        _apply_default_temp()
    else:
        _schedule_countdown_tick()

def _apply_default_temp():
    global boot_stage, temp_band_choice, outdoor_temp_f, countdown_active
    with lock:
        band = default_temp_band
        countdown_active = False
    if band == "auto":
        t1 = current_temp1; t2 = current_temp2
        temps = [t for t in [t1,t2] if t is not None]
        avg_c  = sum(temps)/len(temps) if temps else None
        temp_f = celsius_to_fahrenheit(avg_c) if avg_c is not None else 50.0
        resolved = temp_band_from_f(temp_f)
    else:
        temp_f   = TEMP_BAND_MIDPOINT_F.get(band, 50.0)
        resolved = band
    with lock:
        temp_band_choice = resolved
        outdoor_temp_f   = temp_f
        boot_stage       = "pick_zone"
    print(f"Default temp applied: {band} → {resolved} ({temp_f:.0f}°F)")

def _cancel_countdown():
    """Called when user clicks during countdown — cancel auto-select."""
    global countdown_active, _countdown_timer
    with lock:
        countdown_active = False
    if _countdown_timer:
        _countdown_timer.cancel()
        _countdown_timer = None
    print("Countdown cancelled — manual selection")

def _resolve_temp_band(clicks):
    c = max(1, min(5, clicks))
    return TEMP_CLICK_BANDS[c]

def _temp_timeout():
    """Called 3s after last click in pick_temp — commit the choice."""
    global boot_stage, temp_band_choice, outdoor_temp_f, _temp_click_timer
    with lock:
        if boot_stage != "pick_temp": return
        clicks = temp_clicks
        t1 = current_temp1; t2 = current_temp2

    band = _resolve_temp_band(clicks)
    if band == "auto":
        temps = [t for t in [t1,t2] if t is not None]
        avg_c  = sum(temps)/len(temps) if temps else None
        temp_f = celsius_to_fahrenheit(avg_c) if avg_c is not None else 50.0
        resolved = temp_band_from_f(temp_f)
    else:
        temp_f   = TEMP_BAND_MIDPOINT_F[band]
        resolved = band

    with lock:
        temp_band_choice  = resolved
        outdoor_temp_f    = temp_f
        _temp_click_timer = None

    # If no default saved yet → go to save_default stage
    # If default already exists → skip straight to pick_zone
    cfg = load_config()
    if "default_temp_band" not in cfg:
        with lock:
            global boot_stage
            boot_stage = "save_default"
        print(f"Temp selected: {resolved} — prompting to save as default")
    else:
        # Update the saved default with the new manual selection
        cfg["default_temp_band"] = _resolve_temp_band(clicks)
        save_config(cfg)
        with lock:
            boot_stage = "pick_zone"
        print(f"Temp updated to {resolved}, default updated")

def _zone_timeout():
    global boot_stage, _zone_click_timer
    with lock:
        if boot_stage == "pick_zone":
            boot_stage = "lock_baseline"
            _zone_click_timer = None
            print(f"Zone locked: {climate_zone}")

def advance_boot_stage():
    global boot_stage, temp_clicks, zone_clicks, climate_zone
    global baseline1, baseline2, target1, target2
    global _temp_click_timer, _zone_click_timer

    with lock:
        stage = boot_stage

    if stage == "pick_temp":
        # If countdown was running, cancel it and start fresh manual pick
        with lock:
            ca = countdown_active
        if ca:
            _cancel_countdown()
            with lock:
                temp_clicks = 0
            # First click already happened — count it
        with lock:
            temp_clicks += 1
            clicks = temp_clicks
        if _temp_click_timer: _temp_click_timer.cancel()
        _temp_click_timer = threading.Timer(CLICK_TIMEOUT, _temp_timeout)
        _temp_click_timer.start()
        print(f"Temp click {clicks} → {TEMP_CLICK_BANDS.get(min(5,clicks),'?')}")

    elif stage == "save_default":
        # One click here saves the selection as default and moves on
        with lock:
            band = temp_band_choice
        cfg = load_config()
        # Save the original click-band (not resolved), so "auto" stays as "auto"
        # We stored the resolved band — save that
        cfg["default_temp_band"] = band
        save_config(cfg)
        with lock:
            global default_temp_band
            default_temp_band = band
            boot_stage = "pick_zone"
        print(f"Default saved: {band}")

    elif stage == "pick_zone":
        with lock:
            zone_clicks += 1
            zc = zone_clicks
            zones = [ZONE_MILD, ZONE_MODERATE, ZONE_SEVERE]
            climate_zone = zones[(zc-1) % 3]
        if _zone_click_timer: _zone_click_timer.cancel()
        _zone_click_timer = threading.Timer(CLICK_TIMEOUT, _zone_timeout)
        _zone_click_timer.start()
        print(f"Zone click {zc} → {climate_zone}")

    elif stage == "lock_baseline":
        with lock:
            p1 = current_pressure1; p2 = current_pressure2
            tf = outdoor_temp_f if outdoor_temp_f is not None else 50.0
            z  = climate_zone
            baseline1  = p1; baseline2 = p2
            target1    = lookup_target(z, tf, p1) if p1 is not None else None
            target2    = lookup_target(z, tf, p2) if p2 is not None else None
            boot_stage = "running"
        print(f"Baseline S1: {baseline1} → target {target1} Pa")
        print(f"Baseline S2: {baseline2} → target {target2} Pa")

def re_enter_setup():
    global boot_stage, temp_clicks, temp_band_choice, outdoor_temp_f
    global zone_clicks, climate_zone, baseline1, baseline2, target1, target2
    global _temp_click_timer, _zone_click_timer, countdown_active, countdown_value
    if _temp_click_timer: _temp_click_timer.cancel()
    if _zone_click_timer: _zone_click_timer.cancel()
    if _countdown_timer:  _countdown_timer.cancel()
    with lock:
        boot_stage       = "pick_temp"
        temp_clicks      = 0
        temp_band_choice = None
        outdoor_temp_f   = None
        zone_clicks      = 0
        climate_zone     = DEFAULT_ZONE
        baseline1        = None; baseline2 = None
        target1          = None; target2   = None
        countdown_active = False
        countdown_value  = 3
    # Restart countdown if default exists
    with lock:
        has_default = default_temp_band is not None
    if has_default:
        _start_countdown()
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
    if _button_press_time is None: return
    held = time.time() - _button_press_time
    _button_press_time = None

    with lock:
        stage = boot_stage

    if held >= HOLD_SECONDS:
        if stage == "running":
            re_enter_setup()
        return

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

def image_to_pixels(img):
    pixels = []
    for r,g,b in img.getdata():
        rgb565 = ((r&0xF8)<<8)|((g&0xFC)<<3)|(b>>3)
        pixels.extend([(rgb565>>8)&0xFF, rgb565&0xFF])
    return pixels

def _font(fp, fr, size, bold=False):
    try:
        return ImageFont.truetype(fp if bold else fr, size)
    except Exception:
        return ImageFont.load_default()

def make_screen_boot(stage, temp_c, zone, temp_clicks_count,
                     zone_clicks_count, temp_band_chosen,
                     countdown_val, countdown_on, saved_default):
    img  = Image.new('RGB', (240, 280), (0,0,0))
    draw = ImageDraw.Draw(img)
    fp   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fr   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_big   = _font(fp, fr, 34, True)
    f_med   = _font(fp, fr, 18, True)
    f_small = _font(fp, fr, 14, False)
    f_tiny  = _font(fp, fr, 12, False)

    draw.text((6,4), DEVICE_NAME, font=f_small, fill=(120,120,255))
    draw.text((160,4), "SETUP",   font=f_small, fill=(255,200,50))
    draw.line([(0,24),(240,24)], fill=(50,50,50), width=1)

    if stage == "zeroing":
        draw.text((20,60), "Zeroing sensors...", font=f_small, fill=(180,180,180))
        draw.text((20,90), "Please wait",        font=f_tiny,  fill=(120,120,120))

    elif stage == "pick_temp":

        draw.text((6,32), "Outdoor temp?", font=f_med, fill=(200,200,255))

        if countdown_on and saved_default:
            # ── Countdown mode ───────────────────────────────
            # Show saved default name
            label = TEMP_BAND_LABELS.get(saved_default, saved_default)
            draw.text((6, 68), label, font=f_med, fill=(200,220,100))
            draw.text((6,100), "DEFAULT", font=f_tiny, fill=(120,120,160))

            # 3 yellow dots draining left→right as countdown ticks
            # countdown_val: 3=all lit, 2=two lit, 1=one lit, 0=none
            DOT_Y = 140; DOT_R = 10; DOT_GAP = 30; DOT_X0 = 20
            for i in range(3):
                x = DOT_X0 + i * DOT_GAP
                # dots drain from right: dot 2 goes first, then 1, then 0
                lit = i < countdown_val
                col = (255,200,50) if lit else (50,50,50)
                draw.ellipse([x, DOT_Y, x+DOT_R*2, DOT_Y+DOT_R*2], fill=col)

            draw.text((6,200), "Click to change", font=f_tiny, fill=(180,180,80))

        elif temp_clicks_count == 0:
            # ── No default, nothing clicked yet ──────────────
            draw.text((6, 68), "1 click  = AUTO (thermometer)", font=f_tiny, fill=(100,220,255))
            draw.text((6, 86), "2 clicks = below -4\u00b0F",         font=f_tiny, fill=(150,150,200))
            draw.text((6,104), "3 clicks = -4\u00b0F to 14\u00b0F",  font=f_tiny, fill=(150,150,200))
            draw.text((6,122), "4 clicks = 14\u00b0F to 32\u00b0F",  font=f_tiny, fill=(150,150,200))
            draw.text((6,140), "5 clicks = above 32\u00b0F",         font=f_tiny, fill=(150,150,200))
            draw.text((6,200), "Press to begin", font=f_tiny, fill=(180,180,80))

        else:
            # ── Manual selection in progress ─────────────────
            c    = min(5, temp_clicks_count)
            band = TEMP_CLICK_BANDS[c]
            if band == "auto":
                temp_f = celsius_to_fahrenheit(temp_c) if temp_c is not None else None
                label  = f"AUTO: {temp_f:.0f}\u00b0F" if temp_f else "AUTO: --\u00b0F"
                color  = (100,220,255)
            else:
                label = TEMP_BAND_LABELS[band]
                color = (200,220,100)

            # 5 click-indicator dots
            for i in range(5):
                col = (255,200,50) if i < c else (50,50,50)
                draw.ellipse([6+i*22, 58, 6+i*22+14, 72], fill=col)

            draw.text((6,82), label, font=f_med, fill=color)
            draw.text((6,200), "Wait 3s to confirm", font=f_tiny, fill=(180,180,80))

    elif stage == "save_default":
        # ── Prompt to save as default ─────────────────────────
        label = TEMP_BAND_LABELS.get(temp_band_chosen, temp_band_chosen or "?")
        draw.text((6, 32), "Save as default?",  font=f_med,   fill=(200,200,255))
        draw.text((6, 68), label,               font=f_med,   fill=(200,220,100))
        draw.text((6,110), "Press to save",     font=f_small, fill=(180,180,80))
        draw.text((6,135), "and continue",      font=f_small, fill=(180,180,80))
        draw.text((6,200), "(Long press skips)",font=f_tiny,  fill=(120,120,120))

    elif stage == "pick_zone":
        zones   = [ZONE_MILD, ZONE_MODERATE, ZONE_SEVERE]
        zc      = zone_clicks_count
        current = zones[(zc-1)%3] if zc > 0 else DEFAULT_ZONE
        draw.text((6,32), "Climate zone?", font=f_med, fill=(200,200,255))
        colors  = {ZONE_MILD:(100,255,100), ZONE_MODERATE:(255,200,50), ZONE_SEVERE:(255,80,80)}
        draw.text((6,68), current.upper(), font=f_big, fill=colors.get(current,(200,200,200)))
        draw.text((6,130), "1 click  = Mild",    font=f_tiny, fill=(150,150,150))
        draw.text((6,148), "2 clicks = Moderate", font=f_tiny, fill=(150,150,150))
        draw.text((6,166), "3 clicks = Severe",  font=f_tiny, fill=(150,150,150))
        draw.text((6,210), "Wait 3s to confirm", font=f_tiny, fill=(180,180,80))
        if temp_band_chosen:
            draw.text((6,254), f"Temp: {TEMP_BAND_LABELS.get(temp_band_chosen,'?')}",
                      font=f_tiny, fill=(80,80,160))

    elif stage == "lock_baseline":
        draw.text((6, 32), "Ready to lock",       font=f_med,   fill=(200,200,255))
        draw.text((6, 58), "baseline pressure",   font=f_med,   fill=(200,200,255))
        draw.text((6,110), "Fan must be OFF",     font=f_small, fill=(255,180,50))
        draw.text((6,132), "Tubes in test holes", font=f_small, fill=(255,180,50))
        if temp_band_chosen:
            draw.text((6,175), f"Temp: {TEMP_BAND_LABELS.get(temp_band_chosen,'?')}",
                      font=f_tiny, fill=(80,120,200))
        draw.text((6,195), f"Zone: {zone.upper()}", font=f_tiny, fill=(80,120,200))
        draw.text((6,230), "Press to lock baseline", font=f_tiny, fill=(180,180,80))

    with lock:
        batt = current_battery
    draw_battery_bar(draw, batt)
    return image_to_pixels(img)

def make_screen_running(p1, p2, t1, tgt1, tgt2, mode):
    img  = Image.new('RGB', (240,280), (0,0,0))
    draw = ImageDraw.Draw(img)
    fp   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fr   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_big   = _font(fp, fr, 38, True)
    f_med   = _font(fp, fr, 18, True)
    f_small = _font(fp, fr, 14, False)
    f_tiny  = _font(fp, fr, 12, False)

    mode_colors = {"home":(100,200,100),"host":(200,150,50),
                   "client":(100,180,255),"searching":(160,160,160)}
    mode_labels = {"home":"HOME","host":"HOST","client":"CLIENT","searching":"..."}
    draw.text((6,4), DEVICE_NAME, font=f_small, fill=(120,120,255))
    draw.text((160,4), mode_labels.get(mode,"?"), font=f_small,
              fill=mode_colors.get(mode,(160,160,160)))
    draw.line([(0,24),(240,24)], fill=(50,50,50), width=1)

    def draw_sensor(label, pressure, target, y_top):
        draw.text((6,y_top), label, font=f_tiny, fill=(150,150,150))
        if pressure is None:
            draw.text((6,y_top+16), "--", font=f_big, fill=(80,80,80))
            draw.line([(0,y_top+85),(240,y_top+85)], fill=(40,40,40), width=1)
            return
        if target is None:
            color = (80,160,255)
            draw.text((6,  y_top+16), f"{pressure:.2f}", font=f_big, fill=color)
            draw.text((148,y_top+16), "Pa",              font=f_med, fill=(100,140,200))
        else:
            passed = pressure <= target
            color  = (0,230,0) if passed else (255,60,60)
            draw.text((180,y_top+16), "PASS" if passed else "FAIL", font=f_med, fill=color)
            draw.text((6,  y_top+16), f"{pressure:.2f}", font=f_big, fill=color)
            draw.text((148,y_top+16), "Pa",              font=f_med, fill=color)
            draw.text((6,  y_top+65), f"Target: {target:.2f} Pa", font=f_small, fill=(180,180,180))
        draw.line([(0,y_top+85),(240,y_top+85)], fill=(40,40,40), width=1)

    draw_sensor("SENSOR 1", p1, tgt1, 28)
    draw_sensor("SENSOR 2", p2, tgt2, 118)

    with lock:
        tbc = temp_band_choice
        z   = climate_zone
    band_label = TEMP_BAND_LABELS.get(tbc,"?") if tbc else "Temp not set"
    draw.text((6,215), band_label, font=f_tiny, fill=(100,100,200))
    draw.text((6,230), z.upper(), font=f_tiny, fill=(100,100,200))

    if mode == "host":
        with lock:
            count = len(sensor_data)
        draw.text((6,245), f"Devices: {count}", font=f_tiny, fill=(0,180,80))
    elif mode == "client":
        draw.text((6,245), "Reporting to host", font=f_tiny, fill=(100,180,255))

    ip = get_host_ip()
    draw.text((6,260), f"http://{ip}" if ip else "...",
              font=f_tiny, fill=(100,180,255) if ip else (160,160,160))

    with lock:
        batt = current_battery
    draw_battery_bar(draw, batt)
    return image_to_pixels(img)

def screen_thread(board, splash):
    if splash:
        board.draw_image(0,0,240,280,splash)
    last_state = None
    while True:
        with lock:
            is_active = active
            stage  = boot_stage
            p1     = current_pressure1; p2 = current_pressure2
            t1     = current_temp1
            tgt1   = target1;           tgt2 = target2
            mode   = wifi_mode
            tc     = temp_clicks;       zc   = zone_clicks
            tbc    = temp_band_choice
            batt   = current_battery
            cv     = countdown_value
            ca     = countdown_active
            sd     = default_temp_band

        if not is_active:
            time.sleep(0.5); continue

        p1r = round(p1,1) if p1 is not None else None
        p2r = round(p2,1) if p2 is not None else None
        t1r = round(t1,1) if t1 is not None else None
        br  = round(batt)  if batt is not None else None
        state = (stage,p1r,p2r,t1r,tgt1,tgt2,mode,tc,zc,tbc,cv,ca,sd,br)

        if state != last_state:
            if stage == "running":
                pixels = make_screen_running(p1,p2,t1,tgt1,tgt2,mode)
            else:
                pixels = make_screen_boot(stage,t1,climate_zone,tc,zc,tbc,cv,ca,sd)
            board.draw_image(0,0,240,280,pixels)
            last_state = state

        time.sleep(0.5)


# =============================================================
# Web dashboard
# =============================================================

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        if self.path == '/data':
            with lock:
                data = dict(sensor_data)
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Access-Control-Allow-Origin','*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif self.path in ('/','index.html'):
            self.send_response(200)
            self.send_header('Content-Type','text/html')
            self.end_headers()
            self.wfile.write(build_dashboard_html().encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == '/report':
            length  = int(self.headers.get('Content-Length',0))
            body    = self.rfile.read(length)
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
                self.send_response(200); self.end_headers()
                self.wfile.write(b'OK')
            except Exception:
                self.send_response(400); self.end_headers()

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
.pass-val { color: #00e874; } .fail-val { color: #ff6060; }
.blue-val { color: #5090ff; } .na-val { color: #3a4a6a; font-size: 1.3em; }
.sensor-sub { font-size: 0.72em; margin-top: 4px; }
.pass-val-dim { color: #00a050; } .fail-val-dim { color: #a03030; } .blue-val-dim { color: #3060a0; }
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
function sensorBox(label, val, tgt, stale) {
  if (stale || val===null || val===undefined) {
    return `<div class="sensor-box"><div class="s-top"><span class="sensor-label">${label}</span></div>
      <div class="sensor-value na-val">--</div><div class="sensor-sub">&nbsp;</div></div>`;
  }
  if (tgt===null || tgt===undefined) {
    return `<div class="sensor-box blue"><div class="s-top"><span class="sensor-label">${label}</span></div>
      <div class="sensor-value blue-val">${fmtPa(val)}<span class="sensor-unit">Pa</span></div>
      <div class="sensor-sub">&nbsp;</div></div>`;
  }
  const pass=val<=tgt, cls=pass?'pass':'fail', vc=pass?'pass-val':'fail-val',
        dc=pass?'pass-val-dim':'fail-val-dim', bc=pass?'s-badge-pass':'s-badge-fail',
        bt=pass?'PASS':'FAIL';
  return `<div class="sensor-box ${cls}">
    <div class="s-top"><span class="sensor-label">${label}</span><span class="s-badge ${bc}">${bt}</span></div>
    <div class="sensor-value ${vc}">${fmtPa(val)}<span class="sensor-unit">Pa</span></div>
    <div class="sensor-sub ${dc}">Target: ${fmtPa(tgt)} Pa</div></div>`;
}
async function refresh() {
  try {
    const res=await fetch('/data'), data=await res.json(), now=Date.now()/1000;
    const names=Object.keys(data).sort(), grid=document.getElementById('grid');
    if (!names.length) { grid.innerHTML='<div class="no-sensors">Waiting for sensors...</div>'; return; }
    grid.innerHTML=names.map(name=>{
      const s=data[name], stale=(now-s.time)>30;
      const badge=stale?'<span class="badge badge-offline">OFFLINE</span>':'';
      return `<div class="card"><div class="card-top"><span class="card-name">${name}</span>${badge}</div>
        <div class="sensors">${sensorBox('Sensor 1',s.s1,s.tgt1,stale)}${sensorBox('Sensor 2',s.s2,s.tgt2,stale)}</div></div>`;
    }).join('');
    document.getElementById('footer').textContent='Updated: '+new Date().toLocaleTimeString();
  } catch(e) { document.getElementById('footer').textContent='Connection lost...'; }
}
refresh(); setInterval(refresh,1000);
  </script>
</body>
</html>"""

def run_web_server():
    try:
        server = HTTPServer(('0.0.0.0',WEB_PORT), DashboardHandler)
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
                p1=current_pressure1; p2=current_pressure2
                t1=current_temp1;     tg1=target1; tg2=target2
            payload = json.dumps({'name':DEVICE_NAME,'s1':p1,'s2':p2,
                                  'tgt1':tg1,'tgt2':tg2,'temp1':t1}).encode()
            req = Request(url, data=payload,
                          headers={'Content-Type':'application/json'})
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
    board.draw_image(0,0,240,280,splash)
    time.sleep(1.5)

threading.Thread(target=screen_thread,    args=(board,None), daemon=True).start()
threading.Thread(target=battery_poll_loop,                   daemon=True).start()
threading.Thread(target=lambda: (
    setup_wifi(),
    run_web_server() if wifi_mode=="host" else None
), daemon=True).start()

with SMBus(1) as bus:
    init_sensor(bus)
    do_zeroing(bus)   # also starts countdown if default exists

    while True:
        p1_raw,t1 = read_sdp_raw(bus, SDP_ADDR_1)
        p2_raw,t2 = read_sdp_raw(bus, SDP_ADDR_2)

        p1 = (p1_raw - zero_offset1) if p1_raw is not None else None
        p2 = (p2_raw - zero_offset2) if p2_raw is not None else None

        with lock:
            current_pressure1=p1; current_temp1=t1
            current_pressure2=p2; current_temp2=t2

        if wifi_mode=="host" and boot_stage=="running":
            with lock:
                sensor_data[DEVICE_NAME] = {
                    's1':p1,'s2':p2,'tgt1':target1,'tgt2':target2,
                    'temp1':t1,'time':time.time()
                }

        if wifi_mode=="client" and boot_stage=="running":
            host_ip = get_host_ip()
            if host_ip:
                threading.Thread(target=report_data_loop,
                                 args=(host_ip,), daemon=True).start()
                wifi_mode = "client_reporting"

        s1_str = f"{p1:.2f} Pa" if p1 is not None else "--"
        s2_str = f"{p2:.2f} Pa" if p2 is not None else "--"
        print(f"S1: {s1_str}  S2: {s2_str}  Stage: {boot_stage}")
        time.sleep(1)
