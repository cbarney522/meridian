"""Tests for app/core/badges.py and app/core/storage.py.   Run:  python3 -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core import badges, storage  # noqa: E402
from core.badges import KeyAssembler, normalize, validate_settings, DEFAULT_SETTINGS  # noqa: E402

CODE = {c: k for k, (c, _) in badges.KEYMAP.items() if k < 60}   # char -> keycode (main block)


def press(asm, text, enter=True):
    out = []
    for ch in text:
        if ch.isupper() or ch == ":":
            asm.feed(1, badges.KEY_LEFTSHIFT, 1)
            out.append(asm.feed(1, CODE[ch.lower() if ch.isalpha() else ";"], 1))
            asm.feed(1, badges.KEY_LEFTSHIFT, 0)
        else:
            out.append(asm.feed(1, CODE[ch], 1))
            asm.feed(1, CODE[ch], 0)                       # key up is ignored
    if enter:
        out.append(asm.feed(1, badges.KEY_ENTER, 1))
    return [o for o in out if o]


class AssemblerTests(unittest.TestCase):
    def test_enter_terminated_with_shift(self):
        self.assertEqual(press(KeyAssembler(), "I105:10442"), ["I105:10442"])

    def test_timeout_terminated(self):
        t = [0.0]
        asm = KeyAssembler(timeout_ms=100, clock=lambda: t[0])
        press(asm, "12345", enter=False)
        self.assertIsNone(asm.poll())
        t[0] = 0.5
        self.assertEqual(asm.poll(), "12345")

    def test_gap_splits_two_scans(self):
        t = [0.0]
        asm = KeyAssembler(timeout_ms=100, clock=lambda: t[0])
        press(asm, "111", enter=False)
        t[0] = 1.0
        self.assertEqual(press(asm, "222"), ["111", "222"])

    def test_ignores_non_key_events_and_max_len(self):
        asm = KeyAssembler(max_len=3)
        self.assertIsNone(asm.feed(0, 0, 0))
        self.assertEqual(press(asm, "123456"), ["123"])


class NormalizeTests(unittest.TestCase):
    def test_default_strips_prefix_splits_and_zeros(self):
        r = normalize("I105:010442")
        self.assertTrue(r["ok"])
        self.assertEqual((r["badge_id"], r["facility"], r["card"]), ("10442", "105", "10442"))
        self.assertEqual(len(r["steps"]), 3)

    def test_facility_card_output(self):
        r = normalize("O105:10442", dict(DEFAULT_SETTINGS, output="facility_card"))
        self.assertEqual(r["badge_id"], "105:10442")

    def test_pattern_rejects(self):
        r = normalize("I105:ABC")
        self.assertFalse(r["ok"])
        self.assertIn("pattern", r["error"])

    def test_empty(self):
        self.assertFalse(normalize("   ")["ok"])

    def test_raw_output_keeps_everything(self):
        r = normalize("I105:10442", dict(DEFAULT_SETTINGS, output="raw", pattern=""))
        self.assertEqual(r["badge_id"], "I105:10442")
        self.assertTrue(r["ok"])

    def test_validate_settings(self):
        self.assertEqual(validate_settings(DEFAULT_SETTINGS), [])
        bad = dict(DEFAULT_SETTINGS, pattern="(", output="x", timeout_ms=5, strip_prefixes=["toolongprefix"])
        self.assertEqual(len(validate_settings(bad)), 4)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mounts = os.path.join(self.tmp.name, "mounts")
        with open(self.mounts, "w") as f:
            f.write("/dev/mmcblk0p3 / ext4 ro,relatime 0 0\n"
                    "/dev/sda1 /run/media/data\\040drive ext4 rw,noatime 0 0\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_mount_partition_and_escape(self):
        m = storage.find_mount(["/dev/sda"], self.mounts)
        self.assertEqual(m, {"device": "/dev/sda1", "mount_point": "/run/media/data drive",
                             "fs_type": "ext4", "read_only": False})
        self.assertIsNone(storage.find_mount(["/dev/sdb"], self.mounts))
        self.assertIsNone(storage.find_mount(["/dev/sda"], os.path.join(self.tmp.name, "none")))

    def test_capacity_and_dir_size(self):
        d = os.path.join(self.tmp.name, "data")
        os.makedirs(os.path.join(d, "sub"))
        for name, n in (("a.db", 1000), ("sub/b.log", 234)):
            with open(os.path.join(d, name), "wb") as f:
                f.write(b"x" * n)
        self.assertEqual(storage.dir_size(d), 1234)
        self.assertEqual(storage.dir_size(os.path.join(d, "a.db")), 1000)
        cap = storage.capacity(d)
        self.assertGreater(cap["size"], 0)

    def test_warnings(self):
        w = storage.warnings_for({"fs_type": "vfat", "read_only": True}, {"size": 100, "free": 5}, 10)
        self.assertEqual(len(w), 3)


if __name__ == "__main__":
    unittest.main()
