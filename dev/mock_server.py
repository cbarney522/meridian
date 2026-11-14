#!/usr/bin/env python3
"""Meridian mock server — develop the web UI without an R700.

Serves app/www/ and implements the same /api/... contract the real CAP server will
(see api.md), with simulated reader data and a live event stream.

    python3 dev/mock_server.py                 # http://127.0.0.1:8080
    python3 dev/mock_server.py --host 0.0.0.0  # reachable from a tablet on your LAN

Standard library only, same as the CAP.
MOCK-ONLY: there is no login. The current role is held in memory and can be switched
with POST /api/dev/role so you can see each role's view. The real server replaces this
with proper authentication; everything else here should carry over.
"""
import argparse
import collections
import json
import os
import random
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
from core.access import Access  # noqa: E402
from core import usb, badges, storage  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "dev"))
import fake_sysfs  # noqa: E402

WWW = os.path.join(ROOT, "app", "www")
ACCESS = Access()
STATE = {"role": "developer", "user": "cliff"}
STARTED = time.time()

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# ---------------------------------------------------------------- simulated reader
ZONES = ["drawer1", "drawer2", "drawer3", "portal"]
TOOLS = ["Torque wrench #4471", "Drill DCD791 #1180", "Caliper 6in #0932", "Impact driver #2210",
         "Multimeter #0751", "Crimper #3302", "Borescope #0044", "Rivet gun #1907"]
USERS = [("10442", "J. Smith"), ("10517", "M. Alvarez"), ("10603", "R. Chen"), ("10688", "T. Okafor")]


class Sim:
    def __init__(self):
        self.lock = threading.Condition()
        self.events = collections.deque(maxlen=200)
        self.seq = 0
        self.zone_counts = {"drawer1": 12, "drawer2": 9, "drawer3": 14, "portal": 0}
        self.read_rate = 300
        self.outbox = 0
        self.session = None
        self.checked_out = set()   # tools currently out of the crib
        self.on_badge = None       # hook so the badge-reader page sees the same swipes

    def emit(self, etype, **data):
        with self.lock:
            self.seq += 1
            self.events.append({"id": self.seq, "type": etype,
                                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **data})
            self.lock.notify_all()

    def run(self):
        while True:
            time.sleep(random.uniform(1.5, 4.0))
            self.read_rate = max(0, self.read_rate + random.randint(-40, 40))
            self.outbox = max(0, self.outbox + random.choice([-2, -1, 0, 0, 1]))
            r = random.random()
            if self.session is None and r < 0.25:
                uid, name = random.choice(USERS)
                self.session = {"badge": uid, "user": name, "out": [], "in": []}
                self.emit("badge_scan", source="badge_in", badge=uid, user=name, result="granted")
                if self.on_badge:
                    self.on_badge(uid)
            elif self.session and r < 0.55:
                zone = random.choice(ZONES[:3])
                available = [t for t in TOOLS if t not in self.checked_out]
                take = available and (not self.checked_out or random.random() < 0.6)
                if take and self.zone_counts[zone] > 0:
                    tool = random.choice(available)
                    self.checked_out.add(tool)
                    self.zone_counts[zone] -= 1
                    self.session["out"].append(tool)
                    self.emit("tag_exit", zone=zone, tool=tool, user=self.session["user"])
                elif self.checked_out:
                    tool = random.choice(sorted(self.checked_out))
                    self.checked_out.discard(tool)
                    self.zone_counts[zone] += 1
                    self.session["in"].append(tool)
                    self.emit("tag_enter", zone=zone, tool=tool, user=self.session["user"])
            elif self.session and r < 0.65:
                s = self.session
                self.session = None
                self.emit("session_closed", user=s["user"], removed=len(s["out"]), returned=len(s["in"]))
            elif r < 0.72:
                self.emit("gpi_change", input="door_1", state=random.choice(["open", "closed"]))
            elif r < 0.76:
                self.emit("rule_fired", rule="low-stock-drawer3", detail="count below 5 for 10 s")
            elif r < 0.78:
                self.emit("badge_scan", source="badge_in", badge="99999", user=None, result="denied_unknown")
                if self.on_badge:
                    self.on_badge("99999")

    def status(self):
        up = int(time.time() - STARTED) + 282441
        return {
            "reader": {"name": "Crib 2 portal", "serial": "37017090614", "site": "plant-atl",
                       "firmware": "10.4.0", "app_version": "0.1.0-dev", "profile": "tool-crib-portal",
                       "uptime_s": up, "ntp_synced": True, "temp_c": round(41 + random.random() * 2, 1)},
            "rfid": {"running": True, "read_rate": self.read_rate,
                     "tags_in_field": sum(self.zone_counts.values()), "zones": dict(self.zone_counts)},
            "mqtt": {"connected": True, "outbox_depth": self.outbox},
            "usb": {"drive": True, "free_mb": 28110, "badge_in": True, "badge_out": True},
            "session": self.session,
        }


