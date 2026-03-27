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
# Wait for internet (up to 30 seconds)
for i in $(seq 1 15); do
    ping -c 1 -W 1 8.8.8.8 > /dev/null 2>&1 && break
    echo "Waiting for internet... ($i)" >> "$LOG"
    sleep 2
done

cd "$REPO_DIR"
git fetch origin main >> "$LOG" 2>&1 || echo "Fetch failed, continuing" >> "$LOG"
git pull origin main >> "$LOG" 2>&1 || echo "Pull failed, continuing" >> "$LOG"
echo "Code updated." >> "$LOG"

exec python3 "$MAIN_SCRIPT" >> "$LOG" 2>&1
