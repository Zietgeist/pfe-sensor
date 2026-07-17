# pfe-sensor

Sensor unit for measuring pressure field extension (PFE) under a concrete slab —
used to diagnose and design radon mitigation systems. Multiple units form a
mesh: one device hosts a WiFi network (`PFE-NET`) in the field, the rest
connect to it as clients and report their readings to the host, which serves
a live dashboard. Which device hosts is a manual decision made with the
physical button at boot — not something devices negotiate automatically.

## About this project (read before helping)

This project is being built by Ivan, who has limited programming experience.
If you're an AI assistant or another developer picking this up:

- Give exact, copy-pasteable commands — not "just update the config" or
  "run the installer," but the literal command to type.
- When you change how install or setup works, update **both**
  `setup_pfe.sh` and this README in the same pass. Don't let them drift —
  that's caused real confusion before (fixes existed in the repo that
  nobody realized weren't reflected in the docs, or vice versa).
- When editing files on GitHub directly (no local git client is normally
  used for this repo), double check the commit actually lands on `main`
  before assuming a fix is live. `raw.githubusercontent.com` and GitHub's
  own pages can serve cached/stale content for several minutes after a
  push — don't trust a single fetch that "looks old" as proof nothing
  changed; re-check with a cache-busting query param or wait a bit before
  concluding a push failed.

## How devices stay current

Every boot, each `PFE-X` unit automatically:

1. Checks for `PFE-home` WiFi and connects briefly if it's in range, to
   pull the latest code from this repo's `main` branch
   (`update_and_run.sh`) and check itself into the shared
   `device_registry.json` on GitHub with its hardware info, IP, current
   code version, and a local timestamp (`report_status.sh`). Then
   disconnects — home wifi is only used for this, not ongoing operation.
2. Auto-detects and sets its own local timezone from IP geolocation
   (`update_and_run.sh`) — units get sold and deployed anywhere, and
   pressure readings need to line up with local time of day (furnace
   cycles, people coming/going, etc.) for data logging to make sense.
   Only touches the clock if the detected zone is different from what's
   already set.
3. Pauses on a "Host or Join PFE-NET?" screen partway through the boot
   sequence and waits for a physical button press: **long press = host,
   short press = join** (see `setup_wifi()` in `pressure_display.py`).
   Only asked once per boot — re-entering setup afterward (long-hold from
   the running screen) skips straight back to the temp/zone/baseline
   steps without asking again.
4. Starts the sensor app / dashboard once the network decision is made.

This is fully automatic up through step 2 — there's no manual "update"
step for a deployed device. If a device hasn't shown up in
`device_registry.json` recently, that means step 1 is failing (usually a
DNS/networking timing issue at boot — see `update_and_run.sh` for the
current handling of that), not that it needs to be reflashed.

Who hosts `PFE-NET` is a manual, human decision made with the physical
button at boot, not something devices try to silently self-negotiate.
Earlier versions tried to auto-elect a host — first by having every device
broadcast a uniquely-numbered network (`PFE-NET-<n>`) and having others
scan and join the lowest number found, later by a Bluetooth election
(`ble_election.py`) — but both kept failing in real field tests: scans
that missed a genuinely-up host, BLE that never reliably worked, races
between devices booting at different speeds. Since a person is standing
right there setting up the mesh anyway, one button press settles it
instantly with no ambiguity, and it's back to one shared network name.

## Hardware

- **Board:** Raspberry Pi Zero family — **any version**. The goal is for
  the software to be hardware-agnostic across the whole Zero line (Zero W,
  Zero 2 W, and whatever comes next), so units can be built on whichever
  board is cheapest/available at the time without needing different code.
  Currently deployed: most units on Zero 2 W, PFE-4 and PFE-5 on the
  original Zero W (v1) — Zero 2 W currently costs ~3x as much, so Zero W v1
  is the fallback when cost matters more than the extra CPU headroom.
  **Not fully agnostic yet** — see Known Issues.