SIM = Sim()


# ---------------------------------------------------------------- simulated site (50 readers)
# On a real reader this list comes from the retained MQTT heartbeats every Meridian reader
# publishes (evt/{site}/{serial}/status) plus Last Will "online" flags. Here it's generated.
class Fleet:
    KINDS = [("Crib portal", "tool-crib-portal"), ("Drawer cabinet", "cabinet-drawer"),
             ("Bin shelf", "bin-shelf"), ("Dock door", "generic")]
    BUILDINGS = ["Bldg A", "Bldg B", "Bldg C", "Maintenance", "Receiving"]

    def __init__(self, n=50, seed=7):
        rnd = random.Random(seed)
        now = time.time()
        self.readers = []
        for i in range(n):
            kind, profile = self.KINDS[i % len(self.KINDS)]
            r = {
                "serial": str(37017090614 + i * 37),
                "name": f"{kind} {i // len(self.KINDS) + 1}",
                "location": rnd.choice(self.BUILDINGS),
                "profile": profile,
                "app_version": "0.1.0-dev" if i == 0 else rnd.choices(["0.4.1", "0.4.0", "0.3.2"], [8, 3, 1])[0],
                "firmware": rnd.choices(["10.4.0", "10.3.1"], [9, 1])[0],
                "ip": f"10.20.{10 + i // 20}.{100 + i % 20}",
                "tags_in_field": rnd.randint(0, 120),
                "outbox_depth": 0,
                "usb_drive": True,
                "last_seen": now,
                "offline_since": None,
            }
            self.readers.append(r)
        self.readers[0]["name"] = "Crib 2 portal"          # this reader (matches /api/session)
        self.readers[0]["location"] = "Bldg A"
        for i in (13, 27, 41):                              # a few offline for hours
            self.readers[i]["offline_since"] = now - rnd.randint(600, 4 * 3600)
        self.readers[9]["usb_drive"] = False                # one with a missing USB drive
        self.readers[22]["outbox_depth"] = 340              # one that can't reach the broker
        self.rnd = rnd

    def snapshot(self):
        now = time.time()
        latest = max((r["app_version"] for r in self.readers if not r["app_version"].endswith("-dev")),
                     key=lambda v: tuple(int(x) for x in v.split(".")))
        out = []
        for idx, r in enumerate(self.readers):
            online = r["offline_since"] is None
            if online:
                r["last_seen"] = now - self.rnd.uniform(0, 55)
                r["tags_in_field"] = max(0, r["tags_in_field"] + self.rnd.randint(-2, 2))
                if r["outbox_depth"]:
                    r["outbox_depth"] += self.rnd.randint(0, 6)
            alerts = []
            if not online:
                alerts.append("offline")
            if not r["usb_drive"]:
                alerts.append("usb_missing")
            if r["outbox_depth"] > 100:
                alerts.append("outbox_backlog")
            out.append({
                **{k: r[k] for k in ("serial", "name", "location", "profile", "app_version", "firmware", "ip",
                                     "outbox_depth", "usb_drive")},
                "this_reader": idx == 0,
                "online": online,
                "last_seen_s": int(now - (r["last_seen"] if online else r["offline_since"])),
                "tags_in_field": r["tags_in_field"] if online else None,
                "alerts": alerts,
                "url": f"https://{r['ip']}:8443/",
            })
        return {"site": "plant-atl", "latest_version": latest, "readers": out}


