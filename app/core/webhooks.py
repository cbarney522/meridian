"""Webhooks: POST each event, as JSON, to the URLs a site admin configures.

Deliveries are queued in a small SQLite file (rw_dir/webhooks.db) so nothing is lost while a
receiver is down or the reader restarts. Each request is signed with the endpoint's secret:

    X-Meridian-Signature: sha256=<hex HMAC-SHA256 of "<X-Meridian-Timestamp>.<body>">

A receiver answers 2xx to accept. Anything else (or no answer) is retried after 30 s, 2 min,
10 min, 30 min, then hourly; after 24 h of failures the delivery is marked failed. While an
endpoint is failing its other deliveries wait too, so events arrive in order.
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("meridian.webhooks")

DEFAULTS = {"endpoints": []}
MAX_ENDPOINTS = 5
BACKOFF_S = [30, 120, 600, 1800, 3600]
GIVE_UP_S = 24 * 3600
KEEP_DONE_S = 7 * 24 * 3600          # delivered / failed rows kept this long for the log
MAX_PENDING = 10000                  # per endpoint; oldest dropped beyond this
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)
ID_RE = re.compile(r"^[a-z0-9]{4,16}$")


# ---------------------------------------------------------------- settings
def new_secret():
    return "whsec_" + secrets.token_hex(24)


def validate(body, current, known_types=()):
    """Return (clean, errors). Secrets are kept by endpoint id, created for new endpoints,
    and replaced when an endpoint has regenerate_secret set."""
    errors = []
    if not isinstance(body, dict) or not isinstance(body.get("endpoints", []), list):
        return None, ["Settings must be an object with an endpoints list."]
    old = {e["id"]: e for e in current.get("endpoints", [])}
    eps, seen = [], set()
    if len(body["endpoints"]) > MAX_ENDPOINTS:
        errors.append(f"At most {MAX_ENDPOINTS} endpoints.")
    for n, raw in enumerate(body.get("endpoints", [])[:MAX_ENDPOINTS], 1):
        if not isinstance(raw, dict):
            errors.append(f"Endpoint {n}: not an object.")
            continue
        eid = str(raw.get("id") or "").strip().lower()
        if not ID_RE.match(eid) or eid in seen:
            eid = uuid.uuid4().hex[:8]
        seen.add(eid)
        name = str(raw.get("name") or "").strip()[:60] or f"Endpoint {n}"
        url = str(raw.get("url") or "").strip()
        if not URL_RE.match(url) or len(url) > 500:
            errors.append(f"{name}: enter a full http:// or https:// address.")
        events = raw.get("events") or []
        if not isinstance(events, list):
            events = []
        events = ["*"] if "*" in events else sorted({str(t) for t in events})
        unknown = [t for t in events if t != "*" and known_types and t not in known_types]
        if unknown:
            errors.append(f"{name}: unknown event types {', '.join(unknown)}.")
        if not events:
            errors.append(f"{name}: choose at least one event type, or All events.")
        try:
            timeout = int(raw.get("timeout_s", 10))
            if not 2 <= timeout <= 30:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{name}: timeout must be 2–30 seconds.")
            timeout = 10
        prev = old.get(eid, {})
        secret = new_secret() if raw.get("regenerate_secret") or not prev.get("secret") else prev["secret"]
        eps.append({"id": eid, "name": name, "url": url, "enabled": bool(raw.get("enabled", True)),
                    "events": events, "verify_cert": bool(raw.get("verify_cert", True)),
                    "timeout_s": timeout, "secret": secret})
    return {"endpoints": eps}, errors


def public(settings, show_secrets):
    """Settings for a browser. Secrets only for people who may edit webhooks."""
    eps = []
    for e in settings.get("endpoints", []):
        p = dict(e)
        p["secret"] = e["secret"] if show_secrets else None
        eps.append(p)
    return {"endpoints": eps}


def sign(secret, ts, body):
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify(secret, ts, body, header, tolerance_s=300, now=None):
    """Receiver-side check (used by dev/webhook_receiver.py and the tests)."""
    try:
        if abs((now or time.time()) - int(ts)) > tolerance_s:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(secret, ts, body), header or "")


def subscribed(ep, etype):
    return ep.get("enabled") and ("*" in ep["events"] or etype in ep["events"])


# ---------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):        # a redirect counts as a failure, not a silent GET
        return None


def post(ep, body, event_type, event_id, user_agent="Meridian"):
    """POST body (bytes) to ep. Returns {"ok", "code", "ms", "error", "response"}."""
    ts = str(int(time.time()))
    req = urllib.request.Request(ep["url"], data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": user_agent,
        "X-Meridian-Event": event_type, "X-Meridian-Delivery": event_id,
        "X-Meridian-Timestamp": ts, "X-Meridian-Signature": sign(ep["secret"], ts, body)})
    handlers = [_NoRedirect()]
    if ep["url"].lower().startswith("https"):
        ctx = ssl.create_default_context()
        if not ep.get("verify_cert", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    t0 = time.monotonic()
    out = {"ok": False, "code": None, "error": None, "response": ""}
    try:
        with opener.open(req, timeout=ep.get("timeout_s", 10)) as r:
            out["code"] = r.status
            out["response"] = r.read(300).decode("utf-8", "replace")
            out["ok"] = 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        out["code"] = e.code
        try:
            out["response"] = e.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        out["error"] = f"HTTP {e.code} {e.reason}" + (" (redirects aren't followed; use the final address)" if 300 <= e.code < 400 else "")
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            out["error"] = f"certificate not trusted ({reason.verify_message}); untick Verify certificate for a self-signed receiver"
        elif isinstance(reason, ConnectionRefusedError):
            out["error"] = "connection refused (is the receiver running and listening on that port?)"
        elif isinstance(reason, (TimeoutError, OSError)) and "timed out" in str(reason):
            out["error"] = "no answer in time (firewall, or wrong address?)"
        else:
            out["error"] = str(reason)
    except (TimeoutError, OSError) as e:
        out["error"] = "no answer in time" if "timed out" in str(e) else str(e)
    except ValueError as e:
        out["error"] = str(e)
    out["ms"] = int((time.monotonic() - t0) * 1000)
    if not out["ok"] and not out["error"]:
        out["error"] = f"HTTP {out['code']}"
    return out


# ---------------------------------------------------------------- queue + sender
SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL, endpoint_id TEXT NOT NULL, type TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',          -- pending | delivered | failed
  attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, next_at REAL NOT NULL,
  last_at REAL, last_code INTEGER, last_error TEXT, last_ms INTEGER, response TEXT);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, next_at);
CREATE INDEX IF NOT EXISTS deliveries_ep ON deliveries(endpoint_id, id);
"""


