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
from bless import BlessServer, BlessGATTCharacteristic, GATTCharacteristicProperties, GATTAttributePermissions

sys.path.append('/home/ivan/Whisplay/Driver')
from WhisPlay import WhisPlayBoard

# --- Constants ---
DEVICE_NAME        = os.uname().nodename
SDP_ADDRESS        = 0x25
SERVICE_UUID       = "12345678-1234-5678-1234-56789abcdef0"
PRESSURE_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
TARGET_CHAR_UUID   = "12345678-1234-5678-1234-56789abcdef2"

HOME_SSID          = "PFE-home"
HOME_PASSWORD      = "pferadon1"
SITE_SSID          = "PFE-NET"
SITE_PASSWORD      = "pferadon1"

HOST_IP            = "192.168.4.1"
WEB_PORT           = 80
TARGET_PRESSURE    = -12.5   # Pa — adjust as needed

# --- Shared state ---
lock             = threading.Lock()
active           = False
current_pressure = 0.0
current_temp     = 0.0
target_pressure  = TARGET_PRESSURE
is_host          = False
wifi_mode        = "searching"   # "home" | "host" | "client" | "searching"

# Sensor data store (host only) { "PFE-1": {"pressure": -12.3, "temp": 20.1, "time": 123456} }
sensor_data = {}


# =============================================================
# WiFi
# =============================================================

