# Meridian API contract

The UI talks only to these endpoints. `dev/mock_server.py` implements them with simulated data;
the CAP's server must implement them identically. All responses are JSON unless noted.
Errors: `{"error": "<code>", ...}` with HTTP 400 / 403 / 404.

On the reader, page files (HTML/CSS/JS) are public; every `/api/` call except `login-info`,
`login` and `logout` needs a **session cookie** (`meridian_session`: random token, `HttpOnly`,
`Secure`, `SameSite=Strict`). Without one the API answers
`401 {"error": "not_signed_in"}` and the UI goes to `login.html#<route>`. Sessions end after
30 minutes idle (`--session-idle`), 12 hours after sign-in, on sign-out, or when the server
restarts. Writes from another origin are refused (`403`). Endpoints for parts not built yet answer
`501 {"error": "not_implemented", "details": ["... isn't available in this build yet."]}`
(currently: `/api/site/readers`, `/api/storage/eject|mount|format`). `/api/status` reports
`rfid.available: false` and `mqtt.available: false` until the engine exists, and unknown values as `null`.

Every endpoint except `/api/session` checks a permission server-side. A `403` body includes
`"needs": "<permission>"`.

## Sign-in

### `GET /api/login-info` — public
`{"reader": "<hostname>", "app_version": "0.1.0.6"}` for the sign-in page.

### `POST /api/login` — public
`{"user": "admin", "password": "..."}` → `200 {"ok": true, "must_change_password": true}` with
`Set-Cookie: meridian_session=...` · `401 {"error": "bad_credentials"}` ·
`429 {"error": "locked"}` after 5 failures in 5 minutes from one address **or** for one user name
(locked for 60 s).

### `POST /api/logout`
Ends the session and clears the cookie. `200 {"ok": true}`.

## Session and navigation

### `GET /api/session`
Who is signed in and what they may do.
```json
{
  "user": "cliff", "role": "technician", "role_label": "Technician",
  "permissions": ["dashboard.view", "rfid.view", "..."],
  "dev_mode": false,
  "roles": [{"id": "developer", "label": "Developer", "description": "..."}],
  "reader": {"name": "Crib 2 portal", "serial": "37017090614"}
}
```
`dev_mode` and `roles` exist so the mock server can offer the "View as" switcher; the real
server returns `dev_mode: false` and adds `must_change_password` (true while the default
password is in use) and `app_version`.

### `POST /api/account/password` — any signed-in user
`{"current": "...", "new": "..."}` → `200 {"ok": true}` · `400 {"details": ["..."]}`
(at least 8 characters, not the default or the user name). The current session stays signed in;
the user's other sessions are signed out.

### `GET /api/menu`
`menu.json` filtered to the caller's permissions. Groups and menus with nothing visible are removed.
```json
{"menus": [
  {"id": "dashboard", "label": "Dashboard", "route": "/dashboard"},
  {"id": "hardware", "label": "Hardware", "groups": [
    {"label": "RFID", "items": [{"id": "antennas", "label": "Antennas", "route": "/hardware/antennas"}]}
  ]}
]}
```

### `GET /api/route-check?route=/hardware/io-map`
`200 {"ok": true, "perm": "hardware.view"}` · `403 {"error": "forbidden", "needs": "..."}` · `404`.
The router calls this before rendering any page.

## Accounts (System › Accounts) — need `accounts.manage`

### `GET /api/accounts`
```json
{"me": "admin",
 "accounts": [{"user": "jsmith", "name": "J. Smith", "role": "technician", "role_label": "Technician",
               "disabled": false, "must_change": true, "created": "...", "last_login": null,
               "sessions": 0, "you": false, "editable": true}],
 "roles": [{"id": "developer", "label": "Developer", "description": "...", "assignable": true}]}
```
### `POST /api/accounts` `{"user", "name", "role", "password"?}`
No password → a temporary one is generated and returned once: `201 {"ok": true, "temporary_password": "k7Rq-mX2p-9vTe"}`.
User names: 2–32 characters, `a-z 0-9 . _ -`. New accounts must change their password at first sign-in.
### `PUT /api/accounts/<user>` `{"name"?, "role"?, "disabled"?}`
### `POST /api/accounts/<user>/reset-password` `{"password"?}` → `{"ok": true, "temporary_password": "..."}`
### `DELETE /api/accounts/<user>`

