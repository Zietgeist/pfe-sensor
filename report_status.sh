#!/bin/bash
# report_status.sh
# Reports this device's hardware info to a shared registry file on GitHub.
# Called automatically every boot from update_and_run.sh.

REPO_DIR="/home/pi/pfe-sensor"
REGISTRY_FILE="device_registry.json"
DEVICE_NAME=$(hostname)

cd "$REPO_DIR" || exit 0

# --- Gather hardware info ---
RPI_MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown")

HAS_SCREEN="no"
[ -d /home/pi/Whisplay ] && HAS_SCREEN="yes"

HAS_BATTERY="no"
systemctl is-active --quiet pisugar-server && HAS_BATTERY="yes"

I2C_SCAN=$(sudo i2cdetect -y 1 2>/dev/null)
HAS_S1="no"; echo "$I2C_SCAN" | grep -q " 25 " && HAS_S1="yes"
HAS_S2="no"; echo "$I2C_SCAN" | grep -q " 26 " && HAS_S2="yes"
HAS_MUX="no"; echo "$I2C_SCAN" | grep -q " 70 " && HAS_MUX="yes"

BT_STATUS="no"
systemctl is-active --quiet bluetooth && BT_STATUS="yes"

SD_CARD_SIZE=$(df -h / | awk 'NR==2{print $2}')
DISK_USED_PERCENT=$(df -h / | awk 'NR==2{print $5}')
IP_ADDRESS=$(hostname -I | awk '{print $1}')
CODE_VERSION=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
CPU_TEMP=$(vcgencmd measure_temp 2>/dev/null | grep -o '[0-9.]*' || echo "unknown")

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# --- Save it into the shared registry, retry if another device is mid-save ---
for attempt in 1 2 3 4 5; do
    git fetch origin main > /dev/null 2>&1
    git reset --hard origin/main > /dev/null 2>&1

    if [ ! -f "$REGISTRY_FILE" ]; then
        echo "{}" > "$REGISTRY_FILE"
    fi

    python3 - "$REGISTRY_FILE" "$DEVICE_NAME" "$RPI_MODEL" "$HAS_SCREEN" "$HAS_BATTERY" \
        "$HAS_S1" "$HAS_S2" "$HAS_MUX" "$BT_STATUS" "$SD_CARD_SIZE" "$DISK_USED_PERCENT" \
        "$IP_ADDRESS" "$CODE_VERSION" "$CPU_TEMP" "$TIMESTAMP" <<'PYEOF'
import json, sys
path, name, model, screen, batt, s1, s2, mux, bt, sd_size, disk_used, ip, code_ver, temp, ts = sys.argv[1:]
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {}
data[name] = {
    "rpi_model": model,
    "has_screen": screen,
    "has_battery": batt,
    "sensor_1": s1,
    "sensor_2": s2,
    "mux_present": mux,
    "bluetooth": bt,
    "sd_card_size": sd_size,
    "disk_used_percent": disk_used,
    "ip_address": ip,
    "code_version": code_ver,
    "cpu_temp_c": temp,
    "last_seen": ts,
}
with open(path, "w") as f:
    json.dump(data, f, indent=2, sort_keys=True)
PYEOF

    git add "$REGISTRY_FILE"
    git commit -m "Status update: $DEVICE_NAME" > /dev/null 2>&1
    if git push origin main > /dev/null 2>&1; then
        echo "Status reported for $DEVICE_NAME"
        break
    else
        echo "Registry busy, retrying..."
        sleep $((RANDOM % 4 + 1))
    fi
done