def scan_for(ssid, retries=2):
    """Return True if ssid is visible in a WiFi scan."""
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
    """Connect to a WiFi network. Returns True on success."""
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
    """Create PFE-NET hotspot. Returns True on success."""
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
    """Find the gateway IP on wlan0 (the host's IP)."""
    try:
        result = subprocess.run(
            ['ip', 'route', 'show', 'dev', 'wlan0'],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if 'default' in line:
                return line.split()[2]
    except Exception:
        pass
    return HOST_IP

def setup_wifi():
    """
    WiFi priority logic — runs once at startup.
    Returns: "home" | "host" | "client"
    """
    global wifi_mode

    # 1. Check for PFE-home
    print(f"Checking for {HOME_SSID}...")
    if scan_for(HOME_SSID):
        print(f"{HOME_SSID} found — connecting for updates")
        if connect_to(HOME_SSID, HOME_PASSWORD):
            wifi_mode = "home"
            return "home"

    # 2. Random delay to prevent two devices creating hotspot simultaneously
    delay = random.uniform(1, 5)
    print(f"No home network. Waiting {delay:.1f}s before checking for {SITE_SSID}...")
    time.sleep(delay)

    # 3. Check for PFE-NET
    print(f"Checking for {SITE_SSID}...")
    if scan_for(SITE_SSID):
        print(f"{SITE_SSID} found — joining as client")
        if connect_to(SITE_SSID, SITE_PASSWORD):
            wifi_mode = "client"
            return "client"

    # 4. No networks found — become the host
    print(f"No networks found — becoming host")
    if create_hotspot():
        wifi_mode = "host"
        return "host"

    # Fallback
    wifi_mode = "searching"
    return "searching"


# =============================================================
# Web Server (host only)
# =============================================================

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress noisy request logs

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
                pressure = payload.get('pressure')
                temp = payload.get('temp')
                if name and pressure is not None:
                    with lock:
                        sensor_data[name] = {
                            'pressure': pressure,
                            'temp': temp,
                            'time': time.time()
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
    body {{ font-family: Arial, sans-serif; background: #111; color: #eee; margin: 0; padding: 20px; }}
    h1 {{ color: #6af; text-align: center; margin-bottom: 6px; }}
    .subtitle {{ text-align: center; color: #555; font-size: 0.85em; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; max-width: 960px; margin: 0 auto; }}
    .card {{ background: #222; border-radius: 12px; padding: 20px; text-align: center; border: 2px solid #444; }}
    .card.pass {{ border-color: #0f0; }}
    .card.fail {{ border-color: #f44; }}
    .card.stale {{ border-color: #555; opacity: 0.5; }}
    .sensor-name {{ font-size: 1.1em; color: #adf; margin-bottom: 10px; font-weight: bold; }}
    .pressure {{ font-size: 2.5em; font-weight: bold; }}
    .unit {{ font-size: 0.85em; color: #888; margin-top: 4px; }}
    .temp {{ font-size: 0.85em; color: #aaa; margin-top: 6px; }}
    .status {{ margin-top: 10px; font-size: 1.1em; font-weight: bold; }}
    .pass-text {{ color: #0f0; }} .fail-text {{ color: #f44; }} .stale-text {{ color: #666; }}
    #footer {{ text-align: center; color: #444; font-size: 0.8em; margin-top: 24px; }}
    .no-sensors {{ text-align: center; color: #555; margin-top: 60px; font-size: 1.1em; grid-column: 1/-1; }}
  </style>
</head>
<body>
  <h1>PFE Radon Sensor Dashboard</h1>
  <p class="subtitle">Target: {TARGET_PRESSURE} Pa &nbsp;|&nbsp; {abs(TARGET_PRESSURE)/249.0:.4f} inWC</p>
  <div class="grid" id="grid"><div class="no-sensors">Waiting for sensors...</div></div>
  <div id="footer"></div>
  <script>
    const TARGET = {TARGET_PRESSURE};
    async function refresh() {{
      try {{
        const res = await fetch('/data');
        const data = await res.json();
        const grid = document.getElementById('grid');
        const now = Date.now() / 1000;
        const names = Object.keys(data).sort();
        if (names.length === 0) {{
          grid.innerHTML = '<div class="no-sensors">Waiting for sensors...</div>';
          return;
        }}
        grid.innerHTML = names.map(name => {{
          const s = data[name];
          const stale = (now - s.time) > 10;
          const pass = s.pressure <= TARGET;
          const inwc = (Math.abs(s.pressure) / 249.0).toFixed(4);
          const cls = stale ? 'stale' : (pass ? 'pass' : 'fail');
          const statusCls = stale ? 'stale-text' : (pass ? 'pass-text' : 'fail-text');
          const statusTxt = stale ? 'OFFLINE' : (pass ? 'PASS' : 'FAIL');
          const color = stale ? '#555' : (pass ? '#0f0' : '#f44');
          const temp = s.temp !== null ? s.temp.toFixed(1) + ' °C' : '—';
          return `<div class="card ${{cls}}">
            <div class="sensor-name">${{name}}</div>
            <div class="pressure" style="color:${{color}}">${{s.pressure.toFixed(2)}}</div>
            <div class="unit">Pa &nbsp;|&nbsp; ${{inwc}} inWC</div>
            <div class="temp">Temp: ${{temp}}</div>
            <div class="status ${{statusCls}}">${{statusTxt}}</div>
          </div>`;
        }}).join('');
        document.getElementById('footer').textContent = 'Updated: ' + new Date().toLocaleTimeString();
      }} catch(e) {{
        document.getElementById('footer').textContent = 'Connection lost...';
      }}
    }}
    refresh();
    setInterval(refresh, 3000);
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
    """Send pressure readings to the host every 2 seconds."""
    url = f"http://{host_ip}/report"
    while True:
        try:
            with lock:
                p = current_pressure
                t = current_temp
            payload = json.dumps({
                'name': DEVICE_NAME,
                'pressure': p,
                'temp': t
            }).encode()
            req = Request(url, data=payload, headers={'Content-Type': 'application/json'})
            urlopen(req, timeout=3)
        except Exception as e:
            print(f"Report error: {e}")
        time.sleep(2)


# =============================================================
# Sensor
# =============================================================

def init_sensor(bus):
    bus.i2c_rdwr(i2c_msg.write(0x00, [0x06]))
    time.sleep(0.05)

def read_pressure(bus):
    try:
        bus.i2c_rdwr(i2c_msg.write(SDP_ADDRESS, [0x36, 0x2F]))
        time.sleep(0.1)
        read = i2c_msg.read(SDP_ADDRESS, 9)
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
        img = Image.open('/home/ivan/marten_screen.png').convert('RGB')
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

def make_screen(pressure, temperature, target, mode):
    img = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if os.path.exists(font_path):
        font_big   = ImageFont.truetype(font_path, 52)
        font_med   = ImageFont.truetype(font_path, 24)
        font_small = ImageFont.truetype(font_path, 18)
    else:
        font_big = font_med = font_small = ImageFont.load_default()

    # Device name
    draw.text((10, 5), DEVICE_NAME, font=font_small, fill=(100, 100, 255))

    # WiFi mode indicator
    mode_colors = {
        "home":      (100, 200, 100),
        "host":      (200, 150, 50),
        "client":    (100, 180, 255),
        "searching": (180, 180, 180),
    }
    mode_labels = {
        "home":      "HOME",
        "host":      "HOST",
        "client":    "CLIENT",
        "searching": "...",
    }
    draw.text((170, 5), mode_labels.get(mode, "?"),
              font=font_small, fill=mode_colors.get(mode, (180, 180, 180)))

    if pressure is not None:
        pa = abs(pressure)
        inwc = pa / 249.0
        passed = pressure <= target
        color = (0, 255, 0) if passed else (255, 60, 60)
        draw.text((170, 25), "PASS" if passed else "FAIL", font=font_med, fill=color)
        draw.text((10, 40),  f"{pressure:.2f}", font=font_big, fill=color)
        draw.text((10, 100), "Pa", font=font_med, fill=(200, 200, 200))
        draw.text((80, 105), f"{inwc:.4f} inWC", font=font_small, fill=(180, 180, 180))
        target_inwc = abs(target) / 249.0
        draw.text((10, 140), f"Target: {target:.1f} Pa", font=font_small, fill=(150, 150, 255))
        draw.text((10, 162), f"        {target_inwc:.4f} inWC", font=font_small, fill=(150, 150, 255))
        draw.text((10, 200), f"Temp: {temperature:.1f} C", font=font_small, fill=(150, 150, 150))

        # Show sensor count if host
        if mode == "host":
            with lock:
                count = len(sensor_data)
            draw.text((10, 225), f"Sensors: {count}", font=font_small, fill=(0, 200, 100))
        elif mode == "home":
            draw.text((10, 225), "Idle - home network", font=font_small, fill=(100, 200, 100))
    else:
        draw.text((10, 100), "NO SENSOR", font=font_big, fill=(255, 0, 0))

    return image_to_pixels(img)

def screen_thread(board, splash):
    if splash:
        board.draw_image(0, 0, 240, 280, splash)
    while True:
        with lock:
            is_active = active
            p  = current_pressure
            t  = current_temp
            tgt = target_pressure
            mode = wifi_mode
        if is_active:
            screen_data = make_screen(p, t, tgt, mode)
            board.draw_image(0, 0, 240, 280, screen_data)
        time.sleep(1)


# =============================================================
# BLE
# =============================================================

async def run_ble():
    server = BlessServer(name=DEVICE_NAME)
    server.read_request_func  = handle_read
    server.write_request_func = handle_write
    await server.add_new_service(SERVICE_UUID)
    await server.add_new_characteristic(
        SERVICE_UUID, PRESSURE_CHAR_UUID,
        GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
        None, GATTAttributePermissions.readable
    )
    await server.add_new_characteristic(
        SERVICE_UUID, TARGET_CHAR_UUID,
        GATTCharacteristicProperties.read | GATTCharacteristicProperties.write,
        None, GATTAttributePermissions.readable | GATTAttributePermissions.writeable
    )
    await server.start()
    while True:
        with lock:
            p = current_pressure
        val = struct.pack('f', p)
        server.get_characteristic(PRESSURE_CHAR_UUID).value = val
        server.update_value(SERVICE_UUID, PRESSURE_CHAR_UUID)
        await asyncio.sleep(1)

def handle_read(characteristic, **kwargs):
    return characteristic.value

def handle_write(characteristic, value, **kwargs):
    global target_pressure
    if characteristic.uuid == TARGET_CHAR_UUID:
        with lock:
            target_pressure = struct.unpack('f', bytes(value))[0]
        print(f"New target: {target_pressure:.2f} Pa")

def start_ble():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_ble())


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

# WiFi setup — determines role
mode = setup_wifi()
print(f"WiFi mode: {mode}")

if mode == "host":
    # Start web dashboard
    threading.Thread(target=run_web_server, daemon=True).start()

elif mode == "client":
    # Start sending data to host
    host_ip = get_host_ip()
    print(f"Reporting to host at {host_ip}")
    threading.Thread(target=report_data_loop, args=(host_ip,), daemon=True).start()

elif mode == "home":
    print("Home network — idle, ready for updates")

# BLE always runs
threading.Thread(target=start_ble, daemon=True).start()

# Screen always runs
threading.Thread(target=screen_thread, args=(board, splash), daemon=True).start()

# Sensor loop — always reads, host also logs its own data to dashboard
with SMBus(1) as bus:
    init_sensor(bus)
    while True:
        pressure, temperature = read_pressure(bus)
        if pressure is not None:
            with lock:
                current_pressure = pressure
                current_temp     = temperature
            # Host logs its own data into the dashboard
            if mode == "host":
                with lock:
                    sensor_data[DEVICE_NAME] = {
                        'pressure': pressure,
                        'temp':     temperature,
                        'time':     time.time()
                    }
            print(f"Pressure: {pressure:.2f} Pa  Temp: {temperature:.1f} C  Mode: {mode}")
        time.sleep(1)
