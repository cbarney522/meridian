"""Tests for app/core/config.py and core.inputs.parse_events.   Run:  python3 -m unittest discover tests"""
import json
import logging
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core.config import AccountError, Store, generate_password  # noqa: E402
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


ROLES = {"developer", "site_admin", "technician", "viewer"}
MANAGERS = {"developer", "site_admin"}


class AccountAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name)
        self.s.ensure_default_account()                       # admin (developer)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_validates(self):
        self.s.create_account("jsmith", "J. Smith", "technician", "Temp-pass-1", ROLES)
        a = {x["user"]: x for x in self.s.list_accounts()}["jsmith"]
        self.assertEqual((a["name"], a["role"], a["must_change"], a["disabled"]), ("J. Smith", "technician", True, False))
        for bad in ("J", "UPPER", "has space", "x" * 40, "-lead"):
            with self.assertRaises(AccountError):
                self.s.create_account(bad, "", "technician", "Temp-pass-1", ROLES)
        with self.assertRaises(AccountError):
            self.s.create_account("jsmith", "", "technician", "Temp-pass-1", ROLES)     # duplicate
        with self.assertRaises(AccountError):
            self.s.create_account("bob", "", "wizard", "Temp-pass-1", ROLES)           # unknown role
        with self.assertRaises(AccountError):
            self.s.create_account("bob", "", "viewer", "short", ROLES)

    def test_disable_blocks_sign_in_and_session_lookup(self):
        self.s.create_account("tech", "", "technician", "Temp-pass-1", ROLES)
        self.assertIsNotNone(self.s.authenticate("tech", "Temp-pass-1"))
        self.assertTrue(self.s.update_account("tech", {"disabled": True}, ROLES, MANAGERS, "admin"))
        self.assertIsNone(self.s.authenticate("tech", "Temp-pass-1"))
        self.assertIsNone(self.s.account("tech"))
        self.s.update_account("tech", {"disabled": False}, ROLES, MANAGERS, "admin")
        self.assertIsNotNone(self.s.account("tech"))

    def test_self_protection_and_last_manager(self):
        with self.assertRaises(AccountError):
            self.s.update_account("admin", {"disabled": True}, ROLES, MANAGERS, "admin")
        with self.assertRaises(AccountError):
            self.s.update_account("admin", {"role": "viewer"}, ROLES, MANAGERS, "admin")
        with self.assertRaises(AccountError):
            self.s.delete_account("admin", MANAGERS, "admin")
        self.s.create_account("boss", "", "site_admin", "Temp-pass-1", ROLES)
        # boss may not remove the only other manager... but admin remains, so demoting admin via boss is fine
        self.s.update_account("admin", {"role": "viewer"}, ROLES, MANAGERS, "boss")
        with self.assertRaises(AccountError):        # boss is now the last manager; can't be disabled by admin
            self.s.update_account("boss", {"disabled": True}, ROLES, MANAGERS, "admin")
        with self.assertRaises(AccountError):
            self.s.delete_account("boss", MANAGERS, "admin")

    def test_reset_generates_and_forces_change(self):
        self.s.create_account("tech", "", "technician", "Temp-pass-1", ROLES)
        self.s.change_password("tech", "Temp-pass-1", "Chosen-pass-2")
        pw = self.s.reset_password("tech", "")
        self.assertRegex(pw, r"^[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$")
        self.assertIsNone(self.s.authenticate("tech", "Chosen-pass-2"))
        self.assertTrue(self.s.authenticate("tech", pw)["must_change"])
        with self.assertRaises(AccountError):
            self.s.reset_password("ghost", "")

    def test_delete_and_record_login(self):
        self.s.create_account("tech", "", "technician", "Temp-pass-1", ROLES)
        self.s.record_login("tech")
        self.assertIsNotNone({x["user"]: x for x in self.s.list_accounts()}["tech"]["last_login"])
        self.s.delete_account("tech", MANAGERS, "admin")
        self.assertNotIn("tech", [x["user"] for x in self.s.list_accounts()])
        chars = set("".join(generate_password() for _ in range(50)).replace("-", ""))
        self.assertFalse(chars & set("0O1lI"))               # ambiguous characters never used


class ParseEventsTests(unittest.TestCase):
    def test_split_and_leftover(self):
        ev = struct.pack("llHHi", 1, 2, 1, 30, 1) + struct.pack("llHHi", 1, 2, 0, 0, 0)
        events, rest = parse_events(ev + ev[:5])
        self.assertEqual(events, [(1, 30, 1), (0, 0, 0)])
        self.assertEqual(rest, ev[:5])


if __name__ == "__main__":
    unittest.main()
