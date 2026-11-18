"""Network tools: input checks, port/TLS tests, DNS, routes and the DHCP verdict."""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core import nettools  # noqa: E402


def write(root, path, text):
    full = root + path
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(text)
    return full


class InputTests(unittest.TestCase):
    def test_hosts(self):
        for good, want in (("broker.example.com", "broker.example.com"), ("192.168.0.20", "192.168.0.20"),
                           ("[::1]", "::1"), ("my_host", "my_host"), ("host.", "host")):
            self.assertEqual(nettools.clean_host(good), want)
        for bad in ("", "a b", "http://x", "x" * 300, "-bad", "a;rm -rf /"):
            with self.assertRaises(ValueError):
                nettools.clean_host(bad)

    def test_ports(self):
        self.assertEqual(nettools.clean_port("8883"), 8883)
        for bad in (0, 65536, "x", None):
            with self.assertRaises(ValueError):
                nettools.clean_port(bad)


class PortTests(unittest.TestCase):
    def setUp(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.stop = False

        def accept():
            while not self.stop:
                try:
                    c, _ = self.srv.accept()
                    time.sleep(0.2)
                    c.close()
                except OSError:
                    return
        threading.Thread(target=accept, daemon=True).start()

    def tearDown(self):
        self.stop = True
        self.srv.close()

    def test_open(self):
        r = nettools.port_test("127.0.0.1", self.port)
        self.assertTrue(r["ok"], r)
        self.assertIn("is open", r["message"])
        self.assertIn("connect_ms", r)

    def test_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        r = nettools.port_test("127.0.0.1", closed)
        self.assertFalse(r["ok"])
        self.assertIn("refused", r["message"])

    def test_tls_on_plain_port(self):
        r = nettools.port_test("127.0.0.1", self.port, tls=True, timeout=2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["step"], "tls")
        self.assertIn("TLS", r["message"])

    def test_unknown_name(self):
        r = nettools.port_test("no-such-host.invalid", 1883, timeout=2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["step"], "dns")

    def test_dns_lookup(self):
        r = nettools.dns_lookup("localhost")
        self.assertTrue(r["ok"])
        self.assertTrue(r["addresses"])
        self.assertTrue(nettools.dns_lookup("127.0.0.1")["ok"])
        self.assertFalse(nettools.dns_lookup("no-such-host.invalid")["ok"])


class StatusTests(unittest.TestCase):
    def test_routes(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                    "eth0\t00000000\t0100A8C0\t0003\t0\t0\t0\t00000000\n"
                    "eth0\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n")
        try:
            self.assertEqual(nettools.routes(f.name), [{"interface": "eth0", "gateway": "192.168.0.1", "metric": 0}])
        finally:
            os.unlink(f.name)

    def test_status_shape(self):
        st = nettools.status()
        for k in ("hostname", "interfaces", "gateways", "dns", "dhcp"):
            self.assertIn(k, st)
        self.assertIn(st["dhcp"]["verdict"], ("dhcp", "dhcp_likely", "static", "apipa", "none", "unknown"))


class DhcpTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.roots = {"prefix": self.root}
        write(self.root, "/sys/class/net/eth0/ifindex", "2\n")

    def verdict(self, ip, clients=()):
        return nettools.dhcp_status("eth0", ip, roots=self.roots, clients=list(clients))

    def test_none_and_apipa(self):
        self.assertEqual(self.verdict(None)["verdict"], "none")
        self.assertEqual(self.verdict("169.254.10.3")["verdict"], "apipa")

    def test_networkd_lease(self):
        write(self.root, "/run/systemd/netif/leases/2",
              "ADDRESS=192.168.0.188\nNETMASK=255.255.255.0\nROUTER=192.168.0.1\nSERVER_ADDRESS=192.168.0.1\nLIFETIME=86400\n")
        d = self.verdict("192.168.0.188")
        self.assertEqual(d["verdict"], "dhcp")
        self.assertEqual(d["lease"]["server"], "192.168.0.1")
        self.assertGreater(d["lease"]["expires_in_s"], 80000)
        self.assertIn("192.168.0.1", d["message"])

    def test_lease_for_other_address_ignored(self):
        write(self.root, "/run/systemd/netif/leases/2", "ADDRESS=10.0.0.5\nLIFETIME=60\n")
        self.assertEqual(self.verdict("192.168.0.188")["verdict"], "unknown")

    def test_dhclient_lease(self):
        write(self.root, "/var/lib/dhcp/dhclient.eth0.leases",
              'lease {\n  interface "eth0";\n  fixed-address 192.168.0.50;\n  option routers 192.168.0.1;\n'
              '  option dhcp-server-identifier 192.168.0.1;\n  renew 2 2099/01/01 00:00:00;\n  expire 3 2099/01/02 00:00:00;\n}\n'
              'lease {\n  interface "eth0";\n  fixed-address 192.168.0.188;\n  option dhcp-server-identifier 192.168.0.2;\n'
              '  expire 3 2099/01/02 00:00:00;\n}\n')
        d = self.verdict("192.168.0.188")
        self.assertEqual((d["verdict"], d["lease"]["server"]), ("dhcp", "192.168.0.2"))

    def test_client_running_without_lease(self):
        d = self.verdict("192.168.0.188", clients=[{"client": "udhcpc", "cmdline": "udhcpc -i eth0 -b"}])
        self.assertEqual(d["verdict"], "dhcp_likely")
        self.assertTrue(any("udhcpc" in e for e in d["evidence"]))

    def test_static_config(self):
        write(self.root, "/etc/network/interfaces", "auto eth0\niface eth0 inet static\n  address 192.168.0.188\n")
        self.assertEqual(self.verdict("192.168.0.188")["verdict"], "static")

    def test_networkd_config_dhcp(self):
        write(self.root, "/etc/systemd/network/10-eth.network", "[Match]\nName=eth*\n[Network]\nDHCP=yes\n")
        self.assertEqual(self.verdict("192.168.0.188")["verdict"], "dhcp_likely")


class QuickCheckTests(unittest.TestCase):
    def st(self, carrier=True, ip="192.168.0.188", verdict="dhcp_likely"):
        return {"interface": "eth0", "hostname": "r", "gateways": [{"interface": "eth0", "gateway": "127.0.0.1", "metric": 0}],
                "dns": {"servers": ["192.168.0.1"], "search": []},
                "interfaces": [{"name": "eth0", "carrier": carrier, "speed_mbps": 1000, "duplex": "full", "ipv4": ip, "prefix": 24}],
                "dhcp": {"verdict": verdict, "message": "m"}}

    def test_no_link_stops_early(self):
        res = nettools.quick_checks([], st=self.st(carrier=False))
        self.assertEqual([(r["name"], r["status"]) for r in res], [("Cable / link", "fail")])

    def test_targets_tested(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            res = nettools.quick_checks([{"label": "MQTT broker", "host": "127.0.0.1", "port": port, "tls": False}],
                                        ntp={"available": True, "tracking": {"synced": True, "reference": "x", "offset_ms": 1}},
                                        st=self.st())
        finally:
            srv.close()
        names = {r["name"]: r["status"] for r in res}
        self.assertEqual(names[f"MQTT broker: 127.0.0.1:{port}"], "pass")
        self.assertEqual(names["Time sync"], "pass")
        self.assertEqual(names["IP address"], "pass")

    def test_targets_from_settings(self):
        t = nettools.targets_from({"host": "broker.lan", "port": 8883, "tls": True},
                                  {"endpoints": [{"enabled": True, "name": "ERP", "url": "https://erp.example.com/x"},
                                                 {"enabled": False, "name": "Off", "url": "http://a/"}]})
        self.assertEqual([(x["host"], x["port"], x["tls"]) for x in t], [("broker.lan", 8883, True), ("erp.example.com", 443, True)])


if __name__ == "__main__":
    unittest.main()


class NetworkManagerTests(unittest.TestCase):
    NMCLI = ("DHCP4.OPTION[1]:dhcp_lease_time = 86400\nDHCP4.OPTION[2]:dhcp_server_identifier = 192.168.0.1\n"
             "DHCP4.OPTION[3]:expiry = 4102444800\nDHCP4.OPTION[4]:ip_address = 192.168.0.188\n"
             "DHCP4.OPTION[5]:routers = 192.168.0.1\n")

    def test_parse(self):
        o = nettools.parse_nmcli_dhcp4(self.NMCLI)
        self.assertEqual((o["ip_address"], o["expiry"]), ("192.168.0.188", "4102444800"))

    def test_lease_via_nmcli(self):
        class P:
            returncode, stdout, stderr = 0, self.NMCLI, ""
        root = tempfile.mkdtemp()
        orig = nettools.shutil.which
        nettools.shutil.which = lambda n: "/usr/bin/" + n if n == "nmcli" else None
        try:
            d = nettools.dhcp_status("eth0", "192.168.0.188", roots={"prefix": root},
                                     clients=[{"client": "NetworkManager", "cmdline": "/usr/sbin/NetworkManager --no-daemon"}],
                                     nm_run=lambda args: P())
        finally:
            nettools.shutil.which = orig
        self.assertEqual(d["verdict"], "dhcp")
        self.assertEqual(d["lease"]["server"], "192.168.0.1")
        self.assertEqual(d["lease"]["obtained"], 4102444800 - 86400)
        self.assertTrue(any(e.startswith("nmcli:") for e in d["evidence"]))

    def test_nm_unavailable_stays_likely(self):
        root = tempfile.mkdtemp()
        orig = nettools.shutil.which
        nettools.shutil.which = lambda n: None
        try:
            d = nettools.dhcp_status("eth0", "192.168.0.188", roots={"prefix": root},
                                     clients=[{"client": "NetworkManager", "cmdline": "/usr/sbin/NetworkManager"}])
        finally:
            nettools.shutil.which = orig
        self.assertEqual(d["verdict"], "dhcp_likely")
        self.assertIn("nmcli: not installed", d["evidence"])
