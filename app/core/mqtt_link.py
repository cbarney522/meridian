"""MQTT link: keeps one connection to the site broker, publishes status + events, takes commands.

Topics (prefix defaults to "meridian"; <reader> is the reader's serial number, or its hostname
until the serial is known; <site> is the Site ID, or "unassigned"):

    <prefix>/<site>/<reader>/status        retained; {"state":"online",...}; the broker publishes
                                          {"state":"offline"} (Last Will) if the reader drops off
    <prefix>/<site>/<reader>/evt/<type>    events (USB attached, badge scans, ...)
    <prefix>/<site>/<reader>/cmd/<name>    commands to this reader     (JSON {"id":..., "args":{...}})
    <prefix>/<site>/all/cmd/<name>         commands to every reader at the site
    <prefix>/<site>/<reader>/ack           replies {"id","cmd","ok","result"|"error","ts"}

Use the reader ID as its broker user name so one ACL line covers every reader:
    pattern readwrite meridian/+/%u/#      and   pattern read meridian/+/all/#

Messages published while disconnected wait in a bounded in-memory queue (oldest dropped first).
A persistent SQLite outbox replaces it when the rules engine lands.
"""
import collections
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor"))
import paho.mqtt.client as paho  # noqa: E402  (vendored, pure Python)

log = logging.getLogger("meridian.mqtt")

DEFAULTS = {"enabled": False, "host": "", "port": 1883, "tls": False, "verify_cert": True, "ca_pem": "",
            "username": "", "password": "", "prefix": "meridian", "heartbeat_s": 60, "qos": 1}
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*$")
QUEUE_MAX = 1000
LOG_MAX = 100


def validate(body, current):
    """Return (clean, errors). A blank password keeps the saved one; clear_password removes it."""
    errors = []
    if not isinstance(body, dict):
        return None, ["Settings must be an object."]
    out = dict(DEFAULTS)
    out.update({k: current.get(k, v) for k, v in DEFAULTS.items()})
    out["enabled"] = bool(body.get("enabled", out["enabled"]))
    host = str(body.get("host", out["host"]) or "").strip()
    if host and not HOST_RE.match(host):
        errors.append("Broker: a host name or IP address.")
    if out["enabled"] and not host:
        errors.append("Enter the broker's host name or IP address.")
    out["host"] = host
    try:
        out["port"] = int(body.get("port", out["port"]))
        if not 1 <= out["port"] <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Port must be 1–65535.")
    out["tls"] = bool(body.get("tls", out["tls"]))
    out["verify_cert"] = bool(body.get("verify_cert", out["verify_cert"]))
    pem = str(body.get("ca_pem", out["ca_pem"]) or "").strip()
    if pem and "BEGIN CERTIFICATE" not in pem:
        errors.append("CA certificate must be PEM text (-----BEGIN CERTIFICATE-----).")
    out["ca_pem"] = pem[:20000]
    out["username"] = str(body.get("username", out["username"]) or "").strip()[:128]
    if body.get("clear_password"):
        out["password"] = ""
    elif isinstance(body.get("password"), str) and body["password"]:
        out["password"] = body["password"][:256]
    prefix = str(body.get("prefix", out["prefix"]) or "").strip().strip("/")
    if not TOPIC_RE.match(prefix):
        errors.append("Topic prefix: letters, digits, - and _, optionally with / between levels.")
    out["prefix"] = prefix
    try:
        out["heartbeat_s"] = int(body.get("heartbeat_s", out["heartbeat_s"]))
        if not 10 <= out["heartbeat_s"] <= 3600:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Heartbeat must be 10–3600 seconds.")
    out["qos"] = 1 if int(body.get("qos", out["qos"]) or 0) else 0
    return out, errors


def public(settings):
    """Settings safe to send to a browser."""
    s = {k: v for k, v in settings.items() if k != "password"}
    s["password_set"] = bool(settings.get("password"))
    return s