Rules (403 / 400 with `details`): only a Developer may assign the developer role or change a
Developer account; nobody may change the role of, disable, delete or reset **their own** account;
at least one enabled account with `accounts.manage` must remain. Disabling, deleting or resetting
signs the user out immediately; disabled accounts can't sign in. The mock server runs the same
logic (`core/accounts_api.py`) on a throwaway account store.

## Status

### `GET /api/status` — needs `dashboard.view`
```json
{
  "reader":  {"name": "...", "serial": "...", "site": "...", "firmware": "10.4.0",
              "app_version": "0.1.0", "profile": "tool-crib-portal",
              "uptime_s": 282441, "ntp_synced": true, "temp_c": 41.5},
  "rfid":    {"running": true, "read_rate": 312, "tags_in_field": 35,
              "zones": {"drawer1": 12, "drawer2": 9}},
  "mqtt":    {"connected": true, "outbox_depth": 0},
  "usb":     {"drive": true, "free_mb": 28110, "badge_in": true, "badge_out": true},
  "session": null | {"badge": "10442", "user": "J. Smith", "out": ["..."], "in": ["..."]}
}
```

## Site

### `GET /api/site/readers` — needs `site.view`
Every Meridian reader at the site. On a real reader this is built from the **retained MQTT
heartbeats** each reader publishes on `evt/{site}/{serial}/status`, plus the Last Will
`evt/{site}/{serial}/online` flag, so no central server is needed. `url` is where that
reader's own UI is served; the UI opens it in a new tab.
```json
{
  "site": "plant-atl",
  "latest_version": "0.4.1",
  "readers": [{
    "serial": "37017090614", "name": "Crib 2 portal", "location": "Bldg A",
    "profile": "tool-crib-portal", "app_version": "0.4.1", "firmware": "10.4.0",
    "ip": "10.20.10.100", "url": "https://10.20.10.100:8443/",
    "this_reader": true, "online": true, "last_seen_s": 12,
    "tags_in_field": 47, "outbox_depth": 0, "usb_drive": true,
    "alerts": []
  }]
}
```
`alerts` values so far: `offline`, `usb_missing`, `outbox_backlog`. `tags_in_field` is `null`
when the reader is offline.

## USB

### `GET /api/usb/devices` — needs `hardware.view`
Every USB device from sysfs (`app/core/usb.py`), merged with the port assignments. Rows are in
port order so hubs are followed by what's plugged into them. Assigned ports with nothing in them
appear as `missing` rows.
```json
{
  "revision": 3,
  "devices": [{
    "port": "1-1.1", "bus": 1, "depth": 1, "parent": "1-1",
    "vid": "0c27", "pid": "3bfa", "manufacturer": "RFIDeas", "product": "pcProx Plus RDR-80081AKU",
    "serial": null, "speed_mbps": "12", "max_power_ma": 100,
    "type": "hid-keyboard", "type_label": "Keyboard (HID)",
    "drivers": ["usbhid"], "nodes": ["/dev/input/event2", "/dev/hidraw0"],
    "role": "badge_in", "status": "ok"
  }],
  "roles": [{"id": "badge_in", "label": "Badge reader (in)", "expects": ["hid-keyboard"], "single": true}],
  "summary": {"devices": 8, "hubs": 1, "assigned": 7, "problems": 1, "unassigned": 2, "power_ma": 1264}
}
```
`type`: `hub`, `hid-keyboard`, `hid`, `storage`, `serial-cdc`, `serial-usb`, `audio`, `video`, `wireless`, `vendor`, `other`.
`status`: `ok`, `mismatch` (wrong kind of device for the role), `changed` (right kind, different
VID:PID than when assigned), `missing`, `unassigned`, `ignored`, `hub`. Non-OK rows carry `status_detail`.

