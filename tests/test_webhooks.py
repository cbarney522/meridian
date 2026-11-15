"""Webhooks: settings validation, signing, the durable queue, retries, ordering, test and resend."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core import webhooks  # noqa: E402

IDENT = {"reader": "37025501450", "crib": 1204, "site": "plant-atl", "name": "Crib 2"}
TYPES = ["badge", "usb_attached", "tag_enter"]


class Receiver:
    """Local HTTP server that records requests; `codes` decides the answers in turn."""
    def __init__(self):
        self.got, self.codes = [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.got.append((dict(self.headers), body))
                code = outer.codes.pop(0) if outer.codes else 200
                self.send_response(code)
                if code in (301, 302):
                    self.send_header("Location", "http://example.invalid/")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class ValidateTests(unittest.TestCase):
    def test_valid_and_secret_kept(self):
        clean, e = webhooks.validate({"endpoints": [{"name": "ERP", "url": "https://erp.example.com/x", "events": ["badge"]}]},
                                     webhooks.DEFAULTS, TYPES)
        self.assertEqual(e, [])
        ep = clean["endpoints"][0]
        self.assertTrue(ep["secret"].startswith("whsec_"))
        self.assertTrue(ep["enabled"] and ep["verify_cert"])
        again, _ = webhooks.validate({"endpoints": [dict(ep, secret=None, name="ERP 2")]}, clean, TYPES)
        self.assertEqual(again["endpoints"][0]["secret"], ep["secret"])          # kept by id
        new, _ = webhooks.validate({"endpoints": [dict(ep, regenerate_secret=True)]}, clean, TYPES)
        self.assertNotEqual(new["endpoints"][0]["secret"], ep["secret"])

    def test_errors(self):
        _, e = webhooks.validate({"endpoints": [{"url": "ftp://x", "events": []},
                                                {"url": "http://ok/", "events": ["nope"], "timeout_s": 99}]}, webhooks.DEFAULTS, TYPES)
        self.assertEqual(len(e), 4)
        _, e = webhooks.validate({"endpoints": [{"url": "http://a/", "events": ["*"]}] * 6}, webhooks.DEFAULTS, TYPES)
        self.assertIn("At most", e[0])

    def test_public_hides_secret(self):
        clean, _ = webhooks.validate({"endpoints": [{"url": "http://a/", "events": ["*"]}]}, webhooks.DEFAULTS, TYPES)
        self.assertIsNone(webhooks.public(clean, False)["endpoints"][0]["secret"])
        self.assertTrue(webhooks.public(clean, True)["endpoints"][0]["secret"])

    def test_sign_verify(self):
        ts, body = str(int(time.time())), b'{"a":1}'
        sig = webhooks.sign("s3cret", ts, body)
        self.assertTrue(webhooks.verify("s3cret", ts, body, sig))
        self.assertFalse(webhooks.verify("other", ts, body, sig))
        self.assertFalse(webhooks.verify("s3cret", ts, b'{"a":2}', sig))
        self.assertFalse(webhooks.verify("s3cret", str(int(ts) - 1000), body, webhooks.sign("s3cret", str(int(ts) - 1000), body)))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.rx = Receiver()
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        self.wh = webhooks.Webhooks(os.path.join(self.dir, "wh.db"), lambda: dict(IDENT), "Meridian/test", clock=self.clock)
        self.cfg, _ = webhooks.validate({"endpoints": [{"id": "erp1", "name": "ERP", "url": self.rx.url, "events": ["badge"]}]},
                                        webhooks.DEFAULTS, TYPES)
        self.wh.apply(self.cfg)
        self.secret = self.cfg["endpoints"][0]["secret"]

    def tearDown(self):
        self.wh.stop()
        self.rx.close()

    def test_delivers_signed_envelope(self):
        self.assertEqual(self.wh.enqueue("badge", {"raw": "123"}), 1)
        self.assertEqual(self.wh.enqueue("usb_attached", {"port": "1-1"}), 0)       # not subscribed
        self.assertEqual(self.wh.run_due(), 1)
        headers, body = self.rx.got[0]
        env = json.loads(body)
        self.assertEqual((env["type"], env["data"]["raw"], env["reader"]["crib"]), ("badge", "123", 1204))
        self.assertEqual(headers["X-Meridian-Delivery"], env["id"])
        self.assertTrue(webhooks.verify(self.secret, headers["X-Meridian-Timestamp"], body, headers["X-Meridian-Signature"]))
        st = self.wh.status()["endpoints"]["erp1"]
        self.assertEqual((st["delivered"], st["pending"], st["state"]), (1, 0, "ok"))

    def test_retry_backoff_and_order(self):
        self.rx.codes = [500]
        self.wh.enqueue("badge", {"n": 1})
        self.wh.enqueue("badge", {"n": 2})
        self.assertEqual(self.wh.run_due(), 1)            # first fails; second must not jump ahead
        self.assertEqual(len(self.rx.got), 1)
        st = self.wh.status()["endpoints"]["erp1"]
        self.assertEqual((st["state"], st["pending"], st["retry_in_s"]), ("failing", 2, 30))
        self.assertIn("500", st["last_error"])
        self.assertEqual(self.wh.run_due(), 0)            # nothing due yet
        self.clock.t += 31
        self.assertEqual(self.wh.run_due(), 2)
        self.assertEqual([json.loads(b)["data"]["n"] for _, b in self.rx.got], [1, 1, 2])
        self.assertEqual(self.wh.status()["endpoints"]["erp1"]["state"], "ok")

    def test_gives_up_after_a_day(self):
        self.rx.codes = [503] * 50
        self.wh.enqueue("badge", {})
        for _ in range(40):
            self.wh.run_due()
            self.clock.t += 3601
        st = self.wh.status()["endpoints"]["erp1"]
        self.assertEqual((st["pending"], st["failed"]), (0, 1))

    def test_redirect_is_failure(self):
        self.rx.codes = [302]
        self.wh.enqueue("badge", {})
        self.wh.run_due()
        d = self.wh.deliveries()[0]
        self.assertEqual((d["status"], d["last_code"]), ("pending", 302))
        self.assertIn("redirect", d["last_error"])

    def test_survives_restart(self):
        self.rx.codes = [500]
        self.wh.enqueue("badge", {"n": 1})
        self.wh.run_due()
        again = webhooks.Webhooks(os.path.join(self.dir, "wh.db"), lambda: dict(IDENT), clock=self.clock)
        again.apply(self.cfg)
        self.clock.t += 31
        self.assertEqual(again.run_due(), 1)
        self.assertEqual(again.status()["endpoints"]["erp1"]["delivered"], 1)

    def test_test_resend_retry_now(self):
        r = self.wh.test("erp1")
        self.assertTrue(r["ok"])
        self.assertEqual(json.loads(self.rx.got[0][1])["type"], "test")
        self.wh.enqueue("badge", {"n": 7})
        self.wh.run_due()
        first = [d for d in self.wh.deliveries() if d["type"] == "badge"][0]
        self.wh.resend(first["id"])
        self.wh.run_due()
        self.assertEqual(self.rx.got[-1][0]["X-Meridian-Delivery"], first["event_id"])   # same id for dedupe
        self.rx.codes = [500]
        self.wh.enqueue("badge", {})
        self.wh.run_due()
        self.assertEqual(self.wh.retry_now("erp1"), 1)
        self.assertEqual(self.wh.run_due(), 1)
        with self.assertRaises(ValueError):
            self.wh.test("nope")

    def test_unreachable_receiver(self):
        cfg, _ = webhooks.validate({"endpoints": [{"id": "dead1", "url": "http://127.0.0.1:9/", "events": ["*"], "timeout_s": 2}]},
                                   webhooks.DEFAULTS, TYPES)
        self.wh.apply(cfg)
        self.wh.enqueue("tag_enter", {})
        self.wh.run_due()
        st = self.wh.status()["endpoints"]["dead1"]
        self.assertEqual(st["state"], "failing")
        self.assertTrue(st["last_error"])

    def test_removed_endpoint_drops_queue(self):
        self.rx.codes = [500]
        self.wh.enqueue("badge", {})
        self.wh.run_due()
        self.wh.apply({"endpoints": []})
        self.assertEqual([d for d in self.wh.deliveries() if d["status"] == "pending"], [])


if __name__ == "__main__":
    unittest.main()
