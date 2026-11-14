"""Meridian's SQLite database on the reader (rw_dir/meridian.db). Standard library only.

Reference tables (CribMaster items, bins, serialids, rfid, …) are imported from CSV exactly as
exported: the source's column names are kept (every value stored as TEXT), and each import
replaces the whole table in one transaction, so readers never see a half-loaded table.

    db = Database(path, datasets)          # datasets from app/defs/datasets.json
    db.preview_csv("items", text)          # header, row count, sample rows, problems
    db.import_csv("items", text, user=..)  # atomic replace
    db.tables()                            # status of every dataset
    db.rows("items", q="wrench", offset=0, limit=50)
"""
import csv
import hashlib
import io
import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 1
MAX_ROWS = 500_000
MAX_COLUMNS = 200
SAMPLE_ROWS = 20


class DataError(Exception):
    pass


def _q(name):
    """Quote an SQL identifier (column names come from the CSV header)."""
    return '"' + name.replace('"', '""') + '"'


def parse_csv(text):
    """-> (header, rows, problems). Detects , ; tab or | delimiters; strips a BOM."""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.strip():
        raise DataError("The file is empty.")
    # delimiter = whichever of , ; tab | is most common in the header line (Excel/CribMaster exports
    # use , or ; depending on regional settings)
    first = text.split("\n", 1)[0]
    delim = max(",;\t|", key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim if first.count(delim) else ",")
    try:
        header = next(reader)
    except StopIteration:
        raise DataError("The file has no header row.") from None
    header = [h.strip() for h in header]
    problems = []
    if not any(header):
        raise DataError("The first row must contain the column names.")
    if len(header) > MAX_COLUMNS:
        raise DataError(f"Too many columns ({len(header)}; the limit is {MAX_COLUMNS}).")
    seen = {}
    for i, h in enumerate(header):
        if not h:
            header[i] = h = f"column_{i + 1}"
            problems.append(f"Column {i + 1} has no name; it was called “{h}”.")
        key = h.lower()
        if key in seen:
            new = f"{h}_{i + 1}"
            problems.append(f"Column name “{h}” appears twice; the second was renamed “{new}”.")
            header[i] = new
        seen[header[i].lower()] = i
    rows, short, long_ = [], 0, 0
    width = len(header)
    for n, row in enumerate(reader, start=2):
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) < width:
            short += 1
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            long_ += 1
            row = row[:width]
        rows.append([c.strip() for c in row])
        if len(rows) > MAX_ROWS:
            raise DataError(f"Too many rows (more than {MAX_ROWS:,}).")
    if short:
        problems.append(f"{short:,} row(s) had fewer values than columns; the missing values are empty.")
    if long_:
        problems.append(f"{long_:,} row(s) had more values than columns; the extra values were dropped.")
    return header, rows, problems