### `PUT /api/usb/assignments` — needs `hardware.edit`
`{"revision": 3, "assignments": {"1-1.1": "badge_in", "1-2": "storage"}}` — the **complete** desired
map of port → role. The server pins each newly assigned port to the VID:PID currently in it.
`200 {"ok": true, "revision": 4}` · `409 {"error": "conflict", "revision": 5}` if someone saved in between ·
`400 {"error": "invalid", "details": [...]}` (bad port, unknown role, a single-use role on two ports).
On the reader, assignments are stored in `config.json` in `rw_dir`.

## Badge readers

### `GET /api/badges` — needs `hardware.view`
```json
{
  "revision": 2,
  "settings": {"strip_prefixes": ["I", "O"], "strip_suffixes": [], "separator": ":", "output": "card",
               "strip_leading_zeros": true, "pattern": "^\\d{1,10}$", "end_of_scan": "enter", "timeout_ms": 100},
  "readers": [{"role": "badge_in", "label": "Badge reader (in)", "port": "1-1.1", "assigned": true,
               "status": "ok", "product": "pcProx Plus RDR-80081AKU", "nodes": ["/dev/input/event2"],
               "last_scan": {"ts": "...", "raw": "I105:010442", "badge_id": "10442", "ok": true},
               "scans_today": 14, "errors_today": 1}],
  "other_keyboards": [{"port": "1-1.4", "product": "1950g", "role": "scanner", "nodes": ["/dev/input/event4"]}]
}
```
`status` as for USB rows, plus `unassigned` when no port has the role. Normalization is
`core/badges.py: normalize()`; settings are checked by `validate_settings()`.

### `PUT /api/badges/settings` — needs `hardware.edit`
`{"revision": 2, "settings": {...}}` → `200 {"ok": true, "revision": 3}` · `409` · `400 {"details": [...]}`.

### `POST /api/badges/preview` — needs `hardware.view`
`{"raw": "I105:0010442", "settings": {...optional, defaults to saved}}` →
`{"ok": true, "badge_id": "10442", "facility": "105", "card": "10442", "steps": ["removed prefix 'I'", ...], "error": null}`

## Data drive

### `GET /api/storage` — needs `hardware.view`
```json
{
  "revision": 1, "assigned_port": "1-2",
  "drive": {"present": true, "port": "1-2", "product": "Ultra", "manufacturer": "SanDisk", "serial": "...",
            "node": "/dev/sda", "mounted": true,
            "mount": {"device": "/dev/sda1", "mount_point": "...", "fs_type": "ext4", "read_only": false},
            "capacity": {"size": 30709016166, "used": 879638995, "free": 29829377171}},
  "usage": [{"id": "outbox", "label": "Event outbox (SQLite)", "path": "data/outbox.db", "bytes": 2103150}],
  "internal": {"path": "/cust/rw_dir", "size": 134217728, "used": 24117248, "free": 110100480, "buffering": false},
  "settings": {"retention_days_events": 90, "retention_days_logs": 30, "alert_free_pct": 10},
  "warnings": ["Filesystem is vfat; ext4 is recommended ..."]
}
```
Mount lookup, capacity and warnings come from `core/storage.py`. The drive is the port with role `storage`.

### `GET /api/storage/browse?scope=meridian|drive&path=<folder>` — needs `hardware.view`
Read-only listing of one folder on the data drive: `{"scope", "exists", "path", "entries": [{"name",
"type": "dir|file|link|other", "size" (files only), "modified" (unix time)}], "total", "truncated",
"files_bytes", "can_browse_drive"}`. Folders first, at most 500 entries. `scope=meridian` (default) is
Meridian's own `meridian/` folder; `scope=drive` is the whole drive and also needs `hardware.edit` or
`dev.tools` (403 otherwise). Paths are relative; `..`, links and anything resolving outside the
drive are refused (400). No endpoint reads file contents. Each `usage` item in `GET /api/storage`
carries `browse: {scope, path}` so the UI can jump to it.

