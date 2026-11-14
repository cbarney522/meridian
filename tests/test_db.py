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

    def test_explorer(self):
        self.db.import_csv("items", ITEMS)
        self.db.import_csv("rfid", "EPC,ItemNumber\nE2801,TW-100\nE2802,DR-200\n")
        obj = {o["name"]: o for o in self.db.objects()["objects"]}
        self.assertEqual(obj["cm_items"]["kind"], "import")
        self.assertEqual(obj["imports"]["kind"], "system")
        self.assertEqual(obj["cm_rfid"]["rows"], 2)
        sch = self.db.schema("cm_items")
        self.assertEqual([c["name"] for c in sch["columns"]], ["ItemNumber", "Description", "ItemClass"])
        self.assertIn("CREATE TABLE", sch["sql"])
        self.assertEqual(self.db.table_rows("imports")["total"], 2)
        r = self.db.query("SELECT r.EPC, i.Description FROM cm_rfid r JOIN cm_items i ON i.ItemNumber = r.ItemNumber ORDER BY r.EPC")
        self.assertEqual(r["columns"], ["EPC", "Description"])
        self.assertEqual(r["rows"][1], ["E2802", "Drill, cordless"])
        self.assertEqual(self.db.query("WITH x AS (SELECT 1 AS n) SELECT n FROM x;")["rows"], [[1]])
        self.assertEqual(self.db.query("PRAGMA table_info(cm_items)")["row_count"], 3)
        big = self.db.query("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 5000) SELECT i FROM n", max_rows=100)
        self.assertTrue(big["truncated"]); self.assertEqual(big["row_count"], 100)
        csvtext = "".join(self.db.export_csv("cm_items"))
        self.assertTrue(csvtext.startswith("ItemNumber,Description,ItemClass\r\n"))
        self.assertIn('"Drill, cordless"', csvtext)
        self.assertTrue(self.db.integrity()["ok"])

    def test_query_is_read_only(self):
        self.db.import_csv("items", ITEMS)
        for bad in ("DELETE FROM cm_items", "DROP TABLE cm_items", "UPDATE cm_items SET ItemClass='x'",
                    "INSERT INTO cm_items VALUES (1,2,3)", "SELECT 1; DROP TABLE cm_items",
                    "ATTACH DATABASE '/tmp/x.db' AS x", "PRAGMA journal_mode=DELETE", "PRAGMA writable_schema=ON",
                    "VACUUM", "", "   ;  "):
            with self.assertRaises(dbmod.DataError, msg=bad):
                self.db.query(bad)
        # sneaky: a write disguised as a CTE is stopped by the read-only connection itself
        with self.assertRaises(dbmod.DataError):
            self.db.query("WITH x AS (SELECT 1) DELETE FROM cm_items")
        self.assertEqual(self.db.table_rows("cm_items")["total"], 2)
        # a semicolon inside a string is fine
        self.assertEqual(self.db.query("SELECT 'a;b' AS s")["rows"], [["a;b"]])
        # runaway queries stop at the time limit
        with self.assertRaises(dbmod.DataError) as cm:
            self.db.query("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n", timeout_s=0.5)
        self.assertIn("Stopped", str(cm.exception))

    def test_unknown_dataset(self):
        with self.assertRaises(dbmod.DataError):
            self.db.import_csv("nope", ITEMS)


if __name__ == "__main__":
    unittest.main()
