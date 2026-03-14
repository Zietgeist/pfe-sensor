#!/usr/bin/env python3
"""
PFE Pressure Sensor
- At home: connects to PFE-home for updates/testing
- In field: auto-assigns as host (creates PFE-NET + runs dashboard)
            or client (joins PFE-NET + sends data)
Same code runs on every device.
"""

import sys
import os
import time
import random
import asyncio
import struct
import threading
import subprocess
import socket
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError
from smbus2 import SMBus, i2c_msg
from PIL import Image, ImageDraw, ImageFont


sys.path.append('/home/pi/Whisplay/Driver')
from WhisPlay import WhisPlayBoard

# --- Constants ---
DEVICE_NAME        = os.uname().nodename
SDP_ADDR_1         = 0x25   # Sensor 1 (inlet)
SDP_ADDR_2         = 0x26   # Sensor 2 (outlet)

HOME_SSID          = "PFE-home"
HOME_PASSWORD      = "pferadon1"
SITE_SSID          = "PFE-NET"
SITE_PASSWORD      = "pferadon1"

HOST_IP            = "192.168.4.1"
WEB_PORT           = 80
TARGET_PRESSURE    = -12.5   # Pa — adjust as needed

# --- Shared state ---
lock              = threading.Lock()
active            = True
current_pressure1 = None   # Sensor 1 (0x25)
current_temp1     = None
current_pressure2 = None   # Sensor 2 (0x26)
current_temp2     = None
target_pressure   = TARGET_PRESSURE
is_host           = False
wifi_mode         = "searching"   # "home" | "host" | "client" | "searching"

# Sensor data store (host only)
# { "PFE-1": {"s1": -12.3, "s2": -11.9, "temp1": 20.1, "temp2": 20.3, "time": 123456} }
sensor_data = {}


# =============================================================
# WiFi
# =============================================================

def scan_for(ssid, retries=2):
    for _ in range(retries):
        try:
            result = subprocess.run(
                ['sudo', 'nmcli', '-t', '-f', 'SSID', 'dev', 'wifi', 'list', '--rescan', 'yes'],
                capture_output=True, text=True, timeout=20
            )
            if ssid in result.stdout:
                return True
        except Exception as e:
            print(f"Scan error: {e}")
        time.sleep(2)
    return False

def connect_to(ssid, password):
    try:
        subprocess.run([
            'sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid,
            'password', password
        ], check=True, timeout=30)
        time.sleep(3)
        print(f"Connected to {ssid}")
        return True
    except Exception as e:
        print(f"Failed to connect to {ssid}: {e}")
        return False

def create_hotspot():
    try:
        print(f"Creating hotspot: {SITE_SSID}")
        subprocess.run([
            'sudo', 'nmcli', 'dev', 'wifi', 'hotspot',
            'ifname', 'wlan0',
            'ssid', SITE_SSID,
            'password', SITE_PASSWORD
        ], check=True, timeout=30)
        time.sleep(3)
        print(f"Hotspot up. IP: {HOST_IP}")
        return True
    except Exception as e:
        print(f"Failed to create hotspot: {e}")
        return False

def get_host_ip():
    try:
        result = subprocess.run(
            ['ip', 'route', 'show', 'default', 'dev', 'wlan0'],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if 'default' in line:
                return line.split()[2]
    except Exception:
        pass
    return HOST_IP

def setup_wifi():
    global wifi_mode

    print(f"Checking for {HOME_SSID}...")
    if scan_for(HOME_SSID):
        print(f"{HOME_SSID} found — connecting for updates")
        if connect_to(HOME_SSID, HOME_PASSWORD):
            wifi_mode = "home"
            return "home"

    delay = random.uniform(1, 5)
    print(f"No home network. Waiting {delay:.1f}s before checking for {SITE_SSID}...")
    time.sleep(delay)

    print(f"Checking for {SITE_SSID}...")
    if scan_for(SITE_SSID):
        print(f"{SITE_SSID} found — joining as client")
        if connect_to(SITE_SSID, SITE_PASSWORD):
            wifi_mode = "client"
            return "client"

    print(f"No networks found — becoming host")
    if create_hotspot():
        wifi_mode = "host"
        return "host"

    wifi_mode = "searching"
    return "searching"


# =============================================================
# Web Server (host only)
# =============================================================

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

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
            html = build_dashboard_html()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/report':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                name = payload.get('name')
                if name:
                    with lock:
                        sensor_data[name] = {
                            's1':    payload.get('s1'),
                            's2':    payload.get('s2'),
                            'temp1': payload.get('temp1'),
                            'temp2': payload.get('temp2'),
                            'time':  time.time()
                        }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'OK')
            except Exception:
                self.send_response(400)
                self.end_headers()

