"""Tests for app/core/usb.py against a fake sysfs tree.   Run:  python3 -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "dev"))

import fake_sysfs  # noqa: E402
from core import usb  # noqa: E402

ASSIGN = {
    "1-1.1.1": {"role": "badge_in", "vid": "0c27", "pid": "3bfa"},
    "1-1.1.2": {"role": "badge_out", "vid": "0c27", "pid": "3bfa"},
    "1-1.1.3": {"role": "io_board", "vid": "2e8a", "pid": "000a"},
    "1-1.2":   {"role": "storage"},
}


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sys = fake_sysfs.build(os.path.join(self.tmp.name, "sys"))
        self.devs = {d["port"]: d for d in usb.scan(self.sys)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_all_devices_not_root_hub_or_interfaces(self):
        self.assertEqual(sorted(self.devs), sorted(d["port"] for d in fake_sysfs.R700_SETUP))

    def test_order_and_tree(self):
        ports = [d["port"] for d in usb.scan(self.sys)]
        self.assertEqual(ports[:4], ["1-1", "1-1.1", "1-1.1.1", "1-1.1.2"])
        self.assertEqual(self.devs["1-1.1.3"]["parent"], "1-1.1")
        self.assertEqual(self.devs["1-1.1.3"]["depth"], 2)
        self.assertEqual(self.devs["1-1.2"]["parent"], "1-1")
        self.assertIsNone(self.devs["1-1"]["parent"])

    def test_r700_port_labels(self):
        d = self.devs
        self.assertEqual(d["1-1"]["location"], "R700 internal hub")
        self.assertEqual(d["1-1"]["product"], "R700 internal USB hub")
        self.assertEqual(d["1-1.1"]["location"], "Reader USB left")
        self.assertEqual(d["1-1.2"]["location"], "Reader USB middle")
        self.assertEqual(d["1-1.3"]["location"], "Reader USB right")
        self.assertEqual(d["1-1.1.4.1"]["location"], "via Reader USB left")

    def test_no_labels_without_internal_hub(self):
        fake_sysfs.build(self.sys, [x for x in fake_sysfs.R700_SETUP if x["port"] != "1-1"])
        self.assertTrue(all(x["location"] is None for x in usb.scan(self.sys)))

    def test_classification(self):
        t = {p: d["type"] for p, d in self.devs.items()}
        self.assertEqual(t["1-1.1"], "hub")
        self.assertEqual(t["1-1.1.1"], "hid-keyboard")
        self.assertEqual(t["1-1.1.3"], "serial-cdc")
        self.assertEqual(t["1-1.1.4.2"], "hid")
        self.assertEqual(t["1-1.1.4.3"], "serial-usb")
        self.assertEqual(t["1-1.2"], "storage")

    def test_nodes_drivers_attrs(self):
        self.assertEqual(self.devs["1-1.1.1"]["nodes"], ["/dev/input/event2", "/dev/hidraw0"])
        self.assertEqual(self.devs["1-1.1.3"]["nodes"], ["/dev/ttyACM0"])
        self.assertEqual(self.devs["1-1.1.4.3"]["nodes"], ["/dev/ttyUSB0"])
        self.assertEqual(self.devs["1-1.2"]["nodes"], ["/dev/sda"])
        self.assertEqual(self.devs["1-1.1.3"]["drivers"], ["cdc_acm"])
        self.assertEqual(self.devs["1-1.2"]["max_power_ma"], 224)
        self.assertEqual(self.devs["1-1.1.3"]["serial"], "E6614C311B7A3F2C")
        self.assertIsNone(self.devs["1-1.1.1"]["serial"])

    def test_missing_sysfs_returns_empty(self):
        self.assertEqual(usb.scan(os.path.join(self.tmp.name, "nope")), [])


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "sys")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, devices, assign):
        fake_sysfs.build(self.root, devices)
        return {r["port"]: r for r in usb.evaluate(usb.scan(self.root), assign)}

    def test_statuses(self):
        r = self.rows(fake_sysfs.R700_SETUP, ASSIGN)
        self.assertEqual(r["1-1"]["status"], "hub")
        self.assertEqual(r["1-1.1"]["status"], "hub")
        self.assertEqual(r["1-1.1.1"]["status"], "ok")
        self.assertEqual(r["1-1.1.4.1"]["status"], "unassigned")

    def test_missing_when_unplugged(self):
        r = self.rows(fake_sysfs.without(["1-1.1.2"]), ASSIGN)
        self.assertEqual(r["1-1.1.2"]["status"], "missing")
        self.assertEqual(r["1-1.1.2"]["role"], "badge_out")
        self.assertEqual(r["1-1.1.2"]["location"], "via Reader USB left")

    def test_unplugging_hub_makes_children_missing(self):
        r = self.rows(fake_sysfs.without(["1-1.1"]), ASSIGN)
        self.assertEqual({p: r[p]["status"] for p in ("1-1.1.1", "1-1.1.2", "1-1.1.3")},
                         {"1-1.1.1": "missing", "1-1.1.2": "missing", "1-1.1.3": "missing"})
        self.assertEqual(r["1-1.2"]["status"], "ok")

    def test_mismatch_and_changed(self):
        assign = dict(ASSIGN, **{"1-1.1.4.2": {"role": "badge_in"}})   # relay board where a badge reader should be
        del assign["1-1.1.1"]
        assign["1-1.1.4.1"] = {"role": "scanner", "vid": "05e0", "pid": "1200"}  # was a different scanner
        r = self.rows(fake_sysfs.R700_SETUP, assign)
        self.assertEqual(r["1-1.1.4.2"]["status"], "mismatch")
        self.assertEqual(r["1-1.1.4.1"]["status"], "changed")

    def test_validate(self):
        self.assertEqual(usb.validate_assignments(ASSIGN), [])
        errs = usb.validate_assignments({"1-1.1.1": {"role": "badge_in"}, "1-1.1.2": {"role": "badge_in"}})
        self.assertTrue(any("both" in e for e in errs))
        self.assertTrue(usb.validate_assignments({"../etc": {"role": "storage"}}))
        self.assertTrue(usb.validate_assignments({"1-1.1": {"role": "nope"}}))
        self.assertTrue(usb.validate_assignments({"1-1.1": {"role": "storage", "vid": "zz"}}))


if __name__ == "__main__":
    unittest.main()
