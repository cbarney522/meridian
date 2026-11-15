"""Tests for core/timeloc.py."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core import timeloc  # noqa: E402


def mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class SunTests(unittest.TestCase):
    def near(self, got, want, tol=3):
        self.assertLessEqual(abs(mins(got) - mins(want)), tol, f"{got} vs {want}")

    def test_atlanta_solstice(self):
        # almanac: Atlanta 2026-06-21 sunrise 06:27, sunset 20:52 EDT
        s = timeloc.sun_times(33.749, -84.388, "America/New_York", dt.date(2026, 6, 21))
        self.near(s["sunrise"], "06:27"); self.near(s["sunset"], "20:52")

    def test_atlanta_winter(self):
        # 2026-12-21 sunrise 07:39, sunset 17:32 EST
        s = timeloc.sun_times(33.749, -84.388, "America/New_York", dt.date(2026, 12, 21))
        self.near(s["sunrise"], "07:39"); self.near(s["sunset"], "17:32")

    def test_london(self):
        # 2026-03-20 sunrise 06:03, sunset 18:14 GMT
        s = timeloc.sun_times(51.5074, -0.1278, "Europe/London", dt.date(2026, 3, 20))
        self.near(s["sunrise"], "06:03"); self.near(s["sunset"], "18:14")

    def test_polar(self):
        s = timeloc.sun_times(78.2, 15.6, "UTC", dt.date(2026, 6, 21))
        self.assertIsNone(s["sunrise"]); self.assertIsNone(s["day_length"])


class SiteTests(unittest.TestCase):
    def test_valid(self):
        s, e = timeloc.validate_site({"site_id": "Plant-ATL", "site_name": "Atlanta", "timezone": "America/New_York",
                                      "latitude": "33.7490", "longitude": -84.388, "clock_24h": 1})
        self.assertEqual(e, [])
        self.assertEqual(s["site_id"], "plant-atl")
        self.assertEqual(s["latitude"], 33.749)
        self.assertTrue(s["clock_24h"])

    def test_errors(self):
        _, e = timeloc.validate_site({"site_id": "bad id!", "timezone": "Mars/Base", "latitude": 95, "longitude": "x"})
        self.assertEqual(len(e), 4)
        _, e = timeloc.validate_site({"latitude": 10})
        self.assertIn("both", e[0])

    def test_crib_number(self):
        for given, want in (("1204", 1204), (7, 7), ("9999", 9999), ("", None), (None, None)):
            s, e = timeloc.validate_site({"crib_number": given})
            self.assertEqual((e, s["crib_number"]), ([], want), given)
        for bad in ("0042", "0", "10000", "12a", -5, True, "1 2"):
            s, e = timeloc.validate_site({"crib_number": bad})
            self.assertEqual(len(e), 1, bad)
            self.assertIn("Crib number", e[0])
            self.assertIsNone(s["crib_number"])

    def test_zones(self):
        z = timeloc.available_zones()
        self.assertIn("America/New_York", z)
        self.assertIn("UTC", z)


class ChronyTests(unittest.TestCase):
    def test_parse(self):
        src = timeloc.parse_sources("^,*,129.6.15.28,1,10,377,500,-0.000012,-0.000013,0.012\n"
                                    "^,?,10.0.0.1,0,6,0,-,0.0,0.0,0.0\n")
        self.assertEqual(src[0]["state"], "in use")
        self.assertTrue(src[0]["selected"])
        self.assertEqual(src[0]["reach"], 255)
        self.assertEqual(src[0]["offset_ms"], -0.012)
        self.assertIsNone(src[1]["last_rx_s"])
        tr = timeloc.parse_tracking("81060F1C,129.6.15.28,2,1794650000.1,-0.000003,0,0,0,0,0,0.02,0.001,1030.5,Normal")
        self.assertTrue(tr["synced"]); self.assertEqual(tr["stratum"], 2)
        self.assertIsNone(timeloc.parse_tracking(""))


if __name__ == "__main__":
    unittest.main()
