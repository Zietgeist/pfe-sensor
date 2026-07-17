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

# This whole script runs as root, but setup_pfe.sh only ever configured
# git identity for the "pi" user (git config --global there writes to
# /home/pi/.gitconfig, not root's). That left root able to stage and push
# but never actually commit — report_status.sh's "git commit" was failing
# silently every boot, and the follow-up "git push" trivially "succeeded"
# with nothing new to send, so devices looked like they were checking in
# when they never actually were. Setting it here, every boot, self-heals
# any device that still has the old setup.
git config --global user.email "pfe-device@local" >> "$LOG" 2>&1
git config --global user.name "PFE Device" >> "$LOG" 2>&1

if [ ! -d "$REPO_DIR" ]; then
    git clone "$REPO_URL" "$REPO_DIR" >> "$LOG" 2>&1 || echo "Clone failed, continuing" >> "$LOG"
fi

# Clear stale hotspot/site connection profiles every boot.
# These get created automatically when a device hosts or joins PFE-NET in
# the field (nmcli names the auto-created hotspot profile "Hotspot"; the
# client-side join profile is "site-PFE-NET" — see connect_to() in
# pressure_display.py). Who hosts is now a manual button decision at boot
# (see "host_or_join" in pressure_display.py), so it's back to one shared
# network name instead of a per-device numbered one. Left in place, stale
# profiles can distract NetworkManager on the next boot and delay it
# settling on the real home WiFi network, which delays DNS coming up —
# which was silently breaking the GitHub check-in below.
nmcli connection delete Hotspot >> "$LOG" 2>&1
nmcli connection delete PFE-NET >> "$LOG" 2>&1
nmcli connection delete site-PFE-NET >> "$LOG" 2>&1

# Wait for internet — check actual DNS resolution, not just raw IP
# reachability. A ping to 8.8.8.8 can succeed before DNS is working yet,
# which was causing git fetch/pull to fail with "Could not resolve host"
# even though this loop thought we were already online.
for i in $(seq 1 30); do
    getent hosts github.com > /dev/null 2>&1 && break
    echo "Waiting for DNS... ($i)" >> "$LOG"
    sleep 2
done

# Auto-detect and set the system timezone from IP geolocation, every boot.
# Units get sold and shipped to customers anywhere — a Pi's clock defaults
# to UTC and stays there forever unless something sets it. Data logging
# needs local time of day (furnace cycles, people coming/going, etc.), not
# a fixed zone, so this keeps whatever zone the device is physically in,
# automatically, with no manual step. Only actually touches the clock if
# the detected zone differs from what's already set, so this is a no-op
# almost every boot. If there's no real internet right now (e.g. the
# device is off hosting or joining PFE-NET in the field), both lookups
# just time out quickly and the existing timezone is left alone.
DETECTED_TZ=$(curl -s --max-time 8 https://ipapi.co/timezone)
if [ -z "$DETECTED_TZ" ] || [[ "$DETECTED_TZ" == *"error"* ]] || [[ "$DETECTED_TZ" == *"<"* ]]; then
    DETECTED_TZ=$(curl -s --max-time 8 http://worldtimeapi.org/api/ip | grep -o '"timezone":"[^"]*"' | cut -d'"' -f4)
fi
if [ -n "$DETECTED_TZ" ]; then
    CURRENT_TZ=$(timedatectl show --property=Timezone --value)
    if [ "$DETECTED_TZ" != "$CURRENT_TZ" ]; then
        if timedatectl set-timezone "$DETECTED_TZ" >> "$LOG" 2>&1; then
            echo "Timezone updated: $CURRENT_TZ -> $DETECTED_TZ" >> "$LOG"
        else
            echo "Timezone update to $DETECTED_TZ failed, keeping $CURRENT_TZ" >> "$LOG"
        fi
    else
        echo "Timezone already correct: $CURRENT_TZ" >> "$LOG"
    fi
else
    echo "Timezone detection failed (no internet or API down), keeping $(timedatectl show --property=Timezone --value)" >> "$LOG"
fi

cd "$REPO_DIR"
git fetch origin main >> "$LOG" 2>&1 || echo "Fetch failed, continuing" >> "$LOG"
git pull origin main >> "$LOG" 2>&1 || echo "Pull failed, continuing" >> "$LOG"
echo "Code updated." >> "$LOG"

bash "$REPO_DIR/report_status.sh" >> "$LOG" 2>&1

# Everything above this line (DNS wait, git pull, timezone check, WiFi
# decisions) gets appended to $LOG — that's a handful of lines per boot,
# stays small, and is easy to grep/cat. The sensor app below prints a
# status line about once a second while running, forever, and used to get
# appended to this same file too — with nothing ever clearing it out, that
# made pfe_update.log grow without bound and turned it into a slog to read
# through. Since this runs as a systemd service, dropping the redirect
# here doesn't lose that output — it flows to the system journal
# automatically instead, which already handles rotation/size-capping on
# its own. Watch it live with: sudo journalctl -u pfe-sensor -f
exec python3 "$MAIN_SCRIPT"
