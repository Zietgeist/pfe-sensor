#!/usr/bin/env python3
import sys
import os
import time
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
DEVICE_NAME         = os.uname().nodename
SDP_ADDRESS         = 0x25
SERVICE_UUID        = "12345678-1234-5678-1234-56789abcdef0"
PRESSURE_CHAR_UUID  = "12345678-1234-5678-1234-56789abcdef1"
TARGET_CHAR_UUID    = "12345678-1234-5678-1234-56789abcdef2"
WIFI_SSID           = "PFE-NET"
WIFI_PASSWORD       = "pferadon1"
HOST_IP             = "192.168.4.1"
WEB_PORT            = 80
DATA_PORT           = 5000

# --- Shared state ---
lock = threading.Lock()
active = False
current_pressure = 0.0
current_temp = 0.0
target_pressure = -1.0
is_host = False

# Stores data from all sensors (host only)
# { "PFE-1": {"pressure": -12.3, "time": 1234567890}, ... }
sensor_data = {}

# --- WiFi Functions ---
def wifi_network_exists(ssid):
    """Scan for a WiFi network by name."""
    try:
        result = subprocess.run(
            ['sudo', 'nmcli', '-t', '-f', 'SSID', 'dev', 'wifi', 'list', '--rescan', 'yes'],
            capture_output=True, text=True, timeout=15
        )
        return ssid in result.stdout
    except Exception as e:
        print(f"WiFi scan error: {e}")
        return False

def create_hotspot():
    """Create a WiFi hotspot named PFE-NET."""
    print(f"Creating hotspot: {WIFI_SSID}")
    subprocess.run([
        'sudo', 'nmcli', 'dev', 'wifi', 'hotspot',
        'ifname', 'wlan0',
        'ssid', WIFI_SSID,
        'password', WIFI_PASSWORD
    ], check=True)
    time.sleep(3)
    print(f"Hotspot created. IP: {HOST_IP}")

def connect_to_hotspot():
    """Connect to the PFE-NET hotspot as a client."""
    print(f"Connecting to {WIFI_SSID}...")
    subprocess.run([
        'sudo', 'nmcli', 'dev', 'wifi', 'connect', WIFI_SSID,
        'password', WIFI_PASSWORD
    ], check=True)
    time.sleep(3)
    print("Connected to PFE-NET")

