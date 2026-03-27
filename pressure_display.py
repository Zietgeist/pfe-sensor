#!/usr/bin/env python3
"""
PFE Pressure Sensor — Main
Handles: WiFi, web dashboard, screen, data reporting, sensor loop.

Same code runs on every device.
- At home:  connects to PFE-home (updates/testing)
- In field: one device becomes host (hotspot + dashboard)
            others become clients (join hotspot, send data)
"""

import sys
import os
import time
import random
import threading
import subprocess
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from smbus2 import SMBus
from PIL import Image, ImageDraw, ImageFont

sys.path.append('/home/pi/Whisplay/Driver')
from WhisPlay import WhisPlayBoard

import config
import data_store as ds
from sensor import init_sensor, read_sdp, zero_sensors

# Shorthand
lock = ds.lock


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
        print(f"Creating hotspot: {config.SITE_SSID}")
        subprocess.run([
            'sudo', 'nmcli', 'dev', 'wifi', 'hotspot',
            'ifname', 'wlan0',
            'ssid', config.SITE_SSID,
            'password', config.SITE_PASSWORD
        ], check=True, timeout=30)
        time.sleep(3)
        print(f"Hotspot up. IP: {config.HOST_IP}")
        return True
    except Exception as e:
        print(f"Failed to create hotspot: {e}")
        return False


