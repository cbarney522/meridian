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

All events also carry `id`, `type`, `ts` (ISO 8601 local time).

## Mock only

### `POST /api/dev/role` `{"role": "supervisor"}`
Switches the mock server's current role. Not implemented by the real server.
