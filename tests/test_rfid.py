"""Tests for core/rfid.py: parsing, the tag tracker, and the engine against a fake IoT Device Interface."""
import base64
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core import rfid  # noqa: E402
from core.readerapi import ReaderApi, ApiError  # noqa: E402

EPC = "E28011700000020F1B2C3D4E"
EPC2 = "300833B2DDD9014000000001"


def tag_line(epc=EPC, port=1, rssi=-5500, b64=False, v0=False):
    ev = {"antennaPort": port, "peakRssiCdbm": rssi}
    if b64:
        ev["epc"] = base64.b64encode(bytes.fromhex(epc)).decode()
    else:
        ev["epcHex"] = epc
    if v0:
        return json.dumps({"TagInventoryEvent": ev, "timestamp": "2026-11-14T08:00:00Z"})
    return json.dumps({"timestamp": "2026-11-14T08:00:00Z", "hostname": "impinj-17-1b-e6",
                       "eventType": "tagInventory", "tagInventoryEvent": ev})


class ParseTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(rfid.parse_event(tag_line()), ("tag", EPC, 1, -5500))
        self.assertEqual(rfid.parse_event(tag_line(b64=True, port=3))[1:3], (EPC, 3))
        self.assertEqual(rfid.parse_event(tag_line(v0=True, b64=True))[1], EPC)
        # base64url (Impinj v0 docs) with - and _
        url = base64.urlsafe_b64encode(bytes.fromhex("FBFF" + "00" * 10)).decode()
        self.assertEqual(rfid.epc_hex({"epc": url}), "FBFF" + "00" * 10)
        self.assertEqual(rfid.epc_hex({"epc": EPC}), EPC)                  # hex in "epc"
        g = rfid.parse_event(json.dumps({"eventType": "gpiTransition", "gpiTransitionEvent": {"gpi": 2, "transition": "low-to-high"}}))
        self.assertEqual(g, ("gpi", 2, "high"))
        self.assertEqual(rfid.parse_event('{"inventoryStatusEvent": {"inventoryStatus": "running"}}'), ("other", "inventorystatusevent"))
        self.assertEqual(rfid.parse_event("not json")[0], "other")


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.t = rfid.TagTracker(dict(rfid.DEFAULTS, zones={"1": "portal", "2": "drawer1"}, exit_timeout_s=5,
                                      enter_reads=2, move_quiet_s=1))

    def test_enter_needs_two_reads(self):
        self.assertEqual(self.t.read(EPC, 1, -5000, now=0), [])
        ev = self.t.read(EPC, 1, -5000, now=0.2)
        self.assertEqual(ev, [("tag_enter", {"epc": EPC, "zone": "portal", "antenna": 1, "rssi_dbm": -50.0})])
        self.assertEqual(self.t.read(EPC, 1, -5000, now=0.4), [])           # no repeat while present

    def test_stray_read_forgotten(self):
        self.t.read(EPC, 1, -5000, now=0)
        self.t.sweep(now=6)
        self.assertEqual(self.t.read(EPC, 1, -5000, now=6.1), [])           # count restarted

    def test_exit_after_timeout_with_dwell(self):
        for i in range(5):
            self.t.read(EPC, 2, -6000, now=i)
        self.assertEqual(self.t.sweep(now=8), [])
        ev = self.t.sweep(now=9.5)
        self.assertEqual(ev[0][0], "tag_exit")
        self.assertEqual(ev[0][1]["zone"], "drawer1")
        self.assertEqual(ev[0][1]["dwell_s"], 4)
        self.assertEqual(ev[0][1]["reason"], "gone")
        self.assertEqual(self.t.snapshot(now=9.5)["tags_in_field"], 0)

    def test_move_between_zones(self):
        self.t.read(EPC, 1, -5000, now=0); self.t.read(EPC, 1, -5000, now=0.1)
        # read once in drawer1 while still seen in portal: no move
        self.assertEqual(self.t.read(EPC, 2, -5000, now=0.5), [])
        self.t.read(EPC, 1, -5000, now=0.6)
        self.assertEqual(self.t.read(EPC, 2, -5000, now=0.7), [])
        # portal silent >1 s (last read 0.6) and a second read in drawer1: move
        ev = self.t.read(EPC, 2, -5000, now=1.8)
        self.assertEqual([e[0] for e in ev], ["tag_exit", "tag_enter"])
        self.assertEqual(ev[0][1]["to_zone"], "drawer1")
        self.assertEqual(ev[1][1]["from_zone"], "portal")

    def test_filters_and_unzoned(self):
        t = rfid.TagTracker(dict(rfid.DEFAULTS, zones={"1": "portal"}, rssi_min_dbm=-60, epc_prefix="E280",
                                 ignore_unzoned=True, enter_reads=1))
        self.assertEqual(t.read(EPC2, 1, -5000, now=0), [])                # wrong prefix
        self.assertEqual(t.read(EPC, 1, -7000, now=0), [])                 # too weak
        self.assertEqual(t.read(EPC, 3, -5000, now=0), [])                 # antenna without zone
        self.assertEqual(t.read(EPC, 1, -5000, now=0)[0][0], "tag_enter")
        self.assertEqual(t.snapshot(now=0)["ignored"], 3)
        u = rfid.TagTracker(dict(rfid.DEFAULTS, enter_reads=1))
        self.assertEqual(u.read(EPC, 4, None, now=0)[0][1]["zone"], "Antenna 4")

    def test_actionable_lifecycle(self):
        rows = lambda now: {r["epc"]: r for r in self.t.snapshot(now=now)["tags"]}
        self.t.read(EPC, 1, -5000, now=0)
        r = rows(0.1)[EPC]
        self.assertEqual((r["state"], r["actionable"], r["reads"], r["reads_needed"]), ("confirming", False, 1, 2))
        self.t.read(EPC, 1, -5000, now=0.5)
        self.assertEqual((rows(0.6)[EPC]["state"], rows(0.6)[EPC]["actionable"]), ("present", True))
        self.t.sweep(now=6)                                    # unread for the gone-after time
        r = rows(6)[EPC]
        self.assertEqual((r["state"], r["actionable"]), ("gone", False))
        self.t.read(EPC, 1, -5000, now=7)                     # back: has to be confirmed again
        self.assertEqual((rows(7)[EPC]["state"], rows(7)[EPC]["actionable"]), ("confirming", False))
        self.t.sweep(now=13)                                   # a pending tag that stops being read is listed as gone
        self.assertEqual(rows(13)[EPC]["state"], "gone")
        self.t.sweep(now=13 + rfid.RECENT_KEEP_S + 1)          # and dropped after a while
        self.assertNotIn(EPC, rows(13 + rfid.RECENT_KEEP_S + 1))
        s = self.t.snapshot(now=0)
        self.assertIn("confirming", s)

    def test_snapshot(self):
        for i in range(10):
            self.t.read(EPC, 1, -5000, now=i * 0.1)
            self.t.read(EPC2, 2, -5000, now=i * 0.1)
        s = self.t.snapshot(now=1)
        self.assertEqual(s["tags_in_field"], 2)
        self.assertEqual(s["zones"], {"drawer1": 1, "portal": 1})
        self.assertEqual(s["read_rate"], 4.0)


