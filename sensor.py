#!/usr/bin/env python3
"""
PFE Sensor
Handles all SDP800 differential pressure sensor communication.
"""
import time
from smbus2 import SMBus, i2c_msg


def init_sensor(bus):
    """Send a general-call reset to wake/init all sensors on the bus."""
    try:
        bus.i2c_rdwr(i2c_msg.write(0x00, [0x06]))
        time.sleep(0.05)
    except Exception:
        pass


def read_sdp(bus, address):
    """
    Read one SDP800 sensor at the given I2C address.
    Returns (pressure_pa, temp_c) or (None, None) if sensor is missing/error.
    """
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