FLEET = Fleet()


# ---------------------------------------------------------------- simulated USB (real scanner, fake sysfs)
class UsbMock:
    """Runs the real app/core/usb.py scanner against a fake sysfs tree. Unplugging is simulated by
    rebuilding the tree without that port. Assignments live in memory here; on the reader they
    go in config.json in rw_dir."""

    def __init__(self):
        self.lock = threading.Lock()
        self.root = os.path.join(tempfile.mkdtemp(prefix="meridian-sysfs-"), "sys")
        self.unplugged = set()
        self.revision = 1
        self.assignments = {
            "1-1.1.1": {"role": "badge_in", "vid": "0c27", "pid": "3bfa", "product": "pcProx Plus RDR-80081AKU"},
            "1-1.1.2": {"role": "badge_out", "vid": "0c27", "pid": "3bfa", "product": "pcProx Plus RDR-80081AKU"},
            "1-1.1.3": {"role": "io_board", "vid": "2e8a", "pid": "000a", "product": "Meridian I/O"},
            "1-1.1.4.1": {"role": "scanner", "vid": "0c2e", "pid": "0b61", "product": "1950g"},
            "1-1.1.4.4": {"role": "keypad", "vid": "05e0", "pid": "1200", "product": "Foot pedal"},  # not plugged in
            "1-1.2":   {"role": "storage", "vid": "0781", "pid": "5581", "product": "Ultra"},
        }
        fake_sysfs.build(self.root)

    def snapshot(self):
        with self.lock:
            rows = usb.evaluate(usb.scan(self.root), self.assignments)
            rev = self.revision
        present = [r for r in rows if r["status"] != "missing"]
        return {
            "revision": rev,
            "devices": rows,
            "roles": [{"id": k, "label": v["label"], "expects": v["expects"], "single": v["single"]}
                      for k, v in usb.ROLES.items()],
            "summary": {
                "devices": sum(1 for r in present if r["type"] != "hub"),
                "hubs": sum(1 for r in present if r["type"] == "hub"),
                "assigned": sum(1 for r in rows if r["role"] and r["role"] != "ignore"),
                "problems": sum(1 for r in rows if r["status"] in ("missing", "mismatch", "changed")),
                "unassigned": sum(1 for r in rows if r["status"] == "unassigned"),
                "power_ma": sum(r["max_power_ma"] for r in present),
            },
            # MOCK-ONLY: lets the page offer "plug back in" for simulated unplugs
            "dev_unplugged": [{"port": d["port"], "product": d.get("product")}
                              for d in fake_sysfs.R700_SETUP if d["port"] in self.unplugged],
        }

    def save(self, revision, wanted):
        """wanted: {port: role}. VID:PID are pinned from whatever is in the port now, so a later
        swap to a different device shows up as 'changed'."""
        with self.lock:
            if revision != self.revision:
                return 409, {"error": "conflict", "revision": self.revision}
            if not isinstance(wanted, dict):
                return 400, {"error": "bad_request", "details": ["assignments must be an object"]}
            current = {d["port"]: d for d in usb.scan(self.root)}
            new = {}
            for port, role in wanted.items():
                if not role:
                    continue
                old = self.assignments.get(port)
                if old and old["role"] == role:
                    new[port] = old                      # unchanged: keep original pin
                    continue
                d = current.get(port)
                new[port] = {"role": role, "vid": d["vid"] if d else None, "pid": d["pid"] if d else None,
                             "product": d["product"] if d else None}
                new[port] = {k: v for k, v in new[port].items() if v is not None}
            errors = usb.validate_assignments(new)
            if errors:
                return 400, {"error": "invalid", "details": errors}
            self.assignments = new
            self.revision += 1
            return 200, {"ok": True, "revision": self.revision}

    def toggle(self, port):
        with self.lock:
            known = {d["port"]: d for d in fake_sysfs.R700_SETUP}
            if port not in known:
                return None
            attached = port in self.unplugged
            (self.unplugged.discard if attached else self.unplugged.add)(port)
            fake_sysfs.build(self.root, fake_sysfs.without(self.unplugged))
        SIM.emit("usb_attached" if attached else "usb_detached", port=port, product=known[port].get("product"))
        return attached


