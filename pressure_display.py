#!/usr/bin/env python3
import sys
import os
import time
import asyncio
import struct
import threading
from smbus2 import SMBus, i2c_msg
from PIL import Image, ImageDraw, ImageFont
from bless import BlessServer, BlessGATTCharacteristic, GATTCharacteristicProperties, GATTAttributePermissions

sys.path.append('/home/ivan/Whisplay/Driver')
from WhisPlay import WhisPlayBoard

# --- Constants ---
DEVICE_NAME = os.uname().nodename
SDP_ADDRESS         = 0x25
SERVICE_UUID        = "12345678-1234-5678-1234-56789abcdef0"
PRESSURE_CHAR_UUID  = "12345678-1234-5678-1234-56789abcdef1"
TARGET_CHAR_UUID    = "12345678-1234-5678-1234-56789abcdef2"

# --- Shared state ---
lock = threading.Lock()
active = False
current_pressure = 0.0
current_temp = 0.0
target_pressure = -1.0

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
    import socket
#    hostname = socket.gethostname()
#    draw.text((10, 5), hostname, font=font_small, fill=(100, 100, 255))
#    draw.text((10, 5), "PFE Sensor", font=font_small, fill=(100, 100, 255))
    draw.text((10, 5), DEVICE_NAME, font=font_small, fill=(100, 100, 255))

    if pressure is not None:
        pa = abs(pressure)
        inwc = pa / 249.0
        passed = pressure <= target
        status_color = (0, 255, 0) if passed else (255, 60, 60)
        status_text  = "PASS" if passed else "FAIL"
        draw.text((170, 5), status_text, font=font_med, fill=status_color)
        draw.text((10, 40), f"{pressure:.2f}", font=font_big, fill=status_color)
        draw.text((10, 100), "Pa", font=font_med, fill=(200, 200, 200))
        draw.text((80, 105), f"{inwc:.4f} inWC", font=font_small, fill=(180, 180, 180))
        target_inwc = abs(target) / 249.0
        draw.text((10, 140), f"Target: {target:.1f} Pa", font=font_small, fill=(150, 150, 255))
        draw.text((10, 162), f"        {target_inwc:.4f} inWC", font=font_small, fill=(150, 150, 255))
        draw.text((10, 200), f"Temp: {temperature:.1f} C", font=font_small, fill=(150, 150, 150))
        draw.text((10, 225), "BLE: Active", font=font_small, fill=(0, 200, 100))
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
    # Show splash until button pressed
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
        None,
        GATTAttributePermissions.readable
    )
    await server.add_new_characteristic(
        SERVICE_UUID, TARGET_CHAR_UUID,
        GATTCharacteristicProperties.read | GATTCharacteristicProperties.write,
        None,
        GATTAttributePermissions.readable | GATTAttributePermissions.writeable
    )

    await server.start()
    print("BLE advertising as 'PFE Sensor'")

    while True:
        with lock:
            p = current_pressure
        val = struct.pack('f', p)
        server.get_characteristic(PRESSURE_CHAR_UUID).value = val
        server.update_value(SERVICE_UUID, PRESSURE_CHAR_UUID)
        await asyncio.sleep(1)

def handle_read(characteristic: BlessGATTCharacteristic, **kwargs):
    return characteristic.value

def handle_write(characteristic: BlessGATTCharacteristic, value, **kwargs):
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
            print(f"Pressure: {pressure:.2f} Pa  Temp: {temperature:.1f} C  Target: {target_pressure:.2f} Pa")
        time.sleep(1)