def build_dashboard_html():
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PFE Sensor Dashboard</title>
  <style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Courier New', monospace; background: #0a0f1a; color: #e8edf5; padding: 20px; }}
h1 {{ color: #e8edf5; text-align: center; margin-bottom: 4px; font-size: 1.5em; letter-spacing: 2px; font-weight: 700; }}
.subtitle {{ text-align: center; color: #7a8aaa; font-size: 0.85em; margin-bottom: 24px; letter-spacing: 1px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; max-width: 1100px; margin: 0 auto; }}
.device-card {{ background: #131c2e; border-radius: 10px; padding: 18px; border: 1.5px solid #2a3a5c; }}
.device-card.pass {{ border-color: #00c060; background: #0d1f17; }}
.device-card.fail {{ border-color: #e03030; background: #1f0d0d; }}
.device-card.stale {{ border-color: #3a4460; opacity: 0.55; }}
.device-name {{ font-size: 1em; font-weight: 700; color: #c8d8f8; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; letter-spacing: 1px; }}
.status-badge {{ font-size: 0.75em; padding: 3px 10px; border-radius: 4px; font-weight: 700; letter-spacing: 1px; }}
.badge-pass {{ background: #003d20; color: #00e874; border: 1px solid #00c060; }}
.badge-fail {{ background: #3d0000; color: #ff6060; border: 1px solid #e03030; }}
.badge-stale {{ background: #1e2535; color: #7a8aaa; border: 1px solid #3a4460; }}
.sensors {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
.sensor-box {{ background: #0d1525; border-radius: 8px; padding: 12px; text-align: center; border: 1px solid #1e2e4a; }}
.sensor-label {{ font-size: 0.7em; color: #5a7aaa; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 700; }}
.sensor-value {{ font-size: 2em; font-weight: 700; }}
.sensor-inwc {{ font-size: 0.72em; color: #5a7aaa; margin-top: 4px; }}
.pass-val {{ color: #00e874; }}
.fail-val {{ color: #ff6060; }}
.na-val {{ color: #3a4a6a; font-size: 1.4em; }}
#footer {{ text-align: center; color: #4a5a7a; font-size: 0.78em; margin-top: 24px; font-family: monospace; }}
.no-sensors {{ text-align: center; color: #4a5a7a; margin-top: 60px; font-size: 1em; grid-column: 1/-1; letter-spacing: 1px; }}
  </style>
</head>
<body>
  <h1>PFE RADON SENSOR DASHBOARD</h1>
  <p class="subtitle">TARGET: {TARGET_PRESSURE} Pa &nbsp;|&nbsp; {abs(TARGET_PRESSURE)/249.0:.4f} inWC</p>
  <div class="grid" id="grid"><div class="no-sensors">Waiting for sensors...</div></div>
  <div id="footer"></div>
  <script>
    const TARGET = {TARGET_PRESSURE};
    function fmtPa(v) {{ return v !== null && v !== undefined ? v.toFixed(2) : '--'; }}
    function fmtInWC(v) {{ return v !== null && v !== undefined ? (Math.abs(v)/249.0).toFixed(4) : '--'; }}
    function valClass(v, stale) {{
      if (stale || v === null || v === undefined) return 'na-val';
      return v <= TARGET ? 'pass-val' : 'fail-val';
    }}
    async function refresh() {{
      try {{
        const res = await fetch('/data');
        const data = await res.json();
        const grid = document.getElementById('grid');
        const now = Date.now() / 1000;
        const names = Object.keys(data).sort();
        if (!names.length) {{ grid.innerHTML = '<div class="no-sensors">Waiting for sensors...</div>'; return; }}
        grid.innerHTML = names.map(name => {{
          const s = data[name];
          const stale = (now - s.time) > 30;
          const bothPass = !stale && (s.s1 !== null && s.s1 <= TARGET) && (s.s2 === null || s.s2 <= TARGET);
          const anyFail  = !stale && ((s.s1 !== null && s.s1 > TARGET) || (s.s2 !== null && s.s2 > TARGET));
          const cls      = stale ? 'stale' : (bothPass ? 'pass' : (anyFail ? 'fail' : ''));
          const badgeCls = stale ? 'badge-stale' : (bothPass ? 'badge-pass' : (anyFail ? 'badge-fail' : 'badge-stale'));
          const badgeTxt = stale ? 'OFFLINE' : (bothPass ? 'PASS' : (anyFail ? 'FAIL' : '?'));
          const c1 = valClass(s.s1, stale);
          const c2 = valClass(s.s2, stale);
          const s2html = (s.s2 !== null && s.s2 !== undefined)
            ? `<div class="sensor-value ${{c2}}">${{fmtPa(s.s2)}}</div><div class="sensor-inwc">${{fmtInWC(s.s2)}} inWC</div>`
            : `<div class="sensor-value na-val">--</div><div class="sensor-inwc">&nbsp;</div>`;
          return `<div class="device-card ${{cls}}">
            <div class="device-name">${{name}}<span class="status-badge ${{badgeCls}}">${{badgeTxt}}</span></div>
            <div class="sensors">
              <div class="sensor-box">
                <div class="sensor-label">Sensor 1 &mdash; Inlet</div>
                <div class="sensor-value ${{c1}}">${{fmtPa(s.s1)}}</div>
                <div class="sensor-inwc">${{fmtInWC(s.s1)}} inWC</div>
              </div>
              <div class="sensor-box">
                <div class="sensor-label">Sensor 2 &mdash; Outlet</div>
                ${{s2html}}
              </div>
            </div>
          </div>`;
        }}).join('');
        document.getElementById('footer').textContent = 'Updated: ' + new Date().toLocaleTimeString();
      }} catch(e) {{
        document.getElementById('footer').textContent = 'Connection lost...';
      }}
    }}
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>"""
def run_web_server():
    server = HTTPServer(('0.0.0.0', WEB_PORT), DashboardHandler)
    print(f"Dashboard running at http://{HOST_IP}")
    server.serve_forever()


# =============================================================
# Data Reporter (client only)
# =============================================================

def report_data_loop(host_ip):
    url = f"http://{host_ip}/report"
    while True:
        try:
            with lock:
                p1 = current_pressure1
                t1 = current_temp1
                p2 = current_pressure2
                t2 = current_temp2
            payload = json.dumps({
                'name':  DEVICE_NAME,
                's1':    p1,
                's2':    p2,
                'temp1': t1,
                'temp2': t2,
            }).encode()
            req = Request(url, data=payload, headers={'Content-Type': 'application/json'})
            urlopen(req, timeout=3)
        except Exception as e:
            print(f"Report error: {e}")
        time.sleep(1)


# =============================================================
# Sensor
# =============================================================

def init_sensor(bus):
    try:
        bus.i2c_rdwr(i2c_msg.write(0x00, [0x06]))
        time.sleep(0.05)
    except Exception:
        pass

def read_sdp(bus, address):
    """Read one SDP sensor. Returns (pressure_pa, temp_c) or (None, None) if missing."""
    try:
        bus.i2c_rdwr(i2c_msg.write(address, [0x36, 0x2F]))
        time.sleep(0.1)
        read = i2c_msg.read(address, 9)
        bus.i2c_rdwr(read)
        data = list(read)
        raw_p = (data[0] << 8) | data[1]
        if raw_p > 32767:
            raw_p -= 65536
        raw_t = (data[3] << 8) | data[4]
        if raw_t > 32767:
            raw_t -= 65536
        scale = (data[6] << 8) | data[7]
        if scale == 0:
            return None, None
        return raw_p / scale, raw_t / 200.0
    except Exception:
        return None, None


# =============================================================
# Screen
# =============================================================

def load_splash():
    try:
        img = Image.open('/home/pi/pfe-sensor/marten_screen.png').convert('RGB')
        return image_to_pixels(img)
    except Exception as e:
        print(f"Splash load error: {e}")
        return None

def image_to_pixels(img):
    pixels = []
    for r, g, b in img.getdata():
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        pixels.extend([(rgb565 >> 8) & 0xFF, rgb565 & 0xFF])
    return pixels

def make_screen(p1, p2, target, mode):
    """
    Draw the screen with two sensor readings.
    p1 / p2 are floats or None (None = sensor not present → show --)
    """
    img  = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fp   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fr   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if os.path.exists(fp):
        f_big   = ImageFont.truetype(fp, 44)
        f_med   = ImageFont.truetype(fp, 20)
        f_small = ImageFont.truetype(fr, 15)
        f_tiny  = ImageFont.truetype(fr, 13)
    else:
        f_big = f_med = f_small = f_tiny = ImageFont.load_default()

    # ── Header bar ──────────────────────────────────────────
    mode_colors = {
        "home":      (100, 200, 100),
        "host":      (200, 150,  50),
        "client":    (100, 180, 255),
        "searching": (160, 160, 160),
    }
    mode_labels = {"home": "HOME", "host": "HOST", "client": "CLIENT", "searching": "..."}
    draw.text((6,  4), DEVICE_NAME,               font=f_small, fill=(120, 120, 255))
    draw.text((160, 4), mode_labels.get(mode, "?"), font=f_small, fill=mode_colors.get(mode, (160,160,160)))

    # ── Divider ──────────────────────────────────────────────
    draw.line([(0, 24), (240, 24)], fill=(50, 50, 50), width=1)

    # ── Helper: draw one sensor block ────────────────────────
    def draw_sensor(label, pressure, y_top):
        draw.text((6, y_top), label, font=f_tiny, fill=(150, 150, 150))

        if pressure is None:
            # Sensor not installed
            draw.text((6, y_top + 16), "--", font=f_big, fill=(80, 80, 80))
            
        else:
            passed = pressure <= target
            color  = (0, 230, 0) if passed else (255, 60, 60)
            label2 = "PASS" if passed else "FAIL"
            draw.text((185, y_top + 16), label2, font=f_med, fill=color)
            draw.text((6,   y_top + 16), f"{pressure:.2f}", font=f_big, fill=color)
            draw.text((6,   y_top + 65), "Pa", font=f_small, fill=(180, 180, 180))

        draw.line([(0, y_top + 85), (240, y_top + 85)], fill=(40, 40, 40), width=1)

    # ── Sensor 1 (Inlet) — top half ─────────────────────────
    draw_sensor("SENSOR 1 ", p1,  28)

    # ── Sensor 2 (Outlet) — bottom half ─────────────────────
    draw_sensor("SENSOR 2 ", p2, 118)

    # ── Footer: target + mode info ───────────────────────────
    
    draw.text((6, 215), f"Target: {target:.1f} Pa", font=f_tiny, fill=(100, 100, 200))
    if mode == "host":
        with lock:
            count = len(sensor_data)
        draw.text((6, 234), f"Devices online: {count}", font=f_tiny, fill=(0, 180, 80))
    elif mode == "client":
        draw.text((6, 234), "Reporting to host", font=f_tiny, fill=(100, 180, 255))
    elif mode == "home":
        draw.text((6, 234), "Home network — idle", font=f_tiny, fill=(100, 200, 100))

    draw.text((6, 254), f"http://{get_host_ip()}", font=f_tiny, fill=(100, 180, 255))
    return image_to_pixels(img)

def screen_thread(board, splash):
    if splash:
        board.draw_image(0, 0, 240, 280, splash)
    last = (None, None, None, None)
    while True:
        with lock:
            is_active = active
            p1   = current_pressure1
            p2   = current_pressure2
            tgt  = target_pressure
            mode = wifi_mode
        current = (round(p1, 1) if p1 else None,
                   round(p2, 1) if p2 else None,
                   tgt, mode)
        if is_active and current != last:
            screen_data = make_screen(p1, p2, tgt, mode)
            board.draw_image(0, 0, 240, 280, screen_data)
            last = current
        time.sleep(1)


# =============================================================
# Button
# =============================================================

def button_pressed():
    global active
    with lock:
        active = not active
    if active:
        board.set_backlight(80)
    else:
        board.set_backlight(0)
        board.fill_screen(0)


# =============================================================
# Main
# =============================================================

print(f"Starting PFE Sensor — {DEVICE_NAME}")
board = WhisPlayBoard()
board.set_backlight(80)
board.on_button_press(button_pressed)

splash = load_splash()
if splash:
    board.draw_image(0, 0, 240, 280, splash)

# WiFi setup
mode = setup_wifi()
print(f"WiFi mode: {mode}")

if mode == "host":
    threading.Thread(target=run_web_server, daemon=True).start()
elif mode == "client":
    host_ip = get_host_ip()
    print(f"Reporting to host at {host_ip}")
    threading.Thread(target=report_data_loop, args=(host_ip,), daemon=True).start()
elif mode == "home":
    print("Home network — idle, ready for updates")

# Screen always runs
threading.Thread(target=screen_thread, args=(board, splash), daemon=True).start()

# ── Sensor loop ───────────────────────────────────────────────
with SMBus(1) as bus:
    init_sensor(bus)
    while True:
        p1, t1 = read_sdp(bus, SDP_ADDR_1)
        p2, t2 = read_sdp(bus, SDP_ADDR_2)

        with lock:
            current_pressure1 = p1
            current_temp1     = t1
            current_pressure2 = p2
            current_temp2     = t2

        # Host logs its own data into the dashboard
        if mode == "host":
            with lock:
                sensor_data[DEVICE_NAME] = {
                    's1':    p1,
                    's2':    p2,
                    'temp1': t1,
                    'temp2': t2,
                    'time':  time.time()
                }

        s1_str = f"{p1:.2f} Pa" if p1 is not None else "--"
        s2_str = f"{p2:.2f} Pa" if p2 is not None else "--"
        print(f"S1: {s1_str}  S2: {s2_str}  Mode: {mode}")
        time.sleep(1)