def get_host_ip():
    """Find the host's IP on the PFE-NET network (gateway)."""
    try:
        result = subprocess.run(
            ['ip', 'route', 'show', 'dev', 'wlan0'],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if 'default' in line:
                return line.split()[2]
        return HOST_IP
    except:
        return HOST_IP

# --- Web Server (Host only) ---
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress request logs

    def do_GET(self):
        if self.path == '/data':
            # Return sensor data as JSON
            with lock:
                data = dict(sensor_data)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == '/' or self.path == '/index.html':
            # Serve the dashboard HTML
            html = self.build_dashboard()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/report':
            # Sensors POST their data here
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                name = payload.get('name')
                pressure = payload.get('pressure')
                if name and pressure is not None:
                    with lock:
                        sensor_data[name] = {
                            'pressure': pressure,
                            'time': time.time()
                        }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'OK')
            except Exception as e:
                self.send_response(400)
                self.end_headers()

    def build_dashboard(self):
        return """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PFE Sensor Dashboard</title>
  <style>
    body {
      font-family: Arial, sans-serif;
      background: #111;
      color: #eee;
      margin: 0;
      padding: 20px;
    }
    h1 {
      color: #6af;
      text-align: center;
      margin-bottom: 20px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      max-width: 900px;
      margin: 0 auto;
    }
    .card {
      background: #222;
      border-radius: 12px;
      padding: 20px;
      text-align: center;
      border: 2px solid #444;
    }
    .card.pass { border-color: #0f0; }
    .card.fail { border-color: #f44; }
    .card.stale { border-color: #888; opacity: 0.6; }
    .sensor-name {
      font-size: 1.1em;
      color: #adf;
      margin-bottom: 10px;
    }
    .pressure {
      font-size: 2.5em;
      font-weight: bold;
    }
    .unit {
      font-size: 0.9em;
      color: #aaa;
      margin-top: 4px;
    }
    .status {
      margin-top: 10px;
      font-size: 1.1em;
      font-weight: bold;
    }
    .pass-text { color: #0f0; }
    .fail-text { color: #f44; }
    .stale-text { color: #888; }
    #updated {
      text-align: center;
      color: #666;
      font-size: 0.8em;
      margin-top: 20px;
    }
    .no-sensors {
      text-align: center;
      color: #666;
      margin-top: 60px;
      font-size: 1.2em;
    }
  </style>
</head>
<body>
  <h1>🐦 PFE Radon Sensor Dashboard</h1>
  <div class="grid" id="grid">
    <div class="no-sensors">Waiting for sensors...</div>
  </div>
  <div id="updated"></div>
  <script>
    const TARGET = -12.5; // Pa — adjust as needed

    async function refresh() {
      try {
        const res = await fetch('/data');
        const data = await res.json();
        const grid = document.getElementById('grid');
        const now = Date.now() / 1000;
        const names = Object.keys(data).sort();

        if (names.length === 0) {
          grid.innerHTML = '<div class="no-sensors">Waiting for sensors...</div>';
          return;
        }

        grid.innerHTML = names.map(name => {
          const s = data[name];
          const stale = (now - s.time) > 10;
          const pass = s.pressure <= TARGET;
          const inwc = (Math.abs(s.pressure) / 249.0).toFixed(4);
          const cls = stale ? 'stale' : (pass ? 'pass' : 'fail');
          const statusCls = stale ? 'stale-text' : (pass ? 'pass-text' : 'fail-text');
          const statusTxt = stale ? 'OFFLINE' : (pass ? 'PASS' : 'FAIL');
          return `
            <div class="card ${cls}">
              <div class="sensor-name">${name}</div>
              <div class="pressure" style="color:${stale?'#888':pass?'#0f0':'#f44'}">${s.pressure.toFixed(2)}</div>
              <div class="unit">Pa &nbsp;|&nbsp; ${inwc} inWC</div>
              <div class="status ${statusCls}">${statusTxt}</div>
            </div>`;
        }).join('');

        document.getElementById('updated').textContent =
          'Last updated: ' + new Date().toLocaleTimeString();
      } catch(e) {
        document.getElementById('updated').textContent = 'Connection error...';
      }
    }

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>"""

def run_web_server():
    server = HTTPServer(('0.0.0.0', WEB_PORT), DashboardHandler)
    print(f"Web dashboard running on port {WEB_PORT}")
    server.serve_forever()

# --- Data Reporter (Client only) ---
def report_data_loop(host_ip):
    """Clients send their pressure to the host every 2 seconds."""
    url = f"http://{host_ip}/report"
    while True:
        try:
            with lock:
                p = current_pressure
            payload = json.dumps({'name': DEVICE_NAME, 'pressure': p}).encode()
            req = Request(url, data=payload, headers={'Content-Type': 'application/json'})
            urlopen(req, timeout=3)
        except Exception as e:
            print(f"Report error: {e}")
        time.sleep(2)

# --- Load splash image ---
def load_splash():
    try:
        img = Image.open('/home/ivan/marten_screen.png').convert('RGB')
        pixels = []
        for r, g, b in img.getdata():
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            pixels.extend([(rgb565 >> 8) & 0xFF, rgb565 & 0xFF])
        return pixels
    except Exception as e:
        print(f"Splash load error: {e}")
        return None

# --- Sensor ---
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
    except Exception as e:
        return None, None

