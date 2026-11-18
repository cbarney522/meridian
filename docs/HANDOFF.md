# Meridian — project handoff (18 Nov 2026)

Start a new chat with: *"Read the Meridian handoff doc in this project's knowledge and pick up from there."*

## What Meridian is
A master CAP (Custom Application Partition) for the **Impinj R700** RFID reader, owned by Cliff (Meridian Sensor Group). One app on every reader, configured per solution by technicians through a role-based web UI the reader serves itself. The reader is a **reporting endpoint, not an inventory system**: it captures and qualifies RFID reads, badge scans and (later) I/O, shows a kiosk, and reports to a master database / fleet manager, which owns all inventory logic.

## Where things are
| What | Where |
| --- | --- |
| Code | github.com/cbarney522/meridian, branch **`ui-shell`** (not merged to `main`; repo is public, make private before product code) |
| Current version | **0.1.3.18** (commit 69304f4), running on the bench R700 |
| API contract | `api.md` in the repo (every endpoint, MQTT topics, webhook format, network tools) |
| Antenna-bank spec | `docs/antenna-banks.md` (spec only, not built; target 0.1.4.x) |
| Hardware facts and earlier design notes | `docs/design-notes.md` in the repo (copy of the RFID Discussion project doc; its status section is from 14 Nov) |
| Reader data model design | Docs: **Meridian data model & observation modes** — https://claude.ai/artifact/MXYDZVkoM4yNnUv7L9LhUm |
| Fleet manager requirements | Docs: **Meridian Fleet Manager requirements** — https://claude.ai/artifact/FdmCqXrHHfMj1wb7jkaZoF |

## Build and install (WSL)
```
cd ~/meridian && git pull origin ui-shell
./cap/build.sh
cp cap/meridian_$(sed -n 's/^Version = //p' cap/cap_description.in).upgx /mnt/c/Users/$(cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r')/Downloads/
```
Upload the `.upgx` in the Impinj web UI, reboot, browse to `https://192.168.0.188:8443/`. build.sh runs the tests first (~138, ~6 skipped on WSL). ETK ≥ 10.3.0 at `/opt/10.3.0_Octane_Embedded_Development_Tools`; tzdata must be installed in WSL.

**Versioning rule:** bump only the last part per build: 0.1.3.1 … 0.1.3.50, then 0.1.4.0. Commercial launch = 1.0.0.0. (An old stray `meridian_0.1.4.0.upgx` from 14 Nov predates the rule: ignore it.)