### `PUT /api/storage/settings` — needs `hardware.edit`
`{"revision": 1, "settings": {...}}`; each value a whole number (retention 1–3650 days, alert 1–50 %).

### `POST /api/storage/eject` · `/api/storage/mount` · `/api/storage/format` — need `hardware.edit`
Not supported on FW 10.4: the CAP runs as the non-root `cap` user, the reader auto-mounts drives
(twice: `/run/media/disks/<id>` and `/run/mount/external/<id>`, `vfat`, `sync`, group `disk`
writable) and `umount` refuses ("must be superuser"). These answer
`409 {"error": "not_supported", ...}`; `GET /api/storage` reports `can_eject: false, can_format: false`
and the UI hides the buttons. Meridian supports FAT32 drives and keeps each file under 4 GB.

## Developer tools

### `GET /api/dev/diagnostics` — needs `dev.tools`
`{"diag": "<rw_dir/diag.txt>", "log": "<last 64 KB of rw_dir/log/meridian.log>", "badge_listener": {...}, "app_version": "...", "data_dir": "..."}`

## Live events

### `GET /api/events` — needs `dashboard.view`
Server-Sent Events (`text/event-stream`). Each message has `id:` (monotonic), `event:` (type) and
`data:` (JSON). Clients reconnect with `Last-Event-ID` and receive what they missed from a buffer.

| event | data fields |
|---|---|
| `badge_scan` | `source`, `badge`, `user` (null if unknown), `result` (`granted`, `denied_unknown`, …) |
| `tag_exit` / `tag_enter` | `zone`, `tool`, `user` |
| `session_closed` | `user`, `removed`, `returned` |
| `gpi_change` | `input`, `state` |
| `rule_fired` | `rule`, `detail` |
| `usb_attached` / `usb_detached` | `port`, `product` |
| `badge_raw` | `port`, `role`, `product`, `raw`, `result` (as `/api/badges/preview`) — every scan from a keyboard-type device |
| `storage_changed` | `action` (`eject`, `mount`, `format`), `port` |

All events also carry `id`, `type`, `ts` (ISO 8601 local time).

## Mock only

### `POST /api/dev/badge/swipe` `{"port": "1-1.1", "garbled": false}` · `POST /api/dev/storage/fs`
Simulate a badge swipe on a keyboard-type port (optionally misread), and flip the simulated
drive between ext4 and vfat. `GET /api/storage` also returns `dev_fs_type` in the mock.

### `POST /api/dev/usb/toggle` `{"port": "1-1.2"}`
Simulates unplugging / replugging a port (and anything behind it). `GET /api/usb/devices` also
returns `dev_unplugged` in the mock. Not implemented by the real server.

### `POST /api/login` / `/api/logout` (mock)
Always succeed; the mock has no accounts.

### `POST /api/dev/role` `{"role": "supervisor"}`
Switches the mock server's current role. Not implemented by the real server.

## Reader details (System › About this reader)

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/api/reader/info` | system.view | `items` (description, sku, hla, pcba, serial, firmware, region, rfid_interface) from the Impinj REST API; `net` (interface, mac, ipv4, ipv6) from Linux; `time`, `timezone`, `uptime_s`; `api` {ok, error, fetched, username, password_set}. Refreshed every 60 s. |
| GET | `/api/reader/api-settings` | system.view | `{revision, username, password_set}`. The password is never returned. |
| PUT | `/api/reader/api-settings` | system.edit | `{revision, username, password?, clear_password?}`. Blank password keeps the saved one. Triggers an immediate re-read. |
| POST | `/api/reader/api-test` | system.edit | `{username?, password?}` → `{ok, error, items}`. Doesn't save anything. |

Meridian calls `https://localhost/api/v1/system`, `/system/image`, `/system/region` and `/system/rfid/interface` with HTTP Basic auth. Until a password is saved it tries the factory default (`root` / `impinj`). Field names are matched loosely; the raw responses appear under `reader_api_raw` in `/api/diagnostics` (Developer › Debug log). `/api/status` now also carries these fields in `reader`.

