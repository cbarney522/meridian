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
import collections
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from core import badges, storage, usb  # noqa: E402
from core.access import Access  # noqa: E402
from core.config import Store  # noqa: E402
from core.inputs import BadgeListener, UsbWatcher  # noqa: E402
from core.sessions import Sessions, Throttle  # noqa: E402
from core import accounts_api, netinfo, timeloc, mqtt_link, rfid  # noqa: E402
from core.readerapi import ApiError  # noqa: E402
from core.readerapi import ReaderApi  # noqa: E402
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
def load_event_types():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "defs", "events.json"), encoding="utf-8") as f:
        return json.load(f)["events"]


EVENT_TYPES = load_event_types()
REPLAY_EVENTS = 30          # recent events sent to a newly connected browser
READER_API_DEFAULTS = {"username": "root", "password": ""}
FACTORY_PASSWORD = "impinj"          # R700 factory default; tried only while no password is saved
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
        self.sessions = Sessions(idle=args.session_idle * 60, absolute=12 * 3600)
        self.throttle = Throttle(limit=5, window=300, lockout=60)
        self.write_checks = {}      # mount point -> (time, error or None); re-tested every 60 s
        self.started = time.time()

    def start_watchers(self):
        UsbWatcher(self.sysfs, self.bus.emit).start()
        threading.Thread(target=self._reader_info_loop, daemon=True, name="reader-info").start()
        self.rfid = rfid.RfidEngine(lambda: self.reader_api()[0], self.store.get("rfid", rfid.DEFAULTS)[1], self.bus.emit)
        self.rfid.start_threads()
        self.mqtt = mqtt_link.MqttLink(self.mqtt_identity, self.mqtt_command, self.bus.emit)
        threading.Thread(target=self._mqtt_start, daemon=True, name="mqtt-start").start()
        threading.Thread(target=self._mqtt_bridge, daemon=True, name="mqtt-bridge").start()
        if not self.args.no_badges:
            self.badges = BadgeListener(self.sysfs, self.args.dev_root, self.usb_assignments,
                                        self.badge_settings, self.bus.emit)
            self.badges.start()

    # -- reader facts from the Impinj REST API (refreshed in the background every 60 s)
    def reader_api(self, username=None, password=None):
        _, cfg = self.store.get("reader_api", READER_API_DEFAULTS)
        user = username if username is not None else cfg.get("username") or "root"
        pw = password if password is not None else cfg.get("password") or FACTORY_PASSWORD
        return ReaderApi(self.args.reader_api, user, pw), not (password or cfg.get("password"))

    def refresh_reader_info(self):
        api, using_default = self.reader_api()
        r = api.fetch()
        r["using_default"] = using_default
        self.reader_cache = r
        return r

    def _reader_info_loop(self):
        self.reader_wake = threading.Event()
        while True:
            try:
                self.refresh_reader_info()
            except Exception:
                log.exception("reader info refresh failed")
            self.reader_wake.wait(60)
            self.reader_wake.clear()

    def reader_info(self):
        r = getattr(self, "reader_cache", None) or {"ok": False, "error": "not read yet", "items": {}, "raw": {},
                                                     "fetched": None, "using_default": True}
        net = netinfo.summary()
        _, cfg = self.store.get("reader_api", READER_API_DEFAULTS)
        return {
            "hostname": socket.gethostname(),
            "items": r["items"],
            "api": {"ok": r["ok"], "error": r["error"], "fetched": r["fetched"], "using_default": r.get("using_default"),
                    "username": cfg.get("username") or "root", "password_set": bool(cfg.get("password"))},
            "net": net,
            "time": timeloc.now_in(self.site().get("timezone")).strftime("%Y-%m-%dT%H:%M:%S%z"),
            "timezone": timeloc.now_in(self.site().get("timezone")).strftime("%Z"),
            "clock_24h": self.site().get("clock_24h", False),
            "uptime_s": int(read_first("/proc/uptime", float) or 0),
            "app_version": app_version(),
        }

    def reader_api_save(self, revision, body):
        if not isinstance(body, dict):
            return 400, {"error": "invalid", "details": ["settings must be an object"]}
        _, cur = self.store.get("reader_api", READER_API_DEFAULTS)
        user = body.get("username", cur.get("username"))
        if not isinstance(user, str) or not 0 < len(user) <= 64:
            return 400, {"error": "invalid", "details": ["Enter the reader's user name."]}
        pw = cur.get("password", "")
        if body.get("clear_password"):
            pw = ""
        elif isinstance(body.get("password"), str) and body["password"]:
            pw = body["password"][:256]
        rev = self.store.put("reader_api", revision, {"username": user, "password": pw})
        if rev is None:
            return 409, {"error": "conflict"}
        if getattr(self, "reader_wake", None):
            self.reader_wake.set()          # re-read now with the new login
        return 200, {"ok": True, "revision": rev}

    # -- MQTT
    MQTT_SKIP = {"mqtt_state", "identify"}           # bus events not forwarded to the broker
    MQTT_RENAME = {"badge_raw": "badge"}

    def mqtt_identity(self):
        it = (getattr(self, "reader_cache", None) or {}).get("items", {})
        site = self.site()
        return {"reader": it.get("serial") or socket.gethostname(), "site": site.get("site_id") or None,
                "name": site.get("site_name") or socket.gethostname(), "location": site.get("location") or None,
                "version": app_version(), "firmware": it.get("firmware"), "ip": netinfo.summary()["ipv4"],
                "uptime_s": int(read_first("/proc/uptime", float) or 0)}

    def mqtt_command(self, name, args):
        if name == "ping":
            return {"pong": True, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if name == "identify":
            secs = max(1, min(int(args.get("seconds", 10) or 10), 120))
            self.bus.emit("identify", seconds=secs, source="mqtt")
            return {"identifying_for_s": secs}
        if name == "status":
            s = self.status()
            return {k: s[k] for k in ("reader", "usb", "mqtt")}
        if name == "info":
            return self.reader_info()
        if name == "inventory":
            action = str(args.get("action", "")).lower()
            try:
                if action == "start":
                    self.rfid.start_inventory(("pattern:" + str(args["pattern"])) if args.get("pattern") else (args.get("preset") or None))
                elif action == "stop":
                    self.rfid.stop_inventory()
                elif action != "status":
                    raise ValueError("args.action must be start, stop or status")
            except ApiError as e:
                raise ValueError(f"reader refused: {e}") from None
            st = self.rfid.status()
            return {k: st[k] for k in ("running", "active_preset", "pattern", "tags_in_field", "read_rate", "zones")}
        raise ValueError(f"unknown command '{name}' (try ping, identify, status, info or inventory)")

    def mqtt_settings(self):
        return self.store.get("mqtt", mqtt_link.DEFAULTS)

    def _mqtt_start(self):
        # give the reader API a moment so the first connection already uses the serial number
        for _ in range(20):
            if getattr(self, "reader_cache", None):
                break
            time.sleep(0.5)
        self.mqtt.apply(self.mqtt_settings()[1])

    def _mqtt_bridge(self):
        last = self.bus.seq
        while True:
            for ev in self.bus.since(last, 30):
                last = max(last, ev["id"])
                if ev["type"] in self.MQTT_SKIP:
                    continue
                data = {k: v for k, v in ev.items() if k not in ("id", "type")}
                self.mqtt.publish_event(self.MQTT_RENAME.get(ev["type"], ev["type"]), data)

    def mqtt_save(self, revision, body):
        _, cur = self.mqtt_settings()
        clean, errors = mqtt_link.validate(body, cur)
        if errors:
            return 400, {"error": "invalid", "details": errors}
        rev = self.store.put("mqtt", revision, clean)
        if rev is None:
            return 409, {"error": "conflict"}
        threading.Thread(target=self.mqtt.apply, args=(clean,), daemon=True).start()
        return 200, {"ok": True, "revision": rev}

    # -- time & location
    def site(self):
        return self.store.get("site", timeloc.SITE_DEFAULTS)[1]

    def time_status(self):
        site = self.site()
        tz = site.get("timezone") or "UTC"
        local = timeloc.now_in(tz)
        sun = (timeloc.sun_times(site["latitude"], site["longitude"], tz)
               if site.get("latitude") is not None and site.get("longitude") is not None else None)
        api_raw = (getattr(self, "reader_cache", None) or {}).get("raw", {})
        ntp_cfg = next((api_raw[k] for k in ("ntp", "time") if isinstance(api_raw.get(k), dict)
                        and "_error" not in api_raw[k]), None)
        return {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "local": local.isoformat(timespec="seconds"), "timezone": tz,
                "tz_abbrev": local.strftime("%Z"), "utc_offset": local.strftime("%z"),
                "tz_database": timeloc.zone("America/New_York") is not None,
                "tz_path": list(getattr(timeloc.zoneinfo, "TZPATH", ())),
                "ntp": timeloc.ntp_status(), "reader_ntp_config": ntp_cfg, "sun": sun}

    # -- RFID
    def rfid_view(self, refresh=False):
        rev, cfg = self.store.get("rfid", rfid.DEFAULTS)
        if refresh or not self.rfid.presets_cache:
            self.rfid.refresh_presets()
            self.rfid.refresh_status()
        return {"revision": rev, "settings": cfg, "status": self.rfid.status(), "presets": self.rfid.presets_cache}

    def rfid_save(self, revision, body):
        _, cur = self.store.get("rfid", rfid.DEFAULTS)
        clean, errors = rfid.validate(body, cur)
        if errors:
            return 400, {"error": "invalid", "details": errors}
        rev = self.store.put("rfid", revision, clean)
        if rev is None:
            return 409, {"error": "conflict"}
        self.rfid.configure(clean)
        return 200, {"ok": True, "revision": rev}

    def _rfid_brief(self):
        r = getattr(self, "rfid", None)
        if not r:
            return {"available": False, "running": False, "read_rate": 0, "tags_in_field": 0, "zones": {}}
        st = r.status()
        return {k: st[k] for k in ("available", "running", "read_rate", "tags_in_field", "zones", "active_preset",
                                   "stream_connected", "reader_error")}

    def _mqtt_brief(self):
        m = getattr(self, "mqtt", None)
        if not m:
            return {"available": False, "connected": False, "outbox_depth": 0}
        st = m.status()
        return {"available": True, "enabled": st["enabled"], "connected": st["connected"],
                "outbox_depth": st["queued"], "broker": st["broker"], "error": st["error"]}

    def site_save(self, revision, body):
        clean, errors = timeloc.validate_site(body)
        if errors:
            return 400, {"error": "invalid", "details": errors}
        rev = self.store.put("site", revision, clean)
        if rev is None:
            return 409, {"error": "conflict"}
        return 200, {"ok": True, "revision": rev}

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
                "location": r.get("location") if r else None,
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
        write_error = None
        if mount:
            t, err = self.write_checks.get(mount["mount_point"], (0, None))
            if time.time() - t > 60:
                err = storage.write_test(mount["mount_point"])
                self.write_checks[mount["mount_point"]] = (time.time(), err)
                if err:
                    log.warning("data drive not writable: %s", err)
            write_error = err
        usage = []
        if cap:
            mdir = os.path.join(mount["mount_point"], "meridian")
            mine = storage.dir_size(mdir)
            usage = [{"id": "meridian", "label": "Meridian data", "path": "meridian/", "bytes": mine,
                      "browse": {"scope": "meridian", "path": ""}},
                     {"id": "other", "label": "Other files", "path": "/", "bytes": max(0, cap["used"] - mine),
                      "browse": {"scope": "drive", "path": ""}}]
        internal = storage.capacity(self.args.data_dir) or {"size": None, "used": None, "free": None}
        return {
            "revision": rev, "assigned_port": port,
            "drive": {"present": dev is not None, "port": port,
                      "product": dev["product"] if dev else None, "manufacturer": dev["manufacturer"] if dev else None,
                      "serial": dev["serial"] if dev else None, "node": nodes[0] if nodes else None,
                      "location": dev.get("location") if dev else None,
                      "mounted": bool(mount), "mount": mount, "capacity": cap,
                      "writable": (write_error is None) if mount else None, "write_error": write_error},
            "usage": usage,
            # FW 10.4 runs CAPs as the non-root 'cap' user: the reader mounts drives itself and won't
            # let a CAP unmount or format them (verified on hardware, Nov 2026).
            "can_eject": False, "can_format": False,
            "internal": {"path": self.args.data_dir, **internal, "buffering": not mount},
            "settings": settings,
            "warnings": storage.warnings_for(mount, cap, settings["alert_free_pct"])
                        + ([f"Meridian can't write to the drive: {write_error}"] if write_error else [])
                        + ([] if mount or not dev else ["The drive is plugged in but the reader hasn't mounted it. "
                                                        "Try unplugging it and plugging it back in."]),
        }

    def storage_browse(self, scope, rel, can_drive):
        """Read-only listing of one folder on the data drive (Meridian's folder, or the whole drive)."""
        assign = self.usb_assignments()
        port = next((p for p, a in assign.items() if a["role"] == "storage"), None)
        dev = {d["port"]: d for d in usb.scan(self.sysfs)}.get(port) if port else None
        mount = storage.find_mount(dev["nodes"], self.args.proc_mounts) if dev and dev["nodes"] else None
        if not mount:
            return 409, {"error": "no_drive", "details": ["No mounted data drive."]}
        if scope == "drive" and not can_drive:
            return 403, {"error": "forbidden", "details": ["Browsing the whole drive needs the hardware.edit or dev.tools permission."]}
        base = mount["mount_point"] if scope == "drive" else os.path.join(mount["mount_point"], "meridian")
        if scope != "drive" and not os.path.isdir(base):
            return 200, {"scope": scope, "exists": False, "path": "", "entries": [], "total": 0, "truncated": False,
                         "files_bytes": 0, "can_browse_drive": can_drive}
        try:
            r = storage.list_dir(base, rel)
        except storage.BrowseError as e:
            return 400, {"error": "invalid", "details": [str(e)]}
        return 200, dict(r, scope=scope, exists=True, can_browse_drive=can_drive)

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

    def ntp_synced(self):
        """True/False from chronyc if available (cached 60 s); None if unknown."""
        t, val = getattr(self, "_ntp", (0, None))
        if time.time() - t < 60:
            return val
        val = None
        try:
            r = subprocess.run(["chronyc", "-n", "tracking"], capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                if line.startswith("Leap status"):
                    val = line.split(":", 1)[1].strip() == "Normal"
        except (OSError, subprocess.SubprocessError):
            pass
        self._ntp = (time.time(), val)
        return val

    # -- diagnostics (developer tools)
    def diagnostics(self, log_bytes=65536):
        def read(path, tail=None):
            try:
                with open(path, "rb") as f:
                    if tail:
                        f.seek(0, os.SEEK_END)
                        f.seek(max(0, f.tell() - tail))
                    return f.read().decode("utf-8", "replace")
            except OSError as e:
                return f"({e.strerror}: {path})"
        d = self.args.data_dir
        return {"diag": read(os.path.join(d, "diag.txt")),
                "log": read(os.path.join(d, "log", "meridian.log"), log_bytes),
                "badge_listener": self.badges.status() if self.badges else None,
                "reader_api_raw": (getattr(self, "reader_cache", None) or {}).get("raw"),
                "rfid": {k: v for k, v in self.rfid.status().items() if k != "tags"} if getattr(self, "rfid", None) else None,
                "rfid_reader_status_raw": getattr(self, "rfid", None) and self.rfid.reader.get("raw"),
                "rfid_unrecognised_events": list(self.rfid.last_other) if getattr(self, "rfid", None) else None,
                "app_version": app_version(), "data_dir": d}

    # -- status
    def status(self):
        assign = self.usb_assignments()
        rows = {r["port"]: r for r in usb.evaluate(usb.scan(self.sysfs), assign)}

        def role_ok(role):
            port = next((p for p, a in assign.items() if a["role"] == role), None)
            return bool(port and rows.get(port, {}).get("status") in ("ok", "changed"))

        temp = read_first("/sys/class/thermal/thermal_zone0/temp", int)
        drive = self.storage_snapshot()["drive"]
        info = self.reader_info()
        it = info["items"]
        return {
            "reader": {"name": self.site().get("site_name") or socket.gethostname(), "hostname": socket.gethostname(),
                       "serial": it.get("serial"), "site": self.site().get("site_id") or None,
                       "clock_24h": info["clock_24h"], "firmware": it.get("firmware"),
                       "app_version": app_version(), "profile": None,
                       "description": it.get("description"), "region": it.get("region"),
                       "rfid_interface": it.get("rfid_interface"), "sku": it.get("sku"),
                       "ipv4": info["net"]["ipv4"], "ipv6": info["net"]["ipv6"], "mac": info["net"]["mac"],
                       "time": info["time"], "timezone": info["timezone"],
                       "api_ok": info["api"]["ok"], "api_error": info["api"]["error"],
                       "uptime_s": int(read_first("/proc/uptime", float) or 0), "ntp_synced": self.ntp_synced(),
                       "temp_c": round(temp / 1000, 1) if temp else None},
            "rfid": self._rfid_brief(),
            "mqtt": self._mqtt_brief(),
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
    token = None
    COOKIE = "meridian_session"

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

    # -- auth: session cookie issued by POST /api/login
    def session_token(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        m = c.get(self.COOKIE)
        return m.value if m else None

    def authenticate(self):
        token = self.session_token()
        user = APP.sessions.user_for(token)
        acct = APP.store.account(user) if user else None
        if acct:
            self.account, self.token = acct, token
            return True
        if user:                                   # account deleted since sign-in
            APP.sessions.drop(token)
        self.send_json({"error": "not_signed_in", "details": ["Please sign in."]}, 401)
        return False

    def cookie(self, value, max_age):
        secure = "" if APP.args.insecure_http else "; Secure"
        return f"{self.COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def login(self):
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad_request"}, 400)
        user, pw = body.get("user"), body.get("password")
        if not isinstance(user, str) or not isinstance(pw, str) or not user or len(user) > 64 or len(pw) > 256:
            return self.send_json({"error": "invalid", "details": ["Enter your user name and password."]}, 400)
        ip, ukey = "ip:" + self.client_address[0], "user:" + user.lower()
        wait = APP.throttle.locked(ip, ukey)
        if wait:
            return self.send_json({"error": "locked", "details": [f"Too many failed sign-ins. Try again in {wait} seconds."]}, 429)
        acct = APP.store.authenticate(user, pw)
        if not acct:
            APP.throttle.fail(ip, ukey)
            log.warning("failed sign-in for %r from %s", user, self.client_address[0])
            return self.send_json({"error": "bad_credentials", "details": ["Wrong user name or password."]}, 401)
        APP.throttle.success(ip, ukey)
        APP.store.record_login(user)
        token = APP.sessions.create(user)
        log.info("%s signed in from %s", user, self.client_address[0])
        return self.send_json({"ok": True, "must_change_password": acct["must_change"]}, 200,
                              {"Set-Cookie": self.cookie(token, 12 * 3600)})

    def logout(self):
        token = self.session_token()
        if token:
            APP.sessions.drop(token)
        return self.send_json({"ok": True}, 200, {"Set-Cookie": self.cookie("", 0)})

    def can(self, perm):
        return APP.access.can(self.account["role"], perm)

    def require(self, perm):
        if self.can(perm):
            return True
        self.send_json({"error": "forbidden", "needs": perm}, 403)
        return False

    def same_origin(self):
        # Defence in depth next to SameSite=Strict: refuse writes sent from another site.
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
        url = urlparse(self.path)
        p = url.path
        if not p.startswith("/api/"):
            return self.static(p)          # page files are public; all data comes from the API
        if p == "/api/login-info":        # shown on the sign-in page
            return self.send_json({"reader": socket.gethostname(), "app_version": app_version()})
        if not self.authenticate():
            return
        role = self.account["role"]
        if p == "/api/session":
            r = APP.access.roles.get(role, {"label": role})
            return self.send_json({
                "user": self.account["user"], "role": role, "role_label": r["label"],
                "permissions": sorted(APP.access.permissions_for(role)),
                "must_change_password": self.account["must_change"],
                "dev_mode": False, "roles": [],
                "reader": {"name": APP.site().get("site_name") or socket.gethostname(), "serial": None},
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
        if p == "/api/accounts" or p.startswith("/api/accounts/"):
            return self.accounts("GET")
        if p == "/api/dev/diagnostics":
            if self.require("dev.tools"):
                self.send_json(APP.diagnostics())
            return
        if p == "/api/rfid":
            if self.require("rfid.view"):
                self.send_json(APP.rfid_view(refresh="refresh=1" in (urlparse(self.path).query or "")))
            return
        if p == "/api/mqtt":
            if self.require("integrations.view"):
                rev, cfg = APP.mqtt_settings()
                self.send_json({"revision": rev, "settings": mqtt_link.public(cfg), "status": APP.mqtt.status(),
                                "event_types": EVENT_TYPES})
            return
        if p == "/api/time":
            if self.require("system.view"):
                self.send_json(APP.time_status())
            return
        if p == "/api/site-settings":
            if self.require("system.view"):
                rev, cfg = APP.store.get("site", timeloc.SITE_DEFAULTS)
                self.send_json({"revision": rev, "settings": cfg, "zones": timeloc.available_zones(),
                                "common_zones": timeloc.COMMON_ZONES})
            return
        if p == "/api/reader/info":
            if self.require("system.view"):
                self.send_json(APP.reader_info())
            return
        if p == "/api/reader/api-settings":
            if self.require("system.view"):
                rev, cfg = APP.store.get("reader_api", READER_API_DEFAULTS)
                self.send_json({"revision": rev, "username": cfg.get("username") or "root",
                                "password_set": bool(cfg.get("password"))})
            return
        if p == "/api/storage/browse":
            if self.require("hardware.view"):
                q = parse_qs(url.query)
                status, result = APP.storage_browse(q.get("scope", ["meridian"])[0], q.get("path", [""])[0],
                                                    self.can("hardware.edit") or self.can("dev.tools"))
                self.send_json(result, status)
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
        if not self.same_origin() or not self.authenticate():
            return
        p = urlparse(self.path).path
        if p.startswith("/api/accounts/"):
            return self.accounts("PUT")
        if p == "/api/reader/api-settings":
            if not self.require("system.edit"):
                return
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, 400)
            status, result = APP.reader_api_save(body.get("revision"), body)
            return self.send_json(result, status)
        if p == "/api/rfid":
            if not self.require("rfid.edit"):
                return
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, 400)
            status, result = APP.rfid_save(body.get("revision"), body.get("settings"))
            return self.send_json(result, status)
        if p == "/api/mqtt":
            if not self.require("integrations.edit"):
                return
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, 400)
            status, result = APP.mqtt_save(body.get("revision"), body.get("settings"))
            return self.send_json(result, status)
        if p == "/api/site-settings":
            if not self.require("system.edit"):
                return
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, 400)
            status, result = APP.site_save(body.get("revision"), body.get("settings"))
            return self.send_json(result, status)
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
        p = urlparse(self.path).path
        if not self.same_origin():
            return
        if p == "/api/login":
            return self.login()
        if p == "/api/logout":
            return self.logout()
        if not self.authenticate():
            return
        if p == "/api/accounts" or p.startswith("/api/accounts/"):
            return self.accounts("POST")
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad_request"}, 400)
        if p == "/api/account/password":
            err = APP.store.change_password(self.account["user"], body.get("current", ""), body.get("new", ""))
            if err:
                return self.send_json({"error": "invalid", "details": [err]}, 400)
            APP.sessions.drop_user(self.account["user"], keep=self.token)   # sign out their other sessions
            log.info("%s changed their password", self.account["user"])
            return self.send_json({"ok": True})
        if p in ("/api/rfid/start", "/api/rfid/stop"):
            if not self.require("rfid.edit"):
                return
            try:
                if p.endswith("start"):
                    pid = APP.rfid.start_inventory(body.get("preset") or None)
                    log.info("%s started inventory (%s)", self.account["user"], pid)
                else:
                    APP.rfid.stop_inventory()
                    log.info("%s stopped inventory", self.account["user"])
            except ValueError as e:
                return self.send_json({"error": "invalid", "details": [str(e)]}, 400)
            except ApiError as e:
                hint = " Check the reader login on System › About this reader." if e.code in (401, 403) else ""
                return self.send_json({"error": "reader", "details": [f"The reader refused: {e}.{hint}"]}, 502)
            return self.send_json({"ok": True, "status": APP.rfid.status()})
        if p == "/api/mqtt/test":
            if not self.require("integrations.edit"):
                return
            _, cur = APP.mqtt_settings()
            clean, errors = mqtt_link.validate(dict(body.get("settings") or {}, enabled=False), cur)
            if errors:
                return self.send_json({"ok": False, "error": " ".join(errors)})
            return self.send_json(mqtt_link.test_connection(clean, f"meridian-test-{socket.gethostname()}"))
        if p == "/api/reader/api-test":
            if not self.require("system.edit"):
                return
            user = body.get("username") if isinstance(body.get("username"), str) else None
            pw = body.get("password") if isinstance(body.get("password"), str) and body.get("password") else None
            api, _ = APP.reader_api(user, pw)
            r = api.fetch()
            return self.send_json({"ok": r["ok"], "error": r["error"], "items": r["items"]})
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
                self.send_json({"error": "not_supported", "details": [
                    "The reader mounts USB drives itself and doesn't let apps unmount or format them. "
                    "Drives are mounted with 'sync', so unplugging one is fairly safe."]}, 409)
            return
        return self.send_json({"error": "not_found"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if not self.same_origin() or not self.authenticate():
            return
        if p.startswith("/api/accounts/"):
            return self.accounts("DELETE")
        return self.send_json({"error": "not_found"}, 404)

    def accounts(self, method):
        if not self.require("accounts.manage"):
            return
        body = {}
        if method in ("POST", "PUT"):
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "bad_request"}, 400)
        if method != "GET" and not APP.store.writable:
            return self.send_json({"error": "read_only", "details": [f"{APP.store.dir} is not writable."]}, 503)
        status, result = accounts_api.handle(method, urlparse(self.path).path, body, self.account,
                                             APP.store, APP.access, APP.sessions)
        if method != "GET" and status < 300:
            log.info("%s: %s %s", self.account["user"], method, urlparse(self.path).path)
        return self.send_json(result, status)

    def stream_events(self):
        # A reconnecting browser sends Last-Event-ID and gets what it missed; a fresh connection
        # gets the most recent events too (marked "replay"), so pages can show recent history.
        hdr = self.headers.get("Last-Event-ID")
        fresh = not (hdr and hdr.isdigit())
        last = int(hdr) if not fresh else max(0, APP.bus.seq - REPLAY_EVENTS)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        replay_upto = APP.bus.seq
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                pending = APP.bus.since(last, 15)
                if not pending:
                    self.wfile.write(b": keepalive\n\n")
                for e in pending:
                    if fresh and e["id"] <= replay_upto:
                        e = dict(e, replay=True)
                    # no "event:" line: every type arrives as a plain message, so new types need no client change
                    self.wfile.write(f"id: {e['id']}\ndata: {json.dumps(e)}\n\n".encode())
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
    ap.add_argument("--reader-api", default=os.environ.get("MERIDIAN_READER_API", "https://localhost"),
                    help="base URL of the reader's Impinj REST API")
    ap.add_argument("--no-badges", action="store_true", help="don't read badge readers")
    ap.add_argument("--insecure-http", action="store_true", help="plain HTTP (bench use only)")
    ap.add_argument("--session-idle", type=int, default=int(os.environ.get("MERIDIAN_SESSION_IDLE_MIN", "30")),
                    help="minutes without activity before a sign-in expires")
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
