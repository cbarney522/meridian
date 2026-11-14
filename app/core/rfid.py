"""RFID engine: drives the R700's inventory through its IoT Device Interface on localhost, reads
the tag stream, and turns raw reads into tag_enter / tag_exit events per zone.

    ImpinjControl  presets, start/stop, status        (REST on https://localhost/api/v1)
    parse_event    one line of /api/v1/data/stream -> ("tag", ...) | ("gpi", ...) | ("other", ...)
    TagTracker     zones from antenna ports, debounced enter/exit/move, dwell, read rate
    RfidEngine     threads: stream reader (auto-reconnect), sweeper, status poll, auto-start

Meridian starts a preset that's already saved on the reader (create and tune presets in the
Impinj web UI: antennas, power, session, search mode). The preset should report every read
(reporting interval 0), so a tag that stops being read can be detected as gone.
Standard library only.
"""
import base64
import binascii
import collections
import json
import logging
import re
import socket
import threading
import time

from .readerapi import ApiError

log = logging.getLogger("meridian.rfid")

DEFAULTS = {
    "auto_start": False,         # start the preset when Meridian starts
    "preset": "",                # preset id on the reader
    "zones": {},                 # {"1": "portal", "2": "drawer1", ...}
    "ignore_unzoned": False,     # ignore reads from antennas without a zone
    "exit_timeout_s": 5.0,       # not read for this long -> tag_exit
    "enter_reads": 2,            # reads needed before tag_enter (filters stray reads)
    "move_quiet_s": 1.0,         # old zone must be silent this long before a move is accepted
    "rssi_min_dbm": None,        # ignore weaker reads (e.g. -70)
    "epc_prefix": "",            # only track EPCs starting with this hex prefix
}
ZONE_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,32}$")
HEX_RE = re.compile(r"^[0-9A-Fa-f]*$")


def validate(body, current):
    errors = []
    if not isinstance(body, dict):
        return None, ["Settings must be an object."]
    out = dict(DEFAULTS, **{k: current.get(k, v) for k, v in DEFAULTS.items()})
    out["auto_start"] = bool(body.get("auto_start", out["auto_start"]))
    out["preset"] = str(body.get("preset", out["preset"]) or "").strip()[:64]
    if out["auto_start"] and not out["preset"]:
        errors.append("Pick a preset to start automatically.")
    zones = body.get("zones", out["zones"]) or {}
    clean = {}
    if not isinstance(zones, dict):
        errors.append("Zones must map antenna port numbers to names.")
        zones = {}
    for port, name in zones.items():
        name = str(name or "").strip()
        if not name:
            continue
        if not str(port).isdigit() or not 1 <= int(port) <= 32:
            errors.append(f"Antenna port {port!r} isn't 1–32.")
        elif not ZONE_RE.match(name):
            errors.append(f"Zone name “{name}”: letters, digits, spaces, - and _, up to 32 characters.")
        else:
            clean[str(int(port))] = name
    out["zones"] = clean
    out["ignore_unzoned"] = bool(body.get("ignore_unzoned", out["ignore_unzoned"]))
    for key, lo, hi, label in (("exit_timeout_s", 1, 600, "Gone after"), ("move_quiet_s", 0, 30, "Move delay")):
        try:
            out[key] = float(body.get(key, out[key]))
            if not lo <= out[key] <= hi:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label} must be {lo}–{hi} seconds.")
    try:
        out["enter_reads"] = int(body.get("enter_reads", out["enter_reads"]))
        if not 1 <= out["enter_reads"] <= 50:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Reads to confirm must be 1–50.")
    rssi = body.get("rssi_min_dbm", out["rssi_min_dbm"])
    if rssi in ("", None):
        out["rssi_min_dbm"] = None
    else:
        try:
            out["rssi_min_dbm"] = float(rssi)
            if not -100 <= out["rssi_min_dbm"] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("Minimum signal must be between -100 and 0 dBm (or empty).")
    prefix = str(body.get("epc_prefix", out["epc_prefix"]) or "").strip().upper()
    if not HEX_RE.match(prefix) or len(prefix) > 24:
        errors.append("EPC prefix: hex digits only (0-9, A-F), up to 24.")
    out["epc_prefix"] = prefix
    return out, errors


# ---------------------------------------------------------------- stream parsing
def _lower_keys(d):
    return {k.lower(): v for k, v in d.items()} if isinstance(d, dict) else {}


