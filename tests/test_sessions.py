"""Tests for app/core/sessions.py.   Run:  python3 -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core.sessions import Sessions, Throttle  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.c = Clock()
        self.s = Sessions(idle=60, absolute=600, clock=self.c)

    def test_create_lookup_and_idle_expiry(self):
        tok = self.s.create("cliff")
        self.assertGreaterEqual(len(tok), 40)
        self.c.t += 50
        self.assertEqual(self.s.user_for(tok), "cliff")       # refreshes idle timer
        self.c.t += 50
        self.assertEqual(self.s.user_for(tok), "cliff")
        self.c.t += 61
        self.assertIsNone(self.s.user_for(tok))

    def test_absolute_expiry_despite_activity(self):
        tok = self.s.create("cliff")
        for _ in range(12):
            self.c.t += 55
            self.s.user_for(tok)
        self.assertIsNone(self.s.user_for(tok))                # 660 s > 600 s absolute

    def test_unknown_and_drop(self):
        self.assertIsNone(self.s.user_for(None))
        self.assertIsNone(self.s.user_for("nope"))
        tok = self.s.create("a")
        self.s.drop(tok)
        self.assertIsNone(self.s.user_for(tok))

    def test_drop_user_keeps_current(self):
        a1, a2, b = self.s.create("a"), self.s.create("a"), self.s.create("b")
        self.s.drop_user("a", keep=a1)
        self.assertEqual((self.s.user_for(a1), self.s.user_for(a2), self.s.user_for(b)), ("a", None, "b"))


class ThrottleTests(unittest.TestCase):
    def test_locks_after_limit_and_unlocks(self):
        c = Clock()
        t = Throttle(limit=3, window=300, lockout=60, clock=c)
        for _ in range(2):
            t.fail("ip:1", "user:x")
        self.assertEqual(t.locked("ip:1"), 0)
        t.fail("ip:1", "user:x")
        self.assertGreater(t.locked("ip:1"), 0)
        self.assertGreater(t.locked("ip:2", "user:x"), 0)      # same user from another address
        c.t += 61
        self.assertEqual(t.locked("ip:1", "user:x"), 0)

    def test_success_resets_and_old_failures_expire(self):
        c = Clock()
        t = Throttle(limit=3, window=100, lockout=60, clock=c)
        t.fail("k"); t.fail("k"); t.success("k"); t.fail("k")
        self.assertEqual(t.locked("k"), 0)
        t.fail("k"); c.t += 101; t.fail("k")
        self.assertEqual(t.locked("k"), 0)


if __name__ == "__main__":
    unittest.main()
