"""app/defs/events.json must match what the reader code actually publishes over MQTT."""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {"mqtt_state", "identify"}          # bus events the bridge doesn't forward (see server.App.MQTT_SKIP)
RENAME = {"badge_raw": "badge"}            # server.App.MQTT_RENAME


def emitted_types():
    found = set()
    for d in ("app/core", "app"):
        for name in os.listdir(os.path.join(ROOT, d)):
            if name.endswith(".py"):
                with open(os.path.join(ROOT, d, name), encoding="utf-8") as f:
                    found |= set(re.findall(r'emit\(\s*"([a-z_]+)"', f.read()))
    return {RENAME.get(t, t) for t in found - SKIP}


class CatalogTests(unittest.TestCase):
    def test_catalog_matches_code(self):
        with open(os.path.join(ROOT, "app", "defs", "events.json"), encoding="utf-8") as f:
            events = json.load(f)["events"]
        built = {e["type"] for e in events if e["built"]}
        self.assertEqual(emitted_types(), built, "update app/defs/events.json 'built' flags")
        for e in events:
            self.assertEqual(e["sample"]["type"], e["type"])
            self.assertTrue(e["label"] and e["meaning"])


if __name__ == "__main__":
    unittest.main()
