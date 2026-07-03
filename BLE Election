#!/usr/bin/env python3
"""
BLE Election — figure out which PFE device should host PFE-NET,
using Bluetooth (BLE) since there's no WiFi network yet to talk on.

How it works:
  1. Every device broadcasts its own name over BLE ("I'm PFE-44!")
  2. Every device also listens for other PFE devices nearby
  3. After ELECTION_SECONDS, whoever saw the LOWEST NUMBER agrees
     that device is the host
  4. The host creates PFE-NET. Everyone else connects to it.

Requires (run once per device):
    pip3 install bleak bluezero
"""

import asyncio
import time
from bleak import BleakScanner
from bluezero import broadcaster

ELECTION_SECONDS = 15   # how long to listen before deciding


def _device_number(name):
    """Turn 'PFE-44' into 44 so we can compare numbers."""
    try:
        return int(name.split('-')[1])
    except Exception:
        return 999999  # anything unrecognized loses the election


async def _scan_for_names(duration, seen):
    async with BleakScanner() as scanner:
        end = time.time() + duration
        while time.time() < end:
            await asyncio.sleep(1)
            for d in scanner.discovered_devices:
                if d.name and d.name.startswith("PFE-"):
                    seen.add(d.name)


def elect_host(device_name, duration=ELECTION_SECONDS):
    """
    Broadcasts our name over BLE, listens for other PFE devices,
    and decides who should host PFE-NET.

    Returns (am_i_host, host_name)
    """
    seen = {device_name}

    print(f"BLE election starting — listening {duration}s...")
    adv = broadcaster.Broadcaster(local_name=device_name)
    adv.start_beacon()

    try:
        asyncio.run(_scan_for_names(duration, seen))
    finally:
        adv.stop_beacon()

    host_name = sorted(seen, key=_device_number)[0]
    am_i_host = (host_name == device_name)

    print(f"BLE election done — saw: {sorted(seen)} — host: {host_name}")
    return am_i_host, host_name
