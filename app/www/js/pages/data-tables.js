// Data › Storage › Database tables: the reader's SQLite reference tables (CribMaster exports).
// Import a CSV per table (preview first, then an atomic replace) and browse/search the rows.
import { api, h, fill } from "../api.js";
import { toast, errText } from "../ui.js";

const PAGE = 50;

export function render(root, ctx) {
  const canImport = ctx.session.permissions.includes("data.import");
  const msg = toast();
  const summary = h("p", { class: "subtitle" });
  const list = h("div", { class: "grid grid-2" });
  const browser = h("section", { class: "card full" });
  const state = { tables: [], previews: {}, busy: {} };

  root.append(
    h("h1", {}, "Database tables"),
    h("p", { class: "subtitle" }, "Reference data stored on this reader, imported from CSV exports (e.g. CribMaster). ",
      "Each import replaces the whole table and keeps the file's own column names."),
    summary, msg.el, list, browser);

  async function load() {
    try {
      const d = await api.dataTables();
      state.tables = d.tables;
      summary.textContent = `Database size: ${fmtBytes(d.db_bytes)}.`;
      draw();
    } catch (err) { fill(list, h("div", { class: "notice bad" }, "Can't load tables: ", errText(err))); }
  }

  function draw() {
    fill(list, state.tables.map(card));
  }

  function card(t) {
    const last = t.last_import;
    const pv = state.previews[t.id];
    const file = h("input", { type: "file", accept: ".csv,.txt,text/csv", hidden: true });
    file.addEventListener("change", () => { if (file.files[0]) preview(t, file.files[0]); file.value = ""; });
    const busy = state.busy[t.id];
    return h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, t.label), h("span", { class: "muted" }, `${t.source} · table ${t.table}`)),
      h("p", { class: "hint" }, t.description),
      t.loaded ? h("dl", { class: "kv" },
        h("dt", {}, "Rows"), h("dd", {}, t.rows.toLocaleString()),
        h("dt", {}, "Columns"), h("dd", { class: "cols-list" }, t.columns.join(", ")),
        last ? [h("dt", {}, "Imported"), h("dd", {}, new Date(last.imported_at * 1000).toLocaleString(),
          last.imported_by ? ` by ${last.imported_by}` : "", last.file_name ? ` from ${last.file_name}` : "")] : null)
        : h("p", {}, h("span", { class: "pill neutral" }, "Not loaded yet")),
      pv ? previewBox(t, pv) : null,
      h("div", { class: "actions", style: { marginTop: "12px" } },
        canImport ? [file, h("button", { type: "button", class: "btn btn-primary", disabled: busy, onclick: () => file.click() },
          busy || (t.loaded ? "Replace with CSV…" : "Import CSV…"))] : null,
        t.loaded ? h("button", { type: "button", class: "btn", onclick: () => openExplorer(t.table, true) }, "Browse") : null,
        t.loaded && canImport ? h("button", { type: "button", class: "btn", onclick: () => clearTable(t) }, "Clear") : null));
  }

  function previewBox(t, pv) {
    const cols = pv.columns;
    return h("div", { class: "import-preview" },
      h("h3", {}, `Preview: ${pv.fileName}`),
      h("p", {}, h("b", {}, pv.rows.toLocaleString()), ` rows, ${cols.length} columns.`,
        pv.replaces_rows ? ` This replaces the current ${pv.replaces_rows.toLocaleString()} rows.` : ""),
      pv.column_changes ? h("div", { class: "notice warn" }, "The columns differ from the current table. ",
        pv.column_changes.added.length ? `New: ${pv.column_changes.added.join(", ")}. ` : "",
        pv.column_changes.removed.length ? `No longer present: ${pv.column_changes.removed.join(", ")}.` : "") : null,
      pv.problems.length ? h("div", { class: "notice warn" }, h("ul", {}, pv.problems.map((p) => h("li", {}, p)))) : null,
      h("div", { class: "table-scroll" }, h("table", { class: "table simple sample-table" },
        h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, c)))),
        h("tbody", {}, pv.sample.slice(0, 8).map((r) => h("tr", {}, r.map((v) => h("td", {}, v))))))),
      pv.rows > 8 ? h("p", { class: "muted" }, `First 8 of ${pv.rows.toLocaleString()} rows.`) : null,
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn btn-primary", disabled: !pv.rows || state.busy[t.id], onclick: () => doImport(t, pv) },
          `Import ${pv.rows.toLocaleString()} rows`),
        h("button", { type: "button", class: "btn", onclick: () => { delete state.previews[t.id]; draw(); } }, "Cancel")));
  }

  async function preview(t, f) {
    state.busy[t.id] = "Reading file…"; draw();
    try {
      const pv = await api.dataPreview(t.id, f);
      state.previews[t.id] = { ...pv, file: f, fileName: f.name };
    } catch (err) { msg.show(`${f.name}: ${errText(err)}`, true); }
    finally { delete state.busy[t.id]; draw(); }
  }

  async function doImport(t, pv) {
    state.busy[t.id] = "Importing…"; draw();
    try {
      const r = await api.dataImport(t.id, pv.file);
      msg.show(`${t.label}: imported ${r.rows.toLocaleString()} rows in ${r.seconds} s.`);
      delete state.previews[t.id];
      await load();
      await loadObjects();
      if (ex.name === t.table) openExplorer(t.table);
    } catch (err) { msg.show(`${t.label}: ${errText(err)}`, true); }
    finally { delete state.busy[t.id]; draw(); }
  }

  async function clearTable(t) {
    if (!confirm(`Remove all ${t.rows.toLocaleString()} rows from ${t.label}? You can import it again later.`)) return;
    try {
      await api.dataClear(t.id);
      msg.show(`${t.label} cleared.`);
      await load();
      await loadObjects();
      if (ex.name === t.table) openExplorer(t.table);
    } catch (err) { msg.show(errText(err), true); }
  }

  // ---------- SQLite explorer (every table and view; Data / Schema tabs; SQL console for developers)
  const canQuery = ctx.session.permissions.includes("dev.tools");
  const canExport = ctx.session.permissions.includes("data.export");
  const ex = { name: null, tab: "data", q: "", offset: 0, sort: "", desc: false, objects: [] };
  const exList = h("div", { class: "ex-list" });
  const exInfo = h("p", { class: "muted ex-info" });
  const exPanel = h("div", { class: "ex-panel" }, h("p", { class: "muted" }, "Choose a table on the left."));
  const integrityOut = h("span");
  const integrityBtn = h("button", { type: "button", class: "btn btn-small" }, "Check integrity");
  integrityBtn.addEventListener("click", async () => {
    fill(integrityOut, h("span", { class: "muted" }, " Checking…"));
    try {
      const r = await api.sqliteIntegrity();
      fill(integrityOut, r.ok ? h("span", { class: "pill ok" }, "Database OK") : h("span", { class: "pill bad", title: r.messages.join("\n") }, "Problems found"));
    } catch (err) { fill(integrityOut, h("span", { class: "bad-text" }, errText(err))); }
  });
  browser.hidden = false;
  fill(browser,
    h("div", { class: "card-head" }, h("h2", {}, "SQLite explorer"),
      h("div", { class: "actions" }, integrityBtn, integrityOut)),
    exInfo,
    h("div", { class: "ex-layout" }, exList, exPanel),
    canQuery ? sqlConsole() : null);

  async function loadObjects() {
    try {
      const d = await api.sqliteObjects();
      ex.objects = d.objects;
      const db = d.db;
      exInfo.textContent = `SQLite ${db.sqlite_version} · ${fmtBytes(db.bytes)} · journal ${db.journal_mode} · ${d.objects.length} tables/views`;
      drawList();
    } catch (err) { fill(exList, h("div", { class: "notice bad" }, errText(err))); }
  }

  function drawList() {
    const groups = [["import", "Imported"], ["derived", "Meridian"], ["system", "System"]];
    fill(exList, groups.map(([kind, label]) => {
      const items = ex.objects.filter((o) => o.kind === kind);
      return items.length ? [h("div", { class: "ex-group" }, label), items.map((o) => {
        const b = h("button", { type: "button", class: "ex-item" + (ex.name === o.name ? " on" : "") },
          h("span", { class: "mono" }, o.name), o.type === "view" ? h("span", { class: "muted" }, " view") : null,
          h("span", { class: "ex-n" }, o.rows === null ? "?" : o.rows.toLocaleString()));
        b.addEventListener("click", () => openExplorer(o.name));
        return b;
      })] : null;
    }));
  }

  function openExplorer(name, scroll = false) {
    Object.assign(ex, { name, tab: "data", q: "", offset: 0, sort: "", desc: false });
    drawList();
    buildPanel();
    if (scroll) browser.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const ep = {};
  function buildPanel() {
    const tabBtn = (id, label) => {
      const b = h("button", { type: "button", class: "tab" + (ex.tab === id ? " on" : "") }, label);
      b.addEventListener("click", () => { ex.tab = id; buildPanel(); });
      return b;
    };
    ep.body = h("div");
    fill(exPanel,
      h("div", { class: "ex-head" }, h("b", { class: "mono" }, ex.name),
        h("div", { class: "tabs" }, tabBtn("data", "Data"), tabBtn("schema", "Schema")),
        canExport ? h("a", { class: "btn btn-small", href: api.sqliteExportUrl(ex.name), download: `${ex.name}.csv` }, "Download CSV") : null),
      ep.body);
    if (ex.tab === "data") buildData(); else loadSchema();
  }

  function buildData() {
    ep.search = h("input", { type: "search", placeholder: "Search all columns…", "aria-label": "Search rows", value: ex.q });
    let timer;
    ep.search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { ex.q = ep.search.value.trim(); ex.offset = 0; loadRows(); }, 300); });
    ep.info = h("span", { class: "muted" });
    ep.prev = h("button", { type: "button", class: "btn btn-small", onclick: () => { ex.offset = Math.max(0, ex.offset - PAGE); loadRows(); } }, "‹ Previous");
    ep.next = h("button", { type: "button", class: "btn btn-small", onclick: () => { ex.offset += PAGE; loadRows(); } }, "Next ›");
    ep.table = h("div", { class: "table-scroll" });
    fill(ep.body, h("div", { class: "actions browse-bar" }, ep.search, ep.prev, ep.next, ep.info), ep.table);
    loadRows();
  }

  async function loadRows() {
    try {
      const d = await api.sqliteRows(ex.name, { q: ex.q, offset: ex.offset, limit: PAGE, sort: ex.sort, desc: ex.desc ? "1" : "0" });
      const end = Math.min(d.offset + d.rows.length, d.matched);
      ep.info.textContent = d.matched ? `${(d.offset + 1).toLocaleString()}–${end.toLocaleString()} of ${d.matched.toLocaleString()}`
        + (ex.q ? ` matching (${d.total.toLocaleString()} rows in all)` : "") : (ex.q ? "No rows match." : "No rows.");
      ep.prev.disabled = d.offset === 0;
      ep.next.disabled = end >= d.matched;
      fill(ep.table, grid(d.columns, d.rows, (c) => { ex.desc = ex.sort === c ? !ex.desc : false; ex.sort = c; ex.offset = 0; loadRows(); }, ex.sort, ex.desc));
    } catch (err) { fill(ep.table, h("div", { class: "notice bad" }, errText(err))); }
  }

  async function loadSchema() {
    fill(ep.body, h("p", { class: "muted" }, "Loading…"));
    try {
      const s = await api.sqliteSchema(ex.name);
      fill(ep.body,
        h("table", { class: "table simple" },
          h("thead", {}, h("tr", {}, ["#", "Column", "Type", "Not null", "Default", "Primary key"].map((x) => h("th", {}, x)))),
          h("tbody", {}, s.columns.map((c) => h("tr", {},
            h("td", {}, String(c.cid)), h("td", { class: "mono" }, c.name), h("td", {}, c.type || "—"),
            h("td", {}, c.notnull ? "yes" : ""), h("td", { class: "mono" }, c.default ?? ""), h("td", {}, c.pk ? String(c.pk) : ""))))),
        h("h3", { class: "sub-head" }, "Indexes"),
        s.indexes.length ? h("table", { class: "table simple" },
          h("thead", {}, h("tr", {}, ["Name", "Columns", "Unique"].map((x) => h("th", {}, x)))),
          h("tbody", {}, s.indexes.map((i) => h("tr", {}, h("td", { class: "mono" }, i.name), h("td", { class: "mono" }, i.columns.join(", ")), h("td", {}, i.unique ? "yes" : "")))))
          : h("p", { class: "muted" }, "No indexes."),
        h("h3", { class: "sub-head" }, "Definition"),
        h("pre", { class: "mono sample" }, s.sql || "(none)"));
    } catch (err) { fill(ep.body, h("div", { class: "notice bad" }, errText(err))); }
  }

  function sqlConsole() {
    const ta = h("textarea", { class: "mono sql-input", rows: 6, spellcheck: "false", "aria-label": "SQL query",
      placeholder: "SELECT * FROM cm_items LIMIT 20" });
    try { ta.value = sessionStorage.getItem("meridian.sql") || ""; } catch { /* storage unavailable */ }
    const out = h("div");
    const run = h("button", { type: "button", class: "btn btn-primary" }, "Run");
    const exec = async () => {
      try { sessionStorage.setItem("meridian.sql", ta.value); } catch { /* ignore */ }
      fill(out, h("p", { class: "muted" }, "Running…"));
      run.disabled = true;
      try {
        const r = await api.sqliteQuery(ta.value);
        fill(out,
          h("p", { class: "muted" }, `${r.row_count.toLocaleString()} row${r.row_count === 1 ? "" : "s"} in ${r.ms} ms`,
            r.truncated ? ` (showing the first ${r.max_rows.toLocaleString()}; add LIMIT or WHERE)` : ""),
          r.columns.length ? h("div", { class: "table-scroll" }, grid(r.columns, r.rows)) : null);
      } catch (err) { fill(out, h("div", { class: "notice bad" }, errText(err))); }
      finally { run.disabled = false; }
    };
    run.addEventListener("click", exec);
    ta.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); exec(); } });
    const examples = [
      ["Tables", "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"],
      ["Import log", "SELECT dataset, rows, file_name, datetime(imported_at, 'unixepoch', 'localtime') AS imported, imported_by FROM imports ORDER BY id DESC"],
      ["Columns of a table", "PRAGMA table_info(cm_items)"]];
    return h("div", { class: "sql-console" },
      h("h3", { class: "sub-head" }, "SQL console"),
      h("p", { class: "hint" }, "Read-only: SELECT, WITH, EXPLAIN and informational PRAGMAs, one statement at a time; up to 1,000 rows and 5 seconds. ",
        "Ctrl+Enter runs. Examples: ",
        examples.map(([label, sql], i) => [i ? " · " : "", h("a", { href: "#", onclick: (e) => { e.preventDefault(); ta.value = sql; ta.focus(); } }, label)]).flat()),
      ta, h("div", { class: "actions", style: { marginTop: "8px" } }, run), out);
  }

  function grid(cols, rows, onSort, sort, desc) {
    return h("table", { class: "table simple data-grid" },
      h("thead", {}, h("tr", {}, cols.map((c) => {
        const th = h("th", { class: onSort ? "sortable" : null }, c, sort === c ? (desc ? " ▼" : " ▲") : "");
        if (onSort) th.addEventListener("click", () => onSort(c));
        return th;
      }))),
      h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v) => h("td", { class: v === null ? "null" : null }, v === null ? "NULL" : String(v)))))));
  }

  load();
  loadObjects();
  return () => msg.dispose();
}

function fmtBytes(n) {
  if (!n) return "0 KB";
  return n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}
