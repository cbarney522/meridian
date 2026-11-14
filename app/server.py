#!/usr/bin/env python3
"""Meridian server — runs inside the CAP on the R700.

Serves the web UI (app/www) over HTTPS and implements the API in api.md with real data:
USB devices from sysfs, badge readers from /dev/input, the data drive from /proc/mounts,
settings and accounts in rw_dir. Parts not built yet (RFID engine, MQTT, site list, drive
eject/format) answer 501 with an explanation.

Standard library only.

    python3 server.py --data-dir /cust/rw_dir                       # on the reader (via ./start)
    python3 server.py --data-dir /tmp/m --sysfs /tmp/fake/sys --insecure-http --port 8080   # bench
"""
import argparse
import base64
import collections
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from core import badges, storage, usb  # noqa: E402
from core.access import Access  # noqa: E402
from core.config import Store  # noqa: E402
from core.inputs import BadgeListener, UsbWatcher  # noqa: E402
from core.tls import ensure_tls  # noqa: E402

WWW_DIR = os.path.join(APP_DIR, "www")
MAX_BODY = 64 * 1024
log = logging.getLogger("meridian")

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".json": "application/json"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
STORAGE_DEFAULTS = {"retention_days_events": 90, "retention_days_logs": 30, "alert_free_pct": 10}


