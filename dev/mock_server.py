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
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
from core.access import Access  # noqa: E402

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
        if p == "/api/events":
            if self.require("dashboard.view"):
                self.stream_events(url)
            return
        return self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/dev/role":  # MOCK-ONLY role switcher
            try:
                role = self.read_json().get("role")
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad_request"}, HTTPStatus.BAD_REQUEST)
            if role not in ACCESS.roles:
                return self.send_json({"error": "unknown_role"}, HTTPStatus.BAD_REQUEST)
            STATE["role"] = role
            return self.send_json({"ok": True, "role": role})
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