- **Pressure sensor:** Sensirion SDP810 and SDP811, one of each per pair.
  These two part numbers exist specifically because they have different
  hardcoded I2C addresses (SDP810 = `0x25`, SDP811 = `0x26`) — using one of
  each lets two sensors share an I2C bus with no address conflict and no
  multiplexer needed. A third and fourth sensor (all at `0x25`) can be
  added via a TCA9548A/PCA9548A I2C multiplexer at `0x70`, switched by
  channel — see `detect_hardware()` / `read_all_raw()` in
  `pressure_display.py`.
- **Screen:** PiSugar Whisplay HAT (SPI display, RGB LED, physical button).
- **Power:** PiSugar battery pack (PiSugar 3), managed by `pisugar-server`.
- **Networking:** onboard WiFi only. Who hosts `PFE-NET` in the field is a
  manual button decision at boot (see "How devices stay current" above),
  not automatic — no Bluetooth-based coordination is in use (see
  `ble_election.py` — present in the repo but not wired in; an earlier
  auto-election approach that didn't prove reliable).

## Safe power-off in the field

Field units only have the physical PiSugar button — no SSH, no `sudo
shutdown -now`. `setup_pfe.sh`'s install instructions configure PiSugar
so a **long press does a clean shutdown** before power actually cuts,
instead of a hard cutoff, and so the device **shuts itself down cleanly
at 5% battery** instead of browning out. Both are one-time settings
stored in PiSugar's own config, so they survive reboots. If a device
doesn't have this set (e.g. it was built before this was added), run
once:

```
echo -e 'set_button_enable long 1\nset_button_shell long "sudo shutdown now"' | nc -U -q1 /tmp/pisugar-server.sock
echo -e 'set_safe_shutdown_level 5\nset_safe_shutdown_delay 30' | nc -U -q1 /tmp/pisugar-server.sock
```

This isn't a complete guarantee against SD card corruption (nothing is,
on a device that can still lose power mid-write) — but between this and
the fact that `update_and_run.sh`/`report_status.sh` already `git reset
--hard` to a known-good state on every boot, a device that does get
corrupted mid-write should self-heal on its next successful boot rather
than staying broken.

## Known issues

- **PiSugar segfaults on Zero W v1** (`pisugar-server` crash-loops with
  `SEGV`, so `/tmp/pisugar-server.sock` never exists and the on-screen
  battery bar never draws). Confirmed on PFE-4 and PFE-5. Likely an
  architecture mismatch in the PiSugar installer's prebuilt binary
  (Zero 2 W is quad-core ARMv7; Zero W v1 is single-core ARMv6). This is
  the main thing blocking true hardware-agnostic support across the Zero
  family — not yet resolved. Battery percentage just won't show on
  Zero W v1 units until this is sorted out.
- **`config.py` and `data_store.py` may be dead code.** The current
  `pressure_display.py` defines its own constants and shared state inline
  and does not appear to import from either file. Worth confirming and
  either removing them or reconnecting them — as-is they're a trap for
  anyone (human or AI) who edits `config.py` expecting it to affect
  runtime behavior.

## Repo layout

- `setup_pfe.sh` — one-shot install script for a fresh Raspberry Pi OS
  Bookworm Lite image. Run via the curl command at the top of the file.
- `update_and_run.sh` — runs on every boot (via `pfe-sensor.service`):
  pulls the latest code, checks in to `device_registry.json`, then starts
  the sensor app.
- `pressure_display.py` — the main app: sensor reads, WiFi/hotspot setup,
  boot sequence UI, dashboard web server. This is the file that actually
  runs — treat it as the source of truth over `config.py`/`data_store.py`.
- `boot_splash.py` — shows the logo immediately after power-on, before
  networking/git/sensors are ready (via `pfe-boot-splash.service`).
- `report_status.sh` — checks a device into the shared
  `device_registry.json` on every boot (hardware info, IP, code version).
- `sensor.py`, `config.py`, `data_store.py` — older sensor I/O, device
  config, and shared-state modules. See Known Issues — may no longer be
  wired into the running app.
- `ble_election.py` — Bluetooth-based host election. Written but not
  currently used.

## Device naming

Each device claims the next available number from `next_device_number.txt`
on first setup and is named `PFE-<n>` accordingly (hostname + WiFi identity).