def app_version():
    try:
        with open(os.path.join(APP_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "dev"


def read_first(path, conv=str):
    try:
        with open(path, encoding="utf-8") as f:
            return conv(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------- live event bus (SSE)
class Bus:
    def __init__(self, size=500):
        self.cond = threading.Condition()
        self.events = collections.deque(maxlen=size)
        self.seq = 0

    def emit(self, etype, **data):
        with self.cond:
            self.seq += 1
            self.events.append({"id": self.seq, "type": etype, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **data})
            self.cond.notify_all()

    def since(self, last, timeout):
        with self.cond:
            if not any(e["id"] > last for e in self.events):
                self.cond.wait(timeout)
            return [e for e in self.events if e["id"] > last]


# ---------------------------------------------------------------- application state
class App:
    def __init__(self, args):
        self.args = args
        self.access = Access()
        self.store = Store(args.data_dir)
        self.store.ensure_default_account()
        self.bus = Bus()
        self.sysfs = args.sysfs
        self.badges = None
        self.failed = {}            # ip -> (count, last) login throttling
        self.started = time.time()

    def start_watchers(self):
        UsbWatcher(self.sysfs, self.bus.emit).start()
        if not self.args.no_badges:
            self.badges = BadgeListener(self.sysfs, self.args.dev_root, self.usb_assignments,
                                        self.badge_settings, self.bus.emit)
            self.badges.start()

    # -- settings sections
    def usb_assignments(self):
        return self.store.get("usb", {})[1]

    def badge_settings(self):
        return dict(badges.DEFAULT_SETTINGS, **self.store.get("badges", badges.DEFAULT_SETTINGS)[1])

    # -- USB
    def usb_snapshot(self):
        rev, assign = self.store.get("usb", {})
        rows = usb.evaluate(usb.scan(self.sysfs), assign)
        present = [r for r in rows if r["status"] != "missing"]
        return {
            "revision": rev, "devices": rows,
            "roles": [{"id": k, "label": v["label"], "expects": v["expects"], "single": v["single"]} for k, v in usb.ROLES.items()],
            "summary": {
                "devices": sum(1 for r in present if r["type"] != "hub"),
                "hubs": sum(1 for r in present if r["type"] == "hub"),
                "assigned": sum(1 for r in rows if r["role"] and r["role"] != "ignore"),
                "problems": sum(1 for r in rows if r["status"] in ("missing", "mismatch", "changed")),
                "unassigned": sum(1 for r in rows if r["status"] == "unassigned"),
                "power_ma": sum(r["max_power_ma"] for r in present),
            },
        }

    def usb_save(self, revision, wanted):
        if not isinstance(wanted, dict):
            return 400, {"error": "invalid", "details": ["assignments must be an object"]}
        cur_rev, old_all = self.store.get("usb", {})
        if revision != cur_rev:
            return 409, {"error": "conflict", "revision": cur_rev}
        present = {d["port"]: d for d in usb.scan(self.sysfs)}
        new = {}
        for port, role in wanted.items():
            if not role:
                continue
            old = old_all.get(port)
            if old and old.get("role") == role:
                new[port] = old
                continue
            d = present.get(port)
            rec = {"role": role}
            if d:
                rec.update(vid=d["vid"], pid=d["pid"], product=d["product"])
            new[port] = {k: v for k, v in rec.items() if v is not None}
        errors = usb.validate_assignments(new)
        if errors:
            return 400, {"error": "invalid", "details": errors}
        rev = self.store.put("usb", revision, new)
        if rev is None:
            return 409, {"error": "conflict"}
        return 200, {"ok": True, "revision": rev}

    # -- badges
    def badges_snapshot(self):
        rev, _ = self.store.get("badges", badges.DEFAULT_SETTINGS)
        rows = {r["port"]: r for r in usb.evaluate(usb.scan(self.sysfs), self.usb_assignments())}
        st = self.badges.status() if self.badges else {"stats": {}, "errors": {}, "listening": {}}
        assign = self.usb_assignments()
        readers = []
        for role in ("badge_in", "badge_out"):
            port = next((p for p, a in assign.items() if a["role"] == role), None)
            r = rows.get(port) if port else None
            s = st["stats"].get(port, {})
            readers.append({
                "role": role, "label": usb.ROLES[role]["label"], "port": port, "assigned": port is not None,
                "status": r["status"] if r else ("missing" if port else "unassigned"),
                "product": r["product"] if r else None, "nodes": r["nodes"] if r else [],
                "last_scan": s.get("last"), "scans_today": s.get("scans", 0), "errors_today": s.get("errors", 0),
                "listener_error": st["errors"].get(port),
            })
        others = [{"port": p, "product": r["product"], "role": r["role"], "nodes": r["nodes"],
                   "listener_error": st["errors"].get(p)}
                  for p, r in rows.items()
                  if r["type"] == "hid-keyboard" and r["role"] not in ("badge_in", "badge_out") and r["status"] != "missing"]
        return {"revision": rev, "settings": self.badge_settings(), "readers": readers, "other_keyboards": others,
                "listener": "disabled" if not self.badges else "running"}

    def badges_save(self, revision, new):
        merged = dict(badges.DEFAULT_SETTINGS, **(new if isinstance(new, dict) else {}))
        errors = badges.validate_settings(merged)
        if errors:
            return 400, {"error": "invalid", "details": errors}
        rev = self.store.put("badges", revision, {k: merged[k] for k in badges.DEFAULT_SETTINGS})
        if rev is None:
            return 409, {"error": "conflict", "revision": self.store.get("badges", {})[0]}
        return 200, {"ok": True, "revision": rev}

    # -- storage
    def storage_snapshot(self):
        rev, settings = self.store.get("storage", STORAGE_DEFAULTS)
        assign = self.usb_assignments()
        port = next((p for p, a in assign.items() if a["role"] == "storage"), None)
        dev = {d["port"]: d for d in usb.scan(self.sysfs)}.get(port) if port else None
        nodes = dev["nodes"] if dev else []
        mount = storage.find_mount(nodes, self.args.proc_mounts) if nodes else None
        cap = storage.capacity(mount["mount_point"]) if mount else None
        usage = []
        if cap:
            mdir = os.path.join(mount["mount_point"], "meridian")
            mine = storage.dir_size(mdir)
            usage = [{"id": "meridian", "label": "Meridian data", "path": "meridian/", "bytes": mine},
                     {"id": "other", "label": "Other files", "path": "/", "bytes": max(0, cap["used"] - mine)}]
        internal = storage.capacity(self.args.data_dir) or {"size": None, "used": None, "free": None}
        return {
            "revision": rev, "assigned_port": port,
            "drive": {"present": dev is not None, "port": port,
                      "product": dev["product"] if dev else None, "manufacturer": dev["manufacturer"] if dev else None,
                      "serial": dev["serial"] if dev else None, "node": nodes[0] if nodes else None,
                      "mounted": bool(mount), "mount": mount, "capacity": cap},
            "usage": usage,
            "internal": {"path": self.args.data_dir, **internal, "buffering": not mount},
            "settings": settings,
            "warnings": storage.warnings_for(mount, cap, settings["alert_free_pct"])
                        + ([] if mount or not dev else ["The drive is plugged in but not mounted. "
                                                        "Automatic mounting arrives with the engine."]),
        }

    def storage_save(self, revision, new):
        if not isinstance(new, dict):
            return 400, {"error": "invalid", "details": ["settings must be an object"]}
        limits = {"retention_days_events": (1, 3650), "retention_days_logs": (1, 3650), "alert_free_pct": (1, 50)}
        errors, out = [], {}
        for k, (lo, hi) in limits.items():
            v = new.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                errors.append(f"{k}: whole number {lo}–{hi}")
            out[k] = v
        if errors:
            return 400, {"error": "invalid", "details": errors}
        rev = self.store.put("storage", revision, out)
        if rev is None:
            return 409, {"error": "conflict"}
        return 200, {"ok": True, "revision": rev}

    # -- status
    def status(self):
        assign = self.usb_assignments()
        rows = {r["port"]: r for r in usb.evaluate(usb.scan(self.sysfs), assign)}

        def role_ok(role):
            port = next((p for p, a in assign.items() if a["role"] == role), None)
            return bool(port and rows.get(port, {}).get("status") in ("ok", "changed"))

        temp = read_first("/sys/class/thermal/thermal_zone0/temp", int)
        drive = self.storage_snapshot()["drive"]
        return {
            "reader": {"name": socket.gethostname(), "serial": None, "site": None, "firmware": None,
                       "app_version": app_version(), "profile": None,
                       "uptime_s": int(read_first("/proc/uptime", float) or 0), "ntp_synced": None,
                       "temp_c": round(temp / 1000, 1) if temp else None},
            "rfid": {"available": False, "running": False, "read_rate": 0, "tags_in_field": 0, "zones": {}},
            "mqtt": {"available": False, "connected": False, "outbox_depth": 0},
            "usb": {"drive": drive["present"], "free_mb": (drive["capacity"]["free"] // 1048576) if drive["capacity"] else 0,
                    "badge_in": role_ok("badge_in"), "badge_out": role_ok("badge_out")},
            "session": None,
        }


APP = None


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Meridian"
    sys_version = ""
    account = None

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def send_json(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def not_built(self, what):
        self.send_json({"error": "not_implemented", "details": [f"{what} isn't available in this build yet."]}, 501)

    # -- auth: HTTP Basic against auth.json, with throttling
    def authenticate(self):
        ip = self.client_address[0]
        count, last = APP.failed.get(ip, (0, 0))
        if count >= 5 and time.time() - last < 30:
            self.send_json({"error": "locked", "details": ["Too many failed logins; wait 30 seconds."]}, 429)
            return False
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                acct = APP.store.authenticate(user, pw)
                if acct:
                    APP.failed.pop(ip, None)
                    self.account = acct
                    return True
            except (ValueError, UnicodeDecodeError):
                pass
            APP.failed[ip] = (count + 1, time.time())
        self.send_json({"error": "authentication required"}, 401,
                       {"WWW-Authenticate": 'Basic realm="Meridian", charset="UTF-8"'})
        return False

    def can(self, perm):
        return APP.access.can(self.account["role"], perm)

    def require(self, perm):
        if self.can(perm):
            return True
        self.send_json({"error": "forbidden", "needs": perm}, 403)
        return False

    def same_origin(self):
        # Browsers resend Basic credentials automatically, so refuse cross-site writes.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.send_json({"error": "cross-origin request refused"}, 403)
            return False
        return True

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.authenticate():
            return
        url = urlparse(self.path)
        p = url.path
        if not p.startswith("/api/"):
            return self.static(p)
        role = self.account["role"]
        if p == "/api/session":
            r = APP.access.roles.get(role, {"label": role})
            return self.send_json({
                "user": self.account["user"], "role": role, "role_label": r["label"],
                "permissions": sorted(APP.access.permissions_for(role)),
                "must_change_password": self.account["must_change"],
                "dev_mode": False, "roles": [],
                "reader": {"name": socket.gethostname(), "serial": None},
                "app_version": app_version(),
            })
        if p == "/api/menu":
            return self.send_json({"menus": APP.access.menu_for(role)})
        if p == "/api/route-check":
            route = parse_qs(url.query).get("route", [""])[0]
            if route not in APP.access.route_perms:
                return self.send_json({"error": "not_found"}, 404)
            if not APP.access.can_route(role, route):
                return self.send_json({"error": "forbidden", "needs": APP.access.route_perms[route]}, 403)
            return self.send_json({"ok": True, "perm": APP.access.route_perms[route]})
        routes = {
            "/api/status": ("dashboard.view", APP.status),
            "/api/usb/devices": ("hardware.view", APP.usb_snapshot),
            "/api/badges": ("hardware.view", APP.badges_snapshot),
            "/api/storage": ("hardware.view", APP.storage_snapshot),
        }
        if p in routes:
            perm, fn = routes[p]
            if self.require(perm):
                self.send_json(fn())
            return
        if p == "/api/site/readers":
            if self.require("site.view"):
                self.not_built("The site list (it needs MQTT)")
            return
        if p == "/api/events":
            if self.require("dashboard.view"):
                self.stream_events()
            return
        return self.send_json({"error": "not_found"}, 404)

    def do_PUT(self):
        if not self.authenticate() or not self.same_origin():
            return
        p = urlparse(self.path).path
        handlers = {
            "/api/usb/assignments": ("hardware.edit", APP.usb_save, "assignments"),
            "/api/badges/settings": ("hardware.edit", APP.badges_save, "settings"),
            "/api/storage/settings": ("hardware.edit", APP.storage_save, "settings"),
        }
        if p not in handlers:
            return self.send_json({"error": "not_found"}, 404)
        perm, fn, key = handlers[p]
        if not self.require(perm):
            return
        if not APP.store.writable:
            return self.send_json({"error": "read_only", "details": [f"{APP.store.dir} is not writable."]}, 503)
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad_request"}, 400)
        status, result = fn(body.get("revision"), body.get(key))
        if status == 200:
            log.info("%s saved %s (revision %s)", self.account["user"], p, result.get("revision"))
        return self.send_json(result, status)

    def do_POST(self):
        if not self.authenticate() or not self.same_origin():
            return
        p = urlparse(self.path).path
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad_request"}, 400)
        if p == "/api/account/password":
            err = APP.store.change_password(self.account["user"], body.get("current", ""), body.get("new", ""))
            if err:
                return self.send_json({"error": "invalid", "details": [err]}, 400)
            log.info("%s changed their password", self.account["user"])
            return self.send_json({"ok": True})
        if p == "/api/badges/preview":
            if not self.require("hardware.view"):
                return
            settings = body.get("settings") or APP.badge_settings()
            merged = dict(badges.DEFAULT_SETTINGS, **settings) if isinstance(settings, dict) else None
            errors = badges.validate_settings(merged) if merged else ["settings must be an object"]
            if errors:
                return self.send_json({"error": "invalid", "details": errors}, 400)
            raw = body.get("raw")
            if not isinstance(raw, str) or len(raw) > 128:
                return self.send_json({"error": "bad_request"}, 400)
            return self.send_json(badges.normalize(raw, merged))
        if p in ("/api/storage/eject", "/api/storage/mount", "/api/storage/format"):
            if self.require("hardware.edit"):
                self.not_built("Ejecting, mounting and formatting the drive")
            return
        return self.send_json({"error": "not_found"}, 404)

    def stream_events(self):
        last = self.headers.get("Last-Event-ID")
        last = int(last) if last and last.isdigit() else APP.bus.seq
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                pending = APP.bus.since(last, 15)
                if not pending:
                    self.wfile.write(b": keepalive\n\n")
                for e in pending:
                    self.wfile.write(f"id: {e['id']}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
                    last = e["id"]
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            return

    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.abspath(os.path.join(WWW_DIR, os.path.normpath(path.lstrip("/"))))
        if not full.startswith(os.path.abspath(WWW_DIR) + os.sep) or not os.path.isfile(full):
            return self.send_json({"error": "not_found"}, 404)
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Browsers that haven't accepted the self-signed certificate yet abort the TLS handshake,
        # and clients drop connections; those are normal and shouldn't fill the log with tracebacks.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            log.debug("connection from %s ended: %s", client_address[0], exc)
            return
        log.exception("error handling request from %s", client_address[0])


def main():
    global APP
    ap = argparse.ArgumentParser(description="Meridian server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("UI_PORT", "8443")))
    ap.add_argument("--bind", default=os.environ.get("UI_BIND", "0.0.0.0"))
    ap.add_argument("--data-dir", default=os.environ.get("CAP_DATA_DIR", os.path.join(os.path.dirname(APP_DIR), "rw_dir")))
    ap.add_argument("--sysfs", default=os.environ.get("MERIDIAN_SYSFS", "/sys"), help="sysfs root (tests use a fake tree)")
    ap.add_argument("--dev-root", default=os.environ.get("MERIDIAN_DEV_ROOT", ""), help="prefix for /dev paths (tests)")
    ap.add_argument("--proc-mounts", default="/proc/mounts")
    ap.add_argument("--no-badges", action="store_true", help="don't read badge readers")
    ap.add_argument("--insecure-http", action="store_true", help="plain HTTP (bench use only)")
    ap.add_argument("--log-level", default=os.environ.get("UI_LOG_LEVEL", "INFO"))
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    APP = App(args)
    APP.start_watchers()
    httpd = Server((args.bind, args.port), Handler)
    scheme = "http"
    if not args.insecure_http:
        cert, key = ensure_tls(args.data_dir, os.path.join(APP_DIR, "tls"))
        if not cert:
            log.critical("no TLS certificate (no openssl, no bundled cert); rebuild with build.sh or use --insecure-http")
            sys.exit(2)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"
    log.info("Meridian %s serving %s://%s:%d/  data=%s writable=%s sysfs=%s",
             app_version(), scheme, args.bind, args.port, args.data_dir, APP.store.writable, args.sysfs)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