class Webhooks:
    def __init__(self, path, identity, user_agent="Meridian", clock=time.time):
        self.path, self.identity, self.user_agent, self.clock = path, identity, user_agent, clock
        self.settings = dict(DEFAULTS)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.dropped = 0
        self._stop = threading.Event()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # deliveries interrupted mid-send by a restart are simply due again
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True, name="webhooks")
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.wake.set()

    def apply(self, settings):
        with self.lock:
            self.settings = settings
            ids = [e["id"] for e in settings.get("endpoints", [])]
            # endpoints removed from the settings: their queue goes too
            q = ",".join("?" * len(ids)) or "''"
            self.db.execute(f"DELETE FROM deliveries WHERE status='pending' AND endpoint_id NOT IN ({q})", ids)
        self.wake.set()

    def endpoint(self, eid):
        return next((e for e in self.settings.get("endpoints", []) if e["id"] == eid), None)

    # -- building messages
    def envelope(self, etype, data, event_id=None):
        i = self.identity()
        return {"id": event_id or "evt_" + uuid.uuid4().hex[:20], "type": etype,
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
                "reader": {"serial": i.get("reader"), "crib": i.get("crib"), "site": i.get("site"), "name": i.get("name")},
                "data": data}

    def enqueue(self, etype, data):
        """Queue an event for every enabled endpoint that wants it. Returns how many were queued."""
        targets = [e for e in self.settings.get("endpoints", []) if subscribed(e, etype)]
        if not targets:
            return 0
        env = self.envelope(etype, data)
        body = json.dumps(env, separators=(",", ":"))
        now = self.clock()
        with self.lock:
            for ep in targets:
                # an endpoint that is backing off keeps its order: new events wait behind the retry
                row = self.db.execute("SELECT MAX(next_at) FROM deliveries WHERE endpoint_id=? AND status='pending'",
                                      (ep["id"],)).fetchone()
                due = max(now, row[0] or 0)
                self.db.execute("INSERT INTO deliveries(event_id, endpoint_id, type, body, created, next_at) "
                                "VALUES (?,?,?,?,?,?)", (env["id"], ep["id"], etype, body, now, due))
                n = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE endpoint_id=? AND status='pending'",
                                    (ep["id"],)).fetchone()[0]
                if n > MAX_PENDING:
                    extra = n - MAX_PENDING
                    self.db.execute("DELETE FROM deliveries WHERE id IN (SELECT id FROM deliveries WHERE endpoint_id=? "
                                    "AND status='pending' ORDER BY id LIMIT ?)", (ep["id"], extra))
                    self.dropped += extra
                    log.warning("webhook %s queue full; dropped %d oldest", ep["name"], extra)
        self.wake.set()
        return len(targets)

    # -- sending
    def _loop(self):
        while not self._stop.is_set():
            try:
                sent = self.run_due()
            except Exception:
                log.exception("webhook sender failed")
                sent = 0
            if not sent:
                self.wake.wait(self._sleep_for())
                self.wake.clear()

    def _sleep_for(self):
        with self.lock:
            row = self.db.execute("SELECT MIN(next_at) FROM deliveries WHERE status='pending'").fetchone()
        if row[0] is None:
            return 30
        return max(0.2, min(30, row[0] - self.clock()))

    def run_due(self, limit=20):
        """Send what is due now (oldest first, one at a time per endpoint). Returns attempts made."""
        now = self.clock()
        with self.lock:
            rows = self.db.execute("SELECT * FROM deliveries WHERE status='pending' AND next_at<=? ORDER BY id LIMIT ?",
                                   (now, limit)).fetchall()
            self._purge(now)
        done, blocked = 0, set()
        for r in rows:
            if r["endpoint_id"] in blocked:
                continue
            ep = self.endpoint(r["endpoint_id"])
            if not ep or not ep["enabled"]:
                continue
            res = post(ep, r["body"].encode(), r["type"], r["event_id"], self.user_agent)
            self._record(r, res)
            done += 1
            if not res["ok"]:
                blocked.add(r["endpoint_id"])
        return done

    def _record(self, r, res):
        now = self.clock()
        attempts = r["attempts"] + 1
        with self.lock:
            if res["ok"]:
                self.db.execute("UPDATE deliveries SET status='delivered', attempts=?, last_at=?, last_code=?, last_error=NULL, "
                                "last_ms=?, response=? WHERE id=?", (attempts, now, res["code"], res["ms"], res["response"], r["id"]))
                return
            wait = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
            give_up = now - r["created"] >= GIVE_UP_S
            nxt = now + wait
            self.db.execute("UPDATE deliveries SET status=?, attempts=?, next_at=?, last_at=?, last_code=?, last_error=?, "
                            "last_ms=?, response=? WHERE id=?",
                            ("failed" if give_up else "pending", attempts, nxt, now, res["code"], res["error"],
                             res["ms"], res["response"], r["id"]))
            if not give_up:   # keep order: later deliveries to this endpoint wait behind this one
                self.db.execute("UPDATE deliveries SET next_at=? WHERE endpoint_id=? AND status='pending' AND id>? AND next_at<?",
                                (nxt, r["endpoint_id"], r["id"], nxt))

    def _purge(self, now):
        self.db.execute("DELETE FROM deliveries WHERE status IN ('delivered','failed') AND COALESCE(last_at, created)<?",
                        (now - KEEP_DONE_S,))

    # -- actions from the web page
    def test(self, eid):
        """Send a sample event now, outside the queue (logged like any delivery)."""
        ep = self.endpoint(eid)
        if not ep:
            raise ValueError("No such endpoint. Save it first.")
        env = self.envelope("test", {"message": "Test from Meridian. If you can read this, the webhook works."})
        body = json.dumps(env, separators=(",", ":"))
        res = post(ep, body.encode(), "test", env["id"], self.user_agent)
        now = self.clock()
        with self.lock:
            self.db.execute("INSERT INTO deliveries(event_id, endpoint_id, type, body, status, attempts, created, next_at, "
                            "last_at, last_code, last_error, last_ms, response) VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?)",
                            (env["id"], eid, "test", body, "delivered" if res["ok"] else "failed", now, now, now,
                             res["code"], res["error"], res["ms"], res["response"]))
        return dict(res, event=env)

    def resend(self, delivery_id):
        """Queue a fresh copy of a past delivery (same event id, so the receiver can ignore duplicates)."""
        with self.lock:
            r = self.db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not r:
                raise ValueError("No such delivery.")
            if not self.endpoint(r["endpoint_id"]):
                raise ValueError("That endpoint no longer exists.")
            now = self.clock()
            cur = self.db.execute("INSERT INTO deliveries(event_id, endpoint_id, type, body, created, next_at) VALUES (?,?,?,?,?,?)",
                                  (r["event_id"], r["endpoint_id"], r["type"], r["body"], now, now))
        self.wake.set()
        return cur.lastrowid

    def retry_now(self, eid):
        """Skip the wait: make everything pending for this endpoint due now."""
        with self.lock:
            n = self.db.execute("UPDATE deliveries SET next_at=? WHERE endpoint_id=? AND status='pending'",
                                (self.clock(), eid)).rowcount
        self.wake.set()
        return n

    # -- views
    def deliveries(self, eid=None, limit=100):
        q, args = "SELECT * FROM deliveries", []
        if eid:
            q += " WHERE endpoint_id=?"
            args.append(eid)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        names = {e["id"]: e["name"] for e in self.settings.get("endpoints", [])}
        out = []
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        for r in rows:
            d = dict(r)
            d["endpoint"] = names.get(d["endpoint_id"], "(removed)")
            d["body"] = d["body"][:4000]
            out.append(d)
        return out

    def status(self):
        with self.lock:
            return self._status()

    def _status(self):
        now = self.clock()
        out = {"dropped": self.dropped, "endpoints": {}}
        for e in self.settings.get("endpoints", []):
            c = {s: n for s, n in self.db.execute(
                "SELECT status, COUNT(*) FROM deliveries WHERE endpoint_id=? GROUP BY status", (e["id"],))}
            last = self.db.execute("SELECT last_at, last_code, last_error, status FROM deliveries WHERE endpoint_id=? "
                                   "AND last_at IS NOT NULL ORDER BY last_at DESC LIMIT 1", (e["id"],)).fetchone()
            ok = self.db.execute("SELECT MAX(last_at) FROM deliveries WHERE endpoint_id=? AND status='delivered'",
                                 (e["id"],)).fetchone()[0]
            nxt = self.db.execute("SELECT MIN(next_at) FROM deliveries WHERE endpoint_id=? AND status='pending'",
                                  (e["id"],)).fetchone()[0]
            failing = bool(last and last["status"] != "delivered" and last["last_error"])
            out["endpoints"][e["id"]] = {
                "pending": c.get("pending", 0), "delivered": c.get("delivered", 0), "failed": c.get("failed", 0),
                "last_ok_at": ok, "last_error": last["last_error"] if failing else None,
                "retry_in_s": max(0, int(nxt - now)) if failing and nxt else None,
                "state": "off" if not e["enabled"] else "failing" if failing else "ok" if ok else "idle"}
        return out