USB = UsbMock()


# ---------------------------------------------------------------- simulated badge readers
class BadgeMock:
    """Per-port scan stats and the site's normalization settings. Swipes run through the real
    core.badges.normalize(); on the reader the raw strings come from KeyAssembler on /dev/input."""
    PREFIX = {"badge_in": "I", "badge_out": "O"}

    def __init__(self):
        self.lock = threading.Lock()
        self.settings = dict(badges.DEFAULT_SETTINGS)
        self.revision = 1
        self.stats = {}          # port -> {"last": {...}, "scans": n, "errors": n}

    def _keyboards(self):
        rows = usb.evaluate(usb.scan(USB.root), USB.assignments)
        return {r["port"]: r for r in rows if r["type"] == "hid-keyboard" or
                (r["status"] == "missing" and r["role"] in ("badge_in", "badge_out"))}

    def swipe(self, port, raw=None, garbled=False):
        kb = self._keyboards().get(port)
        if not kb or kb["status"] == "missing":
            return None
        if raw is None:
            uid = random.choice(USERS)[0]
            card = uid.zfill(6)
            if garbled:
                card = card[:3] + "O" + card[4:]          # a misread character
            raw = self.PREFIX.get(kb["role"], "") + "105:" + card
        with self.lock:
            result = badges.normalize(raw, self.settings)
            st = self.stats.setdefault(port, {"last": None, "scans": 0, "errors": 0})
            st["scans"] += 1
            st["errors"] += 0 if result["ok"] else 1
            st["last"] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "raw": raw,
                          "badge_id": result["badge_id"], "ok": result["ok"]}
        SIM.emit("badge_raw", port=port, role=kb["role"], product=kb["product"], raw=raw, result=result)
        return result

    def background(self, uid):
        port = next((p for p, a in USB.assignments.items() if a["role"] == "badge_in"), None)
        if port:
            self.swipe(port, raw="I105:" + uid.zfill(6))

    def snapshot(self):
        kbs = self._keyboards()
        readers = []
        for role in ("badge_in", "badge_out"):
            port = next((p for p, a in USB.assignments.items() if a["role"] == role), None)
            r = kbs.get(port) if port else None
            st = self.stats.get(port, {})
            readers.append({
                "role": role, "label": usb.ROLES[role]["label"], "port": port,
                "assigned": port is not None,
                "status": r["status"] if r else ("missing" if port else "unassigned"),
                "product": r["product"] if r else None,
                "nodes": r["nodes"] if r else [],
                "location": r.get("location") if r else None,
                "last_scan": st.get("last"), "scans_today": st.get("scans", 0), "errors_today": st.get("errors", 0),
            })
        others = [{"port": p, "product": r["product"], "role": r["role"], "nodes": r["nodes"]}
                  for p, r in kbs.items() if r["role"] not in ("badge_in", "badge_out") and r["status"] != "missing"]
        with self.lock:
            return {"revision": self.revision, "settings": dict(self.settings),
                    "readers": readers, "other_keyboards": others}

    def save(self, revision, new):
        with self.lock:
            if revision != self.revision:
                return 409, {"error": "conflict", "revision": self.revision}
            merged = dict(badges.DEFAULT_SETTINGS, **(new if isinstance(new, dict) else {}))
            errors = badges.validate_settings(merged)
            if errors:
                return 400, {"error": "invalid", "details": errors}
            self.settings = {k: merged[k] for k in badges.DEFAULT_SETTINGS}
            self.revision += 1
            return 200, {"ok": True, "revision": self.revision}


BADGES = BadgeMock()
SIM.on_badge = BADGES.background