def topic_safe(s):
    return re.sub(r"[^A-Za-z0-9_\-]", "-", s or "") or "unknown"


def _tls_context(st):
    ctx = ssl.create_default_context()
    if st.get("ca_pem"):
        ctx.load_verify_locations(cadata=st["ca_pem"])
    if not st.get("verify_cert", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _client(client_id, st):
    c = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=paho.MQTTv311)
    if st.get("username"):
        c.username_pw_set(st["username"], st.get("password") or None)
    if st.get("tls"):
        c.tls_set_context(_tls_context(st))
    c.connect_timeout = 6
    return c


def _reason_text(rc):
    text = str(rc)
    hints = {"Not authorized": "the broker refused this user name/password or client ID",
             "Bad user name or password": "wrong user name or password"}
    return hints.get(text, text)


def test_connection(st, client_id="meridian-test", timeout=7):
    """Try to connect with settings st. Returns {"ok": bool, "error": str|None, "ms": int}."""
    if not st.get("host"):
        return {"ok": False, "error": "Enter the broker's host name or IP address.", "ms": 0}
    done = threading.Event()
    result = {"ok": False, "error": "no answer from the broker"}

    def on_connect(_c, _u, _f, rc, _p):
        result.update(ok=not rc.is_failure, error=None if not rc.is_failure else _reason_text(rc))
        done.set()

    t0 = time.monotonic()
    c = _client(f"{client_id}-{int(time.time()) % 100000}", st)
    c.on_connect = on_connect
    try:
        c.connect(st["host"], int(st["port"]), keepalive=10)
    except ssl.SSLError as e:
        return {"ok": False, "error": f"TLS problem: {e.reason or e}", "ms": 0}
    except (OSError, ValueError) as e:
        msg = str(e)
        if isinstance(e, ConnectionRefusedError):
            msg = "connection refused (is the broker running and listening on that port?)"
        elif isinstance(e, (socket.timeout, TimeoutError)):
            msg = "timed out (check the address, port and firewall)"
        elif isinstance(e, socket.gaierror):
            msg = "can't resolve that host name"
        return {"ok": False, "error": msg, "ms": 0}
    c.loop_start()
    done.wait(timeout)
    c.loop_stop()
    try:
        c.disconnect()
    except Exception:
        pass
    return {**result, "ms": int((time.monotonic() - t0) * 1000)}