# --- Pressure screen ---
def make_screen(pressure, temperature, target):
    img = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if os.path.exists(font_path):
        font_big   = ImageFont.truetype(font_path, 52)
        font_med   = ImageFont.truetype(font_path, 24)
        font_small = ImageFont.truetype(font_path, 18)
    else:
        font_big = font_med = font_small = ImageFont.load_default()

    draw.text((10, 5), DEVICE_NAME, font=font_small, fill=(100, 100, 255))

    # Show host/client status
    role = "HOST" if is_host else "CLIENT"
    draw.text((170, 5), role, font=font_small, fill=(200, 150, 50))

    if pressure is not None:
        pa = abs(pressure)
        inwc = pa / 249.0
        passed = pressure <= target
        status_color = (0, 255, 0) if passed else (255, 60, 60)
        status_text  = "PASS" if passed else "FAIL"
        draw.text((170, 25), status_text, font=font_med, fill=status_color)
        draw.text((10, 40), f"{pressure:.2f}", font=font_big, fill=status_color)
        draw.text((10, 100), "Pa", font=font_med, fill=(200, 200, 200))
        draw.text((80, 105), f"{inwc:.4f} inWC", font=font_small, fill=(180, 180, 180))
        target_inwc = abs(target) / 249.0
        draw.text((10, 140), f"Target: {target:.1f} Pa", font=font_small, fill=(150, 150, 255))
        draw.text((10, 162), f"        {target_inwc:.4f} inWC", font=font_small, fill=(150, 150, 255))
        draw.text((10, 200), f"Temp: {temperature:.1f} C", font=font_small, fill=(150, 150, 150))
        draw.text((10, 225), "WiFi: Active", font=font_small, fill=(0, 200, 100))
    else:
        draw.text((10, 100), "NO SENSOR", font=font_big, fill=(255, 0, 0))

    pixels = []
    for r, g, b in img.getdata():
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        pixels.extend([(rgb565 >> 8) & 0xFF, rgb565 & 0xFF])
    return pixels

# --- Screen thread ---
def screen_thread(board, splash):
    global active, current_pressure, current_temp, target_pressure
    if splash:
        board.draw_image(0, 0, 240, 280, splash)
    while True:
        with lock:
            is_active = active
            p = current_pressure
            t = current_temp
            tgt = target_pressure
        if is_active:
            screen_data = make_screen(p, t, tgt)
            board.draw_image(0, 0, 240, 280, screen_data)
        time.sleep(1)

# --- BLE ---
async def run_ble():
    global current_pressure, target_pressure
    server = BlessServer(name=DEVICE_NAME)
    server.read_request_func = handle_read
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
        print(f"New target received: {target_pressure:.2f} Pa")

def start_ble():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_ble())

# --- Button ---
def button_pressed():
    global active
    with lock:
        active = not active
    if active:
        print("Waking up...")
        board.set_backlight(80)
    else:
        print("Sleeping...")
        board.set_backlight(0)
        board.fill_screen(0)

# --- Main ---
print("Starting PFE Sensor...")
board = WhisPlayBoard()
board.set_backlight(80)

splash = load_splash()
if splash:
    board.draw_image(0, 0, 240, 280, splash)
    print("Splash screen displayed")

board.on_button_press(button_pressed)

# --- WiFi Setup ---
print("Scanning for PFE-NET...")
if wifi_network_exists(WIFI_SSID):
    print("PFE-NET found — joining as client")
    connect_to_hotspot()
    is_host = False
    host_ip = get_host_ip()
    threading.Thread(target=report_data_loop, args=(host_ip,), daemon=True).start()
else:
    print("PFE-NET not found — becoming host")
    create_hotspot()
    is_host = True
    # Host also reports its own data into sensor_data
    threading.Thread(target=run_web_server, daemon=True).start()

threading.Thread(target=start_ble, daemon=True).start()
threading.Thread(target=screen_thread, args=(board, splash), daemon=True).start()

with SMBus(1) as bus:
    init_sensor(bus)
    while True:
        pressure, temperature = read_pressure(bus)
        if pressure is not None:
            with lock:
                current_pressure = pressure
                current_temp = temperature
            # If host, also log own data into the dashboard
            if is_host:
                with lock:
                    sensor_data[DEVICE_NAME] = {
                        'pressure': pressure,
                        'time': time.time()
                    }
            print(f"Pressure: {pressure:.2f} Pa  Temp: {temperature:.1f} C")
        time.sleep(1)
