"""Tests for app/core/config.py and core.inputs.parse_events.   Run:  python3 -m unittest discover tests"""
import json
import logging
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core.config import Store  # noqa: E402
from core.inputs import parse_events  # noqa: E402

logging.disable(logging.CRITICAL)   # these tests deliberately trigger warnings (corrupt config, default login)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sections_are_independent_and_revisioned(self):
        self.assertEqual(self.s.get("usb", {}), (0, {}))
        self.assertEqual(self.s.put("usb", 0, {"1-1": {"role": "storage"}}), 1)
        self.assertEqual(self.s.put("badges", 0, {"x": 1}), 1)        # other section unaffected
        self.assertIsNone(self.s.put("usb", 0, {}))                    # stale revision -> conflict
        self.assertEqual(self.s.get("usb", {}), (1, {"1-1": {"role": "storage"}}))
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "config.json.bak")))

    def test_corrupt_config_falls_back_to_backup(self):
        self.s.put("usb", 0, {"a": 1})
        self.s.put("usb", 1, {"a": 2})            # .bak now holds revision 1
        with open(os.path.join(self.tmp.name, "config.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(self.s.get("usb", {}), (1, {"a": 1}))

    def test_default_account_and_password_change(self):
        self.s.ensure_default_account()
        a = self.s.authenticate("admin", "changeme")
        self.assertEqual((a["role"], a["must_change"]), ("developer", True))
        self.assertIsNone(self.s.authenticate("admin", "nope"))
        self.assertIsNone(self.s.authenticate("ghost", "changeme"))
        self.assertIn("8 characters", self.s.change_password("admin", "changeme", "short"))
        self.assertIn("incorrect", self.s.change_password("admin", "wrong", "Longenough1"))
        self.assertIsNone(self.s.change_password("admin", "changeme", "Longenough1"))
        self.assertIsNone(self.s.authenticate("admin", "changeme"))
        self.assertFalse(self.s.authenticate("admin", "Longenough1")["must_change"])
        with open(os.path.join(self.tmp.name, "auth.json")) as f:
            self.assertNotIn("Longenough1", f.read())            # only the hash is stored

    def test_ensure_default_does_not_overwrite(self):
        self.s.ensure_default_account()
        self.s.change_password("admin", "changeme", "Longenough1")
        self.s.ensure_default_account()
        self.assertIsNotNone(self.s.authenticate("admin", "Longenough1"))


class ParseEventsTests(unittest.TestCase):
    def test_split_and_leftover(self):
        ev = struct.pack("llHHi", 1, 2, 1, 30, 1) + struct.pack("llHHi", 1, 2, 0, 0, 0)
        events, rest = parse_events(ev + ev[:5])
        self.assertEqual(events, [(1, 30, 1), (0, 0, 0)])
        self.assertEqual(rest, ev[:5])


if __name__ == "__main__":
    unittest.main()