# ---------------------------------------------------------------- simulated data drive
class StorageMock:
    """Drive presence comes from the USB mock (the port with role 'storage'); capacity and usage
    are simulated. Warnings come from the real core.storage.warnings_for()."""
    GB = 1024 ** 3

    def __init__(self):
        self.lock = threading.Lock()
        self.mounted = True
        self.fs_type = "ext4"
        self.size = int(28.6 * self.GB)
        self.usage = {"outbox": 2_150_000, "events": 412_000_000, "logs": 85_000_000, "other": 12_000_000}
        self.settings = {"retention_days_events": 90, "retention_days_logs": 30, "alert_free_pct": 10}
        self.revision = 1

    def _drive(self):
        port = next((p for p, a in USB.assignments.items() if a["role"] == "storage"), None)
        if not port:
            return None, None
        dev = {d["port"]: d for d in usb.scan(USB.root)}.get(port)
        return port, dev

    def snapshot(self):
        port, dev = self._drive()
        with self.lock:
            if self.mounted:
                self.usage["events"] += random.randint(0, 40_000)
                self.usage["outbox"] = max(0, self.usage["outbox"] + random.randint(-50_000, 50_000))
            present = dev is not None
            node = (dev["nodes"][0] if dev and dev["nodes"] else None)
            mount = None
            cap = None
            if present and self.mounted:
                mount = {"device": (node + "1") if node else None, "mount_point": "/run/media/disks/disk0",
                         "fs_type": self.fs_type, "read_only": False, "sync": True}
                used = sum(self.usage.values()) + int(self.size * 0.012)      # filesystem overhead
                cap = {"size": self.size, "used": used, "free": self.size - used}
            items = [
                {"id": "outbox", "label": "Event outbox (SQLite)", "path": "data/outbox.db", "bytes": self.usage["outbox"]},
                {"id": "events", "label": "Event history (SQLite)", "path": "data/events.db", "bytes": self.usage["events"]},
                {"id": "logs", "label": "Log files", "path": "data/logs/", "bytes": self.usage["logs"]},
                {"id": "other", "label": "Other", "path": "data/", "bytes": self.usage["other"]},
            ] if cap else []
            return {
                "revision": self.revision,
                "assigned_port": port,
                "drive": {
                    "present": present, "port": port,
                    "product": dev["product"] if dev else None, "manufacturer": dev["manufacturer"] if dev else None,
                    "serial": dev["serial"] if dev else None, "node": node,
                    "location": dev.get("location") if dev else None,
                    "mounted": bool(mount), "mount": mount, "capacity": cap,
                },
                "usage": items,
                "can_eject": False, "can_format": False,    # matches FW 10.4 (CAP runs as non-root 'cap')
                "internal": {"path": "/cust/rw_dir", "size": 128 * 1024 ** 2, "used": 23 * 1024 ** 2,
                             "free": 105 * 1024 ** 2, "buffering": present is False or not self.mounted},
                "settings": dict(self.settings),
                "warnings": storage.warnings_for(mount, cap, self.settings["alert_free_pct"]),
                "dev_fs_type": self.fs_type,
            }

    def save(self, revision, new):
        with self.lock:
            if revision != self.revision:
                return 409, {"error": "conflict", "revision": self.revision}
            errors = []
            if not isinstance(new, dict):
                return 400, {"error": "invalid", "details": ["settings must be an object"]}
            limits = {"retention_days_events": (1, 3650), "retention_days_logs": (1, 3650), "alert_free_pct": (1, 50)}
            out = {}
            for k, (lo, hi) in limits.items():
                v = new.get(k)
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    errors.append(f"{k}: whole number {lo}–{hi}")
                out[k] = v
            if errors:
                return 400, {"error": "invalid", "details": errors}
            self.settings = out
            self.revision += 1
            return 200, {"ok": True, "revision": self.revision}

    def action(self, what, confirm=None):
        port, dev = self._drive()
        if dev is None:
            return 409, {"error": "no_drive"}
        return 409, {"error": "not_supported", "details": ["The reader mounts USB drives itself and doesn't let apps unmount or format them."]}
        with self.lock:
            if what == "eject":
                self.mounted = False
            elif what == "mount":
                self.mounted = True
            elif what == "format":
                if confirm != "FORMAT":
                    return 400, {"error": "confirm", "details": ["Type FORMAT to confirm."]}
                self.fs_type = "ext4"
                self.usage = {"outbox": 0, "events": 0, "logs": 0, "other": 0}
                self.mounted = True
            else:
                return 404, {"error": "not_found"}
        SIM.emit("storage_changed", action=what, port=port)
        return 200, {"ok": True}