class MqttLink:
    """identity() -> {"reader","crib","site","name","version","ip","firmware","uptime_s"};
    on_command(name, args) -> result dict (raise ValueError for a refused command)."""

    def __init__(self, identity, on_command, emit=None):
        self.identity, self.on_command, self.emit = identity, on_command, emit
        self.lock = threading.RLock()
        self.settings = dict(DEFAULTS)
        self.client = None
        self.ident = None
        self.connected = False
        self.since = None
        self.last_error = None
        self.queue = collections.deque(maxlen=QUEUE_MAX)
        self.dropped = 0
        self.sent = 0
        self.received = 0
        self.traffic = collections.deque(maxlen=LOG_MAX)
        self.last_status = 0
        self._stop = threading.Event()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="mqtt-heartbeat").start()

    # ---------------------------------------------------------- topics
    def base(self, ident=None):
        i = ident or self.ident or self.identity()
        return f"{self.settings['prefix']}/{topic_safe(i.get('site') or 'unassigned')}/{topic_safe(i['reader'])}"

    def topics(self):
        i = self.ident or self.identity()
        b = self.base(i)
        site = f"{self.settings['prefix']}/{topic_safe(i.get('site') or 'unassigned')}"
        return {"status": f"{b}/status", "events": f"{b}/evt/<type>", "commands": f"{b}/cmd/<name>",
                "broadcast": f"{site}/all/cmd/<name>", "acks": f"{b}/ack", "subscribe_all": f"{site}/#",
                "client_id": f"meridian-{topic_safe(i['reader'])}"}

    # ---------------------------------------------------------- lifecycle
    def apply(self, settings):
        """(Re)start with new settings. Safe to call any time."""
        self._restart(dict(DEFAULTS, **settings))

    def _restart(self, settings=None):
        # Detach the old client under the lock, stop it outside the lock (stopping joins paho's
        # network thread, whose callbacks also take the lock), then start the new one.
        with self.lock:
            old, old_ident, was = self.client, self.ident, self.connected
            self.client, self.connected, self.since = None, False, None
        self._shutdown(old, old_ident, was)
        with self.lock:
            if settings is not None:
                self.settings = settings
                self.last_error = None
            if self.settings["enabled"] and self.settings["host"] and self.client is None:
                self._start()

    def _start(self):
        st = self.settings
        self.ident = self.identity()
        b = self.base(self.ident)
        c = _client(f"meridian-{topic_safe(self.ident['reader'])}", st)
        c.will_set(f"{b}/status", json.dumps({"state": "offline", "reader": self.ident["reader"]}), qos=1, retain=True)
        c.reconnect_delay_set(1, 60)
        c.on_connect, c.on_disconnect, c.on_message = self._on_connect, self._on_disconnect, self._on_message
        self.client = c
        try:
            c.connect_async(st["host"], int(st["port"]), keepalive=30)
            c.loop_start()
            log.info("MQTT connecting to %s:%s as %s", st["host"], st["port"], b)
        except (OSError, ValueError, ssl.SSLError) as e:
            self.last_error = str(e)
            log.warning("MQTT start failed: %s", e)

    def _shutdown(self, c, ident, was_connected):
        if not c:
            return
        try:
            if was_connected and ident:
                # a clean shutdown: say so (the Last Will only fires on unexpected drops)
                c.publish(f"{self.base(ident)}/status",
                          json.dumps({"state": "offline", "reader": ident["reader"], "reason": "stopped"}),
                          qos=1, retain=True).wait_for_publish(2)
            c.disconnect()
            c.loop_stop()
        except Exception:
            pass

    def stop(self):
        self._restart(dict(self.settings, enabled=False))

    # ---------------------------------------------------------- callbacks (paho network thread)
    def _on_connect(self, c, _u, _flags, rc, _props):
        if c is not self.client:
            return
        if rc.is_failure:
            self.last_error = _reason_text(rc)
            log.warning("MQTT connect refused: %s", self.last_error)
            return
        self.connected, self.since, self.last_error = True, int(time.time()), None
        b = self.base(self.ident)
        site = f"{self.settings['prefix']}/{topic_safe(self.ident.get('site') or 'unassigned')}"
        c.subscribe([(f"{b}/cmd/+", 1), (f"{site}/all/cmd/+", 1)])
        self._publish_status()
        while self.queue:
            topic, payload, qos, retain = self.queue.popleft()
            self._raw_publish(topic, payload, qos, retain)
        log.info("MQTT connected")
        if self.emit:
            self.emit("mqtt_state", connected=True)

    def _on_disconnect(self, c, _u, _flags, rc, _props):
        if c is not self.client:
            return
        was = self.connected
        self.connected, self.since = False, None
        if rc and rc.is_failure:
            self.last_error = _reason_text(rc) if str(rc) != "Unspecified error" else "connection lost"
        if was:
            log.warning("MQTT disconnected: %s", rc)
            if self.emit:
                self.emit("mqtt_state", connected=False)

    def _on_message(self, c, _u, msg):
        if c is not self.client:
            return
        self.received += 1
        text = msg.payload.decode("utf-8", "replace")
        self._log("in", msg.topic, text)
        name = msg.topic.rsplit("/", 1)[-1]
        try:
            body = json.loads(text) if text.strip() else {}
            if not isinstance(body, dict):
                raise ValueError("payload must be a JSON object")
        except ValueError as e:
            return self._ack({"cmd": name, "ok": False, "error": f"bad payload: {e}"})
        cid = body.get("id")
        exp = body.get("expires")
        if isinstance(exp, (int, float)) and exp < time.time():
            return self._ack({"id": cid, "cmd": name, "ok": False, "error": "expired"})
        try:
            result = self.on_command(name, body.get("args") or {})
            self._ack({"id": cid, "cmd": name, "ok": True, "result": result})
        except ValueError as e:
            self._ack({"id": cid, "cmd": name, "ok": False, "error": str(e)})
        except Exception as e:      # never let a command kill the network thread
            log.exception("command %s failed", name)
            self._ack({"id": cid, "cmd": name, "ok": False, "error": f"internal error: {e}"})

    # ---------------------------------------------------------- publishing
    def _ack(self, body):
        body["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.publish_raw(f"{self.base()}/ack", body, retain=False)

    def _status_body(self):
        i = self.identity()
        return {"state": "online", **i, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def _publish_status(self):
        self.last_status = time.time()
        self.publish_raw(f"{self.base()}/status", self._status_body(), retain=True)

    def publish_event(self, etype, data):
        """Publish an event; queued if disconnected. No-op when MQTT is off."""
        if not self.settings.get("enabled"):
            return
        body = {"type": etype, "crib": (self.ident or self.identity()).get("crib"), **data}
        self.publish_raw(f"{self.base()}/evt/{topic_safe(etype)}", body, retain=False)

    def publish_raw(self, topic, body, retain=False):
        payload = json.dumps(body, separators=(",", ":"))
        qos = self.settings.get("qos", 1)
        with self.lock:
            if self.client and self.connected:
                self._raw_publish(topic, payload, qos, retain)
            elif self.settings.get("enabled"):
                if len(self.queue) == self.queue.maxlen:
                    self.dropped += 1
                self.queue.append((topic, payload, qos, retain))

    def _raw_publish(self, topic, payload, qos, retain):
        info = self.client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc == paho.MQTT_ERR_SUCCESS:
            self.sent += 1
            self._log("out", topic, payload)
        else:
            self.queue.appendleft((topic, payload, qos, retain))

    def _log(self, direction, topic, payload):
        self.traffic.appendleft({"ts": time.strftime("%H:%M:%S"), "dir": direction, "topic": topic,
                                 "bytes": len(payload), "payload": payload[:400]})

    # ---------------------------------------------------------- heartbeat / identity changes
    def _heartbeat_loop(self):
        while not self._stop.wait(5):
            try:
                # serial number or site ID changed (e.g. serial read after start-up): new topics
                if self.client and self.ident and self.base(self.identity()) != self.base(self.ident):
                    log.info("MQTT identity changed; reconnecting")
                    self._restart()
                    continue
                with self.lock:
                    if not self.client:
                        continue
                    # name, location or crib number changed: republish the retained status now
                    fresh = self.identity()
                    changed = self.ident and {k: v for k, v in fresh.items() if k != "uptime_s"} != \
                        {k: v for k, v in self.ident.items() if k != "uptime_s"}
                    if changed:
                        self.ident = fresh
                    if self.connected and (changed or time.time() - self.last_status >= self.settings["heartbeat_s"]):
                        self._publish_status()
            except Exception:
                log.exception("MQTT heartbeat failed")

    def status(self):
        st = self.settings
        return {"enabled": bool(st.get("enabled")), "connected": self.connected, "since": self.since,
                "broker": f"{st.get('host')}:{st.get('port')}" if st.get("host") else None,
                "tls": bool(st.get("tls")), "error": self.last_error, "queued": len(self.queue),
                "dropped": self.dropped, "sent": self.sent, "received": self.received,
                "topics": self.topics() if (self.ident or st.get("enabled")) else None,
                "traffic": list(self.traffic)[:50]}