class AdaptTests(unittest.TestCase):
    def test_interval_preset(self):
        t = rfid.TagTracker(dict(rfid.DEFAULTS, exit_timeout_s=5, enter_reads=2))
        t.report_interval_s = 10                    # preset reports each tag once per 10 s
        self.assertEqual((t.enter_reads(), t.exit_timeout()), (2, 22))         # the setting is kept
        self.assertEqual(t.read(EPC, 1, -5000, now=0), [])                     # one report: not yet
        self.assertEqual(t.sweep(now=9), [])                                   # count survives between reports
        self.assertEqual(t.read(EPC, 1, -5000, now=10)[0][0], "tag_enter")     # second report confirms
        self.assertEqual(t.sweep(now=25), [])                                  # not gone between reports
        self.assertEqual(t.sweep(now=33)[0][0], "tag_exit")

    def test_three_reads_needed(self):
        t = rfid.TagTracker(dict(rfid.DEFAULTS, exit_timeout_s=5, enter_reads=3))
        self.assertEqual(t.read(EPC, 1, -5000, now=0), [])
        self.assertEqual(t.read(EPC, 1, -5000, now=0.1), [])
        self.assertFalse(t.snapshot(now=0.2)["tags"][0]["actionable"])
        self.assertEqual(t.read(EPC, 1, -5000, now=0.2)[0][0], "tag_enter")
        self.assertTrue(t.snapshot(now=0.3)["tags"][0]["actionable"])

    def test_summary(self):
        raw = {"eventConfig": {"tagInventory": {"tagReporting": {"reportingIntervalSeconds": 3}}},
               "antennaConfigs": [{"antennaPort": 2, "transmitPowerCdbm": 3000, "inventorySearchMode": "dual-target",
                                   "inventorySession": 2}, {"antennaPort": 1}]}
        self.assertEqual(rfid.preset_summary(raw), {"antennas": [1, 2], "report_interval_s": 3.0, "search_mode": "dual-target",
                                                    "session": 2, "power_dbm": {"2": 30.0}})
        self.assertEqual(rfid.preset_summary({})["antennas"], [])