def epc_hex(ev):
    """EPC as upper-case hex from a tag event (epcHex, or base64/base64url epc)."""
    e = _lower_keys(ev)
    if isinstance(e.get("epchex"), str) and e["epchex"]:
        return e["epchex"].upper()
    raw = e.get("epc")
    if isinstance(raw, str) and raw:
        # Normally base64. A string of hex digits that can't be (or almost certainly isn't) base64
        # is taken as hex: a 96-bit EPC is 24 hex digits but 16 base64 characters.
        if HEX_RE.match(raw) and len(raw) % 2 == 0 and (len(raw) % 4 or len(raw) == 24):
            return raw.upper()
        try:
            pad = "=" * (-len(raw) % 4)
            return base64.b64decode(raw.replace("-", "+").replace("_", "/") + pad).hex().upper()
        except (binascii.Error, ValueError):
            return None
    return None


def parse_event(line):
    """One line of the reader's event stream."""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return ("other", None)
    o = _lower_keys(obj)
    tag = o.get("taginventoryevent")
    if isinstance(tag, dict):
        t = _lower_keys(tag)
        epc = epc_hex(tag)
        if not epc:
            return ("other", "taginventory without epc")
        port = t.get("antennaport")
        rssi = t.get("peakrssicdbm")
        return ("tag", epc, int(port) if isinstance(port, (int, float)) else None,
                int(rssi) if isinstance(rssi, (int, float)) else None)
    gpi = o.get("gpitransitionevent")
    if isinstance(gpi, dict):
        g = _lower_keys(gpi)
        trans = str(g.get("transition", ""))
        return ("gpi", g.get("gpi"), "high" if trans.endswith("high") else "low" if trans.endswith("low") else trans)
    kind = next((k for k in o if k.endswith("event")), o.get("eventtype"))
    return ("other", kind)


# ---------------------------------------------------------------- tracker
class _Tag:
    __slots__ = ("epc", "zone", "antenna", "first", "last", "last_in_zone", "reads", "rssi", "cand", "cand_n")

    def __init__(self, epc, zone, antenna, now, rssi, reads):
        self.epc, self.zone, self.antenna = epc, zone, antenna
        self.first = self.last = self.last_in_zone = now
        self.reads, self.rssi = reads, rssi
        self.cand, self.cand_n = None, 0


