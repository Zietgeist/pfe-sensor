#!/bin/bash
REPO_DIR="/home/pi/pfe-sensor"
REPO_URL="https://github.com/Zietgeist/pfe-sensor.git"
MAIN_SCRIPT="$REPO_DIR/pressure_display.py"
LOG="/home/pi/pfe_update.log"

echo "==============================" >> "$LOG"
echo "Boot: $(date)" >> "$LOG"

if [ ! -d "$REPO_DIR" ]; then
    git clone "$REPO_URL" "$REPO_DIR" >> "$LOG" 2>&1
fi

cd "$REPO_DIR"
git fetch origin main >> "$LOG" 2>&1
git reset --hard origin/main >> "$LOG" 2>&1
echo "Code updated." >> "$LOG"

exec python3 "$MAIN_SCRIPT" >> "$LOG" 2>&1
