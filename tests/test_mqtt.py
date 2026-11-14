"""Tests for core/mqtt_link.py against a real Mosquitto broker (skipped if mosquitto isn't installed)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core import mqtt_link  # noqa: E402
from core.mqtt_link import MqttLink, paho  # noqa: E402

MOSQUITTO = shutil.which("mosquitto") or ("/usr/sbin/mosquitto" if os.path.exists("/usr/sbin/mosquitto") else None)
PASSWD = shutil.which("mosquitto_passwd") or "/usr/bin/mosquitto_passwd"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Broker:
    def __init__(self, users=None):
        self.dir = tempfile.mkdtemp()
        self.port = free_port()
        conf = [f"listener {self.port} 127.0.0.1"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            conf.append("user root")             # don't drop to 'mosquitto' (can't read our temp files)
        if users:
            pw = os.path.join(self.dir, "passwd")
            for i, (u, p) in enumerate(users.items()):
                subprocess.run([PASSWD, "-b"] + (["-c"] if i == 0 else []) + [pw, u, p], check=True, capture_output=True)
            conf += ["allow_anonymous false", f"password_file {pw}"]
        else:
            conf += ["allow_anonymous true"]
        self.conf = os.path.join(self.dir, "m.conf")
        with open(self.conf, "w") as f:
            f.write("\n".join(conf) + "\n")
        self.start()

    def start(self):
        self.proc = subprocess.Popen([MOSQUITTO, "-c", self.conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("mosquitto didn't start")

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


class Watcher:
    """A plain subscriber that records what it sees."""
    def __init__(self, port, topic="#", **auth):
        self.msgs = []
        self.ev = threading.Event()
        self.c = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"w{time.time()}")
        if auth:
            self.c.username_pw_set(auth["user"], auth["pw"])
        self.c.on_message = lambda c, u, m: (self.msgs.append((m.topic, m.payload.decode(), m.retain)), self.ev.set())
        self.c.connect("127.0.0.1", port)
        self.c.subscribe(topic, 1)
        self.c.loop_start()

    def wait_for(self, pred, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            for m in list(self.msgs):
                if pred(m):
                    return m
            time.sleep(0.05)
        raise AssertionError(f"not seen; got {self.msgs}")

    def send(self, topic, body):
        self.c.publish(topic, json.dumps(body), qos=1)

    def close(self):
        self.c.loop_stop(); self.c.disconnect()


def wait(pred, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


IDENT = {"reader": "37025501450", "site": "plant-atl", "name": "Crib 2", "version": "0.1.1", "ip": "10.0.0.5"}


@unittest.skipUnless(MOSQUITTO, "mosquitto not installed")
class MqttTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker()
        self.cmds = []
        self.link = MqttLink(lambda: dict(IDENT), self.on_cmd)
        self.w = Watcher(self.broker.port)

    def tearDown(self):
        self.link.stop()
        self.w.close()
        self.broker.cleanup()

    def on_cmd(self, name, args):
        self.cmds.append((name, args))
        if name == "ping":
            return {"pong": True}
        raise ValueError(f"unknown command '{name}'")

    def settings(self, **kw):
        return dict(mqtt_link.DEFAULTS, enabled=True, host="127.0.0.1", port=self.broker.port, heartbeat_s=10, **kw)

    def test_status_events_commands(self):
        self.link.apply(self.settings())
        self.assertTrue(wait(lambda: self.link.connected))
        t, payload, retained = self.w.wait_for(lambda m: m[0] == "meridian/plant-atl/37025501450/status")
        self.assertEqual(json.loads(payload)["state"], "online")
        self.link.publish_event("usb_attached", {"port": "1-1.2"})
        t, payload, _ = self.w.wait_for(lambda m: m[0].endswith("/evt/usb_attached"))
        self.assertEqual(json.loads(payload)["port"], "1-1.2")
        # command to this reader, then a broadcast, then an unknown one
        self.w.send("meridian/plant-atl/37025501450/cmd/ping", {"id": "a1"})
        self.w.send("meridian/plant-atl/all/cmd/ping", {"id": "a2"})
        self.w.send("meridian/plant-atl/37025501450/cmd/selfdestruct", {"id": "a3"})
        self.w.send("meridian/plant-atl/37025501450/cmd/ping", {"id": "a4", "expires": time.time() - 5})
        acks = {}
        for cid in ("a1", "a2", "a3", "a4"):
            _, p, _ = self.w.wait_for(lambda m, c=cid: m[0].endswith("/ack") and json.loads(m[1]).get("id") == c)
            acks[cid] = json.loads(p)
        self.assertEqual(acks["a1"]["result"], {"pong": True})
        self.assertTrue(acks["a2"]["ok"])
        self.assertIn("unknown command", acks["a3"]["error"])
        self.assertEqual(acks["a4"]["error"], "expired")
        self.assertEqual([c[0] for c in self.cmds], ["ping", "ping", "selfdestruct"])
        st = self.link.status()
        self.assertGreaterEqual(st["received"], 4)
        self.assertTrue(any(x["dir"] == "in" for x in st["traffic"]))

    def test_last_will_on_drop_and_clean_offline(self):
        self.link.apply(self.settings())
        self.assertTrue(wait(lambda: self.link.connected))
        self.link.stop()
        _, p, _ = self.w.wait_for(lambda m: m[0].endswith("/status") and json.loads(m[1])["state"] == "offline")
        self.assertEqual(json.loads(p)["reason"], "stopped")
        # a fresh subscriber gets the retained status
        w2 = Watcher(self.broker.port, "meridian/+/+/status")
        _, p, retained = w2.wait_for(lambda m: True)
        self.assertTrue(retained)
        w2.close()

    def test_queue_while_broker_down(self):
        self.link.apply(self.settings())
        self.assertTrue(wait(lambda: self.link.connected))
        self.w.close()
        self.broker.stop()
        self.assertTrue(wait(lambda: not self.link.connected))
        self.link.publish_event("badge_scan", {"badge": "123"})
        self.assertEqual(self.link.status()["queued"], 1)
        self.broker.start()
        self.w = Watcher(self.broker.port)
        self.assertTrue(wait(lambda: self.link.connected, 20))
        self.w.wait_for(lambda m: m[0].endswith("/evt/badge_scan"), 10)
        self.assertEqual(self.link.status()["queued"], 0)

    def test_identity_change_moves_topics(self):
        ident = dict(IDENT, reader="impinj-17-1b-e6")
        self.link.identity = lambda: dict(ident)
        self.link.apply(self.settings())
        self.assertTrue(wait(lambda: self.link.connected))
        ident["reader"] = "37025501450"          # serial number arrived
        self.w.wait_for(lambda m: m[0] == "meridian/plant-atl/37025501450/status" and "online" in m[1], 15)


@unittest.skipUnless(MOSQUITTO, "mosquitto not installed")
class AuthTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker(users={"meridian-1": "s3cret"})

    def tearDown(self):
        self.broker.cleanup()

    def test_test_connection(self):
        base = dict(mqtt_link.DEFAULTS, host="127.0.0.1", port=self.broker.port, username="meridian-1")
        self.assertTrue(mqtt_link.test_connection(dict(base, password="s3cret"))["ok"])
        bad = mqtt_link.test_connection(dict(base, password="nope"))
        self.assertFalse(bad["ok"])
        self.assertIn("password", bad["error"])
        off = mqtt_link.test_connection(dict(base, port=free_port()))
        self.assertIn("refused", off["error"])


class ValidateTests(unittest.TestCase):
    def test_validate(self):
        cur = dict(mqtt_link.DEFAULTS, password="old")
        s, e = mqtt_link.validate({"enabled": True, "host": "192.168.0.50", "port": "1883", "password": ""}, cur)
        self.assertEqual(e, [])
        self.assertEqual(s["password"], "old")
        self.assertEqual(s["port"], 1883)
        s, e = mqtt_link.validate({"clear_password": True}, cur)
        self.assertEqual(s["password"], "")
        _, e = mqtt_link.validate({"enabled": True, "host": "", "port": 0, "prefix": "a b", "heartbeat_s": 1,
                                   "ca_pem": "junk"}, cur)
        self.assertEqual(len(e), 5)
        self.assertNotIn("password", mqtt_link.public(cur))


if __name__ == "__main__":
    unittest.main()
