#!/bin/bash
REPO_DIR="/home/pi/pfe-sensor"
REPO_URL="https://github.com/Zietgeist/pfe-sensor.git"
MAIN_SCRIPT="$REPO_DIR/pressure_display.py"
LOG="/home/pi/pfe_update.log"

echo "==============================" >> "$LOG"
echo "Boot: $(date)" >> "$LOG"

# Fix ownership so git doesn't complain
chown -R pi:pi "$REPO_DIR"
git config --global --add safe.directory "$REPO_DIR" >> "$LOG" 2>&1

if [ ! -d "$REPO_DIR" ]; then
    git clone "$REPO_URL" "$REPO_DIR" >> "$LOG" 2>&1 || echo "Clone failed, continuing" >> "$LOG"
fi

# Clear stale hotspot/PFE-NET connection profiles every boot.
# These get created automatically when a device hosts or joins PFE-NET
# in the field (nmcli names the auto-created hotspot profile "Hotspot").
# Left in place, they can distract NetworkManager on the next boot and
# delay it settling on the real home WiFi network, which delays DNS
# coming up — which was silently breaking the GitHub check-in below.
nmcli connection delete Hotspot >> "$LOG" 2>&1
nmcli connection delete PFE-NET >> "$LOG" 2>&1

# Wait for internet — check actual DNS resolution, not just raw IP
# reachability. A ping to 8.8.8.8 can succeed before DNS is working yet,
# which was causing git fetch/pull to fail with "Could not resolve host"
# even though this loop thought we were already online.
for i in $(seq 1 30); do
    getent hosts github.com > /dev/null 2>&1 && break
    echo "Waiting for DNS... ($i)" >> "$LOG"
    sleep 2
done

cd "$REPO_DIR"
git fetch origin main >> "$LOG" 2>&1 || echo "Fetch failed, continuing" >> "$LOG"
git pull origin main >> "$LOG" 2>&1 || echo "Pull failed, continuing" >> "$LOG"
echo "Code updated." >> "$LOG"

bash "$REPO_DIR/report_status.sh" >> "$LOG" 2>&1

exec python3 "$MAIN_SCRIPT" >> "$LOG" 2>&1