class TagTracker:
    """Thread-safe. Feed read(); call sweep() regularly. Both return a list of (event_type, payload)."""

    def __init__(self, settings=None, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.tags = {}
        self.pending = {}           # epc -> [zone, antenna, count, first, last, rssi]
        self.read_times = collections.deque()
        self.total_reads = 0
        self.ignored = 0
        self.configure(settings or DEFAULTS)

    def configure(self, settings):
        with self.lock:
            self.s = dict(DEFAULTS, **settings)

    def zone_for(self, port):
        z = self.s["zones"].get(str(port))
        if z:
            return z
        return None if self.s["ignore_unzoned"] else f"Antenna {port}"

    def read(self, epc, port, rssi_cdbm, now=None):
        now = self.clock() if now is None else now
        out = []
        with self.lock:
            s = self.s
            if s["epc_prefix"] and not epc.startswith(s["epc_prefix"]):
                self.ignored += 1
                return out
            if s["rssi_min_dbm"] is not None and rssi_cdbm is not None and rssi_cdbm / 100 < s["rssi_min_dbm"]:
                self.ignored += 1
                return out
            zone = self.zone_for(port)
            if zone is None:
                self.ignored += 1
                return out
            self.total_reads += 1
            self.read_times.append(now)
            rssi = round(rssi_cdbm / 100, 1) if rssi_cdbm is not None else None
            tag = self.tags.get(epc)
            if tag is None:
                p = self.pending.get(epc)
                if p is None or p[0] != zone or now - p[4] > s["exit_timeout_s"]:
                    p = self.pending[epc] = [zone, port, 0, now, now, rssi]
                p[2] += 1
                p[4], p[1], p[5] = now, port, rssi
                if p[2] >= s["enter_reads"]:
                    del self.pending[epc]
                    self.tags[epc] = tag = _Tag(epc, zone, port, now, rssi, p[2])
                    tag.first = p[3]                      # dwell counts from the first read
                    out.append(("tag_enter", {"epc": epc, "zone": zone, "antenna": port, "rssi_dbm": rssi}))
                return out
            tag.last = now
            tag.reads += 1
            if zone == tag.zone:
                tag.last_in_zone, tag.antenna, tag.rssi = now, port, rssi
                tag.cand, tag.cand_n = None, 0
                return out
            # read in another zone: move only after enough reads there and silence in the old one
            if tag.cand == zone:
                tag.cand_n += 1
            else:
                tag.cand, tag.cand_n = zone, 1
            if tag.cand_n >= s["enter_reads"] and now - tag.last_in_zone >= s["move_quiet_s"]:
                old = tag.zone
                out.append(("tag_exit", {"epc": epc, "zone": old, "antenna": tag.antenna,
                                         "dwell_s": round(now - tag.first, 1), "reason": "moved", "to_zone": zone}))
                tag.zone, tag.antenna, tag.rssi = zone, port, rssi
                tag.first = tag.last_in_zone = now
                tag.cand, tag.cand_n = None, 0
                out.append(("tag_enter", {"epc": epc, "zone": zone, "antenna": port, "rssi_dbm": rssi, "from_zone": old}))
        return out

    def sweep(self, now=None):
        now = self.clock() if now is None else now
        out = []
        with self.lock:
            limit = self.s["exit_timeout_s"]
            for epc, tag in list(self.tags.items()):
                if now - tag.last > limit:
                    del self.tags[epc]
                    out.append(("tag_exit", {"epc": epc, "zone": tag.zone, "antenna": tag.antenna,
                                             "dwell_s": round(tag.last - tag.first, 1), "reason": "gone"}))
            for epc, p in list(self.pending.items()):
                if now - p[4] > limit:
                    del self.pending[epc]
            while self.read_times and now - self.read_times[0] > 5:
                self.read_times.popleft()
        return out

    def clear(self):
        """Forget everything (e.g. inventory stopped) without emitting exits."""
        with self.lock:
            self.tags.clear()
            self.pending.clear()
            self.read_times.clear()

    def snapshot(self, now=None, limit=500):
        now = self.clock() if now is None else now
        with self.lock:
            while self.read_times and now - self.read_times[0] > 5:
                self.read_times.popleft()
            zones = collections.Counter(t.zone for t in self.tags.values())
            for name in self.s["zones"].values():
                zones.setdefault(name, 0)
            tags = sorted(self.tags.values(), key=lambda t: (t.zone, t.epc))[:limit]
            return {"tags_in_field": len(self.tags), "read_rate": round(len(self.read_times) / 5, 1),
                    "zones": dict(sorted(zones.items())), "total_reads": self.total_reads, "ignored": self.ignored,
                    "tags": [{"epc": t.epc, "zone": t.zone, "antenna": t.antenna, "rssi_dbm": t.rssi,
                              "reads": t.reads, "first_seen": round(t.first, 1), "last_seen": round(t.last, 1),
                              "seen_ago_s": round(now - t.last, 1)} for t in tags]}


# ---------------------------------------------------------------- reader control
class ImpinjControl:
    PRESETS = "/api/v1/profiles/inventory/presets"

    def __init__(self, api_factory):
        self.api_factory = api_factory      # () -> readerapi.ReaderApi with the saved login

    def presets(self):
        raw = self.api_factory().get(self.PRESETS)
        ids = []
        for p in raw if isinstance(raw, list) else []:
            if isinstance(p, str):
                ids.append(p)
            elif isinstance(p, dict) and p.get("id"):
                ids.append(str(p["id"]))
        return ids

    def preset(self, pid):
        return self.api_factory().get(f"{self.PRESETS}/{pid}")

    def start(self, pid):
        self.api_factory().send("POST", f"{self.PRESETS}/{pid}/start")

    def stop(self):
        self.api_factory().send("POST", "/api/v1/profiles/stop")

    def status(self):
        st = self.api_factory().get("/api/v1/status")
        st = st if isinstance(st, dict) else {}
        active = st.get("activePreset") or st.get("activeProfile") or {}
        return {"status": st.get("status"), "running": str(st.get("status", "")).lower() == "running",
                "active_preset": active.get("id") if isinstance(active, dict) else active or None, "raw": st}


# ---------------------------------------------------------------- engine
class RfidEngine:
    STREAM = "/api/v1/data/stream"
    STREAM_IDLE_S = 120

    def __init__(self, api_factory, settings, emit, clock=time.time):
        self.api_factory, self.emit, self.clock = api_factory, emit, clock
        self.control = ImpinjControl(api_factory)
        self.tracker = TagTracker(settings, clock)
        self.settings = dict(DEFAULTS, **settings)
        self.reader = {"status": None, "running": False, "active_preset": None, "raw": None}
        self.reader_error = None
        self.presets_cache = []
        self.stream_connected = False
        self.stream_error = None
        self.stream_lines = 0
        self.last_other = collections.deque(maxlen=10)   # unrecognised events, for diagnostics
        self._stop = threading.Event()

    # -- lifecycle
    def start_threads(self):
        for target, name in ((self._stream_loop, "rfid-stream"), (self._sweep_loop, "rfid-sweep"),
                             (self._poll_loop, "rfid-poll")):
            threading.Thread(target=target, daemon=True, name=name).start()

    def configure(self, settings):
        self.settings = dict(DEFAULTS, **settings)
        self.tracker.configure(self.settings)

    # -- control
    def start_inventory(self, preset=None):
        pid = preset or self.settings.get("preset")
        if not pid:
            raise ValueError("No preset chosen.")
        self.control.start(pid)
        log.info("inventory started with preset %s", pid)
        self.refresh_status()
        return pid

    def stop_inventory(self):
        self.control.stop()
        log.info("inventory stopped")
        self.refresh_status()
        # everything currently in the field is now unknown; report it gone
        self._emit_all(self.tracker.sweep(self.clock() + 10 ** 6))
        self.tracker.clear()

    def refresh_status(self):
        try:
            self.reader = self.control.status()
            self.reader_error = None
        except (ApiError, OSError, ValueError) as e:
            self.reader_error = str(e)
        return self.reader

    def refresh_presets(self):
        try:
            self.presets_cache = self.control.presets()
        except (ApiError, OSError, ValueError) as e:
            self.reader_error = str(e)
        return self.presets_cache

    # -- threads
    def _emit_all(self, events):
        for etype, data in events:
            self.emit(etype, **data)

    def _stream_loop(self):
        delay = 2
        while not self._stop.is_set():
            try:
                resp = self.api_factory().open_stream(self.STREAM, timeout=self.STREAM_IDLE_S)
                self.stream_connected, self.stream_error, delay = True, None, 2
                log.info("tag stream connected")
                with resp:
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        line = raw.strip()
                        if not line:
                            continue
                        self.stream_lines += 1
                        self.handle_line(line.decode("utf-8", "replace"))
                self.stream_error = "stream closed by the reader"
            except (socket.timeout, TimeoutError):
                # nothing at all for STREAM_IDLE_S (inventory stopped, no keepalives): reconnect quietly
                self.stream_connected = False
                continue
            except Exception as e:                      # urllib/ssl/connection errors: keep trying
                self.stream_error = str(getattr(e, "reason", e)) or e.__class__.__name__
            if self.stream_connected:
                log.warning("tag stream lost: %s", self.stream_error)
            self.stream_connected = False
            self._stop.wait(delay)
            delay = min(delay * 2, 30)

    def handle_line(self, line):
        ev = parse_event(line)
        if ev[0] == "tag":
            _, epc, port, rssi = ev
            self._emit_all(self.tracker.read(epc, port if port is not None else 0, rssi))
        elif ev[0] == "gpi":
            self.emit("gpi_change", input=f"gpi{ev[1]}", state=ev[2], source="reader")
        elif ev[1]:
            self.last_other.appendleft({"ts": time.strftime("%H:%M:%S"), "kind": ev[1], "line": line[:300]})

    def _sweep_loop(self):
        while not self._stop.wait(0.5):
            try:
                self._emit_all(self.tracker.sweep())
            except Exception:
                log.exception("sweep failed")

    def _poll_loop(self):
        started = False
        while True:
            self.refresh_status()
            if not self.presets_cache:
                self.refresh_presets()
            # auto-start once the reader API answers (it can take a while after boot)
            if not started and self.settings.get("auto_start") and self.settings.get("preset") and self.reader_error is None:
                started = True
                if not self.reader.get("running") or self.reader.get("active_preset") != self.settings["preset"]:
                    try:
                        self.start_inventory()
                    except (ApiError, OSError, ValueError) as e:
                        self.reader_error = f"auto-start failed: {e}"
                        started = False
            if self._stop.wait(10):
                return

    # -- reporting
    def status(self):
        snap = self.tracker.snapshot()
        return {"available": True, "running": bool(self.reader.get("running")), "reader_status": self.reader.get("status"),
                "active_preset": self.reader.get("active_preset"), "reader_error": self.reader_error,
                "stream_connected": self.stream_connected, "stream_error": self.stream_error,
                "stream_lines": self.stream_lines, **snap}
