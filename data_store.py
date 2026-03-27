#!/usr/bin/env python3
"""
PFE Data Store
Holds all shared state between threads:
  - Live sensor readings
  - Sensor data received from client devices (host only)
  - Job metadata (client name, address, sensor labels)
  - Before/after snapshots
  - CSV data log
"""
import threading
import time
import csv
import io
import os

from config import TARGET_PRESSURE, REPO_DIR

# ── Thread lock — all shared state must be accessed inside 'with lock' ──
lock = threading.Lock()

# ── WiFi mode ───────────────────────────────────────────────────────────
wifi_mode = "searching"   # "home" | "host" | "client" | "searching"

# ── Live readings from this device's sensors ────────────────────────────
current_pressure1 = None
current_temp1     = None
current_pressure2 = None
current_temp2     = None

# ── Target pressure (can be changed at runtime via dashboard) ────────────
target_pressure = TARGET_PRESSURE

# ── Sensor data from all devices (host only) ────────────────────────────
# { "PFE-1": {"s1": -12.3, "s2": -11.9, "temp1": 20.1, "temp2": 20.3,
#             "label": "Utility Room", "time": 123456789} }
sensor_data = {}

# ── Battery percentage from PiSugar 3 ────────────────────────────────────
battery_pct = None

# ── Job metadata ─────────────────────────────────────────────────────────
job_info = {
    "client_name": "",
    "address":     "",
}

# ── Sensor labels — one label per device ─────────────────────────────────
# { "PFE-1": "Utility Room", "PFE-2": "Crawlspace" }
sensor_labels = {}

# ── Snapshots ────────────────────────────────────────────────────────────
# Each snapshot is a dict: { "PFE-1": {"s1": ..., "s2": ..., "label": ...}, ... }
snapshot_baseline   = None   # Taken before fan on
snapshot_with_fan   = None   # Taken after fan on

# ── CSV data log ─────────────────────────────────────────────────────────
# List of rows: [timestamp, device, label, s1_pa, s2_pa, temp1, temp2]
data_log = []
LOG_PATH = os.path.join(REPO_DIR, "pfe_log.csv")


def take_snapshot(label):
    """
    Capture current readings from all devices as a named snapshot.
    label should be "baseline" or "with_fan"
    Returns the snapshot dict.
    """
    global snapshot_baseline, snapshot_with_fan
    with lock:
        snap = {}
        for device, d in sensor_data.items():
            snap[device] = {
                "s1":    d.get("s1"),
                "s2":    d.get("s2"),
                "temp1": d.get("temp1"),
                "temp2": d.get("temp2"),
                "label": sensor_labels.get(device, ""),
                "time":  d.get("time"),
            }
        if label == "baseline":
            snapshot_baseline = snap
        else:
            snapshot_with_fan = snap
    return snap


def append_log_row(device, d):
    """
    Add one row to the in-memory data log.
    Called once per minute (or whatever interval) by the logging thread.
    """
    with lock:
        row = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "device":    device,
            "label":     sensor_labels.get(device, ""),
            "s1_pa":     round(d["s1"], 3) if d.get("s1") is not None else "",
            "s2_pa":     round(d["s2"], 3) if d.get("s2") is not None else "",
            "temp1_c":   round(d["temp1"], 2) if d.get("temp1") is not None else "",
            "temp2_c":   round(d["temp2"], 2) if d.get("temp2") is not None else "",
        }
        data_log.append(row)


def get_log_csv():
    """Return the full data log as a CSV string for download."""
    with lock:
        rows = list(data_log)
        client = job_info.get("client_name", "")
        address = job_info.get("address", "")

    output = io.StringIO()
    # Header comment rows with job info
    output.write(f"# PFE Data Log\n")
    output.write(f"# Client: {client}\n")
    output.write(f"# Address: {address}\n")
    output.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    return output.getvalue()


def get_snapshot_csv():
    """Return before/after snapshots as a CSV string for download."""
    with lock:
        baseline = dict(snapshot_baseline) if snapshot_baseline else {}
        with_fan = dict(snapshot_with_fan) if snapshot_with_fan else {}
        client   = job_info.get("client_name", "")
        address  = job_info.get("address", "")

    output = io.StringIO()
    output.write(f"# PFE Snapshot Report\n")
    output.write(f"# Client: {client}\n")
    output.write(f"# Address: {address}\n")
    output.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    writer = csv.writer(output)
    writer.writerow(["Device", "Label",
                     "Baseline S1 (Pa)", "Baseline S2 (Pa)",
                     "With Fan S1 (Pa)", "With Fan S2 (Pa)",
                     "Change S1 (Pa)",   "Change S2 (Pa)"])

    all_devices = sorted(set(list(baseline.keys()) + list(with_fan.keys())))
    for dev in all_devices:
        b = baseline.get(dev, {})
        w = with_fan.get(dev, {})
        bs1 = b.get("s1")
        bs2 = b.get("s2")
        ws1 = w.get("s1")
        ws2 = w.get("s2")
        ds1 = round(ws1 - bs1, 3) if (ws1 is not None and bs1 is not None) else ""
        ds2 = round(ws2 - bs2, 3) if (ws2 is not None and bs2 is not None) else ""
        writer.writerow([
            dev,
            b.get("label", ""),
            round(bs1, 3) if bs1 is not None else "",
            round(bs2, 3) if bs2 is not None else "",
            round(ws1, 3) if ws1 is not None else "",
            round(ws2, 3) if ws2 is not None else "",
            ds1, ds2,
        ])

    return output.getvalue()