def get_host_ip():
    """Get this device's IP on wlan0."""
    try:
        result = subprocess.run(
            ['ip', '-4', 'addr', 'show', 'wlan0'],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if 'inet ' in line:
                return line.strip().split()[1].split('/')[0]
    except Exception:
        pass
    return config.HOST_IP


def setup_wifi():
    with lock:
        ds.wifi_mode = "searching"

    print(f"Checking for {config.HOME_SSID}...")
    if scan_for(config.HOME_SSID):
        print(f"{config.HOME_SSID} found — connecting")
        if connect_to(config.HOME_SSID, config.HOME_PASSWORD):
            with lock:
                ds.wifi_mode = "home"
            return "home"

    delay = random.uniform(1, 5)
    print(f"No home network. Waiting {delay:.1f}s before checking for {config.SITE_SSID}...")
    time.sleep(delay)

    print(f"Checking for {config.SITE_SSID}...")
    if scan_for(config.SITE_SSID):
        print(f"{config.SITE_SSID} found — joining as client")
        if connect_to(config.SITE_SSID, config.SITE_PASSWORD):
            with lock:
                ds.wifi_mode = "client"
            return "client"

    print("No networks found — becoming host")
    if create_hotspot():
        with lock:
            ds.wifi_mode = "host"
        return "host"

    return "searching"


# =============================================================
# Web Server (host only)
# =============================================================

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silence access log spam

    def do_GET(self):
        if self.path == '/data':
            with lock:
                payload = {
                    "sensors":          dict(ds.sensor_data),
                    "target":           ds.target_pressure,
                    "job":              dict(ds.job_info),
                    "labels":           dict(ds.sensor_labels),
                    "snapshot_baseline": ds.snapshot_baseline,
                    "snapshot_with_fan": ds.snapshot_with_fan,
                }
            self._send_json(payload)

        elif self.path in ('/', '/index.html'):
            self._send_html(build_dashboard_html())

        elif self.path == '/download/log.csv':
            csv_data = ds.get_log_csv()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="pfe_log.csv"')
            self.end_headers()
            self.wfile.write(csv_data.encode())

        elif self.path == '/download/snapshots.csv':
            csv_data = ds.get_snapshot_csv()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="pfe_snapshots.csv"')
            self.end_headers()
            self.wfile.write(csv_data.encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        if self.path == '/report':
            # Receiving live sensor data from a client device
            name = payload.get('name')
            if name:
                with lock:
                    ds.sensor_data[name] = {
                        's1':    payload.get('s1'),
                        's2':    payload.get('s2'),
                        'temp1': payload.get('temp1'),
                        'temp2': payload.get('temp2'),
                        'label': payload.get('label', ''),
                        'time':  time.time(),
                    }
                    # Keep label registry in sync
                    lbl = payload.get('label', '')
                    if lbl:
                        ds.sensor_labels[name] = lbl
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        elif self.path == '/set_job':
            # Update job info from dashboard form
            with lock:
                ds.job_info['client_name'] = payload.get('client_name', '')
                ds.job_info['address']     = payload.get('address', '')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        elif self.path == '/set_label':
            # Update a device's sensor label
            device = payload.get('device')
            label  = payload.get('label', '')
            if device:
                with lock:
                    ds.sensor_labels[device] = label
                    if device in ds.sensor_data:
                        ds.sensor_data[device]['label'] = label
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        elif self.path == '/snapshot':
            # Take a before or after snapshot
            which = payload.get('which', 'baseline')
            ds.take_snapshot(which)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(body)


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
.subtitle {{ text-align: center; color: #7a8aaa; font-size: 0.85em; margin-bottom: 16px; letter-spacing: 1px; }}

/* Job info bar */
.job-bar {{ background: #131c2e; border: 1px solid #2a3a5c; border-radius: 8px; padding: 12px 16px;
            max-width: 1100px; margin: 0 auto 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
.job-bar input {{ background: #0a0f1a; border: 1px solid #2a3a5c; color: #e8edf5;
                  font-family: 'Courier New', monospace; font-size: 0.85em; padding: 6px 10px;
                  border-radius: 4px; flex: 1; min-width: 160px; }}
.job-bar input:focus {{ outline: none; border-color: #4a7aff; }}
.btn {{ background: #1a2a4a; border: 1px solid #3a5aaa; color: #a8c8ff; font-family: 'Courier New', monospace;
        font-size: 0.8em; padding: 6px 14px; border-radius: 4px; cursor: pointer; letter-spacing: 1px; white-space: nowrap; }}
.btn:hover {{ background: #2a3a6a; }}
.btn-green {{ border-color: #00c060; color: #00e874; }}
.btn-green:hover {{ background: #0d2a1a; }}
.btn-red {{ border-color: #c03030; color: #ff6060; }}
.btn-red:hover {{ background: #2a0d0d; }}

/* Snapshot bar */
.snap-bar {{ max-width: 1100px; margin: 0 auto 16px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
.snap-label {{ color: #5a7aaa; font-size: 0.8em; letter-spacing: 1px; }}

/* Download bar */
.dl-bar {{ max-width: 1100px; margin: 0 auto 20px; display: flex; gap: 10px; flex-wrap: wrap; }}

/* Snapshot comparison table */
.snap-table-wrap {{ max-width: 1100px; margin: 0 auto 24px; display: none; }}
.snap-table-wrap.visible {{ display: block; }}
.snap-table-title {{ color: #7a8aaa; font-size: 0.8em; letter-spacing: 1px; margin-bottom: 8px; }}
.snap-table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
.snap-table th {{ background: #0d1525; color: #5a7aaa; text-align: center; padding: 8px 10px;
                  border: 1px solid #1e2e4a; letter-spacing: 1px; font-size: 0.75em; text-transform: uppercase; }}
.snap-table th.col-device {{ text-align: left; }}
.snap-table td {{ background: #0a0f1a; border: 1px solid #1a2535; padding: 8px 10px; text-align: center; color: #c8d8f8; }}
.snap-table td.col-device {{ text-align: left; color: #7a9adf; font-weight: 700; }}
.snap-table td.col-label  {{ text-align: left; color: #5a7aaa; font-size: 0.85em; }}
.snap-table td.delta-pass {{ color: #00e874; font-weight: 700; }}
.snap-table td.delta-fail {{ color: #ff6060; font-weight: 700; }}
.snap-table td.na         {{ color: #2a3a5a; }}
.snap-table tr:hover td   {{ background: #0d1830; }}

/* Device cards */
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
         gap: 16px; max-width: 1100px; margin: 0 auto; }}
.device-card {{ background: #131c2e; border-radius: 10px; padding: 18px; border: 1.5px solid #2a3a5c; }}
.device-card.pass {{ border-color: #00c060; background: #0d1f17; }}
.device-card.fail {{ border-color: #e03030; background: #1f0d0d; }}
.device-card.stale {{ border-color: #3a4460; opacity: 0.55; }}
.device-name {{ font-size: 1em; font-weight: 700; color: #c8d8f8; margin-bottom: 6px;
                display: flex; justify-content: space-between; align-items: center; letter-spacing: 1px; }}
.status-badge {{ font-size: 0.75em; padding: 3px 10px; border-radius: 4px; font-weight: 700; letter-spacing: 1px; }}
.badge-pass {{ background: #003d20; color: #00e874; border: 1px solid #00c060; }}
.badge-fail {{ background: #3d0000; color: #ff6060; border: 1px solid #e03030; }}
.badge-stale {{ background: #1e2535; color: #7a8aaa; border: 1px solid #3a4460; }}

/* Label selector */
.label-row {{ margin-bottom: 12px; }}
.label-select {{ background: #0a0f1a; border: 1px solid #2a3a5c; color: #a8c8ff;
                 font-family: 'Courier New', monospace; font-size: 0.8em; padding: 4px 8px;
                 border-radius: 4px; width: 100%; cursor: pointer; }}
.label-custom {{ background: #0a0f1a; border: 1px solid #2a3a5c; color: #a8c8ff;
                 font-family: 'Courier New', monospace; font-size: 0.8em; padding: 4px 8px;
                 border-radius: 4px; width: 100%; margin-top: 4px; display: none; }}
.label-custom:focus {{ outline: none; border-color: #4a7aff; }}

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
  <p class="subtitle" id="target-line">Loading...</p>

  <!-- Job Info -->
  <div class="job-bar">
    <input id="client-name" type="text" placeholder="Client Name" />
    <input id="address"     type="text" placeholder="Job Address" />
    <button class="btn" onclick="saveJob()">SAVE JOB INFO</button>
  </div>

  <!-- Snapshot controls -->
  <div class="snap-bar">
    <span class="snap-label">SNAPSHOTS:</span>
    <button class="btn btn-green" onclick="takeSnapshot('baseline')">&#9654; CAPTURE BASELINE</button>
    <button class="btn btn-red"   onclick="takeSnapshot('with_fan')">&#9654; CAPTURE WITH FAN</button>
    <a class="btn" href="/download/snapshots.csv">&#8595; DOWNLOAD SNAPSHOTS</a>
  </div>

  <!-- Download log -->
  <div class="dl-bar">
    <span class="snap-label">DATA LOG:</span>
    <a class="btn" href="/download/log.csv">&#8595; DOWNLOAD FULL LOG (CSV)</a>
  </div>

  <!-- Snapshot comparison table — hidden until both snapshots exist -->
  <div class="snap-table-wrap" id="snap-table-wrap">
    <div class="snap-table-title">SNAPSHOT COMPARISON</div>
    <table class="snap-table" id="snap-table">
      <thead>
        <tr>
          <th class="col-device">DEVICE</th>
          <th class="col-label">LOCATION</th>
          <th>BASELINE S1</th>
          <th>SUCTION S1</th>
          <th>DELTA S1</th>
          <th>BASELINE S2</th>
          <th>SUCTION S2</th>
          <th>DELTA S2</th>
        </tr>
      </thead>
      <tbody id="snap-tbody"></tbody>
    </table>
  </div>

  <div class="grid" id="grid"><div class="no-sensors">Waiting for sensors...</div></div>
  <div id="footer"></div>

  <script>
    const ROOM_OPTIONS = [
      "Utility Room", "Crawlspace", "Sump Pit", "Under Stairs",
      "Storage Room", "Mechanical Room", "Garage", "Other (type below)"
    ];
    const TARGET_KEY = 'pfe_target';
    let TARGET = {config.TARGET_PRESSURE};
    let knownDevices = {{}};

    function fmtPa(v)   {{ return v !== null && v !== undefined ? v.toFixed(2) : '--'; }}
    function fmtInWC(v) {{ return v !== null && v !== undefined ? (Math.abs(v)/249.0).toFixed(4) : '--'; }}
    function valClass(v, stale) {{
      if (stale || v === null || v === undefined) return 'na-val';
      return v <= TARGET ? 'pass-val' : 'fail-val';
    }}

    async function saveJob() {{
      const name = document.getElementById('client-name').value;
      const addr = document.getElementById('address').value;
      await fetch('/set_job', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{client_name: name, address: addr}})
      }});
    }}

    async function takeSnapshot(which) {{
      await fetch('/snapshot', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{which}})
      }});
    }}

    async function setLabel(device, select) {{
      let label = select.value;
      const customInput = document.getElementById('custom-' + device);
      if (label === '__custom__') {{
        customInput.style.display = 'block';
        return;
      }} else {{
        customInput.style.display = 'none';
      }}
      await sendLabel(device, label);
    }}

    async function setCustomLabel(device, input) {{
      await sendLabel(device, input.value);
    }}

    async function sendLabel(device, label) {{
      await fetch('/set_label', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{device, label}})
      }});
    }}

    function buildLabelSelect(device, currentLabel) {{
      const options = ROOM_OPTIONS.map(r =>
        `<option value="${{r === 'Other (type below)' ? '__custom__' : r}}"
          ${{currentLabel === r ? 'selected' : ''}}>${{r}}</option>`
      ).join('');
      // If label isn't in standard list, show it selected
      const isCustom = currentLabel && !ROOM_OPTIONS.slice(0,-1).includes(currentLabel);
      return `
        <select class="label-select" onchange="setLabel('${{device}}', this)">
          <option value="" ${{!currentLabel ? 'selected' : ''}}>-- Select Room --</option>
          ${{options}}
        </select>
        <input class="label-custom" id="custom-${{device}}" type="text"
               placeholder="Type room name..."
               value="${{isCustom ? currentLabel : ''}}"
               style="display:${{isCustom ? 'block' : 'none'}}"
               onchange="setCustomLabel('${{device}}', this)"
               onblur="setCustomLabel('${{device}}', this)" />
      `;
    }}

    async function refresh() {{
      try {{
        const res  = await fetch('/data');
        const data = await res.json();
        TARGET = data.target;

        document.getElementById('target-line').textContent =
          `TARGET: ${{TARGET.toFixed(1)}} Pa | ${{(Math.abs(TARGET)/249.0).toFixed(4)}} inWC`;

        // Populate job fields if empty
        if (data.job) {{
          const cn = document.getElementById('client-name');
          const ad = document.getElementById('address');
          if (!cn.value) cn.value = data.job.client_name || '';
          if (!ad.value) ad.value = data.job.address     || '';
        }}

        const sensors = data.sensors || {{}};
        const labels  = data.labels  || {{}};
        const grid    = document.getElementById('grid');
        const now     = Date.now() / 1000;
        const names   = Object.keys(sensors).sort();

        if (!names.length) {{
          grid.innerHTML = '<div class="no-sensors">Waiting for sensors...</div>';
          return;
        }}

        grid.innerHTML = names.map(name => {{
          const s      = sensors[name];
          const stale  = (now - s.time) > 30;
          const label  = labels[name] || s.label || '';
          const bothPass = !stale && s.s1 !== null && s.s1 <= TARGET && (s.s2 === null || s.s2 <= TARGET);
          const anyFail  = !stale && ((s.s1 !== null && s.s1 > TARGET) || (s.s2 !== null && s.s2 > TARGET));
          const cls      = stale ? 'stale' : (bothPass ? 'pass' : (anyFail ? 'fail' : ''));
          const badgeCls = stale ? 'badge-stale' : (bothPass ? 'badge-pass' : (anyFail ? 'badge-fail' : 'badge-stale'));
          const badgeTxt = stale ? 'OFFLINE'     : (bothPass ? 'PASS'        : (anyFail ? 'FAIL'        : '?'));
          const c1 = valClass(s.s1, stale);
          const c2 = valClass(s.s2, stale);
          const s2html = (s.s2 !== null && s.s2 !== undefined)
            ? `<div class="sensor-value ${{c2}}">${{fmtPa(s.s2)}}</div><div class="sensor-inwc">${{fmtInWC(s.s2)}} inWC</div>`
            : `<div class="sensor-value na-val">--</div><div class="sensor-inwc">&nbsp;</div>`;

          return `<div class="device-card ${{cls}}">
            <div class="device-name">${{name}}<span class="status-badge ${{badgeCls}}">${{badgeTxt}}</span></div>
            <div class="label-row">${{buildLabelSelect(name, label)}}</div>
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

        // ── Snapshot comparison table ─────────────────────
        const bl   = data.snapshot_baseline;
        const wf   = data.snapshot_with_fan;
        const wrap  = document.getElementById('snap-table-wrap');
        const tbody = document.getElementById('snap-tbody');

        if (bl && wf) {{
          wrap.classList.add('visible');
          const allDevices = [...new Set([...Object.keys(bl), ...Object.keys(wf)])].sort();
          tbody.innerHTML = allDevices.map(dev => {{
            const b = bl[dev] || {{}};
            const w = wf[dev] || {{}};
            function deltaCell(bv, wv) {{
              if (bv == null || wv == null) return `<td class="na">--</td>`;
              const d   = wv - bv;
              const cls = d <= 0 ? 'delta-pass' : 'delta-fail';
              return `<td class="${{cls}}">${{d.toFixed(2)}} Pa</td>`;
            }}
            function valCell(v) {{
              return v != null ? `<td>${{v.toFixed(2)}} Pa</td>` : `<td class="na">--</td>`;
            }}
            const lbl = b.label || w.label || labels[dev] || '';
            return `<tr>
              <td class="col-device">${{dev}}</td>
              <td class="col-label">${{lbl}}</td>
              ${{valCell(b.s1)}}${{valCell(w.s1)}}${{deltaCell(b.s1, w.s1)}}
              ${{valCell(b.s2)}}${{valCell(w.s2)}}${{deltaCell(b.s2, w.s2)}}
            </tr>`;
          }}).join('');
        }} else {{
          wrap.classList.remove('visible');
        }}

        document.getElementById('footer').textContent = 'Updated: ' + new Date().toLocaleTimeString();
      }} catch(e) {{
        document.getElementById('footer').textContent = 'Connection lost...';
      }}
    }}

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>"""


def run_web_server():
    server = HTTPServer(('0.0.0.0', config.WEB_PORT), DashboardHandler)
    print(f"Dashboard running at http://{config.HOST_IP}")
    server.serve_forever()


# =============================================================
# Data Reporter (client only)
# =============================================================

def report_data_loop(host_ip):
    """Runs on client: sends live readings to the host every second."""
    url = f"http://{host_ip}/report"
    while True:
        try:
            with lock:
                payload = {
                    'name':  config.DEVICE_NAME,
                    's1':    ds.current_pressure1,
                    's2':    ds.current_pressure2,
                    'temp1': ds.current_temp1,
                    'temp2': ds.current_temp2,
                    'label': ds.sensor_labels.get(config.DEVICE_NAME, ''),
                }
            body = json.dumps(payload).encode()
            req  = Request(url, data=body, headers={'Content-Type': 'application/json'})
            urlopen(req, timeout=3)
        except Exception as e:
            print(f"Report error: {e}")
        time.sleep(1)


# =============================================================
# Data Logger (host only — logs all devices once per minute)
# =============================================================

def data_log_loop():
    """Runs on host: appends one row per device to the in-memory log every 60 seconds."""
    while True:
        time.sleep(60)
        with lock:
            devices = dict(ds.sensor_data)
        for device, d in devices.items():
            ds.append_log_row(device, d)
        print(f"Log: {len(ds.data_log)} rows")


# =============================================================
# Screen
# =============================================================

def get_battery_pct(bus):
    """Read battery % from PiSugar 3 using the already-open I2C bus."""
    try:
        val = bus.read_byte_data(0x57, 0x2a)
        return max(0, min(100, val))
    except Exception:
        return None


def load_splash():
    try:
        img = Image.open(config.SPLASH_PATH).convert('RGB')
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
    img  = Image.new('RGB', (240, 280), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    if os.path.exists(config.FONT_BOLD):
        f_big   = ImageFont.truetype(config.FONT_BOLD, 44)
        f_med   = ImageFont.truetype(config.FONT_BOLD, 20)
        f_small = ImageFont.truetype(config.FONT_REG,  15)
        f_tiny  = ImageFont.truetype(config.FONT_REG,  13)
    else:
        f_big = f_med = f_small = f_tiny = ImageFont.load_default()

    mode_colors  = {"home": (100, 200, 100), "host": (200, 150, 50),
                    "client": (100, 180, 255), "searching": (160, 160, 160)}
    mode_labels  = {"home": "HOME", "host": "HOST", "client": "CLIENT", "searching": "..."}

    draw.text((6,  4), config.DEVICE_NAME,           font=f_small, fill=(120, 120, 255))
    draw.text((160, 4), mode_labels.get(mode, "?"),  font=f_small, fill=mode_colors.get(mode, (160,160,160)))

    # Battery top right
    with lock:
        batt = ds.battery_pct
    if batt is not None:
        batt_color = (0, 200, 0) if batt > 30 else (255, 60, 60)
        draw.text((200, 4), f"{batt}%", font=f_tiny, fill=batt_color)

    draw.line([(0, 24), (240, 24)], fill=(50, 50, 50), width=1)

    def draw_sensor(label, pressure, y_top):
        draw.text((6, y_top), label, font=f_tiny, fill=(150, 150, 150))
        if pressure is None:
            draw.text((6, y_top + 16), "--", font=f_big, fill=(80, 80, 80))
        else:
            passed = pressure <= target
            color  = (0, 230, 0) if passed else (255, 60, 60)
            draw.text((185, y_top + 16), "PASS" if passed else "FAIL", font=f_med, fill=color)
            draw.text((6,   y_top + 16), f"{pressure:.2f}", font=f_big,   fill=color)
            draw.text((6,   y_top + 65), "Pa",              font=f_small, fill=(180, 180, 180))
        draw.line([(0, y_top + 85), (240, y_top + 85)], fill=(40, 40, 40), width=1)

    draw_sensor("SENSOR 1", p1, 28)
    draw_sensor("SENSOR 2", p2, 118)

    draw.text((6, 215), f"Target: {target:.1f} Pa", font=f_tiny, fill=(100, 100, 200))
    if mode == "host":
        with lock:
            count = len(ds.sensor_data)
        draw.text((6, 234), f"Devices online: {count}", font=f_tiny, fill=(0, 180, 80))
    elif mode == "client":
        draw.text((6, 234), "Reporting to host", font=f_tiny, fill=(100, 180, 255))
    elif mode == "home":
        draw.text((6, 234), "Home network — idle", font=f_tiny, fill=(100, 200, 100))

    draw.text((6, 254), f"http://{get_host_ip()}", font=f_tiny, fill=(60, 60, 60))
    return image_to_pixels(img)


def screen_thread(board, splash):
    if splash:
        board.draw_image(0, 0, 240, 280, splash)
    last = (None, None, None, None)
    while True:
        with lock:
            p1   = ds.current_pressure1
            p2   = ds.current_pressure2
            tgt  = ds.target_pressure
            mode = ds.wifi_mode
        current = (round(p1, 1) if p1 else None,
                   round(p2, 1) if p2 else None,
                   tgt, mode)
        if current != last:
            screen_data = make_screen(p1, p2, tgt, mode)
            board.draw_image(0, 0, 240, 280, screen_data)
            last = current
        time.sleep(1)


# =============================================================
# Main
# =============================================================

if __name__ == '__main__':
    board  = WhisPlayBoard()
    splash = load_splash()
    if splash:
        board.draw_image(0, 0, 240, 280, splash)

    mode = setup_wifi()
    print(f"WiFi mode: {mode}")

    if mode == "host":
        threading.Thread(target=run_web_server, daemon=True).start()
        threading.Thread(target=data_log_loop,  daemon=True).start()
    elif mode == "client":
        host_ip = get_host_ip()
        print(f"Reporting to host at {host_ip}")
        threading.Thread(target=report_data_loop, args=(host_ip,), daemon=True).start()
    elif mode == "home":
        print("Home network — idle, ready for updates")

    threading.Thread(target=screen_thread, args=(board, splash), daemon=True).start()

    # ── Sensor loop (always runs) ─────────────────────────────
    with SMBus(1) as bus:
        init_sensor(bus)
        print("Zeroing sensors...")
        offset1, offset2 = zero_sensors(bus, config.SDP_ADDR_1, config.SDP_ADDR_2)
        batt_timer = 0
        while True:
            p1, t1 = read_sdp(bus, config.SDP_ADDR_1)
            p2, t2 = read_sdp(bus, config.SDP_ADDR_2)

            # Apply zero offsets
            if p1 is not None:
                p1 -= offset1
            if p2 is not None:
                p2 -= offset2

            # Read battery every 30 seconds
            batt_timer += 1
            if batt_timer >= 30:
                batt_timer = 0
                with lock:
                    ds.battery_pct = get_battery_pct(bus)

            with lock:
                ds.current_pressure1 = p1
                ds.current_temp1     = t1
                ds.current_pressure2 = p2
                ds.current_temp2     = t2

            # Host feeds its own readings into the shared sensor_data dict
            if mode == "host":
                with lock:
                    ds.sensor_data[config.DEVICE_NAME] = {
                        's1':    p1,
                        's2':    p2,
                        'temp1': t1,
                        'temp2': t2,
                        'label': ds.sensor_labels.get(config.DEVICE_NAME, ''),
                        'time':  time.time(),
                    }

            s1_str = f"{p1:.2f} Pa" if p1 is not None else "--"
            s2_str = f"{p2:.2f} Pa" if p2 is not None else "--"
            print(f"S1: {s1_str}  S2: {s2_str}  Mode: {mode}")
            time.sleep(1)
