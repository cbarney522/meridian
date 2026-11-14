"""Sign-in sessions for the web UI.

A session is a random 256-bit token handed to the browser in a cookie (HttpOnly, Secure,
SameSite=Strict). Only the server knows which user a token belongs to. Sessions end after
`idle` seconds without a request or `absolute` seconds after sign-in, on sign-out, or when the
server restarts (they're kept in memory only).

Login throttling lives here too: too many failures from one address, or for one user name,
lock further attempts for a while.
"""
import secrets
import threading
import time


class Sessions:
    def __init__(self, idle=30 * 60, absolute=12 * 3600, clock=time.time):
        self.idle, self.absolute, self.clock = idle, absolute, clock
        self.lock = threading.Lock()
        self.by_token = {}          # token -> {"user", "created", "seen"}

    def create(self, user):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            self._prune(now)
            self.by_token[token] = {"user": user, "created": now, "seen": now}
        return token

    def user_for(self, token):
        """Return the user name for a live token (and refresh its idle timer), else None."""
        if not token:
            return None
        now = self.clock()
        with self.lock:
            s = self.by_token.get(token)
            if not s:
                return None
            if now - s["seen"] > self.idle or now - s["created"] > self.absolute:
                del self.by_token[token]
                return None
            s["seen"] = now
            return s["user"]

    def drop(self, token):
        with self.lock:
            self.by_token.pop(token, None)

    def drop_user(self, user, keep=None):
        """End every session of `user` except `keep` (used after a password change)."""
        with self.lock:
            for t in [t for t, s in self.by_token.items() if s["user"] == user and t != keep]:
                del self.by_token[t]

    def count_user(self, user):
        now = self.clock()
        with self.lock:
            return sum(1 for s in self.by_token.values() if s["user"] == user
                       and now - s["seen"] <= self.idle and now - s["created"] <= self.absolute)

    def count(self):
        with self.lock:
            return len(self.by_token)

    def _prune(self, now):
        dead = [t for t, s in self.by_token.items()
                if now - s["seen"] > self.idle or now - s["created"] > self.absolute]
        for t in dead:
            del self.by_token[t]


class Throttle:
    """At most `limit` failures per key within `window` seconds, then locked for `lockout` seconds."""

    def __init__(self, limit=5, window=300, lockout=60, clock=time.time):
        self.limit, self.window, self.lockout, self.clock = limit, window, lockout, clock
        self.lock = threading.Lock()
        self.fails = {}             # key -> [timestamps]
        self.locked_until = {}

    def locked(self, *keys):
        """Seconds remaining if any key is locked, else 0."""
        now = self.clock()
        with self.lock:
            return max([int(self.locked_until.get(k, 0) - now) + 1 for k in keys
                        if self.locked_until.get(k, 0) > now] or [0])

    def fail(self, *keys):
        now = self.clock()
        with self.lock:
            for k in keys:
                recent = [t for t in self.fails.get(k, []) if now - t < self.window] + [now]
                self.fails[k] = recent
                if len(recent) >= self.limit:
                    self.locked_until[k] = now + self.lockout
                    self.fails[k] = []

    def success(self, *keys):
        with self.lock:
            for k in keys:
                self.fails.pop(k, None)