## Time & location (System › Time & location)

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/api/time` | system.view | `utc`, `local` (ISO, site zone), `timezone`, `tz_abbrev`, `utc_offset`, `tz_database` (false = only UTC available), `ntp` {available, tracking {reference, stratum, ref_time, offset_ms, update_interval_s, leap, synced}, sources [{name, state, selected, stratum, reach, last_rx_s, offset_ms}]} from `chronyc -c -n`, `sun` {date, sunrise, sunset, day_length} in site time or null, `reader_ntp_config` (raw, if the reader API exposes one). |
| GET | `/api/site-settings` | system.view | `{revision, settings, zones, common_zones}`. settings: `site_id`, `site_name`, `location`, `timezone`, `clock_24h`, `latitude`, `longitude`. |
| PUT | `/api/site-settings` | system.edit | `{revision, settings}` → 400 `invalid` with `details`, 409 `conflict`. |

The reader's clock stays on UTC and is set by chronyd; Meridian can't change the system time zone or NTP servers (no root). It applies the site time zone itself. `site_name` replaces the hostname as the reader name in the top bar, `/api/status` and `/api/session`; `site_id` is `reader.site`. The CAP bundles a time-zone database (`zoneinfo/`, from the build machine's tzdata) used when the reader has none (`PYTHONTZPATH`).

## MQTT (Integrations › MQTT)

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/api/mqtt` | integrations.view | `{revision, settings, status}`. settings: `enabled`, `host`, `port`, `tls`, `verify_cert`, `ca_pem`, `username`, `password_set`, `prefix`, `heartbeat_s`, `qos` (password never returned). status: `enabled`, `connected`, `since`, `broker`, `tls`, `error`, `queued`, `dropped`, `sent`, `received`, `topics`, `traffic` (last 50, newest first). |
| PUT | `/api/mqtt` | integrations.edit | `{revision, settings}`; blank `password` keeps the saved one, `clear_password: true` removes it. Reconnects immediately. |
| POST | `/api/mqtt/test` | integrations.edit | `{settings}` → `{ok, error, ms}`. Connects once with the given values (saved password used if blank); saves nothing. |

`/api/status` → `mqtt`: `{available, enabled, connected, outbox_depth, broker, error}`.

### Topics
`<prefix>/<site>/<reader>/…` where `<reader>` is the serial number (hostname until known) and `<site>` the Site ID or `unassigned`.

| Topic | Direction | Payload |
|---|---|---|
| `…/status` | reader → broker, retained | `{"state":"online", reader, site, name, location, version, firmware, ip, uptime_s, ts}`; Last Will `{"state":"offline"}`; clean stop adds `"reason":"stopped"`. Refreshed every `heartbeat_s`. |
| `…/evt/<type>` | reader → broker | the event as on the live feed (`usb_attached`, `usb_detached`, `badge`, `storage_changed`, …) |
| `…/cmd/<name>`, `<prefix>/<site>/all/cmd/<name>` | broker → reader | `{"id": "...", "args": {...}, "expires": <epoch s, optional>}` |
| `…/ack` | reader → broker | `{"id", "cmd", "ok", "result" \| "error", "ts"}` |

Commands: `ping`, `identify` (`args.seconds`, 1–120), `status`, `info`, `inventory` (`args.action`: `start` | `stop` | `status`, optional `args.preset`). Unknown, malformed or expired commands get `ok:false`. While disconnected, outgoing messages wait in a 1000-message in-memory queue (oldest dropped). The client is paho-mqtt 2.2.0, vendored in `app/vendor/paho` (EPL-2.0 / EDL-1.0).

### Event types