class Database:
    def __init__(self, path, datasets):
        self.path = path
        self.datasets = {d["id"]: d for d in datasets}
        self.lock = threading.Lock()          # one writer at a time (imports)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            c.execute("""CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dataset TEXT NOT NULL, rows INTEGER NOT NULL,
                columns TEXT NOT NULL, file_name TEXT, sha256 TEXT, imported_at REAL NOT NULL,
                imported_by TEXT, problems TEXT)""")
            c.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def dataset(self, ds_id):
        d = self.datasets.get(ds_id)
        if not d:
            raise DataError(f"No table called “{ds_id}”.")
        return d

    # -------------------------------------------------------------- status
    def _columns(self, c, table):
        return [r["name"] for r in c.execute(f"PRAGMA table_info({_q(table)})")]

    def tables(self):
        out = []
        with self.connect() as c:
            for d in self.datasets.values():
                cols = self._columns(c, d["table"])
                n = c.execute(f"SELECT COUNT(*) FROM {_q(d['table'])}").fetchone()[0] if cols else 0
                last = c.execute("SELECT * FROM imports WHERE dataset=? ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
                out.append({**{k: d[k] for k in ("id", "label", "source", "description", "table")},
                            "loaded": bool(cols), "rows": n, "columns": cols,
                            "last_import": dict(last, problems=json.loads(last["problems"] or "[]"),
                                                columns=json.loads(last["columns"])) if last else None})
        return out

    def size_bytes(self):
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    # -------------------------------------------------------------- import
    def preview_csv(self, ds_id, text):
        d = self.dataset(ds_id)
        header, rows, problems = parse_csv(text)
        with self.connect() as c:
            current = self._columns(c, d["table"])
            current_rows = c.execute(f"SELECT COUNT(*) FROM {_q(d['table'])}").fetchone()[0] if current else 0
        changes = None
        if current:
            added = [h for h in header if h not in current]
            removed = [h for h in current if h not in header]
            if added or removed:
                changes = {"added": added, "removed": removed}
        return {"dataset": ds_id, "columns": header, "rows": len(rows), "sample": rows[:SAMPLE_ROWS],
                "problems": problems, "replaces_rows": current_rows, "column_changes": changes}

    def import_csv(self, ds_id, text, user=None, file_name=None):
        d = self.dataset(ds_id)
        header, rows, problems = parse_csv(text)
        table, tmp = d["table"], d["table"] + "__new"
        cols = ", ".join(f"{_q(h)} TEXT" for h in header)
        marks = ", ".join("?" * len(header))
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        t0 = time.time()
        with self.lock, self.connect() as c:
            c.execute(f"DROP TABLE IF EXISTS {_q(tmp)}")
            c.execute(f"CREATE TABLE {_q(tmp)} ({cols})")
            c.executemany(f"INSERT INTO {_q(tmp)} VALUES ({marks})", rows)
            # swap in one transaction: nobody sees an empty or partial table
            c.execute(f"DROP TABLE IF EXISTS {_q(table)}")
            c.execute(f"ALTER TABLE {_q(tmp)} RENAME TO {_q(table)}")
            c.execute("INSERT INTO imports (dataset, rows, columns, file_name, sha256, imported_at, imported_by, problems) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (ds_id, len(rows), json.dumps(header), (file_name or "")[:200], digest, time.time(),
                       user, json.dumps(problems)))
            c.commit()
        return {"dataset": ds_id, "rows": len(rows), "columns": header, "problems": problems,
                "seconds": round(time.time() - t0, 2)}

    def clear(self, ds_id):
        d = self.dataset(ds_id)
        with self.lock, self.connect() as c:
            c.execute(f"DROP TABLE IF EXISTS {_q(d['table'])}")
            c.commit()

    # -------------------------------------------------------------- browse
    def rows(self, ds_id, q="", offset=0, limit=50, sort=None, desc=False):
        d = self.dataset(ds_id)
        return dict(self.table_rows(d["table"], q, offset, limit, sort, desc), dataset=ds_id)

    def table_rows(self, table, q="", offset=0, limit=50, sort=None, desc=False):
        """Browse any table or view by name (must exist in sqlite_master)."""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self.connect() as c:
            if table not in self._object_names(c):
                raise DataError(f"No table called “{table}”.")
            d = {"table": table}
            cols = self._columns(c, d["table"])
            if not cols:
                return {"table": table, "columns": [], "rows": [], "total": 0, "matched": 0}
            where, args = "", []
            if q:
                like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                where = "WHERE " + " OR ".join(f"{_q(col)} LIKE ? ESCAPE '\\'" for col in cols)
                args = [like] * len(cols)
            order = f"ORDER BY {_q(sort)} {'DESC' if desc else 'ASC'}" if sort in cols else "ORDER BY rowid"
            total = c.execute(f"SELECT COUNT(*) FROM {_q(d['table'])}").fetchone()[0]
            matched = c.execute(f"SELECT COUNT(*) FROM {_q(d['table'])} {where}", args).fetchone()[0] if q else total
            data = c.execute(f"SELECT * FROM {_q(d['table'])} {where} {order} LIMIT ? OFFSET ?",
                             args + [limit, offset]).fetchall()
        return {"table": table, "columns": cols, "rows": [list(r) for r in data], "total": total,
                "matched": matched, "offset": offset, "limit": limit}

    # -------------------------------------------------------------- explorer
    def _object_names(self, c):
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                                        "AND name NOT LIKE 'sqlite_%'")}

    def objects(self):
        """Every table and view, with row counts and column names, plus database facts."""
        out = []
        by_table = {d["table"]: d["id"] for d in self.datasets.values()}
        with self.connect() as c:
            for r in c.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table','view') "
                               "AND name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall():
                cols = self._columns(c, r["name"])
                try:
                    n = c.execute(f"SELECT COUNT(*) FROM {_q(r['name'])}").fetchone()[0]
                except sqlite3.Error:
                    n = None
                out.append({"name": r["name"], "type": r["type"], "rows": n, "columns": cols,
                            "dataset": by_table.get(r["name"]),
                            "kind": "import" if r["name"] in by_table else "system" if r["name"] in ("meta", "imports") else "derived"})
            info = {"sqlite_version": sqlite3.sqlite_version,
                    "journal_mode": c.execute("PRAGMA journal_mode").fetchone()[0],
                    "page_size": c.execute("PRAGMA page_size").fetchone()[0],
                    "page_count": c.execute("PRAGMA page_count").fetchone()[0],
                    "schema_version": (c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() or [None])[0]}
        return {"objects": out, "db": {**info, "bytes": self.size_bytes(), "path": self.path}}

    def schema(self, name):
        with self.connect() as c:
            row = c.execute("SELECT type, sql FROM sqlite_master WHERE name=? AND type IN ('table','view')", (name,)).fetchone()
            if not row:
                raise DataError(f"No table called “{name}”.")
            cols = [{"cid": r["cid"], "name": r["name"], "type": r["type"] or "", "notnull": bool(r["notnull"]),
                     "default": r["dflt_value"], "pk": r["pk"]} for r in c.execute(f"PRAGMA table_info({_q(name)})")]
            idx = []
            for r in c.execute(f"PRAGMA index_list({_q(name)})").fetchall():
                icols = [x["name"] for x in c.execute(f"PRAGMA index_info({_q(r['name'])})")]
                sql = (c.execute("SELECT sql FROM sqlite_master WHERE name=?", (r["name"],)).fetchone() or [None])[0]
                idx.append({"name": r["name"], "unique": bool(r["unique"]), "columns": icols, "origin": r["origin"], "sql": sql})
        return {"name": name, "type": row["type"], "sql": row["sql"], "columns": cols, "indexes": idx}

    READ_ONLY_START = ("select", "with", "explain", "pragma", "values")
    SAFE_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo", "foreign_key_list",
                    "integrity_check", "quick_check", "page_count", "page_size", "journal_mode", "compile_options",
                    "database_list", "freelist_count", "user_version", "schema_version", "encoding", "collation_list",
                    "function_list", "table_list"}

    def query(self, sql, max_rows=1000, timeout_s=5.0):
        """Run ONE read-only statement. The connection is opened read-only (mode=ro) with query_only,
        so nothing can be changed even if the checks below were bypassed."""
        text = (sql or "").strip().rstrip(";").strip()
        if not text:
            raise DataError("Enter a query.")
        if len(text) > 20000:
            raise DataError("Query too long.")
        if not sqlite3.complete_statement(text + ";") or self._has_second_statement(text):
            raise DataError("Run one statement at a time.")
        first = text.split(None, 1)[0].lower()
        if first not in self.READ_ONLY_START:
            raise DataError("Only read-only queries are allowed here (SELECT, WITH, EXPLAIN, PRAGMA).")
        if first == "pragma":
            name = text.split(None, 1)[1].split("(")[0].split("=")[0].strip().lower() if len(text.split(None, 1)) > 1 else ""
            name = name.split(".")[-1]
            if name not in self.SAFE_PRAGMAS or "=" in text:
                raise DataError("That PRAGMA isn't allowed here.")
        uri = "file:" + os.path.abspath(self.path) + "?mode=ro"
        c = sqlite3.connect(uri, uri=True, timeout=5)
        deadline = time.time() + timeout_s
        c.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
        t0 = time.time()
        try:
            c.execute("PRAGMA query_only=ON")
            cur = c.execute(text)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise DataError(f"Stopped after {timeout_s:g} s. Narrow the query (WHERE, LIMIT).") from None
            raise DataError(str(e)) from None
        except sqlite3.Error as e:
            raise DataError(str(e)) from None
        finally:
            c.close()
        truncated = len(rows) > max_rows
        return {"columns": cols, "rows": [list(r) for r in rows[:max_rows]], "row_count": min(len(rows), max_rows),
                "truncated": truncated, "max_rows": max_rows, "ms": round((time.time() - t0) * 1000, 1)}

    @staticmethod
    def _has_second_statement(text):
        """True if text holds more than one statement (semicolons outside quotes)."""
        q, i = None, 0
        while i < len(text):
            ch = text[i]
            if q:
                if ch == q:
                    q = None
            elif ch in "'\"`[":
                q = "]" if ch == "[" else ch
            elif ch == "-" and text[i:i + 2] == "--":
                j = text.find("\n", i)
                i = len(text) if j < 0 else j
            elif ch == ";":
                return bool(text[i + 1:].strip())
            i += 1
        return False

    def integrity(self):
        with self.connect() as c:
            res = [r[0] for r in c.execute("PRAGMA integrity_check").fetchall()]
        return {"ok": res == ["ok"], "messages": res[:50]}

    def export_csv(self, name):
        """Yield the table as CSV text chunks."""
        with self.connect() as c:
            if name not in self._object_names(c):
                raise DataError(f"No table called “{name}”.")
            cur = c.execute(f"SELECT * FROM {_q(name)}")
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\r\n")
            w.writerow([d[0] for d in cur.description])
            while True:
                batch = cur.fetchmany(2000)
                if not batch:
                    break
                w.writerows(["" if v is None else v for v in r] for r in batch)
                yield buf.getvalue()
                buf.seek(0); buf.truncate()
            if buf.getvalue():
                yield buf.getvalue()


def load_datasets(defs_dir):
    with open(os.path.join(defs_dir, "datasets.json"), encoding="utf-8") as f:
        return json.load(f)["datasets"]
