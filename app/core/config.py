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
import re
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
    # auth.json: {"format": 1, "users": {name: {salt, hash, iterations, role, must_change,
    #             disabled, name, created, last_login}}}
    def _accounts(self):
        return self._read(self.auth_path, "auth") or {"format": 1, "users": {}}

    def ensure_default_account(self):
        with self.lock:
            acc = self._accounts()
            if not acc["users"]:
                acc["users"][DEFAULT_USER] = self._new_record(DEFAULT_PASSWORD, DEFAULT_ROLE, "Default administrator",
                                                              must_change=True)
                self._write(self.auth_path, "auth", acc)
                log.warning("created default login %s / %s (role %s) — change the password in the UI",
                            DEFAULT_USER, DEFAULT_PASSWORD, DEFAULT_ROLE)

    @staticmethod
    def _hash(password):
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS)
        return {"salt": salt.hex(), "hash": h.hex(), "iterations": PBKDF2_ITERS}

    def _new_record(self, password, role, name="", must_change=True):
        return {**self._hash(password), "role": role, "must_change": must_change, "disabled": False,
                "name": name, "created": _now(), "last_login": None}

    def authenticate(self, user, password):
        """Return the account dict (without hash) if the credentials are valid and the account is
        enabled, else None. Always does one PBKDF2 so timing doesn't reveal whether the user exists."""
        rec = self._accounts()["users"].get(user)
        salt = bytes.fromhex(rec["salt"]) if rec else b"\0" * 16
        iters = rec["iterations"] if rec else PBKDF2_ITERS
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters).hex()
        if rec and hmac.compare_digest(h, rec["hash"]) and not rec.get("disabled"):
            return {"user": user, "role": rec["role"], "must_change": rec.get("must_change", False)}
        return None

    def account(self, user):
        """Current role and flags for a signed-in user (looked up per request so changes apply at once).
        None if the account no longer exists or is disabled."""
        rec = self._accounts()["users"].get(user)
        if not rec or rec.get("disabled"):
            return None
        return {"user": user, "role": rec["role"], "must_change": rec.get("must_change", False)}

    def record_login(self, user):
        with self.lock:
            acc = self._accounts()
            if user in acc["users"]:
                acc["users"][user]["last_login"] = _now()
                self._write(self.auth_path, "auth", acc)

    def change_password(self, user, current, new):
        """Returns an error string, or None on success."""
        err = password_problem(new, user)
        if err:
            return err
        with self.lock:
            acc = self._accounts()
            if not self.authenticate(user, current):
                return "current password is incorrect"
            acc["users"][user].update(self._hash(new), must_change=False)
            self._write(self.auth_path, "auth", acc)
        return None

    # -- administration (System › Accounts). Permission checks on *who may call* these are the
    #    server's job; the rules here protect the account list itself.
    def list_accounts(self):
        out = []
        for user, r in sorted(self._accounts()["users"].items()):
            out.append({"user": user, "name": r.get("name", ""), "role": r["role"],
                        "disabled": r.get("disabled", False), "must_change": r.get("must_change", False),
                        "created": r.get("created"), "last_login": r.get("last_login")})
        return out

    def create_account(self, user, name, role, password, roles):
        if not isinstance(user, str) or not USER_RE.fullmatch(user):
            raise AccountError("User name: 2–32 characters, lower-case letters, digits, '.', '_' or '-'.")
        if role not in roles:
            raise AccountError("Unknown role.")
        name = _clean_name(name)
        err = password_problem(password, user)
        if err:
            raise AccountError(err[0].upper() + err[1:] + ".")
        with self.lock:
            acc = self._accounts()
            if user in acc["users"]:
                raise AccountError(f"An account named '{user}' already exists.")
            acc["users"][user] = self._new_record(password, role, name, must_change=True)
            self._write(self.auth_path, "auth", acc)

    def update_account(self, user, changes, roles, manage_perm_roles, actor):
        """changes may hold role, name, disabled. manage_perm_roles: roles that include accounts.manage."""
        with self.lock:
            acc = self._accounts()
            rec = acc["users"].get(user)
            if not rec:
                raise AccountError("No such account.", 404)
            new = dict(rec)
            if "name" in changes:
                new["name"] = _clean_name(changes["name"])
            if "role" in changes:
                if changes["role"] not in roles:
                    raise AccountError("Unknown role.")
                if user == actor and changes["role"] != rec["role"]:
                    raise AccountError("You can't change your own role.")
                new["role"] = changes["role"]
            if "disabled" in changes:
                if not isinstance(changes["disabled"], bool):
                    raise AccountError("disabled must be true or false.")
                if user == actor and changes["disabled"]:
                    raise AccountError("You can't disable your own account.")
                new["disabled"] = changes["disabled"]
            trial = dict(acc["users"], **{user: new})
            if not _has_manager(trial, manage_perm_roles):
                raise AccountError("At least one enabled account must be able to manage accounts.")
            acc["users"][user] = new
            self._write(self.auth_path, "auth", acc)
            return new["role"] != rec["role"] or new.get("disabled") != rec.get("disabled")

    def reset_password(self, user, password):
        if not password:
            password = generate_password()
        err = password_problem(password, user)
        if err:
            raise AccountError(err[0].upper() + err[1:] + ".")
        with self.lock:
            acc = self._accounts()
            if user not in acc["users"]:
                raise AccountError("No such account.", 404)
            acc["users"][user].update(self._hash(password), must_change=True)
            self._write(self.auth_path, "auth", acc)
        return password

    def delete_account(self, user, manage_perm_roles, actor):
        with self.lock:
            acc = self._accounts()
            if user not in acc["users"]:
                raise AccountError("No such account.", 404)
            if user == actor:
                raise AccountError("You can't delete your own account.")
            trial = {u: r for u, r in acc["users"].items() if u != user}
            if not _has_manager(trial, manage_perm_roles):
                raise AccountError("At least one enabled account must be able to manage accounts.")
            del acc["users"][user]
            self._write(self.auth_path, "auth", acc)


class AccountError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


USER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/l/I


def password_problem(pw, user):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if len(pw) > 256:
        return "password is too long"
    if pw == DEFAULT_PASSWORD or pw.lower() == (user or "").lower():
        return "choose a password other than the default or the user name"
    return None


def generate_password(length=12):
    """Readable temporary password, grouped for reading aloud: e.g. 'k7Rq-mX2p-9vTe'."""
    chars = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
    return "-".join(chars[i:i + 4] for i in range(0, length, 4))


def _clean_name(name):
    name = (name or "").strip() if isinstance(name, str) else ""
    return "".join(ch for ch in name if ch.isprintable())[:64]


def _has_manager(users, manage_perm_roles):
    return any(r["role"] in manage_perm_roles and not r.get("disabled") for r in users.values())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
