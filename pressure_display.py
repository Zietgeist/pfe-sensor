#!/usr/bin/env python3
"""
PFE Pressure Sensor — Main
Same code runs on every device.

Boot sequence:
  1. Zero both sensors
  2. Pick outdoor temp (clicks + 3s timeout, default = sensor reading)
       1 click → auto   2 clicks → <-4F   3 clicks → -4 to 14F
       4 clicks → 14 to 32F   5 clicks → >32F
  3. Pick climate zone (clicks + 3s timeout, default = Severe)
       1 click → Mild   2 clicks → Moderate   3 clicks → Severe
  4. Press to lock baseline → targets calculated automatically
  5. Running: short press = capture suction snapshot
              long hold   = re-enter setup

Hardware auto-detection at boot:
  No MUX → 2 sensors: S1 at 0x25, S2 at 0x26  (original wiring)
  MUX at 0x70 → up to 4 sensors: all at 0x25 on channels 0-3
  Any sensor slot that doesn't respond → None (shown as --)
"""

import sys, os, time, random, threading, subprocess, socket, json, csv, io
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
SDP_ADDR_1    = 0x25   # S1: direct or MUX ch0
SDP_ADDR_2    = 0x26   # S2: direct only (no MUX; all MUX sensors use 0x25)
MUX_ADDR      = 0x70   # TCA9548A / PCA9548A
MUX_CHANNELS  = [0, 1, 2, 3]
HOME_SSID     = "PFE-home"
HOME_PASSWORD = "pferadon1"
SITE_SSID     = "PFE-NET"
SITE_PASSWORD = "pferadon1"
HOST_IP       = "10.42.0.1"
WEB_PORT      = 80
ZONE_MILD     = "mild"
ZONE_MODERATE = "moderate"
ZONE_SEVERE   = "severe"
DEFAULT_ZONE  = ZONE_SEVERE
HOLD_SECONDS  = 2.0
ZERO_SAMPLES  = 5
CLICK_TIMEOUT = 3.0
REPO_DIR      = "/home/pi/pfe-sensor"
SPLASH_PATH   = f"{REPO_DIR}/marten_screen.png"
FONT_BOLD     = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG      = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

TEMP_CLICK_BANDS    = {1:"auto",2:"<-4",3:"14to-4",4:"32to14",5:">32"}
TEMP_BAND_LABELS    = {"auto":"AUTO (thermometer)","<-4":"< -4°F",
                       "14to-4":"-4°F to 14°F","32to14":"14°F to 32°F",">32":"> 32°F"}
TEMP_BAND_MIDPOINT_F = {"<-4":-10.0,"14to-4":5.0,"32to14":23.0,">32":50.0}
ROOM_OPTIONS        = ["Utility Room","Crawlspace","Sump Pit","Under Stairs",
                       "Storage Room","Mechanical Room","Garage"]

# =============================================================
# Target pressure tables
# =============================================================
def _build_mild():
    gt32   = [0.0,-0.1,-0.2,-0.3,-0.4,-0.5,-0.6,-0.7,-0.8,-0.9,-1.0,-1.1,-1.2,-1.3,-1.4,-1.5,-1.6,-1.7,-1.8,-1.9,-2.0,-2.1,-2.2,-2.3,-2.4,-2.5,-2.6,-2.7,-2.8,-2.9,-3.0,-3.1,-3.2,-3.3,-3.4,-3.5,-3.6,-3.7,-3.8,-3.9,-4.0,-4.1,-4.2,-4.3,-4.4,-4.5,-4.6,-4.7,-4.8,-4.9,-5.0,-5.1,-5.2,-5.3,-5.4,-5.5,-5.6,-5.7,-5.8,-5.9,-6.0]
    t32_14 = [0.0,-0.1,-0.1,-0.2,-0.2,-0.2,-0.3,-0.3,-0.4,-0.4,-0.4,-0.5,-0.5,-0.6,-0.6,-0.6,-0.7,-0.7,-0.8,-0.8,-0.8,-0.9,-0.9,-1.0,-1.0,-1.0,-1.1,-1.1,-1.2,-1.2,-1.2,-1.3,-1.3,-1.4,-1.4,-1.4,-1.5,-1.5,-1.6,-1.6,-1.6,-1.7,-1.7,-1.8,-1.8,-1.8,-1.9,-1.9,-2.0,-2.0,-2.0,-2.1,-2.1,-2.2,-2.2,-2.2,-2.3,-2.3,-2.4,-2.4,-2.4]
    return {i: {">32":gt32[i],"32to14":t32_14[i],"14to-4":0.0,"<-4":0.0} for i in range(61)}

def _build_moderate():
    gt32   = [0.0,-0.2,-0.3,-0.4,-0.5,-0.6,-0.8,-0.9,-1.0,-1.1,-1.2,-1.4,-1.5,-1.6,-1.7,-1.8,-2.0,-2.1,-2.2,-2.3,-2.4,-2.6,-2.7,-2.8,-2.9,-3.0,-3.2,-3.3,-3.4,-3.5,-3.6,-3.8,-3.9,-4.0,-4.1,-4.2,-4.4,-4.5,-4.6,-4.7,-4.8,-5.0,-5.1,-5.2,-5.3,-5.4,-5.6,-5.7,-5.8,-5.9,-6.0,-6.2,-6.3,-6.4,-6.5,-6.6,-6.8,-6.9,-7.0,-7.1,-7.2]
    t32_14 = [0.0,-0.1,-0.1,-0.2,-0.2,-0.3,-0.3,-0.4,-0.4,-0.5,-0.5,-0.6,-0.6,-0.7,-0.7,-0.8,-0.8,-0.9,-0.9,-1.0,-1.0,-1.1,-1.1,-1.2,-1.2,-1.3,-1.3,-1.4,-1.4,-1.5,-1.5,-1.6,-1.6,-1.7,-1.7,-1.8,-1.8,-1.9,-1.9,-2.0,-2.0,-2.1,-2.1,-2.2,-2.2,-2.3,-2.3,-2.4,-2.4,-2.5,-2.5,-2.6,-2.6,-2.7,-2.7,-2.8,-2.8,-2.9,-2.9,-3.0,-3.0]
    return {i: {">32":gt32[i],"32to14":t32_14[i],"14to-4":0.0,"<-4":0.0} for i in range(61)}