Defined in `app/defs/events.json` (also returned as `event_types` by `GET /api/mqtt` and shown on the MQTT page). Every payload carries `type` and `ts` (reader local time, ISO 8601). A test checks that every type the code emits is listed with `built: true`.

| Type | In this build | Meaning | Sample payload |
|---|---|---|---|
| `usb_attached` | yes | A USB device appeared on a reader port or hub port. | `{"type":"usb_attached","port":"1-1.2","product":"USB Flash Disk","ts":"2026-11-14T08:15:02"}` |
| `usb_detached` | yes | A USB device was removed. | `{"type":"usb_detached","port":"1-1.2","product":"USB Flash Disk","ts":"2026-11-14T08:16:40"}` |
| `badge` | yes | A keyboard-type device (badge reader, barcode scanner) sent a scan. 'raw' is exactly what it typed; 'result' is the badge ID after this site's badge settings, or why it was rejected. | `{"type":"badge","port":"1-1.1.1","role":"badge_in","product":"RFIDeas pcProx","raw":"I105:0010442","result":{"ok":true,"badge_id":"10442","facility":"105","card":"10442","steps":["removed prefix 'I'","split at ':': facility 105, card 0010442","removed leading zeros"],"error":null},"ts":"2026-11-14T08:17:11"}` |
| `badge_scan` | planned | A badge was checked against the authorized-users list: granted, or denied with the reason. | `{"type":"badge_scan","badge":"10442","user":"M. Alvarez","result":"granted","door":"crib","ts":"2026-11-14T08:17:11"}` |
| `tag_exit` | yes | A tag stopped being read for the 'gone after' time (reason 'gone'), moved to another zone (reason 'moved', with 'to_zone'), or inventory was stopped. dwell_s is how long it was there. | `{"type":"tag_exit","epc":"E28011700000020F1B2C3D4E","zone":"drawer1","antenna":2,"dwell_s":412.6,"reason":"gone","ts":"2026-11-14T08:24:56"}` |
| `tag_enter` | yes | A tag was read enough times in a zone to count as present (or moved there from another zone: then 'from_zone' is set). Tool names and users are added once the tool list and sessions exist. | `{"type":"tag_enter","epc":"E28011700000020F1B2C3D4E","zone":"drawer1","antenna":2,"rssi_dbm":-55.5,"ts":"2026-11-14T08:18:03"}` |
| `session_closed` | planned | A badged-in session ended (badge out or timeout), with what was removed and returned. | `{"type":"session_closed","user":"M. Alvarez","badge":"10442","removed":2,"returned":1,"out":["Torque wrench #4471","Crimper #3302"],"in":["Drill DCD791 #1180"],"ts":"2026-11-14T08:19:30"}` |
| `gpi_change` | yes | One of the R700's own inputs changed (gpi1, gpi2). Names like 'door_1' come with the I/O map. Reported by the reader's event stream; check it on your firmware. | `{"type":"gpi_change","input":"gpi1","state":"high","source":"reader","ts":"2026-11-14T08:17:14"}` |
| `rule_fired` | planned | A configured rule's conditions were met and its actions ran. | `{"type":"rule_fired","rule":"Door held open","detail":"door_1 open > 30 s: buzzer on","ts":"2026-11-14T08:17:45"}` |
| `storage_changed` | planned | The USB data drive was inserted, removed or became unwritable. | `{"type":"storage_changed","action":"removed","port":"1-1.2","ts":"2026-11-14T09:02:10"}` |

## Live events stream

`GET /api/events` (dashboard.view) is Server-Sent Events. Every event is a plain `data:` message (no `event:` name) with an `id:`, so clients get new types without changes. A fresh connection first receives up to the last 30 events with `"replay": true`. A reconnect that sends `Last-Event-ID` receives only what it missed. The browser keeps the last 100 events per tab; the Dashboard shows them on open (pages that react to events, such as USB devices, Storage and Badge readers, ignore replays).

