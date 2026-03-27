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


def zero_sensors(bus, addr1, addr2, duration=0.5):
    """
    Sample both sensors for `duration` seconds, average the results,
    and return (offset1, offset2) to subtract from future readings.
    Call once at startup before the main loop.
    """
    samples1, samples2 = [], []
    end = time.time() + duration
    while time.time() < end:
        p1, _ = read_sdp(bus, addr1)
        p2, _ = read_sdp(bus, addr2)
        if p1 is not None:
            samples1.append(p1)
        if p2 is not None:
            samples2.append(p2)
    offset1 = sum(samples1) / len(samples1) if samples1 else 0.0
    offset2 = sum(samples2) / len(samples2) if samples2 else 0.0
    print(f"Zero offsets — S1: {offset1:.3f} Pa  S2: {offset2:.3f} Pa")
    return offset1, offset2


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
