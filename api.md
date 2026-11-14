# Meridian API contract

The UI talks only to these endpoints. `dev/mock_server.py` implements them with simulated data;
the CAP's server must implement them identically. All responses are JSON unless noted.
Errors: `{"error": "<code>", ...}` with HTTP 400 / 403 / 404.

Every endpoint except `/api/session` checks a permission server-side. A `403` body includes
`"needs": "<permission>"`.

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
server returns `dev_mode: false`.

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

All events also carry `id`, `type`, `ts` (ISO 8601 local time).

## Mock only

### `POST /api/dev/usb/toggle` `{"port": "1-1.2"}`
Simulates unplugging / replugging a port (and anything behind it). `GET /api/usb/devices` also
returns `dev_unplugged` in the mock. Not implemented by the real server.

### `POST /api/dev/role` `{"role": "supervisor"}`
Switches the mock server's current role. Not implemented by the real server.