def _build_severe():
    gt32   = [0.0,-0.2,-0.3,-0.5,-0.6,-0.8,-0.9,-1.1,-1.2,-1.4,-1.5,-1.7,-1.8,-2.0,-2.1,-2.3,-2.4,-2.6,-2.7,-2.9,-3.0,-3.2,-3.3,-3.5,-3.6,-3.8,-3.9,-4.1,-4.2,-4.4,-4.5,-4.7,-4.8,-5.0,-5.1,-5.3,-5.4,-5.6,-5.7,-5.9,-6.0,-6.2,-6.3,-6.5,-6.6,-6.8,-6.9,-7.1,-7.2,-7.4,-7.5,-7.7,-7.8,-8.0,-8.1,-8.3,-8.4,-8.6,-8.7,-8.9,-9.0]
    t32_14 = [0.0,-0.1,-0.2,-0.2,-0.3,-0.3,-0.4,-0.5,-0.5,-0.6,-0.6,-0.7,-0.8,-0.8,-0.9,-0.9,-1.0,-1.1,-1.1,-1.2,-1.2,-1.3,-1.4,-1.4,-1.5,-1.5,-1.6,-1.7,-1.7,-1.8,-1.8,-1.9,-2.0,-2.0,-2.1,-2.1,-2.2,-2.3,-2.3,-2.4,-2.4,-2.5,-2.6,-2.6,-2.7,-2.7,-2.8,-2.9,-2.9,-3.0,-3.0,-3.1,-3.2,-3.2,-3.3,-3.3,-3.4,-3.5,-3.5,-3.6,-3.6]
    t14_n4 = [0.0,-0.1,-0.1,-0.1,-0.1,-0.1,-0.2,-0.2,-0.2,-0.2,-0.2,-0.3,-0.3,-0.3,-0.3,-0.3,-0.4,-0.4,-0.4,-0.4,-0.4,-0.5,-0.5,-0.5,-0.5,-0.5,-0.6,-0.6,-0.6,-0.6,-0.6,-0.7,-0.7,-0.7,-0.7,-0.7,-0.8,-0.8,-0.8,-0.8,-0.8,-0.9,-0.9,-0.9,-0.9,-0.9,-1.0,-1.0,-1.0,-1.0,-1.0,-1.1,-1.1,-1.1,-1.1,-1.1,-1.2,-1.2,-1.2,-1.2,-1.2]
    return {i: {">32":gt32[i],"32to14":t32_14[i],"14to-4":t14_n4[i],"<-4":0.0} for i in range(61)}

TABLES = {ZONE_MILD:_build_mild(), ZONE_MODERATE:_build_moderate(), ZONE_SEVERE:_build_severe()}

def temp_band_from_f(f):
    if f > 32: return ">32"
    elif f >= 14: return "32to14"
    elif f >= -4: return "14to-4"
    else: return "<-4"

def lookup_target(zone, temp_f, baseline_pa):
    try:
        band = temp_band_from_f(temp_f)
        b    = max(0.0, min(6.0, baseline_pa))
        key  = round(round(b * 10))
        return TABLES[zone][key][band]
    except Exception:
        return None

def c_to_f(c):
    return c * 9/5 + 32 if c is not None else None

# =============================================================
# Shared state
# =============================================================
lock = threading.Lock()

# Sensor readings — s1/s2 always exist; s3/s4 only populated when MUX present
current_pressure1 = None
current_temp1     = None
current_pressure2 = None
current_temp2     = None
current_pressure3 = None   # MUX ch2 only
current_pressure4 = None   # MUX ch3 only
zero_offset1      = 0.0
zero_offset2      = 0.0
zero_offset3      = 0.0
zero_offset4      = 0.0

# Hardware config — set once at boot by detect_hardware()
has_mux = False   # True = MUX found; False = direct 0x25/0x26 wiring

# boot stages: zeroing | pick_temp | pick_zone | lock_baseline | running
boot_stage       = "zeroing"
temp_clicks      = 0
temp_band_choice = None
outdoor_temp_f   = None
climate_zone     = DEFAULT_ZONE
zone_clicks      = 0
baseline1        = None
baseline2        = None
baseline3        = None
baseline4        = None
target1          = None
target2          = None
target3          = None
target4          = None

wifi_mode        = "searching"
sensor_data      = {}
active           = True
current_battery  = None

job_info          = {"client_name":"","address":""}
sensor_labels     = {}
snapshot_baseline = None
snapshot_with_fan = None
data_log          = []

_temp_click_timer = None
_zone_click_timer = None

# =============================================================
# Battery
# =============================================================
def read_battery():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect('/tmp/pisugar-server.sock')
        s.sendall(b'get battery\n')
        data = s.recv(64).decode()
        s.close()
        return max(0.0, min(100.0, float(data.split(':')[1].strip())))
    except Exception:
        return None

def battery_poll_loop():
    global current_battery
    while True:
        val = read_battery()
        with lock:
            current_battery = val
        time.sleep(30)

def draw_battery_bar(draw, batt):
    if batt is None: return
    batt = max(0.0, min(100.0, batt))
    bx, by, bw, bh = 208, 5, 26, 14
    draw.rectangle([bx, by, bx+bw, by+bh], outline=(100,100,100))
    draw.rectangle([bx+bw, by+4, bx+bw+3, by+bh-4], fill=(100,100,100))
    fill_w = int((bw-2) * batt / 100)
    color  = (0,200,80) if batt > 50 else (220,180,0) if batt > 20 else (220,50,50)
    if fill_w > 0:
        draw.rectangle([bx+1, by+1, bx+1+fill_w, by+bh-1], fill=color)

# =============================================================
# WiFi
# =============================================================
def scan_for(ssid, retries=2):
    for _ in range(retries):
        try:
            r = subprocess.run(['sudo','nmcli','-t','-f','SSID','dev','wifi','list','--rescan','yes'],
                               capture_output=True, text=True, timeout=20)
            if ssid in r.stdout: return True
        except Exception as e:
            print(f"Scan error: {e}")
        time.sleep(2)
    return False

def connect_to(ssid, password):
    try:
        subprocess.run(['sudo','nmcli','dev','wifi','connect',ssid,'password',password],
                       check=True, timeout=30)
        time.sleep(3)
        print(f"Connected to {ssid}")
        return True
    except Exception as e:
        print(f"Failed: {e}"); return False

def create_hotspot():
    try:
        subprocess.run(['sudo','nmcli','dev','wifi','hotspot','ifname','wlan0',
                        'ssid',SITE_SSID,'password',SITE_PASSWORD],
                       check=True, timeout=30)
        time.sleep(3)
        print(f"Hotspot up: {SITE_SSID}")
        return True
    except Exception as e:
        print(f"Hotspot error: {e}"); return False