STORAGE = StorageMock()


# ---------------------------------------------------------------- HTTP
class Handler(SimpleHTTPRequestHandler):
    server_version = "MeridianMock/0.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WWW, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    # -- helpers
    def send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def require(self, perm):
        """Server-side permission check. Hidden menus are cosmetic; this is the real gate."""
        if ACCESS.can(STATE["role"], perm):
            return True
        self.send_json({"error": "forbidden", "needs": perm}, HTTPStatus.FORBIDDEN)
        return False

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # -- routing
    def do_GET(self):
        url = urlparse(self.path)
        p = url.path
        if not p.startswith("/api/"):
            if p in ("", "/"):
                self.path = "/index.html"
            return super().do_GET()

        if p == "/api/login-info":
            return self.send_json({"reader": "Crib 2 portal (mock)", "app_version": "0.1.0-dev"})
        role = STATE["role"]
        if p == "/api/session":
            r = ACCESS.roles[role]
            return self.send_json({
                "user": STATE["user"], "role": role, "role_label": r["label"],
                "permissions": sorted(ACCESS.permissions_for(role)),
                "dev_mode": True,
                "roles": [{"id": k, "label": v["label"], "description": v["description"]}
                          for k, v in ACCESS.roles.items()],
                "reader": {"name": "Crib 2 portal", "serial": "37017090614"},
            })
        if p == "/api/menu":
            return self.send_json({"menus": ACCESS.menu_for(role)})
        if p == "/api/route-check":
            route = parse_qs(url.query).get("route", [""])[0]
            if route not in ACCESS.route_perms:
                return self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            if not ACCESS.can_route(role, route):
                return self.send_json({"error": "forbidden", "needs": ACCESS.route_perms[route]},
                                      HTTPStatus.FORBIDDEN)
            return self.send_json({"ok": True, "perm": ACCESS.route_perms[route]})
        if p == "/api/status":
            if self.require("dashboard.view"):
                self.send_json(SIM.status())
            return
        if p == "/api/site/readers":
            if self.require("site.view"):
                self.send_json(FLEET.snapshot())
            return
        if p == "/api/dev/diagnostics":
            if self.require("dev.tools"):
                self.send_json({"diag": "Meridian diagnostics (mock server)\nkernel:   Linux mock\n--- USB (sysfs)\n  1-1.1  0c27:3bfa  pcProx Plus\n",
                                "log": "2026-11-14 04:00:00 INFO meridian: mock server — no real log\n",
                                "badge_listener": None, "app_version": "0.1.0-dev", "data_dir": "(mock)"})
            return
        if p == "/api/usb/devices":
            if self.require("hardware.view"):
                self.send_json(USB.snapshot())
            return
        if p == "/api/badges":
            if self.require("hardware.view"):
                self.send_json(BADGES.snapshot())
            return
        if p == "/api/storage":
            if self.require("hardware.view"):
                self.send_json(STORAGE.snapshot())
            return
        if p == "/api/events":
            if self.require("dashboard.view"):
                self.stream_events(url)
            return
        return self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        p = urlparse(self.path).path
        if p in ("/api/login", "/api/logout"):   # MOCK-ONLY: no real accounts; any sign-in works
            return self.send_json({"ok": True, "must_change_password": False})
        if p == "/api/dev/role":  # MOCK-ONLY role switcher
            try:
                role = self.read_json().get("role")
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, HTTPStatus.BAD_REQUEST)
            if role not in ACCESS.roles:
                return self.send_json({"error": "unknown_role"}, HTTPStatus.BAD_REQUEST)
            STATE["role"] = role
            return self.send_json({"ok": True, "role": role})
        if p in ("/api/badges/preview", "/api/dev/badge/swipe", "/api/storage/eject", "/api/storage/mount",
                 "/api/storage/format", "/api/dev/storage/fs"):
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, HTTPStatus.BAD_REQUEST)
            if p == "/api/badges/preview":
                if not self.require("hardware.view"):
                    return
                settings = body.get("settings") or BADGES.settings
                merged = dict(badges.DEFAULT_SETTINGS, **settings) if isinstance(settings, dict) else None
                errors = badges.validate_settings(merged) if merged else ["settings must be an object"]
                if errors:
                    return self.send_json({"error": "invalid", "details": errors}, HTTPStatus.BAD_REQUEST)
                raw = body.get("raw")
                if not isinstance(raw, str) or len(raw) > 128:
                    return self.send_json({"error": "bad_request"}, HTTPStatus.BAD_REQUEST)
                return self.send_json(badges.normalize(raw, merged))
            if p == "/api/dev/badge/swipe":  # MOCK-ONLY
                if not self.require("hardware.view"):
                    return
                r = BADGES.swipe(body.get("port"), garbled=bool(body.get("garbled")))
                if r is None:
                    return self.send_json({"error": "no_keyboard_device"}, HTTPStatus.BAD_REQUEST)
                return self.send_json({"ok": True})
            if p == "/api/dev/storage/fs":  # MOCK-ONLY: flip ext4/vfat to see the warning
                if not self.require("hardware.view"):
                    return
                STORAGE.fs_type = "vfat" if STORAGE.fs_type == "ext4" else "ext4"
                return self.send_json({"ok": True, "fs_type": STORAGE.fs_type})
            if not self.require("hardware.edit"):
                return
            status, result = STORAGE.action(p.rsplit("/", 1)[1], body.get("confirm"))
            return self.send_json(result, status)
        if p == "/api/dev/usb/toggle":  # MOCK-ONLY: simulate unplugging / replugging a port
            if not self.require("hardware.view"):
                return
            try:
                port = self.read_json().get("port")
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, HTTPStatus.BAD_REQUEST)
            attached = USB.toggle(port)
            if attached is None:
                return self.send_json({"error": "unknown_port"}, HTTPStatus.BAD_REQUEST)
            return self.send_json({"ok": True, "attached": attached})
        return self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        p = urlparse(self.path).path
        if p == "/api/usb/assignments":
            if not self.require("hardware.edit"):
                return
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, HTTPStatus.BAD_REQUEST)
            status, result = USB.save(body.get("revision"), body.get("assignments"))
            return self.send_json(result, status)
        if p in ("/api/badges/settings", "/api/storage/settings"):
            if not self.require("hardware.edit"):
                return
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, HTTPStatus.BAD_REQUEST)
            target = BADGES if p.startswith("/api/badges") else STORAGE
            status, result = target.save(body.get("revision"), body.get("settings"))
            return self.send_json(result, status)
        return self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def stream_events(self, url):
        """Server-Sent Events. The browser's EventSource reconnects automatically and sends
        Last-Event-ID, so a reconnecting client gets what it missed from the buffer."""
        last = self.headers.get("Last-Event-ID")
        last = int(last) if last and last.isdigit() else SIM.seq
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        role_at_start = STATE["role"]
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                with SIM.lock:
                    SIM.lock.wait(timeout=15)
                    pending = [e for e in SIM.events if e["id"] > last]
                if STATE["role"] != role_at_start and not ACCESS.can(STATE["role"], "dashboard.view"):
                    return  # permission revoked mid-stream
                if not pending:
                    self.wfile.write(b": keepalive\n\n")
                for e in pending:
                    self.wfile.write(f"id: {e['id']}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
                    last = e["id"]
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    threading.Thread(target=SIM.run, daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"Meridian mock server on http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
