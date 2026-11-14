"""Persistent settings and accounts in the CAP's writable directory (rw_dir).

config.json holds independent *sections* ("usb", "badges", "storage", …), each with its own
revision, so saving badge settings never conflicts with someone saving USB assignments.
Writes are atomic (temp file, fsync, rename, fsync dir) with the previous file kept as .bak.

auth.json holds technician/admin accounts: PBKDF2-SHA256 hashes and a role from roles.json.
A default admin/changeme account (role developer) is created on first start and flagged
must_change until its password is changed.
"""
import copy
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import threading
from datetime import datetime, timezone

log = logging.getLogger("meridian.config")

DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "changeme"
DEFAULT_ROLE = "developer"
PBKDF2_ITERS = 120_000
MIN_PASSWORD = 8


class Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.cfg_path = os.path.join(data_dir, "config.json")
        self.auth_path = os.path.join(data_dir, "auth.json")
        self.lock = threading.Lock()
        self.writable = self._probe()
        self._mem = {"config": None, "auth": None}      # used only if rw_dir isn't writable

    def _probe(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            p = os.path.join(self.dir, ".write_probe")
            with open(p, "w") as f:
                f.write("ok")
            os.remove(p)
            return True
        except OSError as e:
            log.error("data dir %s is not writable (%s); settings will not survive a restart", self.dir, e)
            return False

    # ---------------------------------------------------------- low level
    def _atomic_write(self, path, obj):
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)
        try:
            dfd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _read(self, path, key):
        if not self.writable:
            return copy.deepcopy(self._mem[key])
        for p in (path, path + ".bak"):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if p.endswith(".bak"):
                    log.warning("%s unreadable; using backup", os.path.basename(path))
                return data
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                log.error("could not read %s: %s", p, e)
        return None

    def _write(self, path, key, obj):
        if self.writable:
            self._atomic_write(path, obj)
        else:
            self._mem[key] = copy.deepcopy(obj)

    # ---------------------------------------------------------- sections
    def get(self, section, default):
        """Return (revision, data) for a section; (0, default) if never saved."""
        cfg = self._read(self.cfg_path, "config") or {}
        sec = cfg.get("sections", {}).get(section)
        if not sec:
            return 0, copy.deepcopy(default)
        return sec["revision"], sec["data"]

    def put(self, section, expected_revision, data):
        """Save a section if its revision still matches. Returns the new revision, or None on conflict."""
        with self.lock:
            cfg = self._read(self.cfg_path, "config") or {"format": 1, "sections": {}}
            cur = cfg["sections"].get(section, {"revision": 0})
            if expected_revision != cur["revision"]:
                return None
            cfg["sections"][section] = {
                "revision": cur["revision"] + 1,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "data": data,
            }
            self._write(self.cfg_path, "config", cfg)
            return cfg["sections"][section]["revision"]

    # ---------------------------------------------------------- accounts
    def _accounts(self):
        return self._read(self.auth_path, "auth") or {"format": 1, "users": {}}

    def ensure_default_account(self):
        with self.lock:
            acc = self._accounts()
            if not acc["users"]:
                acc["users"][DEFAULT_USER] = self._record(DEFAULT_PASSWORD, DEFAULT_ROLE, must_change=True)
                self._write(self.auth_path, "auth", acc)
                log.warning("created default login %s / %s (role %s) — change the password in the UI",
                            DEFAULT_USER, DEFAULT_PASSWORD, DEFAULT_ROLE)

    @staticmethod
    def _record(password, role, must_change=False):
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS)
        return {"salt": salt.hex(), "hash": h.hex(), "iterations": PBKDF2_ITERS, "role": role, "must_change": must_change}

    def authenticate(self, user, password):
        """Return the account dict (without hash) if the credentials are valid, else None.
        Always does one PBKDF2 so response time doesn't reveal whether the user exists."""
        rec = self._accounts()["users"].get(user)
        salt = bytes.fromhex(rec["salt"]) if rec else b"\0" * 16
        iters = rec["iterations"] if rec else PBKDF2_ITERS
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters).hex()
        if rec and hmac.compare_digest(h, rec["hash"]):
            return {"user": user, "role": rec["role"], "must_change": rec.get("must_change", False)}
        return None

    def change_password(self, user, current, new):
        """Returns an error string, or None on success."""
        if not isinstance(new, str) or len(new) < MIN_PASSWORD:
            return f"new password must be at least {MIN_PASSWORD} characters"
        if new == DEFAULT_PASSWORD or new == user:
            return "choose a password other than the default or your user name"
        with self.lock:
            acc = self._accounts()
            if not self.authenticate(user, current):
                return "current password is incorrect"
            role = acc["users"][user]["role"]
            acc["users"][user] = self._record(new, role, must_change=False)
            self._write(self.auth_path, "auth", acc)
        return None