## Bench setup
- R700 v2 `impinj-17-1b-e6`, **192.168.0.188**, FW 10.4, serial 37025501450; site ID `cliff-basement-01`.
- Cliff's Windows PC **192.168.0.167**: Mosquitto broker (Windows service) + MQTT Explorer; builds in WSL (user `cbarn`).
- On the PC, Python is **`python3`** (or `py` in PowerShell). Test receivers must run with **Windows Python, not inside WSL** (WSL isn't reachable from the LAN without port forwarding).
- Webhook test receiver: `py dev\webhook_receiver.py --secret whsec_...` (port 8765), from `\\wsl$\Ubuntu\home\cbarn\meridian`.
- Router 192.168.0.1 gives the reader an **infinite DHCP lease** (via NetworkManager) and NTP server 192.36.144.22.

## Built and verified on the reader
| Area | Pages / notes |
| --- | --- |
| Shell | Sign-in, accounts and roles, top menu with hover/click dropdowns, HTTPS with per-reader cert, settings survive upgrades |
| Dashboard / Site | Reader, network, RFID, zones, MQTT, live events with history. Site page is mock-only (to be fed from MQTT status) |
| Hardware › USB | Devices, Badge readers (evdev, grab), Storage + drive browser. Drives auto-mount even behind a hub; USB serial (CP210x, CDC-ACM, FTDI) opens |
| Hardware › RFID | Inventory & zones: reader presets, firing patterns (2+ presets, scan/pause, continuous, per-pattern arrived/left override), zones, detection. "Reads to confirm arrival" is never overridden automatically |
| Operations › Tags in field | Present / confirming / gone, **Actionable?** column |
| Integrations › MQTT | Retained status + Last Will, events, commands (ping, identify, status, info, inventory) with acks; Event types list; copy buttons left of topics/payloads |
| Integrations › HTTP / webhooks | Up to 5 endpoints, per-endpoint event choice, HMAC-SHA256 signature, durable SQLite queue with retries (30 s … hourly, 24 h), in-order delivery, Send test, delivery log, resend, retry now. **Verified 15 Nov**: signed `usb_attached` arrived and verified |
| Data › Database tables | SQLite (`rw_dir/meridian.db`): CribMaster CSV imports (items, bins, serialids, rfid), table browser, read-only SQL console |
| System | About this reader (Impinj REST data), **Identity** (crib number 1–9999, site ID, name, location; crib in top bar, status, MQTT and every event), Time & location (zone, 12/24 h, lat/long, sunrise/sunset), Accounts |
| Help › Troubleshooting › Network tools | Quick check (link, IP, DHCP, gateway, DNS, MQTT broker + webhook receivers, time sync; copy results), network status, **DHCP lease read from NetworkManager via nmcli** (server, router, lease length, what the server sent vs what the reader asked for), port test with optional TLS + certificate details, DNS lookup, ping. Verified 17 Nov |
| Developer › Debug log | diag.txt, log tail, raw replies, serial probe |

Menu items still placeholders: Operations (active sessions, alarms, manual outputs, unknown tags), Hardware (RFID Tag Health, I/O map/board/output test), all of Rules, People & Tools, Integrations (file export, outbox), Kiosk, Data (event log, audit log, **DB Import Mapping**, export), System (network, certificates, license, backup), Help › User manual, Developer (raw stream, API explorer, feature flags).

## Decisions so far
- **Language:** stay on Python 3.12 (bundled; R700 has none) for now. A Go rewrite was costed (~5,800 server lines to redo; UI, API and data formats unchanged) and deferred; revisit before 1.0 if the reader's memory/CPU numbers say so. A Resource use panel on About this reader was offered, not yet agreed.
- **Roles:** Developer, Site Admin, Technician, Supervisor, Viewer, Operator (kiosk only), service accounts. New permissions: `help.view` (all but Operator), `network.diag` (Developer, Site Admin, Technician), `data.import`.
- **UI rules:** real `<select>`s, never rebuild controls during refresh, copy buttons left of the text, hidden (not greyed) menu items, one style for all roles.
- **Data model** (details in the data-model doc): Meridian schema, not CribMaster's (CribMaster import tables to be dropped; a DB mapping config later). Tag kinds tool / consumable / bin. Observation modes: **presence** (portal, empty-bin zone) and **inventory** (cabinet: before/after per session). Missing tags verified by **5 re-scans with special presets**; interrupted → hold and redo all 5. PO optional, unvalidated, skippable. Notices display-only. Bin empty = bin tag visible N s in an empty-bin zone. Zones set up on the reader by a technician; the master knows nothing about RFID capture. Separate tables + one `tag_lookup` view. Photos on the USB drive. One crib per reader, any number accepted, warn later if the master reports a duplicate.
- **Tag health:** history kept only for inventory zones; bit-flip detection while scanning (unknown EPC 1–2 hex chars from a known one, same zone/antenna) flags the known tag; it is never reported as taken because of a flip.
- **Fleet manager** (requirements doc, not being built yet): web-based, holds the master database; readers connect outbound only; solution types (versioned reader templates) pushed to readers, applied at commissioning, updates accepted by a technician; remote "Open Meridian" session through a reader-opened tunnel; integrations via REST API + webhooks, middleware, on-prem connector, or files.
- **Webhooks:** Meridian's own fixed, documented JSON format is the default; receivers adapt. Extra headers (API keys), filters, field selection, templates and batching were discussed: **undecided**, Cliff wants to think more.

## Discussed 17 Nov, not yet written into the fleet doc
- **Sync at ~50 readers:** hub-and-spoke (readers only talk to the master); **one owner per dataset** (master owns reference data, readers own append-only events) so there are no conflicts; downward = reader pulls "changes since N" over HTTPS with full snapshot on a gap, optional MQTT "changes available" nudge (polling alone is fine at this scale); upward = durable outbox POSTed in batches with per-event IDs and receipts; every crib works offline.
- **Transport:** HTTPS REST API carries the data; MQTT optional for instant nudges, commands and live status.
- **Security:** per-reader credential issued at enrollment (client certificate / mTLS, or API key) from a one-time code; identity comes from the credential, not the message; readers can only read their slice and append events for their own crib; validation, rate limits, audit, anomaly alerts, instant revocation; MQTT broker ACLs per reader.
- **Hosting:** cloud by default, on-premise as the same container package (one tenant); standard parts only.
- **Database:** PostgreSQL for the master; support Microsoft SQL Server on-prem only if a customer requires it; keep the database layer portable.

## Open items and next steps
1. Next build candidates: **inventory mode** (cabinet sessions, 5 re-scans, holding uncertain tags), then **RFID Tag Health**. Earlier candidates: tool list (EPC → tool name), badge access, rules engine.
2. Webhook options to decide: extra headers first if anything.
3. Optional: Resource use panel (memory/CPU) on About this reader.
4. Fleet doc updates: add the sync, security, hosting and database decisions above once Cliff agrees.
5. I/O parked until a board is chosen (lead: Sealevel SeaI/O-420U, Modbus RTU; confirm its USB-serial chip). Antenna-bank switch: spec in `docs/antenna-banks.md`, parts on the breadboard.
6. Still to verify on FW 10.4: reading from a USB serial port, ext4 auto-mount, NTP via REST, GPI events on the tag stream.
7. Repo: make private; merge `ui-shell` to `main` when the shell is done.

## How Cliff likes to work
- Explain new terms plainly when asked (e.g. webhooks, deep links, branches); give exact copy-paste WSL / PowerShell commands.
- Design first, show him the doc, build only after he agrees.
- Each pushed build gets the next version number; tell him the version and what to test on the reader.
