"""A small simulated R700 IoT Device Interface for the mock server: presets, start/stop, status
and /api/v1/data/stream with tags that arrive, sit, leave and move between antennas.
Not a complete emulation; just enough for Meridian's RFID engine and pages."""
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRESETS = ["default", "deep-scan", "dense-reader", "crib-portal"]


class World:
    def __init__(self, n=18):
        self.lock = threading.Lock()
        self.running, self.preset = False, None
        # tag -> antenna it's near (None = away)
        self.tags = {f"E28011700000020F1B2C{i:04X}": random.choice([1, 2, 2, 3, 3, 4, None]) for i in range(n)}
        self.gpi = {1: "low", 2: "low"}
        self.pending_gpi = []

    def step(self):
        with self.lock:
            epc = random.choice(list(self.tags))
            r = random.random()
            if r < 0.4:
                self.tags[epc] = None                       # taken away
            elif r < 0.8:
                self.tags[epc] = random.choice([2, 3, 4])   # put back somewhere
            else:
                self.tags[epc] = 1                          # passing the portal
            if random.random() < 0.15:
                g = random.choice([1, 2])
                self.gpi[g] = "high" if self.gpi[g] == "low" else "low"
                self.pending_gpi.append({"gpi": g, "transition": f"{'low' if self.gpi[g] == 'high' else 'high'}-to-{self.gpi[g]}"})

    def reads(self):
        with self.lock:
            out = []
            if self.running:
                for epc, ant in self.tags.items():
                    if ant and random.random() < 0.8:
                        out.append({"epcHex": epc, "antennaPort": ant, "peakRssiCdbm": random.randint(-7200, -4500)})
            gpis, self.pending_gpi = self.pending_gpi, []
            return out, gpis


WORLD = World()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def reply(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/v1/profiles/inventory/presets":
            return self.reply(200, PRESETS)
        if self.path == "/api/v1/status":
            return self.reply(200, {"status": "running" if WORLD.running else "idle",
                                    "activePreset": {"id": WORLD.preset, "profile": "inventory"} if WORLD.running else None,
                                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        if self.path == "/api/v1/data/stream":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                while True:
                    reads, gpis = WORLD.reads()
                    for r in reads:
                        self.wfile.write((json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                      "hostname": "impinj-17-1b-e6", "eventType": "tagInventory",
                                                      "tagInventoryEvent": r}) + "\n").encode())
                    for g in gpis:
                        self.wfile.write((json.dumps({"eventType": "gpiTransition", "gpiTransitionEvent": g}) + "\n").encode())
                    self.wfile.flush()
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        self.reply(404, {"message": f"{self.path} not found"})

    def do_POST(self):
        if self.path.startswith("/api/v1/profiles/inventory/presets/") and self.path.endswith("/start"):
            pid = self.path.split("/")[-2]
            if pid not in PRESETS:
                return self.reply(404, {"message": f"Preset '{pid}' not found"})
            with WORLD.lock:
                WORLD.running, WORLD.preset = True, pid
            return self.reply(204)
        if self.path == "/api/v1/profiles/stop":
            with WORLD.lock:
                WORLD.running, WORLD.preset = False, None
            return self.reply(204)
        self.reply(404, {"message": f"{self.path} not found"})


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="fake-impinj").start()

    def world():
        while True:
            time.sleep(random.uniform(3, 7))
            WORLD.step()
    threading.Thread(target=world, daemon=True, name="fake-impinj-world").start()
    return f"http://127.0.0.1:{srv.server_address[1]}"
