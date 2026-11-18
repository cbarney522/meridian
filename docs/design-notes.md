# Meridian R700 App — design notes

> Copy of the RFID Discussion project doc `meridian-design-notes.md` as of 14 Nov 2026. Its status
> section is out of date: see `docs/HANDOFF.md` for the current state (0.1.3.18, 18 Nov 2026).
> The hardware facts, architecture, I/O and UI sections still apply.

**Repo:** github.com/cbarney522/meridian (branch `ui-shell`, not yet merged to `main`; repo is public, make it private before product code lands)
**Status (2026-11-14, paused):** CAP **0.1.3.6** pushed (commit fc31bf1) and running on the bench R700 v2 (impinj-17-1b-e6, 192.168.0.188, FW 10.4). ~95 unit tests (MQTT tests need Mosquitto; skipped on Cliff's WSL).
Working on the reader: sign-in + accounts, persistent settings across upgrades, Dashboard (reader, network, RFID, zones, MQTT, live events with history), Hardware › USB › Devices / Badge readers / Storage (+ read-only drive browser), Hardware › RFID › Inventory & zones (presets, firing patterns, zones, detection), Operations › Tags › Tags in field (Actionable? column), Integrations › MQTT (to Mosquitto on Cliff's PC, watched in MQTT Explorer), System › Accounts / About this reader / Time & location (site ID `cliff-basement-01`), Developer › Debug log. Site page is mock-only (to be fed from MQTT status). Developer › Feature flags and everything else are placeholders.

**Versioning rule (Cliff):** bump only the last part per build — 0.1.3.1 … 0.1.3.50, then 0.1.4.0 — keep numbers low; commercial launch = 1.0.0.0. The rule is also a comment above `Version` in `cap/cap_description.in`.

**Next (when resumed):** tool list (EPC → tool name/ID/category via CSV upload; names in Tags in field and events; Operations › Unknown tags), then badge access (authorized-users list, `badge_scan` granted/denied), then rules engine; I/O waits for a board choice (see below). Later: kiosk page, Site page from MQTT, feature flags (when the first feature needs one), licensing.

## Concept
One "master" CAP installed on every company R700, configured per solution by technicians through a role-based web UI served by the reader. The reader becomes the local brain (replaces the crib PC): rules engine, I/O, badge access, local data, MQTT, kiosk screen.

## Verified on hardware (R700 v2, FW 10.4, Arago 2023.10, kernel 6.6.58, glibc 2.39, Nov 2026)
- **CAP runs as non-root `cap` (uid 1009)** in groups `disk`, `input`, `dialout`, `ipc`, `rshell`. No system python3 (bundled CPython 3.12 works; OpenSSL 3.5). openssl, curl, busybox, bash, timeout, lsusb, mount/umount, mkfs.ext4/vfat, blkid present; no udisksctl.
- **USB layout:** rear ports hang off an internal TI hub `0451:8142` at `1-1`: left `1-1.1`, middle `1-1.2`, right `1-1.3`. Bus 2 (USB 3) unused. A "7-port" hub is two chained 4-port Realtek `0bda:5411` chips; printed socket numbers don't match port paths.
- **sysfs readable**; **/dev/input/event*** readable and **EVIOCGRAB works** (Chainway "UHF&2D" scanner 2047:0301).
- **USB drives auto-mounted on any port, including behind a hub** (Cruzer at `1-1.1.1` via the 7-port hub: mounted, writable by `cap`, drive browser works) at `/run/media/disks/<id>` and `/run/mount/external/<id>`, vfat, `rw,sync,gid=6`. `umount` refuses (no eject/format from a CAP). FAT32 supported, files < 4 GB. No port is reserved for storage.
- **USB serial works:** kernel has `cp210x` (registered), and `cdc_acm`, `ftdi_sio`, `usbserial` as modules that load on plug-in (no `ch341`/`pl2303`: avoid CH340 and Prolific chips). CompuLab's CP2102N console bridge appeared as `/dev/ttyUSB0`, `root:dialout crw-rw----`; Meridian opened it, set 115200 8N1 and wrote without error. No reply was read (board possibly reset by DTR/RTS on open, or the console didn't redraw); reading will be proven by the first Modbus board.
- `rw_dir` on a 736 MB fs (`/customer`, ~625 MB free).
- **Persistent Data = rw_dir survives upgrade-over-upgrade:** accounts, settings, logins and the TLS cert kept; no new cert warning.
- **No system time-zone database**; the CAP bundles one from the build machine's tzdata (`zoneinfo/`, `PYTHONTZPATH`); build fails without it.
- **chronyd** syncs (NIST). NTP servers come from DHCP; static ones are set on the reader (Impinj web UI or RShell `config network ntp`), not by the CAP.
- **IoT Device Interface from the CAP on localhost** works with the reader's admin login: system/image/region/rfid-interface details, preset list and details, start/stop preset, status, and the `/api/v1/data/stream` tag stream.
- **MQTT from the reader to a LAN broker works** (Mosquitto as a Windows service on Cliff's PC; reader user = serial `37025501450`).
- RFID: `default`, `LPR` and other presets detect tags after the 0.1.3.2 fixes (stop-before-start, reporting-interval handling).

## Architecture (agreed direction)
- **Pipeline:** ingest (IoT DI tag stream on localhost, USB HID badge/scanner via evdev, I/O board) → normalize → tag-state tracker → rules (When / Where / Do, timers, variables, latches) → action dispatcher → sinks via a SQLite outbox.
- **Rules:** declarative JSON, no user code, no raw SQL. Outputs are prioritized *states*; every output has a safe state; watchdog → safe states. Sunrise/sunset available as triggers.
- **Profiles:** shipped in the CAP (tool-crib-portal, cabinet-drawer, bin-shelf, generic); config = profile + overrides; versioned schema with migrations.
- **USB roles:** devices identified by physical port path; roles per port (badge_in, badge_out, scanner, keypad, io_board, relay_board, serial, gps, storage, ignore); VID:PID pinned → "different device" warning.
- **Badges:** keyboard-wedge readers read via evdev with EVIOCGRAB; per-site normalization with live preview; swipe-to-identify. Prox is cloneable: fine for accountability, weak for door access.
- **Access lists & tool cache:** central DB is system of record; reader holds synced read-only copies in SQLite in rw_dir. Unknown EPCs are normal. Consider GS1 GIAI-96.
- **Site identity & time:** Time & location stores Site ID (used in MQTT topics), reader name, location, site time zone, 12/24 h, lat/long (sunrise/sunset via NOAA equations). Reader clock stays UTC.
- **MQTT (built):** paho-mqtt 2.2.0 vendored (`app/vendor/paho`). Topics `<prefix>/<site>/<reader>/{status,evt/<type>,cmd/<name>,ack}` + `<prefix>/<site>/all/cmd/<name>`; reader = serial. Retained status + Last Will; heartbeat; events from the live bus; commands `ping`, `identify`, `status`, `info`, `inventory` (start/stop/status, `preset` or `pattern`) with id-matched acks and expiry. ACL: `pattern readwrite meridian/+/%u/#`, `pattern read meridian/+/all/#`. In-memory queue (1000) while disconnected; SQLite outbox later. Event types catalogued in `app/defs/events.json` (built: usb_attached, usb_detached, badge, tag_enter, tag_exit, gpi_change); a test keeps it in step with the code.
- **RFID engine (built, `core/rfid.py`):** starts presets saved on the reader (created/tuned in the Impinj web UI); stops the running preset before starting another; reads each preset's config (antennas, power, search mode, reporting interval). Tag stream with auto-reconnect (epcHex or base64 epc; gpiTransitionEvent). TagTracker: arrival after "Reads to confirm arrival" reads within the gone-after window (**never overridden automatically**); "gone after" T s unread; move with quiet time; RSSI and EPC-prefix filters; dwell; read rate. Gone-after is stretched to ≥ 2×interval+2 s for presets with a reporting interval, and to ≥ one cycle + 2 s while a firing pattern runs. Tags in field lists present (Actionable? = Yes), confirming (blank, "n of N" reads) and recently gone (blank, greyed, dropped after 5 min).
- **Firing patterns (built):** 2–20 steps of reader preset + scan seconds + pause after; run once or continuously; selectable in the Preset dropdown (`pattern:<id>`), auto-start and MQTT. Each pattern can carry its own Arrived/left detection (5 fields) that overrides the global settings while it runs; global values return when it stops.
- **Feature flags (planned, placeholder page):** named on/off switches per reader (stored with settings, Developer-only, maybe via MQTT) to ship unfinished features dark, enable pilots, provide a field off-switch, and later gate licensed features.
- **Other outputs:** HTTP/webhook via the outbox; Kafka is a back-end concern; large files via HTTPS presigned upload.
- **Kiosk:** R700 has no video out; CAP serves a kiosk page (SSE + POST); any browser display works (TekVisions in kiosk-browser mode, etc.); crib keeps working if the display fails.
- **Fleet:** Site page on every reader built from MQTT retained status. Later "Meridian Central".
- **Licensing (postponed):** annual license, free 60-day trial; System › License placeholder exists; needs an offline-capable license tool.

## I/O (waiting for a board choice)
- Not using the R700's own GPIO (too few). Inputs: door switches, push buttons, light fences, motion sensors. Outputs: maglocks, electric strikes/latches, lights, alarms.
- Board spec: ~16 opto-isolated 12–24 V inputs (NPN/PNP, per-channel debounce, sensor-power terminal) + ~8 outputs (relays for locks/strikes, MOSFETs for lights/sirens); per-output safe state and fail-safe/fail-secure. Form C (SPDT) relays let each door be wired NO or NC; maglocks normally COM+NO with the relay held energized, so power loss releases them. Suppression diodes at inductive locks.
- Safety: fire-alarm release and request-to-exit cut lock power in hardware, independent of Meridian; codes (NFPA 101/IBC, local AHJ) govern maglock egress. Off-the-shelf Modbus boards usually lack a watchdog → safe state; a custom board should add it.
- Protocol direction: **Modbus RTU** (coils = outputs, discrete inputs = inputs; Meridian polls). RS-485 option allows one cable daisy-chaining modules at several doors (120 Ω termination at both ends).
- Leading off-the-shelf choice: **Sealevel SeaI/O-420U** (16 isolated 5–30 VDC inputs, 8 Form C relays 60 VDC @ 2 A, Modbus RTU over USB, 9–30 VDC powered, SeaLATCH locking USB; ~$265, board-only 420U-OEM ~$252; confirm its USB-serial chip). Numato 32-channel USB GPIO (CDC-ACM, 3.3 V TTL) only for cheap bench tests. ACCES USB-IIRO-16 and Advantech USB-4750 need vendor drivers → not for the R700 (fine on Debian).
- Prototype on the same class of board that will ship (Modbus), not on bare TTL GPIO, so the driver carries over.
- CompuLab SBC-IOT-iMX8 boards (Cliff has ~10): not suited as GPIO devices (Linux computer; USB-A ports are host-only; gadget mode only if `/sys/class/udc` exists; only 4 DI + 4 DO via optional add-on). Possible uses: Modbus TCP / MQTT I/O node for bench tests, site gateway, broker.
- **Accessory box idea (future product):** one USB cable to the R700 carrying an industrial hub (e.g. Microchip USB2514Bi, −40…85 °C), the I/O controller (RP2040 speaking Modbus, with heartbeat → safe state, opto inputs, relays/MOSFETs, terminals), an embedded industrial USB flash module (eUSB: Innodisk USB EDC, Swissbit U-48; SLC, wide temp) and spare ports for badge readers/scanner/keypad; 24 V in. Path A (pilots): Sealevel 420U-OEM + eUSB on a header adapter + industrial hub in a DIN-rail enclosure. Path B (volume): custom PCB with the same Modbus register map. Budget for UL/CE/EMC and possibly UL 294.
- Suggested reader port use: left = I/O board (direct), middle = data drive, right = powered industrial hub; with an accessory box, everything rides one cable.
- Meridian side planned: Modbus RTU driver, Hardware › I/O map page (name, direction, active level, debounce, safe state, live state, test), `input_change` events, output commands via UI/MQTT, loud alarm when the board disappears.

## Reader server (app/server.py)
- HTTPS (per-reader self-signed cert). Sign-in page, session cookie `meridian_session` (HttpOnly, Secure, SameSite=Strict; 30 min idle / 12 h absolute); PBKDF2-SHA256 accounts; throttle 5 failures / 5 min → 60 s lock; same-origin check on writes; strict CSP. Default `admin`/`changeme` with a temporary-password banner.
- Settings in `rw_dir/config.json`: revisioned sections (`usb`, `badges`, `storage`, `reader_api`, `site`, `mqtt`, `rfid`, accounts), atomic writes + .bak. Reader-API and MQTT passwords stored readable (must be replayed), never returned to the browser.
- Live events: SSE `/api/events` as plain `data:` messages; a fresh connection gets the last 30 events marked `replay`; browser keeps 100 per tab; pages that react to events ignore replays.
- Diagnostics (Developer › Debug log): diag.txt (incl. `app/tools/serial_probe.py`: serial drivers registered/modules, open+write+read test on each ttyUSB/ttyACM at 115200), meridian.log tail, badge listener, raw reader-API replies, RFID status, reader status raw, unrecognised stream events.
- Mock server (`dev/mock_server.py`) shares the core modules, uses the real MQTT link and the real RFID engine against `dev/fake_impinj.py` (simulated presets incl. one with a 10 s reporting interval, start/stop, tag stream, GPI).

## Still to verify on FW 10.4
Reading from a USB serial port (expected to work; first Modbus board will show); whether the auto-mounter mounts ext4 sticks; whether the REST API exposes NTP settings; GPI events on the tag stream.

## Roles (adopted)
Developer, Site Admin, Technician, Supervisor, Viewer, Operator (kiosk only), service accounts. Permission-based (`rfid.view/edit`, `integrations.view/edit`, `system.view/edit`, `hardware.edit`, `dev.tools`…) in `app/defs/roles.json`; server enforces every call. Only a developer may assign/edit developer accounts; no self-modification. Audit log not built yet.

## UI decisions
- Top menu bar; dropdowns open on hover **and** click/tap; two levels with grouped panels; breadcrumb; one style for all roles; hidden (not greyed) items.
- Plain HTML/CSS/JS ES modules, no build step, no CDNs, strict CSP (styles via CSSOM), textContent-only DOM builder (`h`, `fill`); shared `ui.js` (toast, save bar).
- Use real `<select>` lists, not `<datalist>`. Periodic refreshes must update values in place and never rebuild form controls (an open dropdown closes otherwise).
- Copy buttons sit to the left of the text they copy.

## Repo layout
```
app/server.py                    reader server
app/defs/roles.json, menu.json, events.json
app/core/                        access, usb, badges, storage, config, accounts_api, sessions, inputs, netinfo, readerapi, timeloc, mqtt_link, rfid, tls
app/tools/serial_probe.py        startup serial diagnostic
app/vendor/paho/                 paho-mqtt 2.2.0 (EPL-2.0 / EDL-1.0)
app/www/                         UI (login.html, index.html, js/pages/*)
cap/                             build.sh, cap_description.in, python-runtime.conf, files/{start,input_manifest.json,app.env,rw_dir/}
dev/mock_server.py, fake_sysfs.py, fake_impinj.py
tests/                           unit tests (python3 -m unittest discover -s tests)
api.md                           API contract
```
Build: `cap/build.sh` in WSL (ETK ≥ 10.3.0 at /opt/10.3.0_Octane_Embedded_Development_Tools, tzdata installed). Install: upload .upgx in the Impinj web UI, reboot, https://192.168.0.188:8443/.

## Open questions
- Ownership: CribMaster/SBD vs Meridian Sensor Group (repo visibility, licensing, scope).
- Master database interface for users/tools (REST? export file? direct SQL?).
- Tag encoding (arbitrary serials vs GS1).
- I/O board choice (see I/O above); Sealevel 420U's USB-serial chip.
- TekVisions model in use and whether it shows a web page or runs an app.