def get_own_ip():
    try:
        r = subprocess.run(['ip','-4','addr','show','wlan0'], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if 'inet ' in line:
                return line.strip().split()[1].split('/')[0]
    except Exception:
        pass
    return None

def already_connected_to():
    try:
        r = subprocess.run(['nmcli','-t','-f','ACTIVE,SSID','dev','wifi'],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if line.startswith('yes:'):
                return line.split(':',1)[1].strip()
    except Exception:
        pass
    return None

def setup_wifi():
    global wifi_mode
    cur = already_connected_to()
    if cur == HOME_SSID:  wifi_mode="home";   return "home"
    if cur == SITE_SSID:  wifi_mode="client"; return "client"
    if scan_for(HOME_SSID):
        if connect_to(HOME_SSID, HOME_PASSWORD): wifi_mode="home"; return "home"
    time.sleep(random.uniform(1,5))
    if scan_for(SITE_SSID):
        if connect_to(SITE_SSID, SITE_PASSWORD): wifi_mode="client"; return "client"
    if create_hotspot(): wifi_mode="host"; return "host"
    wifi_mode="searching"; return "searching"

# =============================================================
# Sensor — hardware detection + reading
# =============================================================
def detect_hardware(bus):
    """
    Check for MUX at 0x70. Called once at boot.
    Returns True if MUX found, False if direct wiring.
    """
    try:
        bus.read_byte(MUX_ADDR)
        print("MUX detected at 0x70 — 4-sensor mode (all 0x25 via channels 0-3)")
        return True
    except Exception:
        print("No MUX — direct mode (S1=0x25, S2=0x26)")
        return False

def _select_mux_channel(bus, channel):
    bus.write_byte(MUX_ADDR, 1 << channel)

def _disable_mux(bus):
    bus.write_byte(MUX_ADDR, 0)

def _soft_reset(bus):
    try:
        bus.i2c_rdwr(i2c_msg.write(0x00, [0x06]))
        time.sleep(0.05)
    except Exception:
        pass

def read_sdp_raw(bus, address):
    """Read one SDP sensor at the given address. Returns (pressure_pa, temp_c) or (None, None)."""
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
        return raw_p / scale, raw_t / 200.0
    except Exception:
        return None, None

def read_all_raw(bus, mux_present):
    """
    Read all sensors. Returns four (pressure, temp) tuples.
    Slots with no sensor return (None, None).

    No MUX:   S1=0x25, S2=0x26, S3=None, S4=None
    MUX:      S1=ch0/0x25, S2=ch1/0x25, S3=ch2/0x25, S4=ch3/0x25
    """
    if mux_present:
        results = []
        for ch in MUX_CHANNELS:
            _select_mux_channel(bus, ch)
            time.sleep(0.01)
            _soft_reset(bus)
            results.append(read_sdp_raw(bus, SDP_ADDR_1))
        _disable_mux(bus)
        return results[0], results[1], results[2], results[3]
    else:
        _soft_reset(bus)
        r1 = read_sdp_raw(bus, SDP_ADDR_1)
        r2 = read_sdp_raw(bus, SDP_ADDR_2)
        return r1, r2, (None, None), (None, None)

# =============================================================
# Boot sequence
# =============================================================
def do_zeroing(bus, mux_present):
    global zero_offset1, zero_offset2, zero_offset3, zero_offset4
    global boot_stage, temp_band_choice, outdoor_temp_f
    print("Zeroing sensors...")

    s1, s2, s3, s4, st = [], [], [], [], []
    for _ in range(ZERO_SAMPLES):
        r1, r2, r3, r4 = read_all_raw(bus, mux_present)
        for val, bucket in [(r1, s1), (r2, s2), (r3, s3), (r4, s4)]:
            p, t = val
            if p is not None: bucket.append(p)
            if t is not None: st.append(t)
        time.sleep(0.05)

    with lock:
        zero_offset1 = sum(s1)/len(s1) if s1 else 0.0
        zero_offset2 = sum(s2)/len(s2) if s2 else 0.0
        zero_offset3 = sum(s3)/len(s3) if s3 else 0.0
        zero_offset4 = sum(s4)/len(s4) if s4 else 0.0

    avg_c  = sum(st)/len(st) if st else None
    temp_f = c_to_f(avg_c) if avg_c is not None else 50.0
    resolved = temp_band_from_f(temp_f)
    with lock:
        temp_band_choice = resolved
        outdoor_temp_f   = temp_f
        boot_stage       = "pick_temp"
    print(f"Zero offsets — S1:{zero_offset1:.3f} S2:{zero_offset2:.3f} "
          f"S3:{zero_offset3:.3f} S4:{zero_offset4:.3f}  AutoTemp:{temp_f:.1f}°F → {resolved}")
    _start_temp_default_timer()

def _start_temp_default_timer():
    global _temp_click_timer
    if _temp_click_timer: _temp_click_timer.cancel()
    _temp_click_timer = threading.Timer(CLICK_TIMEOUT, _temp_default_accept)
    _temp_click_timer.start()

def _temp_default_accept():
    global boot_stage, _temp_click_timer
    with lock:
        if boot_stage != "pick_temp": return
        boot_stage = "pick_zone"
        _temp_click_timer = None
    print("Temp defaulted")
    _start_zone_default_timer()

def _start_zone_default_timer():
    global _zone_click_timer
    if _zone_click_timer: _zone_click_timer.cancel()
    _zone_click_timer = threading.Timer(CLICK_TIMEOUT, _zone_default_accept)
    _zone_click_timer.start()

def _zone_default_accept():
    global boot_stage, _zone_click_timer
    with lock:
        if boot_stage != "pick_zone": return
        boot_stage = "lock_baseline"
        _zone_click_timer = None
    print(f"Zone defaulted: {climate_zone}")

def _temp_timeout():
    global boot_stage, temp_band_choice, outdoor_temp_f, _temp_click_timer
    with lock:
        if boot_stage != "pick_temp": return
        clicks = temp_clicks
        t1, t2 = current_temp1, current_temp2
    band = TEMP_CLICK_BANDS.get(max(1,min(5,clicks)), "auto")
    if band == "auto":
        temps  = [t for t in [t1, t2] if t is not None]
        avg_c  = sum(temps)/len(temps) if temps else None
        temp_f = c_to_f(avg_c) if avg_c else 50.0
        resolved = temp_band_from_f(temp_f)
    else:
        temp_f   = TEMP_BAND_MIDPOINT_F[band]
        resolved = band
    with lock:
        temp_band_choice  = resolved
        outdoor_temp_f    = temp_f
        boot_stage        = "pick_zone"
        _temp_click_timer = None
    print(f"Temp set: {temp_f:.1f}°F → {resolved}")
    _start_zone_default_timer()

def _zone_timeout():
    global boot_stage, _zone_click_timer
    with lock:
        if boot_stage == "pick_zone":
            boot_stage = "lock_baseline"
            _zone_click_timer = None
    print(f"Zone set: {climate_zone}")

def advance_boot_stage():
    global boot_stage, temp_clicks, zone_clicks, climate_zone
    global baseline1, baseline2, baseline3, baseline4
    global target1, target2, target3, target4
    global _temp_click_timer, _zone_click_timer, snapshot_baseline, snapshot_with_fan

    with lock:
        stage = boot_stage

    if stage == "pick_temp":
        if _temp_click_timer: _temp_click_timer.cancel()
        with lock:
            temp_clicks += 1
            clicks = temp_clicks
        _temp_click_timer = threading.Timer(CLICK_TIMEOUT, _temp_timeout)
        _temp_click_timer.start()
        print(f"Temp click {clicks}")

    elif stage == "pick_zone":
        if _zone_click_timer: _zone_click_timer.cancel()
        with lock:
            zone_clicks += 1
            zc = zone_clicks
            climate_zone = [ZONE_MILD,ZONE_MODERATE,ZONE_SEVERE][(zc-1)%3]
        _zone_click_timer = threading.Timer(CLICK_TIMEOUT, _zone_timeout)
        _zone_click_timer.start()
        print(f"Zone click {zc} → {climate_zone}")

    elif stage == "lock_baseline":
        with lock:
            p1 = current_pressure1
            p2 = current_pressure2
            p3 = current_pressure3
            p4 = current_pressure4
            tf = outdoor_temp_f if outdoor_temp_f is not None else 50.0
            z  = climate_zone
            baseline1  = p1
            baseline2  = p2
            baseline3  = p3
            baseline4  = p4
            target1    = lookup_target(z, tf, p1) if p1 is not None else None
            target2    = lookup_target(z, tf, p2) if p2 is not None else None
            target3    = lookup_target(z, tf, p3) if p3 is not None else None
            target4    = lookup_target(z, tf, p4) if p4 is not None else None
            boot_stage = "running"
            snapshot_baseline = {DEVICE_NAME:{
                "s1":p1,"s2":p2,"s3":p3,"s4":p4,
                "label":sensor_labels.get(DEVICE_NAME,""),"time":time.time()}}
        print(f"Baseline locked — "
              f"S1:{baseline1}→{target1}Pa  S2:{baseline2}→{target2}Pa  "
              f"S3:{baseline3}→{target3}Pa  S4:{baseline4}→{target4}Pa")

    elif stage == "running":
        with lock:
            snap = {}
            for device, d in sensor_data.items():
                snap[device] = {"s1":d.get("s1"),"s2":d.get("s2"),
                                "s3":d.get("s3"),"s4":d.get("s4"),
                                "label":sensor_labels.get(device, d.get("label","")),"time":d.get("time")}
            snapshot_with_fan = snap
        print("Suction snapshot captured")

def re_enter_setup():
    global boot_stage, temp_clicks, temp_band_choice, outdoor_temp_f
    global zone_clicks, climate_zone
    global baseline1, baseline2, baseline3, baseline4
    global target1, target2, target3, target4
    global snapshot_baseline, snapshot_with_fan, _temp_click_timer, _zone_click_timer
    if _temp_click_timer: _temp_click_timer.cancel()
    if _zone_click_timer: _zone_click_timer.cancel()
    with lock:
        boot_stage=      "pick_temp"; temp_clicks=0; temp_band_choice=None; outdoor_temp_f=None
        zone_clicks=0;   climate_zone=DEFAULT_ZONE
        baseline1=None;  baseline2=None; baseline3=None; baseline4=None
        target1=None;    target2=None;   target3=None;   target4=None
        snapshot_baseline=None; snapshot_with_fan=None
    print("Setup restarted")

# =============================================================
# Button
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
        if stage == "running": re_enter_setup()
        return
    advance_boot_stage()

# =============================================================
# Data logging
# =============================================================
def append_log_row(device, d):
    with lock:
        row = {"timestamp":time.strftime("%Y-%m-%d %H:%M:%S"),"device":device,
               "label":sensor_labels.get(device, d.get("label","")),
               "s1_pa":"","s2_pa":"","s3_pa":"","s4_pa":"",
               "tgt1_pa":"","tgt2_pa":"","tgt3_pa":"","tgt4_pa":"","temp1_c":""}
        for k in ["s1","s2","s3","s4"]:
            if d.get(k) is not None: row[f"{k}_pa"] = round(d[k], 3)
        for k in ["tgt1","tgt2","tgt3","tgt4"]:
            if d.get(k) is not None: row[f"{k}_pa"] = round(d[k], 3)
        if d.get("temp1") is not None: row["temp1_c"] = round(d["temp1"], 2)
        data_log.append(row)

def data_log_loop():
    while True:
        time.sleep(60)
        with lock:
            devices = dict(sensor_data)
        for device, d in devices.items():
            append_log_row(device, d)
        print(f"Log: {len(data_log)} rows")

def get_log_csv():
    with lock:
        rows=list(data_log); client=job_info.get("client_name",""); addr=job_info.get("address","")
    out = io.StringIO()
    out.write(f"# PFE Data Log\n# Client: {client}\n# Address: {addr}\n# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    if rows:
        w = csv.DictWriter(out, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    return out.getvalue()

def get_snapshot_csv():
    with lock:
        bl=dict(snapshot_baseline) if snapshot_baseline else {}
        wf=dict(snapshot_with_fan) if snapshot_with_fan else {}
        client=job_info.get("client_name",""); addr=job_info.get("address","")
    out = io.StringIO()
    out.write(f"# PFE Snapshot Report\n# Client: {client}\n# Address: {addr}\n# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    w = csv.writer(out)
    w.writerow(["Device","Label",
                "Baseline S1","Baseline S2","Baseline S3","Baseline S4",
                "With Fan S1","With Fan S2","With Fan S3","With Fan S4",
                "Delta S1","Delta S2","Delta S3","Delta S4"])
    for dev in sorted(set(list(bl.keys())+list(wf.keys()))):
        b=bl.get(dev,{}); wb=wf.get(dev,{})
        row=[dev, b.get("label","")]
        for k in ["s1","s2","s3","s4"]:
            v=b.get(k); row.append(round(v,3) if v is not None else "")
        for k in ["s1","s2","s3","s4"]:
            v=wb.get(k); row.append(round(v,3) if v is not None else "")
        for k in ["s1","s2","s3","s4"]:
            bv=b.get(k); wv=wb.get(k)
            row.append(round(wv-bv,3) if wv is not None and bv is not None else "")
        w.writerow(row)
    return out.getvalue()

# =============================================================
# Web server
# =============================================================
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        if self.path == '/data':
            with lock:
                now=time.time()
                payload={"sensors":{k:{**v,"age":now-v["time"]} for k,v in sensor_data.items()},
                         "job":dict(job_info),"labels":dict(sensor_labels),
                         "snapshot_baseline":snapshot_baseline,"snapshot_with_fan":snapshot_with_fan}
            self._send_json(payload)
        elif self.path in ('/','index.html'):
            self._send_html(build_dashboard_html())
        elif self.path == '/download/log.csv':
            self._send_csv(get_log_csv(), "pfe_log.csv")
        elif self.path == '/download/snapshots.csv':
            self._send_csv(get_snapshot_csv(), "pfe_snapshots.csv")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
        try: payload = json.loads(body)
        except Exception: self.send_response(400); self.end_headers(); return

        if self.path == '/report':
            name = payload.get('name')
            if name:
                with lock:
                    sensor_data[name]={'s1':payload.get('s1'),'s2':payload.get('s2'),
                                       's3':payload.get('s3'),'s4':payload.get('s4'),
                                       'tgt1':payload.get('tgt1'),'tgt2':payload.get('tgt2'),
                                       'tgt3':payload.get('tgt3'),'tgt4':payload.get('tgt4'),
                                       'temp1':payload.get('temp1'),'label':payload.get('label',''),
                                       'time':time.time()}
                    if payload.get('label'): sensor_labels[name]=payload['label']
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')

        elif self.path == '/set_job':
            with lock:
                job_info['client_name']=payload.get('client_name','')
                job_info['address']=payload.get('address','')
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')

        elif self.path == '/set_label':
            device=payload.get('device'); label=payload.get('label','')
            if device:
                with lock:
                    sensor_labels[device]=label
                    if device in sensor_data: sensor_data[device]['label']=label
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')

        elif self.path == '/snapshot':
            which=payload.get('which','baseline')
            with lock:
                snap={device:{"s1":d.get("s1"),"s2":d.get("s2"),
                              "s3":d.get("s3"),"s4":d.get("s4"),
                              "label":sensor_labels.get(device,d.get("label","")),"time":d.get("time")}
                      for device,d in sensor_data.items()}
                if which=='baseline': snapshot_baseline=snap
                else:                 snapshot_with_fan=snap
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')

        else:
            self.send_response(404); self.end_headers()

    def _send_json(self, data):
        body=json.dumps(data).encode()
        self.send_response(200); self.send_header('Content-Type','application/json')
        self.send_header('Access-Control-Allow-Origin','*'); self.end_headers(); self.wfile.write(body)

    def _send_html(self, html):
        self.send_response(200); self.send_header('Content-Type','text/html')
        self.end_headers(); self.wfile.write(html.encode())

    def _send_csv(self, data, filename):
        self.send_response(200); self.send_header('Content-Type','text/csv')
        self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.end_headers(); self.wfile.write(data.encode())


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
h1 { text-align: center; margin-bottom: 4px; font-size: 1.5em; letter-spacing: 2px; }
.subtitle { text-align: center; color: #7a8aaa; font-size: 0.85em; margin-bottom: 16px; letter-spacing: 1px; }
.job-bar { background:#131c2e; border:1px solid #2a3a5c; border-radius:8px; padding:12px 16px;
           max-width:1100px; margin:0 auto 16px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
.job-bar input { background:#0a0f1a; border:1px solid #2a3a5c; color:#e8edf5;
                 font-family:'Courier New',monospace; font-size:0.85em; padding:6px 10px;
                 border-radius:4px; flex:1; min-width:160px; }
.job-bar input:focus { outline:none; border-color:#4a7aff; }
.btn { background:#1a2a4a; border:1px solid #3a5aaa; color:#a8c8ff; font-family:'Courier New',monospace;
       font-size:0.8em; padding:6px 14px; border-radius:4px; cursor:pointer; letter-spacing:1px;
       white-space:nowrap; text-decoration:none; display:inline-block; }
.btn:hover { background:#2a3a6a; }
.btn-green { border-color:#00c060; color:#00e874; }
.btn-green:hover { background:#0d2a1a; }
.btn-red { border-color:#c03030; color:#ff6060; }
.btn-red:hover { background:#2a0d0d; }
.action-bar,.dl-bar { max-width:1100px; margin:0 auto 16px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
.bar-label { color:#5a7aaa; font-size:0.8em; letter-spacing:1px; }
.snap-table-wrap { max-width:1100px; margin:0 auto 24px; display:none; }
.snap-table-wrap.visible { display:block; }
.snap-table-title { color:#7a8aaa; font-size:0.8em; letter-spacing:1px; margin-bottom:8px; }
.snap-table { width:100%; border-collapse:collapse; font-size:0.85em; }
.snap-table th { background:#0d1525; color:#5a7aaa; text-align:center; padding:8px 10px;
                 border:1px solid #1e2e4a; letter-spacing:1px; font-size:0.75em; text-transform:uppercase; }
.snap-table th.col-left { text-align:left; }
.snap-table td { background:#0a0f1a; border:1px solid #1a2535; padding:8px 10px; text-align:center; color:#c8d8f8; }
.snap-table td.col-left { text-align:left; }
.snap-table td.col-device { color:#7a9adf; font-weight:700; }
.snap-table td.col-label  { color:#5a7aaa; font-size:0.85em; }
.snap-table td.delta-pass { color:#00e874; font-weight:700; }
.snap-table td.delta-fail { color:#ff6060; font-weight:700; }
.snap-table td.na         { color:#2a3a5a; }
.snap-table tr:hover td   { background:#0d1830; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; max-width:1100px; margin:0 auto; }
.card { background:#131c2e; border-radius:10px; padding:18px; border:1.5px solid #2a3a5c; }
.card-top { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
.card-name { font-size:1em; font-weight:700; color:#c8d8f8; letter-spacing:1px; }
.badge { font-size:0.7em; padding:3px 10px; border-radius:4px; font-weight:700; letter-spacing:1px; }
.badge-offline { background:#1e2535; color:#a0b0cc; border:1px solid #3a4460; }
.sensors-2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.sensors-4 { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.sensor-box { background:#0d1525; border-radius:8px; padding:12px; text-align:center; border:1px solid #1e2e4a; }
.sensor-box.pass { background:#0d1f17; border-color:#00c060; }
.sensor-box.fail { background:#1f0d0d; border-color:#e03030; }
.sensor-box.blue { background:#0d1530; border-color:#3060c0; }
.sensor-label { font-size:0.65em; color:#a0b8d8; margin-bottom:4px; text-transform:uppercase; letter-spacing:1.5px; font-weight:700; }
.sensor-box.pass .sensor-label { color:#00c060; }
.sensor-box.fail .sensor-label { color:#e03030; }
.sensor-box.blue .sensor-label { color:#5090ff; }
.s-top { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
.s-badge { font-size:0.65em; padding:2px 7px; border-radius:3px; font-weight:700; }
.s-badge-pass { background:#003d20; color:#00e874; border:1px solid #00c060; }
.s-badge-fail { background:#3d0000; color:#ff6060; border:1px solid #e03030; }
.sensor-value { font-size:1.8em; font-weight:700; }
.sensors-4 .sensor-value { font-size:1.3em; }
.pass-val { color:#00e874; } .fail-val { color:#ff6060; } .blue-val { color:#5090ff; } .na-val { color:#8899bb; }
.sensor-sub { font-size:0.72em; margin-top:4px; color:#5a7aaa; }
.slabel-wrap { margin-top:6px; }
.slabel-input { background:#0a0f1a; border:1px solid #1e2e4a; color:#a8c8ff;
                font-family:'Courier New',monospace; font-size:0.75em; padding:3px 6px;
                border-radius:3px; width:100%; text-align:left; }
.slabel-input:focus { outline:none; border-color:#4a7aff; }
.slabel-input::placeholder { color:#3a4a6a; }
#footer { text-align:center; color:#4a5a7a; font-size:0.78em; margin-top:24px; }
.no-sensors { text-align:center; color:#4a5a7a; margin-top:60px; grid-column:1/-1; }
  </style>
</head>
<body>
  <h1>PFE RADON SENSOR DASHBOARD</h1>
  <p class="subtitle" id="sub">Loading...</p>
  <div class="job-bar">
    <input id="client-name" type="text" placeholder="Client Name" />
    <input id="address" type="text" placeholder="Job Address" />
    <button class="btn" onclick="saveJob()">SAVE JOB INFO</button>
  </div>
  <div class="action-bar">
    <span class="bar-label">SNAPSHOTS:</span>
    <button class="btn btn-green" onclick="takeSnapshot('baseline')">&#9654; CAPTURE BASELINE</button>
    <button class="btn btn-red"   onclick="takeSnapshot('with_fan')">&#9654; CAPTURE WITH FAN</button>
    <a class="btn" href="/download/snapshots.csv">&#8595; DOWNLOAD SNAPSHOTS</a>
  </div>
  <div class="dl-bar">
    <span class="bar-label">DATA LOG:</span>
    <a class="btn" href="/download/log.csv">&#8595; DOWNLOAD FULL LOG (CSV)</a>
  </div>
  <div class="snap-table-wrap" id="snap-wrap">
    <div class="snap-table-title">SNAPSHOT COMPARISON</div>
    <table class="snap-table">
      <thead><tr>
        <th class="col-left">DEVICE</th>
        <th class="col-left">LABEL</th>
        <th>BASE S1</th><th>FAN S1</th><th>DELTA S1</th>
        <th>BASE S2</th><th>FAN S2</th><th>DELTA S2</th>
        <th>BASE S3</th><th>FAN S3</th><th>DELTA S3</th>
        <th>BASE S4</th><th>FAN S4</th><th>DELTA S4</th>
      </tr></thead>
      <tbody id="snap-tbody"></tbody>
    </table>
  </div>
  <div class="grid" id="grid"><div class="no-sensors">Waiting for sensors...</div></div>
  <div id="footer"></div>
  <script>
const localLabels = {};

async function saveJob() {
  await fetch('/set_job',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({client_name:document.getElementById('client-name').value,
                         address:document.getElementById('address').value})});
}

async function takeSnapshot(which) {
  await fetch('/snapshot',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({which})});
}

async function onLabelBlur(key, input) {
  const label = input.value.trim();
  localLabels[key] = label;
  await fetch('/set_label',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({device:key, label})});
}

const SENSOR_DEFS = [
  {k:'s1', tk:'tgt1', label:'Sensor 1'},
  {k:'s2', tk:'tgt2', label:'Sensor 2'},
  {k:'s3', tk:'tgt3', label:'Sensor 3'},
  {k:'s4', tk:'tgt4', label:'Sensor 4'},
];

function hasMuxSensors(s) {
  return s.s3 !== null && s.s3 !== undefined || s.s4 !== null && s.s4 !== undefined;
}

function sensorBoxHTML(sensorKey, headerLabel, val, tgt, stale, currentLabel) {
  let boxCls, valHTML, subHTML, badgeHTML='';
  if (stale || val===null || val===undefined) {
    boxCls=''; valHTML='<div class="sensor-value na-val">--</div>'; subHTML='<div class="sensor-sub">&nbsp;</div>';
  } else if (tgt===null || tgt===undefined) {
    boxCls='blue'; valHTML=`<div class="sensor-value blue-val">${val.toFixed(2)} Pa</div>`; subHTML='<div class="sensor-sub">&nbsp;</div>';
  } else {
    const pass=val<=tgt;
    boxCls=pass?'pass':'fail';
    const vc=pass?'pass-val':'fail-val', bc=pass?'s-badge-pass':'s-badge-fail';
    badgeHTML=`<span class="s-badge ${bc}">${pass?'PASS':'FAIL'}</span>`;
    valHTML=`<div class="sensor-value ${vc}">${val.toFixed(2)} Pa</div>`;
    subHTML=`<div class="sensor-sub">Target: ${tgt.toFixed(2)} Pa</div>`;
  }
  const lbl = (sensorKey in localLabels) ? localLabels[sensorKey] : (currentLabel||'');
  return `<div class="sensor-box ${boxCls}" id="box-${sensorKey}">
    <div class="s-top"><span class="sensor-label">${headerLabel}</span>${badgeHTML}</div>
    ${valHTML}${subHTML}
    <div class="slabel-wrap">
      <input class="slabel-input" id="lbl-${sensorKey}" type="text"
        placeholder="Label this tube..." value="${lbl}"
        onblur="onLabelBlur('${sensorKey}',this)"
        onkeydown="if(event.key==='Enter')this.blur()" />
    </div>
  </div>`;
}

function updateSensorBox(sensorKey, headerLabel, val, tgt, stale) {
  const box = document.getElementById('box-'+sensorKey);
  if (!box) return false;
  let boxCls='', valHTML='', subHTML='', badgeHTML='';
  if (stale || val===null || val===undefined) {
    valHTML='<div class="sensor-value na-val">--</div>'; subHTML='<div class="sensor-sub">&nbsp;</div>';
  } else if (tgt===null || tgt===undefined) {
    boxCls='blue'; valHTML=`<div class="sensor-value blue-val">${val.toFixed(2)} Pa</div>`; subHTML='<div class="sensor-sub">&nbsp;</div>';
  } else {
    const pass=val<=tgt;
    boxCls=pass?'pass':'fail';
    const vc=pass?'pass-val':'fail-val', bc=pass?'s-badge-pass':'s-badge-fail';
    badgeHTML=`<span class="s-badge ${bc}">${pass?'PASS':'FAIL'}</span>`;
    valHTML=`<div class="sensor-value ${vc}">${val.toFixed(2)} Pa</div>`;
    subHTML=`<div class="sensor-sub">Target: ${tgt.toFixed(2)} Pa</div>`;
  }
  box.className='sensor-box'+(boxCls?' '+boxCls:'');
  const stop = box.querySelector('.s-top');
  if (stop) stop.innerHTML=`<span class="sensor-label">${headerLabel}</span>${badgeHTML}`;
  const sv = box.querySelector('.sensor-value');
  if (sv) sv.outerHTML = valHTML;
  const svNew = box.querySelector('.sensor-value');
  if (svNew) svNew.insertAdjacentHTML('afterend', subHTML);
  const oldSub = box.querySelectorAll('.sensor-sub');
  if (oldSub.length > 1) oldSub[0].remove();
  return true;
}

let knownDevices = new Set();

async function refresh() {
  try {
    const res=await fetch('/data'); const data=await res.json();
    const sensors=data.sensors||{}, serverLabels=data.labels||{};

    if (data.job) {
      const cn=document.getElementById('client-name'), ad=document.getElementById('address');
      if (!cn.value) cn.value=data.job.client_name||'';
      if (!ad.value) ad.value=data.job.address||'';
    }

    for (const [key, lbl] of Object.entries(serverLabels)) {
      if (!(key in localLabels)) localLabels[key] = lbl;
    }

    const names=Object.keys(sensors).sort();
    const grid=document.getElementById('grid');

    if (!names.length) {
      grid.innerHTML='<div class="no-sensors">Waiting for sensors...</div>';
      knownDevices.clear();
    } else {
      const newDevices = names.filter(n => !knownDevices.has(n));
      const gone = [...knownDevices].filter(n => !sensors[n]);
      gone.forEach(n => { const el=document.getElementById('card-'+n); if (el) el.remove(); knownDevices.delete(n); });

      newDevices.forEach(name => {
        const s=sensors[name], stale=s.age>30;
        const isFour = hasMuxSensors(s);
        const gridCls = isFour ? 'sensors-4' : 'sensors-2';
        const badge=stale?'<span class="badge badge-offline">OFFLINE</span>':'';
        const slotsToShow = isFour ? SENSOR_DEFS : SENSOR_DEFS.slice(0,2);
        const boxesHTML = slotsToShow.map(def => {
          const key=name+'-'+def.k.toUpperCase();
          const lbl=(key in localLabels)?localLabels[key]:(serverLabels[key]||'');
          return sensorBoxHTML(key, def.label, s[def.k], s[def.tk], stale, lbl);
        }).join('');
        const card=document.createElement('div');
        card.className='card'; card.id='card-'+name;
        card.innerHTML=`
          <div class="card-top"><span class="card-name">${name}</span><span id="badge-${name}">${badge}</span></div>
          <div class="${gridCls}">${boxesHTML}</div>`;
        const idx=names.indexOf(name);
        const cards=[...grid.children].filter(el=>el.classList.contains('card'));
        if (idx>=cards.length) grid.appendChild(card);
        else grid.insertBefore(card,cards[idx]);
        knownDevices.add(name);
      });

      names.forEach(name => {
        if (newDevices.includes(name)) return;
        const s=sensors[name], stale=s.age>30;
        const badge=stale?'<span class="badge badge-offline">OFFLINE</span>':'';
        const badgeEl=document.getElementById('badge-'+name);
        if (badgeEl) badgeEl.innerHTML=badge;
        const isFour = hasMuxSensors(s);
        const slotsToShow = isFour ? SENSOR_DEFS : SENSOR_DEFS.slice(0,2);
        slotsToShow.forEach(def => {
          const key=name+'-'+def.k.toUpperCase();
          updateSensorBox(key, def.label, s[def.k], s[def.tk], stale);
        });
      });

      const ns=grid.querySelector('.no-sensors');
      if (ns) ns.remove();
    }

    document.getElementById('sub').textContent=names.length+' device(s) online';

    const bl=data.snapshot_baseline, wf=data.snapshot_with_fan;
    const wrap=document.getElementById('snap-wrap'), tbody=document.getElementById('snap-tbody');
    if (bl&&wf) {
      wrap.classList.add('visible');
      const devs=[...new Set([...Object.keys(bl),...Object.keys(wf)])].sort();
      tbody.innerHTML=devs.map(dev=>{
        const b=bl[dev]||{}, w=wf[dev]||{};
        function valCell(v) { return v!=null?`<td>${v.toFixed(2)} Pa</td>`:'<td class="na">--</td>'; }
        function deltaCell(bv,wv) {
          if (bv==null||wv==null) return '<td class="na">--</td>';
          const d=wv-bv;
          return `<td class="${d<=0?'delta-pass':'delta-fail'}">${d.toFixed(2)} Pa</td>`;
        }
        return `<tr>
          <td class="col-left col-device">${dev}</td>
          <td class="col-left col-label">${b.label||''}</td>
          ${valCell(b.s1)}${valCell(w.s1)}${deltaCell(b.s1,w.s1)}
          ${valCell(b.s2)}${valCell(w.s2)}${deltaCell(b.s2,w.s2)}
          ${valCell(b.s3)}${valCell(w.s3)}${deltaCell(b.s3,w.s3)}
          ${valCell(b.s4)}${valCell(w.s4)}${deltaCell(b.s4,w.s4)}
        </tr>`;
      }).join('');
    } else { wrap.classList.remove('visible'); }

    document.getElementById('footer').textContent='Updated: '+new Date().toLocaleTimeString();
  } catch(e) { document.getElementById('footer').textContent='Connection lost...'; }
}
refresh(); setInterval(refresh,2000);
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
                p1=current_pressure1; p2=current_pressure2
                p3=current_pressure3; p4=current_pressure4
                t1=current_temp1
                tg1=target1; tg2=target2; tg3=target3; tg4=target4
                lbl=sensor_labels.get(DEVICE_NAME,'')
            payload=json.dumps({'name':DEVICE_NAME,
                                's1':p1,'s2':p2,'s3':p3,'s4':p4,
                                'tgt1':tg1,'tgt2':tg2,'tgt3':tg3,'tgt4':tg4,
                                'temp1':t1,'label':lbl}).encode()
            req=Request(url, data=payload, headers={'Content-Type':'application/json'})
            urlopen(req, timeout=3)
        except Exception as e:
            print(f"Report error: {e}")
        time.sleep(1)

# =============================================================
# Screen
# =============================================================
def load_splash():
    try:
        img=Image.open(SPLASH_PATH).convert('RGB')
        return image_to_pixels(img)
    except Exception:
        return None

def image_to_pixels(img):
    pixels=[]
    for r,g,b in img.getdata():
        rgb565=((r&0xF8)<<8)|((g&0xFC)<<3)|(b>>3)
        pixels.extend([(rgb565>>8)&0xFF, rgb565&0xFF])
    return pixels

def _font(size, bold=False):
    try: return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)
    except Exception: return ImageFont.load_default()

def make_screen_boot(stage, temp_c):
    img=Image.new('RGB',(240,280),(0,0,0)); draw=ImageDraw.Draw(img)
    f_big=_font(34,True); f_med=_font(18,True); f_small=_font(14,False); f_tiny=_font(12,False)
    draw.text((6,4),DEVICE_NAME,font=f_small,fill=(120,120,255))
    draw.text((160,4),"SETUP",font=f_small,fill=(255,200,50))
    draw.line([(0,24),(240,24)],fill=(50,50,50),width=1)

    if stage=="zeroing":
        draw.text((20,60),"Zeroing sensors...",font=f_small,fill=(180,180,180))
        draw.text((20,90),"Please wait",font=f_tiny,fill=(120,120,120))

    elif stage=="pick_temp":
        with lock: tf=outdoor_temp_f; tc=temp_clicks
        draw.text((6,32),"Outdoor temp?",font=f_med,fill=(200,200,255))
        if tc==0:
            temp_f=c_to_f(temp_c) if temp_c is not None else tf
            label=f"AUTO: {temp_f:.0f}°F" if temp_f is not None else "AUTO: --°F"
            draw.text((6,75),label,font=f_med,fill=(100,220,255))
            draw.text((6,110),"Defaulting in 3s...",font=f_small,fill=(180,180,80))
            draw.text((6,135),"Press to change",font=f_tiny,fill=(150,150,150))
        else:
            c=min(5,tc); band=TEMP_CLICK_BANDS[c]
            label=TEMP_BAND_LABELS.get(band,"?")
            color=(100,220,255) if band=="auto" else (200,220,100)
            for i in range(5):
                col=(255,200,50) if i<c else (50,50,50)
                draw.ellipse([6+i*22,58,6+i*22+14,72],fill=col)
            draw.text((6,82),label,font=f_med,fill=color)
            draw.text((6,200),"Wait 3s to confirm",font=f_tiny,fill=(180,180,80))

    elif stage=="pick_zone":
        with lock: zc=zone_clicks; z=climate_zone
        draw.text((6,32),"Climate zone?",font=f_med,fill=(200,200,255))
        colors={ZONE_MILD:(100,255,100),ZONE_MODERATE:(255,200,50),ZONE_SEVERE:(255,80,80)}
        draw.text((6,68),z.upper(),font=f_big,fill=colors.get(z,(200,200,200)))
        if zc==0:
            draw.text((6,130),"Defaulting in 3s...",font=f_small,fill=(180,180,80))
            draw.text((6,155),"Press to change",font=f_tiny,fill=(150,150,150))
        else:
            draw.text((6,130),"1 click  = Mild",font=f_tiny,fill=(150,150,150))
            draw.text((6,148),"2 clicks = Moderate",font=f_tiny,fill=(150,150,150))
            draw.text((6,166),"3 clicks = Severe",font=f_tiny,fill=(150,150,150))
            draw.text((6,210),"Wait 3s to confirm",font=f_tiny,fill=(180,180,80))
        with lock: tbc=temp_band_choice
        if tbc: draw.text((6,254),f"Temp: {TEMP_BAND_LABELS.get(tbc,'?')}",font=f_tiny,fill=(80,80,160))

    elif stage=="lock_baseline":
        draw.text((6,32),"Ready to lock",font=f_med,fill=(200,200,255))
        draw.text((6,58),"baseline pressure",font=f_med,fill=(200,200,255))
        draw.text((6,110),"Fan must be OFF",font=f_small,fill=(255,180,50))
        draw.text((6,132),"Tubes in test holes",font=f_small,fill=(255,180,50))
        with lock: tbc=temp_band_choice; z=climate_zone
        if tbc: draw.text((6,175),f"Temp: {TEMP_BAND_LABELS.get(tbc,'?')}",font=f_tiny,fill=(80,120,200))
        draw.text((6,195),f"Zone: {z.upper()}",font=f_tiny,fill=(80,120,200))
        draw.text((6,230),"Press to lock baseline",font=f_tiny,fill=(180,180,80))

    with lock: batt=current_battery
    draw_battery_bar(draw,batt)
    return image_to_pixels(img)

def make_screen_running(p1, p2, p3, p4, t1, tgt1, tgt2, tgt3, tgt4, mode, mux_present):
    img=Image.new('RGB',(240,280),(0,0,0)); draw=ImageDraw.Draw(img)
    f_big=_font(38,True); f_med=_font(18,True); f_small=_font(14,False); f_tiny=_font(12,False)
    mode_colors={"home":(100,200,100),"host":(0,230,0),"client":(0,230,0),
                 "client_reporting":(0,230,0),"searching":(255,200,50)}
    mode_labels={"home":"HOME","host":"HOST","client":"CLIENT",
                 "client_reporting":"CLIENT","searching":"..."}
    draw.text((6,4),DEVICE_NAME,font=f_small,fill=(120,120,255))
    draw.text((160,4),mode_labels.get(mode,"?"),font=f_small,fill=mode_colors.get(mode,(160,160,160)))
    draw.line([(0,24),(240,24)],fill=(50,50,50),width=1)

    if not mux_present:
        # Original 2-sensor layout (big numbers)
        def draw_sensor(label, pressure, target, y_top):
            draw.text((6,y_top),label,font=f_tiny,fill=(150,150,150))
            if pressure is None:
                draw.text((6,y_top+16),"--",font=f_big,fill=(80,80,80))
            elif target is None:
                draw.text((6,y_top+16),f"{pressure:.2f}",font=f_big,fill=(80,160,255))
                draw.text((148,y_top+16),"Pa",font=f_med,fill=(60,120,200))
            else:
                passed=pressure<=target; color=(0,230,0) if passed else (255,60,60)
                draw.text((180,y_top+16),"PASS" if passed else "FAIL",font=f_med,fill=color)
                draw.text((6,y_top+16),f"{pressure:.2f}",font=f_big,fill=color)
                draw.text((148,y_top+16),"Pa",font=f_med,fill=color)
                draw.text((6,y_top+65),f"Target: {target:.2f} Pa",font=f_small,fill=(180,180,180))
            draw.line([(0,y_top+85),(240,y_top+85)],fill=(40,40,40),width=1)

        draw_sensor("SENSOR 1", p1, tgt1, 28)
        draw_sensor("SENSOR 2", p2, tgt2, 118)

    else:
        # Compact 2x2 layout for 4 sensors
        f_cmed=_font(16,True); f_ctiny=_font(11,False)
        cells=[(p1,tgt1,"S1",0,28),(p2,tgt2,"S2",120,28),
               (p3,tgt3,"S3",0,148),(p4,tgt4,"S4",120,148)]
        for p,tgt,label,x,y in cells:
            draw.text((x+4,y),label,font=f_ctiny,fill=(150,150,150))
            if p is None:
                draw.text((x+4,y+14),"--",font=f_cmed,fill=(80,80,80))
            elif tgt is None:
                draw.text((x+4,y+14),f"{p:.2f}",font=f_cmed,fill=(80,160,255))
                draw.text((x+4,y+36),"Pa",font=f_ctiny,fill=(60,120,200))
            else:
                passed=p<=tgt; color=(0,230,0) if passed else (255,60,60)
                draw.text((x+4,y+14),f"{p:.2f}",font=f_cmed,fill=color)
                draw.text((x+4,y+36),"Pa",font=f_ctiny,fill=color)
                draw.text((x+4,y+52),"PASS" if passed else "FAIL",font=f_ctiny,fill=color)
            if x==0:
                draw.line([(120,y-2),(120,y+110)],fill=(50,50,50),width=1)
            draw.line([(0,y+112),(240,y+112)],fill=(40,40,40),width=1)

    # Footer
    with lock: tbc=temp_band_choice; z=climate_zone
    draw.text((6,215),TEMP_BAND_LABELS.get(tbc,"Temp not set"),font=f_tiny,fill=(100,100,200))
    draw.text((6,230),z.upper(),font=f_tiny,fill=(100,100,200))
    if mode=="host":
        with lock: count=len(sensor_data)
        draw.text((6,245),f"Devices: {count}",font=f_tiny,fill=(0,180,80))
    elif mode in ("client","client_reporting"):
        draw.text((6,245),"Reporting to host",font=f_tiny,fill=(100,180,255))
    ip=get_own_ip()
    draw.text((6,260),f"http://{ip}" if ip else "...",font=f_tiny,
              fill=(100,180,255) if ip else (160,160,160))
    with lock: batt=current_battery
    draw_battery_bar(draw,batt)
    return image_to_pixels(img)

def screen_thread(board, mux_present):
    last=None
    while True:
        with lock:
            p1=current_pressure1; p2=current_pressure2
            p3=current_pressure3; p4=current_pressure4
            t1=current_temp1
            tg1=target1; tg2=target2; tg3=target3; tg4=target4
            mode=wifi_mode; stage=boot_stage
            tc=temp_clicks; zc=zone_clicks; tbc=temp_band_choice; z=climate_zone
        current=(stage,
                 round(p1,2) if p1 is not None else None,
                 round(p2,2) if p2 is not None else None,
                 round(p3,2) if p3 is not None else None,
                 round(p4,2) if p4 is not None else None,
                 tg1,tg2,tg3,tg4,mode,tc,zc,tbc,z)
        if current!=last:
            if stage=="running":
                screen_data=make_screen_running(p1,p2,p3,p4,t1,tg1,tg2,tg3,tg4,mode,mux_present)
            else:
                screen_data=make_screen_boot(stage,t1)
            board.draw_image(0,0,240,280,screen_data)
            last=current
        time.sleep(0.5)

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
    time.sleep(2)

def wifi_and_serve():
    mode = setup_wifi()
    print(f"WiFi mode: {mode}")
    if mode == "host":
        threading.Thread(target=data_log_loop, daemon=True).start()
        run_web_server()

threading.Thread(target=battery_poll_loop, daemon=True).start()
threading.Thread(target=wifi_and_serve, daemon=True).start()

with SMBus(1) as bus:
    # ── Detect hardware once at boot ──────────────────────────
    mux_found = detect_hardware(bus)
    with lock:
        has_mux = mux_found

    # Screen starts after hardware is known (needs mux_found to pick layout)
    threading.Thread(target=screen_thread, args=(board, mux_found), daemon=True).start()

    # ── Zero all present sensors ──────────────────────────────
    do_zeroing(bus, mux_found)

    # ── Main loop ─────────────────────────────────────────────
    while True:
        r1, r2, r3, r4 = read_all_raw(bus, mux_found)

        p1_raw, t1 = r1
        p2_raw, t2 = r2
        p3_raw, t3 = r3
        p4_raw, t4 = r4

        with lock:
            zo1=zero_offset1; zo2=zero_offset2; zo3=zero_offset3; zo4=zero_offset4

        p1 = (p1_raw - zo1) if p1_raw is not None else None
        p2 = (p2_raw - zo2) if p2_raw is not None else None
        p3 = (p3_raw - zo3) if p3_raw is not None else None
        p4 = (p4_raw - zo4) if p4_raw is not None else None

        with lock:
            current_pressure1=p1; current_temp1=t1
            current_pressure2=p2; current_temp2=t2
            current_pressure3=p3
            current_pressure4=p4
            mode=wifi_mode; tg1=target1; tg2=target2; tg3=target3; tg4=target4

        if mode == "host":
            with lock:
                sensor_data[DEVICE_NAME]={'s1':p1,'s2':p2,'s3':p3,'s4':p4,
                                           'tgt1':tg1,'tgt2':tg2,'tgt3':tg3,'tgt4':tg4,
                                           'temp1':t1,'label':sensor_labels.get(DEVICE_NAME,''),
                                           'time':time.time()}

        if mode == "client":
            threading.Thread(target=report_data_loop, args=(HOST_IP,), daemon=True).start()
            with lock: wifi_mode="client_reporting"

        parts = [f"S1:{f'{p1:.2f}Pa' if p1 is not None else '--'}",
                 f"S2:{f'{p2:.2f}Pa' if p2 is not None else '--'}"]
        if mux_found:
            parts += [f"S3:{f'{p3:.2f}Pa' if p3 is not None else '--'}",
                      f"S4:{f'{p4:.2f}Pa' if p4 is not None else '--'}"]
        print("  ".join(parts) + f"  Stage:{boot_stage}  Mode:{wifi_mode}")
        time.sleep(1)
