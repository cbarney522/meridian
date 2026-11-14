"""Tests for core/readerapi.py (against a fake IoT Device Interface) and core/netinfo.py."""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core import netinfo  # noqa: E402
from core.readerapi import ReaderApi  # noqa: E402

FAKE = {
    "/api/v1/system": {"hostname": "impinj-17-1b-e6", "productDescription": "Impinj R700 RAIN RFID Reader rev 2",
                       "productSku": "IPJ-R700-341", "productHla": "104900-04D", "productPcba": "104920-04C",
                       "serialNumber": "37025501450"},
    "/api/v1/system/image": {"primaryFirmware": "10.4.0.240", "secondaryFirmware": "10.2.1.12"},
    "/api/v1/system/region": {"operatingRegion": "FCC Part 15.247"},
    "/api/v1/system/rfid/interface": {"rfidInterface": "rest"},
}
GOOD = "Basic " + base64.b64encode(b"root:impinj").decode()


class FakeReader(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != GOOD:
            self.send_response(401); self.end_headers(); return
        body = FAKE.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


class ReaderApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeReader)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_reads_all_items(self):
        r = ReaderApi(self.base, "root", "impinj").fetch()
        self.assertTrue(r["ok"])
        self.assertIsNone(r["error"])
        self.assertEqual(r["items"], {"description": "Impinj R700 RAIN RFID Reader rev 2", "model": None,
                                      "sku": "IPJ-R700-341", "hla": "104900-04D", "pcba": "104920-04C",
                                      "serial": "37025501450", "firmware": "10.4.0.240",
                                      "region": "FCC Part 15.247", "rfid_interface": "Impinj IoT Device Interface"})
        self.assertIn("secondaryFirmware", r["raw"]["image"])

    def test_wrong_login(self):
        r = ReaderApi(self.base, "root", "nope").fetch()
        self.assertFalse(r["ok"])
        self.assertIn("login rejected", r["error"])

    def test_unreachable(self):
        r = ReaderApi("http://127.0.0.1:1", "root", "impinj", timeout=1).fetch()
        self.assertFalse(r["ok"])
        self.assertIn("can't reach", r["error"])


class NetInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_default_interface_and_ipv6(self):
        route = self.write("route", "Iface\tDestination\tGateway\n"
                                    "eth0\t0000A8C0\t00000000\neth0\t00000000\t0100A8C0\n")
        self.assertEqual(netinfo.default_interface(route), "eth0")
        inet6 = self.write("if_inet6",
                           "fe80000000000000021625fffe171be6 02 40 20 80 eth0\n"
                           "260100c20f004267738fd53b810f3fdb 02 40 00 00 eth0\n"
                           "00000000000000000000000000000001 01 80 10 80 lo\n")
        self.assertEqual(netinfo.ipv6_addresses("eth0", inet6), ["2601:c2:f00:4267:738f:d53b:810f:3fdb"])

    def test_mac(self):
        d = os.path.join(self.tmp.name, "net", "eth0")
        os.makedirs(d)
        with open(os.path.join(d, "address"), "w") as f:
            f.write("00:16:25:17:1b:e6\n")
        self.assertEqual(netinfo.mac_address("eth0", os.path.join(self.tmp.name, "net")), "00:16:25:17:1B:E6")
        self.assertIsNone(netinfo.mac_address("nope", os.path.join(self.tmp.name, "net")))

    def test_summary_runs(self):
        s = netinfo.summary()
        self.assertIn("ipv4", s)


if __name__ == "__main__":
    unittest.main()
