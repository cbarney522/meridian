# Meridian

Meridian R700 App — a configurable on-reader application (CAP) for Impinj R700 readers.
Technicians configure hardware, rules, integrations and the kiosk through a role-based web UI
served by the reader itself.

This repository currently holds the **web UI and a mock server**, so the UI can be designed and
tested without a reader. When the UI is settled, `app/` moves into the CAP unchanged and a real
server implements the same API (see [api.md](api.md)).

## Run it

```sh
python3 dev/mock_server.py                  # then open http://127.0.0.1:8080
python3 dev/mock_server.py --host 0.0.0.0   # reachable from a tablet on your LAN
```

Python 3.8+ standard library only — nothing to install.

Tests: `python3 -m unittest discover tests`

From WSL, `127.0.0.1:8080` opens in your Windows browser directly. To reach it from a tablet,
run with `--host 0.0.0.0` and forward the port from Windows
(`netsh interface portproxy add v4tov4 listenport=8080 connectport=8080 connectaddress=<WSL IP>`),
or run the mock server with Windows Python instead.

The **View as** selector in the top bar (mock server only) switches roles so you can see what each
role sees. Hidden menus are cosmetic; the server checks permissions on every API call, and typing a
hidden page's address shows "No access".

## Layout

```
app/                     → becomes cap/app/ in the CAP
  defs/roles.json        roles and permissions (single source of truth)
  defs/menu.json         navigation; each page names the permission it needs
  core/access.py         role/permission/menu logic shared by mock and real server
  core/usb.py            USB discovery from sysfs + port-assignment checks (runs on the reader)
  core/badges.py         key events -> scan strings, scan -> badge ID normalization
  core/storage.py        data-drive mount lookup, capacity, warnings
  www/                   the UI: plain HTML/CSS/JS ES modules, no build step
    index.html
    css/meridian.css
    js/app.js            shell: session, menu, router, breadcrumb, live events
    js/menu.js           menu bar behaviour
    js/api.js            API wrapper + safe DOM builder (h, fill)
    js/ui.js             shared pieces: toast, save bar, chips, byte formatting
    js/pages/*.js        one module per page
dev/mock_server.py       simulated reader for UI development
dev/fake_sysfs.py        builds a fake sysfs USB tree (realistic R700 setup) for the mock and tests
tests/                   unit tests
api.md                   the API contract the real server must implement
```

## Conventions (so moving into the CAP is painless)

- No CDNs or external fonts — readers may be offline and the CSP blocks them.
- No inline `<script>`, `onclick=` or `style="..."`; the CSP forbids them. Set styles through
  `h(..., { style: { width: "50%" } })`.
- Relative URLs only (`api/status`, not `/api/status` or a full URL).
- Data from the reader goes into the page through `textContent` (the `h()` helper), never `innerHTML`.
- Adding a page: add it to `menu.json` with a permission, create `js/pages/<name>.js` exporting
  `render(root, ctx)` (optionally returning a cleanup function), and register it in `PAGES` in `app.js`.
  Until then the menu entry shows a placeholder.

## Menu behaviour

Dropdowns open on mouse hover (150 ms delay, 300 ms grace on leaving) **and** on click/tap, so
the UI works on touch screens. Clicking a hover-opened menu pins it. Keyboard: ←/→ between menus,
↓ to open, ↑/↓ within, Esc to close. Two levels only: second-level items are grouped inside one
panel, with no cascading flyouts.
