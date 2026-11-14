"""Background watchers that run on the reader.

UsbWatcher    rescans sysfs every couple of seconds and reports devices appearing/disappearing.
BadgeListener reads every keyboard-type USB device's /dev/input/eventN, turns key events into
              scan strings (core.badges.KeyAssembler), normalizes them and reports each scan.
              Ports assigned as badge_in / badge_out are grabbed (EVIOCGRAB) so their keystrokes
              go only to Meridian.

Both take a callback `emit(event_type, **fields)` and keep running if a device misbehaves;
problems are recorded per device and surfaced through status().
"""
import errno
import fcntl
import logging
import os
import select
import struct
import threading
import time

from core import badges, usb

log = logging.getLogger("meridian.inputs")

EVENT = struct.Struct("llHHi")         # struct input_event on 64-bit Linux: timeval(16) + type, code, value
EVIOCGRAB = 0x40044590                 # _IOW('E', 0x90, int)
GRAB_ROLES = ("badge_in", "badge_out")


def parse_events(buf):
    """Split raw bytes from an event device into (type, code, value) tuples; returns (events, leftover)."""
    n = len(buf) // EVENT.size
    out = [EVENT.unpack_from(buf, i * EVENT.size)[2:] for i in range(n)]
    return out, buf[n * EVENT.size:]


class UsbWatcher(threading.Thread):
    def __init__(self, sysfs, emit, interval=2.0):
        super().__init__(daemon=True, name="usb-watcher")
        self.sysfs, self.emit, self.interval = sysfs, emit, interval
        self.known = None

    def run(self):
        while True:
            try:
                now = {d["port"]: d for d in usb.scan(self.sysfs)}
                if self.known is not None:
                    for p in sorted(now.keys() - self.known.keys()):
                        self.emit("usb_attached", port=p, product=now[p]["product"])
                    for p in sorted(self.known.keys() - now.keys()):
                        self.emit("usb_detached", port=p, product=self.known[p]["product"])
                self.known = now
            except Exception:                      # never let the watcher die
                log.exception("usb scan failed")
            time.sleep(self.interval)


class BadgeListener(threading.Thread):
    def __init__(self, sysfs, dev_root, get_assignments, get_settings, emit, rescan=3.0):
        super().__init__(daemon=True, name="badge-listener")
        self.sysfs = sysfs
        self.dev_root = dev_root.rstrip("/")         # "" on the reader; a temp dir in tests
        self.get_assignments = get_assignments
        self.get_settings = get_settings
        self.emit = emit
        self.rescan = rescan
        self.lock = threading.Lock()
        self.devs = {}       # port -> {"fd", "node", "role", "grabbed", "asm", "buf"}
        self.errors = {}     # port -> last error text
        self.stats = {}      # port -> {"last", "scans", "errors"}
        self.on_scan = None  # optional hook for the engine: fn(port, role, raw, result)

    # ---------------------------------------------------------- device management
    def _wanted(self):
        assign = self.get_assignments()
        out = {}
        for d in usb.scan(self.sysfs):
            if d["type"] != "hid-keyboard":
                continue
            node = next((n for n in d["nodes"] if "/input/event" in n), None)
            if node:
                role = assign.get(d["port"], {}).get("role")
                if role != "ignore":
                    out[d["port"]] = {"node": node, "role": role, "product": d["product"]}
        return out

    def _sync_devices(self):
        wanted = self._wanted()
        settings = self.get_settings()
        timeout = settings["timeout_ms"] if settings["end_of_scan"] == "timeout" else 1000
        with self.lock:
            for port in list(self.devs):
                cur = self.devs[port]
                w = wanted.get(port)
                if not w or w["node"] != cur["node"] or (w["role"] in GRAB_ROLES) != cur["grabbed_wanted"]:
                    self._close(port)
            for port, w in wanted.items():
                if port in self.devs:
                    self.devs[port]["role"] = w["role"]
                    self.devs[port]["product"] = w["product"]
                    self.devs[port]["asm"].timeout = timeout / 1000.0
                    continue
                path = self.dev_root + w["node"]
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                except OSError as e:
                    self.errors[port] = f"can't open {w['node']}: {e.strerror}"
                    continue
                grab_wanted = w["role"] in GRAB_ROLES
                grabbed = False
                if grab_wanted:
                    try:
                        fcntl.ioctl(fd, EVIOCGRAB, 1)
                        grabbed = True
                    except OSError as e:
                        self.errors[port] = f"opened {w['node']} but couldn't grab it exclusively: {e.strerror}"
                if grabbed or not grab_wanted:
                    self.errors.pop(port, None)
                self.devs[port] = {"fd": fd, "node": w["node"], "role": w["role"], "product": w["product"],
                                   "grabbed": grabbed, "grabbed_wanted": grab_wanted,
                                   "asm": badges.KeyAssembler(timeout_ms=timeout), "buf": b""}
                log.info("listening on %s (%s, port %s%s)", w["node"], w["product"], port, ", grabbed" if grabbed else "")

    def _close(self, port):
        d = self.devs.pop(port, None)
        if d:
            try:
                os.close(d["fd"])
            except OSError:
                pass

    # ---------------------------------------------------------- reading
    def _scan_done(self, port, raw):
        d = self.devs.get(port, {})
        settings = self.get_settings()
        result = badges.normalize(raw, settings)
        st = self.stats.setdefault(port, {"last": None, "scans": 0, "errors": 0})
        st["scans"] += 1
        st["errors"] += 0 if result["ok"] else 1
        st["last"] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "raw": raw, "badge_id": result["badge_id"], "ok": result["ok"]}
        self.emit("badge_raw", port=port, role=d.get("role"), product=d.get("product"), raw=raw, result=result)
        if self.on_scan:
            try:
                self.on_scan(port, d.get("role"), raw, result)
            except Exception:
                log.exception("scan hook failed")

    def run(self):
        next_sync = 0.0
        while True:
            try:
                if time.monotonic() >= next_sync:
                    self._sync_devices()
                    next_sync = time.monotonic() + self.rescan
                with self.lock:
                    fds = {d["fd"]: port for port, d in self.devs.items()}
                if not fds:
                    time.sleep(0.2)
                    continue
                ready, _, _ = select.select(list(fds), [], [], 0.05)
                with self.lock:
                    for fd in ready:
                        port = fds[fd]
                        d = self.devs.get(port)
                        if not d:
                            continue
                        try:
                            chunk = os.read(fd, EVENT.size * 64)
                        except OSError as e:
                            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                                continue
                            self.errors[port] = f"read failed on {d['node']}: {e.strerror}"
                            self._close(port)
                            continue
                        if not chunk:          # device gone (or FIFO writer closed in tests)
                            self._close(port)
                            continue
                        events, d["buf"] = parse_events(d["buf"] + chunk)
                        for etype, code, value in events:
                            raw = d["asm"].feed(etype, code, value)
                            if raw:
                                self._scan_done(port, raw)
                    for port, d in list(self.devs.items()):   # quiet-gap timeouts
                        raw = d["asm"].poll()
                        if raw:
                            self._scan_done(port, raw)
            except Exception:
                log.exception("badge listener loop error")
                time.sleep(1)

    def status(self):
        with self.lock:
            return {
                "listening": {p: {"node": d["node"], "role": d["role"], "grabbed": d["grabbed"]} for p, d in self.devs.items()},
                "errors": dict(self.errors),
                "stats": {p: dict(s) for p, s in self.stats.items()},
            }