class ValidateTests(unittest.TestCase):
    def test_validate(self):
        s, e = rfid.validate({"zones": {"1": "portal", "2": "", "3": "drawer 1"}, "rssi_min_dbm": "-65", "epc_prefix": "e280"}, {})
        self.assertEqual(e, [])
        self.assertEqual(s["zones"], {"1": "portal", "3": "drawer 1"})
        self.assertEqual(s["rssi_min_dbm"], -65.0)
        self.assertEqual(s["epc_prefix"], "E280")
        _, e = rfid.validate({"auto_start": True, "zones": {"99": "x", "1": "bad/name"}, "exit_timeout_s": 0,
                              "enter_reads": 0, "rssi_min_dbm": 5, "epc_prefix": "XYZ"}, {})
        self.assertEqual(len(e), 7)


# ---------------------------------------------------------------- fake reader
class FakeReader(BaseHTTPRequestHandler):
    # Each test makes its own subclass with its own state (see EngineTests.setUp), so a stream
    # connection left over from an earlier test can't take this test's lines.
    state = None

    def log_message(self, *a):
        pass

    def reply(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass          # the client under test hung up first: expected in some tests

    def do_GET(self):
        st = self.state
        if self.path == "/api/v1/profiles/inventory/presets":
            return self.reply(200, ["default", "crib-portal"])
        if self.path == "/api/v1/profiles/inventory/presets/default":
            return self.reply(200, {"eventConfig": {"tagInventory": {"tagReporting": {"reportingIntervalSeconds": 4}}},
                                    "antennaConfigs": [{"antennaPort": 1}]})
        if self.path == "/api/v1/status":
            return self.reply(200, {"status": "running" if st["running"] else "idle",
                                    "activePreset": {"id": st["preset"]} if st["running"] else None})
        if self.path == "/api/v1/data/stream":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                while st["alive"]:
                    while st["lines"]:
                        self.wfile.write((st["lines"].pop(0) + "\n").encode())
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                return
        self.reply(404, {"message": "no such thing"})

    def do_POST(self):
        st = self.state
        for pid in ("crib-portal", "default"):
            if self.path == f"/api/v1/profiles/inventory/presets/{pid}/start":
                st.update(running=True, preset=pid)
                st["calls"].append((round(time.time(), 1), "start", pid))
                return self.reply(204)
        if self.path.endswith("/start"):
            return self.reply(404, {"message": "Preset not found"})
        if self.path == "/api/v1/profiles/stop":
            st.update(running=False, preset=None)
            st["calls"].append((round(time.time(), 1), "stop", None))
            return self.reply(204)
        self.reply(404, {"message": "no such thing"})


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.state = {"running": False, "preset": None, "lines": [], "calls": [], "alive": True}
        handler = type("FakeReaderForTest", (FakeReader,), {"state": self.state})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.events = []
        self.eng = rfid.RfidEngine(lambda: ReaderApi(base, "root", "impinj"),
                                   dict(rfid.DEFAULTS, zones={"1": "portal"}, preset="crib-portal", exit_timeout_s=1,
                                        enter_reads=2, auto_start=True),
                                   lambda t, **d: self.events.append((t, d)))
        self.eng.start_threads()

    def tearDown(self):
        self.eng._stop.set()
        self.state["alive"] = False            # ends this test's stream connections
        self.srv.shutdown()
        self.srv.server_close()

    def wait(self, pred, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def test_end_to_end(self):
        self.assertTrue(self.wait(lambda: self.eng.status()["running"]), "auto-start")
        self.assertEqual(self.eng.status()["active_preset"], "crib-portal")
        self.assertEqual(self.eng.refresh_presets(), ["default", "crib-portal"])
        self.assertTrue(self.wait(lambda: self.eng.stream_connected))
        self.state["lines"] += [tag_line(), tag_line(b64=True),
                                      json.dumps({"gpiTransitionEvent": {"gpi": 1, "transition": "high-to-low"}}),
                                      json.dumps({"somethingNewEvent": {}})]
        self.assertTrue(self.wait(lambda: any(e[0] == "tag_enter" for e in self.events)))
        self.assertIn(("gpi_change", {"input": "gpi1", "state": "low", "source": "reader"}), self.events)
        self.assertEqual(self.eng.status()["tags_in_field"], 1)
        self.assertEqual(self.eng.last_other[0]["kind"], "somethingnewevent")
        self.assertTrue(self.wait(lambda: any(e[0] == "tag_exit" for e in self.events), 5), "gone after timeout")
        self.eng.stop_inventory()
        self.assertFalse(self.eng.status()["running"])
        with self.assertRaises(ApiError) as cm:
            self.eng.start_inventory("nope")
        self.assertIn("Preset not found", str(cm.exception))

    def test_switch_stops_first_and_adapts(self):
        self.assertTrue(self.wait(lambda: self.eng.status()["running"]), "auto-start")
        self.state["calls"].clear()
        self.eng.start_inventory("default")
        self.assertEqual([(c[1], c[2]) for c in self.state["calls"]], [("stop", None), ("start", "default")])
        eff = self.eng.status()["effective"]
        self.assertEqual((eff["report_interval_s"], eff["enter_reads"], eff["exit_timeout_s"]), (4, 2, 10))
        self.eng.stop_inventory()
        self.assertEqual(self.eng.status()["effective"]["report_interval_s"], 0)

    def test_stop_reports_tags_gone(self):
        self.assertTrue(self.wait(lambda: self.eng.stream_connected))
        self.state["lines"] += [tag_line(EPC2), tag_line(EPC2)]
        self.assertTrue(self.wait(lambda: self.eng.status()["tags_in_field"] == 1))
        self.eng.stop_inventory()
        self.assertEqual(self.eng.status()["tags_in_field"], 0)
        self.assertTrue(any(e[0] == "tag_exit" and e[1]["epc"] == EPC2 for e in self.events))


class PatternTests(EngineTests):
    PATTERN = {"id": "two", "name": "Two step", "continuous": False,
               "steps": [{"preset": "crib-portal", "seconds": 1, "pause_s": 0.5},
                         {"preset": "default", "seconds": 1, "pause_s": 0}]}

    def setUp(self):
        super().setUp()
        self.eng.configure(dict(self.eng.settings, auto_start=False, patterns=[self.PATTERN]))
        self.assertTrue(self.wait(lambda: self.eng.stream_connected))
        self.state["calls"].clear()

    def test_end_to_end(self):          # (inherited test not relevant here)
        pass

    def test_stop_reports_tags_gone(self):
        pass

    def test_switch_stops_first_and_adapts(self):
        pass

    def test_pattern_detection_applies_while_running(self):
        pat = dict(self.PATTERN, id="own", name="Own", continuous=True,
                   detection={"enter_reads": 1, "exit_timeout_s": 60, "move_quiet_s": 0, "rssi_min_dbm": -60, "epc_prefix": ""})
        self.eng.configure(dict(self.eng.settings, patterns=[pat]))
        self.eng.start_inventory("pattern:own")
        eff = self.eng.status()["effective"]
        self.assertEqual((eff["source"], eff["enter_reads"], eff["exit_timeout_s"], eff["rssi_min_dbm"]), ("pattern", 1, 60, -60))
        self.eng.stop_inventory()
        eff = self.eng.status()["effective"]
        self.assertEqual((eff["source"], eff["enter_reads"], eff["exit_timeout_s"]), ("global", 2, 1))

    def test_runs_steps_once(self):
        self.eng.start_inventory("pattern:two")
        st = self.eng.status()
        self.assertTrue(st["running"])
        self.assertEqual((st["pattern"]["name"], st["pattern"]["step"], st["pattern"]["phase"]), ("Two step", 1, "scanning"))
        self.assertGreater(self.eng.tracker.exit_timeout(), 3)        # stretched to a full cycle
        self.assertTrue(self.wait(lambda: (self.eng.pattern_status() or {}).get("phase") == "pause", 3))
        self.assertTrue(self.wait(lambda: (self.eng.pattern_status() or {}).get("step") == 2, 3))
        self.assertTrue(self.wait(lambda: self.eng.pattern_status() is None, 4), "finishes when not continuous")
        calls = [(c[1], c[2]) for c in self.state["calls"]]
        self.assertEqual(calls, [("start", "crib-portal"), ("stop", None), ("start", "default"), ("stop", None)])
        t = self.state["calls"]
        self.assertGreaterEqual(t[2][0] - t[1][0], 0.4)               # the pause between steps
        self.assertEqual(self.eng.tracker.exit_timeout(), 1)          # back to the setting

    def test_continuous_until_stopped(self):
        pat = dict(self.PATTERN, id="loop", name="Loop", continuous=True,
                   steps=[{"preset": "crib-portal", "seconds": 1, "pause_s": 0}, {"preset": "default", "seconds": 1, "pause_s": 0}])
        self.eng.configure(dict(self.eng.settings, patterns=[pat]))
        self.eng.start_inventory("pattern:loop")
        self.assertTrue(self.wait(lambda: (self.eng.pattern_status() or {}).get("cycle") == 2, 5))
        self.eng.stop_inventory()
        self.assertIsNone(self.eng.pattern_status())
        self.assertFalse(self.eng.status()["running"])
        n = len(self.state["calls"])
        time.sleep(1.5)
        self.assertEqual(len(self.state["calls"]), n, "no more steps after stop")

    def test_bad_first_step_is_reported(self):
        bad = dict(self.PATTERN, id="bad", name="Bad", steps=[{"preset": "nope", "seconds": 1, "pause_s": 0}] * 2)
        self.eng.configure(dict(self.eng.settings, patterns=[bad]))
        with self.assertRaises(ApiError):
            self.eng.start_inventory("pattern:bad")
        self.assertIsNone(self.eng.pattern_status())
        with self.assertRaises(ValueError):
            self.eng.start_inventory("pattern:missing")


class OverrideTests(unittest.TestCase):
    def test_override_and_restore(self):
        t = rfid.TagTracker(dict(rfid.DEFAULTS, exit_timeout_s=5, enter_reads=2, epc_prefix=""))
        t.set_override({"enter_reads": 1, "exit_timeout_s": 30, "epc_prefix": "E280", "move_quiet_s": 0, "rssi_min_dbm": None})
        self.assertEqual((t.enter_reads(), t.exit_timeout()), (1, 30))
        self.assertEqual(t.read(EPC2, 1, -5000, now=0), [])               # prefix from the override
        self.assertEqual(t.read(EPC, 1, -5000, now=0)[0][0], "tag_enter")
        t.configure(dict(rfid.DEFAULTS, exit_timeout_s=7))              # global edit while running keeps the override
        self.assertEqual(t.exit_timeout(), 30)
        t.set_override(None)
        self.assertEqual((t.enter_reads(), t.exit_timeout()), (2, 7))


class PatternValidateTests(unittest.TestCase):
    def test_detection_override(self):
        pat = {"name": "P", "steps": [{"preset": "a", "seconds": 1}, {"preset": "b", "seconds": 1}],
               "detection": {"enter_reads": "1", "exit_timeout_s": "30", "move_quiet_s": 2, "rssi_min_dbm": "", "epc_prefix": "e2"}}
        s, e = rfid.validate({"patterns": [pat]}, {})
        self.assertEqual(e, [])
        self.assertEqual(s["patterns"][0]["detection"], {"enter_reads": 1, "exit_timeout_s": 30.0, "move_quiet_s": 2.0,
                                                         "rssi_min_dbm": None, "epc_prefix": "E2"})
        _, e = rfid.validate({"patterns": [dict(pat, detection={"enter_reads": 0, "exit_timeout_s": 0})]}, {})
        self.assertTrue(all(x.startswith("Firing pattern “P”") for x in e) and len(e) == 2, e)
        s, _ = rfid.validate({"patterns": [{k: v for k, v in pat.items() if k != "detection"}]}, {})
        self.assertNotIn("detection", s["patterns"][0])

    def test_validate(self):
        good = {"name": "Portal then drawers", "continuous": True,
                "steps": [{"preset": "LPR", "seconds": "5", "pause_s": "1"}, {"preset": "deep-scan", "seconds": 10}]}
        s, e = rfid.validate({"patterns": [good], "preset": "pattern:portal-then-drawers"}, {})
        self.assertEqual(e, [])
        self.assertEqual(s["patterns"][0]["id"], "portal-then-drawers")
        self.assertEqual(s["patterns"][0]["steps"][1], {"preset": "deep-scan", "seconds": 10.0, "pause_s": 0.0})
        self.assertEqual(rfid.pattern_cycle_s(s["patterns"][0]), 16)
        _, e = rfid.validate({"patterns": [{"name": "", "steps": [{"preset": "", "seconds": 0, "pause_s": -1}]},
                                           {"name": "A", "steps": [{"preset": "x", "seconds": 1}] * 2},
                                           {"name": "a", "steps": [{"preset": "x", "seconds": 1}] * 2}],
                              "preset": "pattern:gone"}, {})
        joined = " ".join(e)
        for frag in ("give it a name", "choose a reader preset", "1–3600", "0–600", "at least two", "Two firing patterns", "no longer exists"):
            self.assertIn(frag, joined)


if __name__ == "__main__":
    unittest.main()