## RFID (Hardware › RFID › Inventory & zones, Operations › Tags › Tags in field)

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/api/rfid[?refresh=1]` | rfid.view | `{revision, settings, status, presets}`. `refresh=1` re-reads the reader's preset list and status. |
| PUT | `/api/rfid` | rfid.edit | `{revision, settings}`. settings: `auto_start`, `preset`, `zones` (`{"1": "portal", ...}`), `ignore_unzoned`, `exit_timeout_s` (1–600), `enter_reads` (1–50), `move_quiet_s` (0–30), `rssi_min_dbm` (-100–0 or null), `epc_prefix` (hex), `patterns` (firing patterns, below). `preset` may be `pattern:<id>`. Applies immediately. |
| POST | `/api/rfid/start` | rfid.edit | `{preset?}`: a reader preset id or `pattern:<id>` (default: the saved choice). 502 `reader` with the reader's message if it refuses. |
| POST | `/api/rfid/stop` | rfid.edit | Stops inventory; tags in the field are reported gone. |

status: `available`, `running`, `reader_status`, `active_preset`, `reader_error`, `stream_connected`, `stream_error`, `stream_lines`, `tags_in_field`, `read_rate` (reads/s over 5 s), `zones` (`{zone: count}`), `total_reads`, `ignored`, `tags` (`[{epc, zone, antenna, rssi_dbm, reads, first_seen, last_seen, seen_ago_s}]`, max 500). `/api/status` → `rfid` carries the summary fields.

Reader calls (Impinj IoT Device Interface, Basic auth with the login from About this reader): `GET /api/v1/profiles/inventory/presets`, `POST /api/v1/profiles/inventory/presets/{id}/start`, `POST /api/v1/profiles/stop`, `GET /api/v1/status`, and the long-lived `GET /api/v1/data/stream` (newline-delimited JSON; `tagInventoryEvent` with `epcHex` or base64 `epc`, `antennaPort`, `peakRssiCdbm`; `gpiTransitionEvent`). The stream reconnects automatically. Unrecognised stream events appear under `rfid_unrecognised_events` in `/api/diagnostics`.

Tracking: a tag is *arrived* after `enter_reads` reads in a zone within `exit_timeout_s`; *left* when unread for `exit_timeout_s` (reason `gone`), or when another zone reads it `enter_reads` times after its own zone has been silent for `move_quiet_s` (reason `moved`, followed by `tag_enter` with `from_zone`). Zones come from antenna ports; unnamed antennas are "Antenna N" unless `ignore_unzoned`.

### Firing patterns

`settings.patterns`: `[{"id", "name", "continuous", "steps": [{"preset", "seconds", "pause_s"}]}]`. 2–20 steps, each a reader preset run for `seconds` (1–3600), then stopped, then `pause_s` (0–600) before the next step. `continuous` repeats the cycle until stopped; otherwise the pattern runs once and inventory ends. Names are unique; `id` is derived from the name if missing. Choose one with `preset: "pattern:<id>"` (also for auto-start), or over MQTT with `inventory` `args.pattern`.

While a pattern runs, `status.pattern` is `{id, name, continuous, steps, step, preset, phase ("scanning" | "pause"), remaining_s, cycle, error}` and `running` is true (also during pauses). "Gone after" is stretched to at least one full cycle plus 2 s, so tags read only by some steps aren't reported as leaving between steps. A step the reader refuses is reported in `pattern.error` and the pattern carries on; if the first step is refused, starting fails with the reader's message.

### Presets with a reporting interval

`GET /api/rfid/preset?id=<preset>` (rfid.view) → `{id, summary: {antennas, report_interval_s, search_mode, session, power_dbm}, raw, error}`, read from `GET /api/v1/profiles/inventory/presets/{id}` (fields matched loosely). Before starting a preset or pattern, Meridian reads the presets involved. If one reports each tag only once per `reportingIntervalSeconds` (> 0), arrival needs 1 report and "gone after" is at least 2 × interval + 2 s while it runs (`status.effective` shows the values in force). Starting a preset first stops any running one.
