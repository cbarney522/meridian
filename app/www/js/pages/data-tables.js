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
  const browser = h("section", { class: "card full", hidden: true });
  const state = { tables: [], previews: {}, busy: {}, browse: null };

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
        t.loaded ? h("button", { type: "button", class: "btn", onclick: () => openBrowser(t.id) }, "Browse") : null,
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
      if (state.browse && state.browse.id === t.id) openBrowser(t.id);
    } catch (err) { msg.show(`${t.label}: ${errText(err)}`, true); }
    finally { delete state.busy[t.id]; draw(); }
  }

  async function clearTable(t) {
    if (!confirm(`Remove all ${t.rows.toLocaleString()} rows from ${t.label}? You can import it again later.`)) return;
    try {
      await api.dataClear(t.id);
      msg.show(`${t.label} cleared.`);
      if (state.browse && state.browse.id === t.id) { state.browse = null; browser.hidden = true; }
      await load();
    } catch (err) { msg.show(errText(err), true); }
  }

  // ---------- browser
  function openBrowser(id) {
    state.browse = { id, q: "", offset: 0, sort: "", desc: false };
    browser.hidden = false;
    buildBrowser();
    loadRows();
    browser.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const br = {};
  function buildBrowser() {
    const b = state.browse, t = state.tables.find((x) => x.id === b.id);
    br.search = h("input", { type: "search", placeholder: "Search all columns…", "aria-label": "Search rows" });
    let timer;
    br.search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { b.q = br.search.value.trim(); b.offset = 0; loadRows(); }, 300); });
    br.info = h("span", { class: "muted" });
    br.prev = h("button", { type: "button", class: "btn btn-small", onclick: () => { b.offset = Math.max(0, b.offset - PAGE); loadRows(); } }, "‹ Previous");
    br.next = h("button", { type: "button", class: "btn btn-small", onclick: () => { b.offset += PAGE; loadRows(); } }, "Next ›");
    br.table = h("div", { class: "table-scroll" });
    fill(browser,
      h("div", { class: "card-head" }, h("h2", {}, `Browse: ${t ? t.label : b.id}`),
        h("button", { type: "button", class: "btn btn-small", onclick: () => { state.browse = null; browser.hidden = true; } }, "Close")),
      h("div", { class: "actions browse-bar" }, br.search, br.prev, br.next, br.info),
      br.table);
  }

  async function loadRows() {
    const b = state.browse;
    if (!b) return;
    try {
      const d = await api.dataRows(b.id, { q: b.q, offset: b.offset, limit: PAGE, sort: b.sort, desc: b.desc ? "1" : "0" });
      const end = Math.min(d.offset + d.rows.length, d.matched);
      br.info.textContent = d.matched ? `${(d.offset + 1).toLocaleString()}–${end.toLocaleString()} of ${d.matched.toLocaleString()}`
        + (b.q ? ` matching (${d.total.toLocaleString()} rows in all)` : "") : (b.q ? "No rows match." : "No rows.");
      br.prev.disabled = d.offset === 0;
      br.next.disabled = end >= d.matched;
      fill(br.table, h("table", { class: "table simple data-grid" },
        h("thead", {}, h("tr", {}, d.columns.map((c) => {
          const th = h("th", { class: "sortable", title: "Sort" }, c, b.sort === c ? (b.desc ? " ▼" : " ▲") : "");
          th.addEventListener("click", () => { b.desc = b.sort === c ? !b.desc : false; b.sort = c; b.offset = 0; loadRows(); });
          return th;
        }))),
        h("tbody", {}, d.rows.map((r) => h("tr", {}, r.map((v) => h("td", {}, v ?? "")))))));
    } catch (err) { fill(br.table, h("div", { class: "notice bad" }, errText(err))); }
  }

  load();
  return () => msg.dispose();
}

function fmtBytes(n) {
  if (!n) return "0 KB";
  return n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}
