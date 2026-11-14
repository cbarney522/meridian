"""Tests for core/db.py."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from core import db as dbmod  # noqa: E402

DEFS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "defs")

ITEMS = "﻿ItemNumber,Description,ItemClass\r\nTW-100,Torque wrench 3/8\",Hand tools\r\nDR-200,\"Drill, cordless\",Power tools\r\n"


class ParseTests(unittest.TestCase):
    def test_basic_and_quirks(self):
        h, rows, p = dbmod.parse_csv(ITEMS)
        self.assertEqual(h, ["ItemNumber", "Description", "ItemClass"])
        self.assertEqual(rows[1], ["DR-200", "Drill, cordless", "Power tools"])
        self.assertEqual(p, [])
        h, rows, p = dbmod.parse_csv("a;b;b;\n1;2\n1;2;3;4;5\n\n")
        self.assertEqual(h, ["a", "b", "b_3", "column_4"])
        self.assertEqual(rows, [["1", "2", "", ""], ["1", "2", "3", "4"]])
        self.assertEqual(len(p), 4)
        h, _, _ = dbmod.parse_csv("x\ty\n1\t2\n")
        self.assertEqual(h, ["x", "y"])
        with self.assertRaises(dbmod.DataError):
            dbmod.parse_csv("   ")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = dbmod.Database(os.path.join(self.tmp.name, "meridian.db"), dbmod.load_datasets(DEFS))

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_preview_browse(self):
        st = {t["id"]: t for t in self.db.tables()}
        self.assertEqual(set(st), {"items", "bins", "serialids", "rfid"})
        self.assertFalse(st["items"]["loaded"])
        pv = self.db.preview_csv("items", ITEMS)
        self.assertEqual((pv["rows"], pv["replaces_rows"], pv["column_changes"]), (2, 0, None))
        r = self.db.import_csv("items", ITEMS, user="cliff", file_name="items.csv")
        self.assertEqual(r["rows"], 2)
        st = {t["id"]: t for t in self.db.tables()}["items"]
        self.assertEqual((st["rows"], st["columns"][0], st["last_import"]["imported_by"]), (2, "ItemNumber", "cliff"))
        page = self.db.rows("items", q="cordless")
        self.assertEqual((page["matched"], page["total"], page["rows"][0][0]), (1, 2, "DR-200"))
        self.assertEqual(self.db.rows("items", q="100%")["matched"], 0)     # % is literal
        self.assertEqual(self.db.rows("items", sort="ItemNumber", desc=True)["rows"][0][0], "TW-100")
        # re-import with different columns replaces the table
        pv = self.db.preview_csv("items", "ItemNumber,UOM\nX,EA\n")
        self.assertEqual(pv["column_changes"], {"added": ["UOM"], "removed": ["Description", "ItemClass"]})
        self.assertEqual(pv["replaces_rows"], 2)
        self.db.import_csv("items", "ItemNumber,UOM\nX,EA\n")
        self.assertEqual(self.db.rows("items")["columns"], ["ItemNumber", "UOM"])
        self.assertEqual(self.db.rows("items")["total"], 1)

    def test_odd_column_names_are_safe(self):
        nasty = 'id,"we""ird",select,"a b";drop\n1,2,3,4\n'
        self.db.import_csv("rfid", nasty)
        self.assertEqual(self.db.rows("rfid")["columns"], ["id", 'we"ird', "select", "a b;drop"])
        self.assertIn("items", [t["id"] for t in self.db.tables()])          # nothing else dropped

    def test_reads_during_import_see_old_or_new(self):
        self.db.import_csv("bins", "Bin,Item\n" + "".join(f"B{i},I{i}\n" for i in range(2000)))
        big = "Bin,Item\n" + "".join(f"N{i},J{i}\n" for i in range(50000))
        seen = set()
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                seen.add(self.db.rows("bins", limit=1)["total"])
        t = threading.Thread(target=reader)
        t.start()
        self.db.import_csv("bins", big)
        stop.set(); t.join()
        self.assertTrue(seen <= {2000, 50000}, seen)

    def test_unknown_dataset(self):
        with self.assertRaises(dbmod.DataError):
            self.db.import_csv("nope", ITEMS)


if __name__ == "__main__":
    unittest.main()
