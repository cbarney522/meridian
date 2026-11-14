// Read-only folder view of the data drive, used on the Storage page. One folder at a time,
// names/sizes/dates only: no opening, downloading, editing or deleting files.
import { api, h, fill } from "../api.js";
import { fmtBytes, errText } from "../ui.js";

export function driveBrowser() {
  const state = { scope: "meridian", path: "", mounted: false, loaded: false, canDrive: false };
  const scopeBox = h("div", { class: "seg", role: "group", "aria-label": "What to show" });
  const refresh = h("button", { type: "button", class: "btn btn-small" }, "Refresh");
  const crumbs = h("nav", { class: "path-crumbs", "aria-label": "Folder path" });
  const tbody = h("tbody");
  const foot = h("p", { class: "hint browse-foot" });
  const el = h("section", { class: "card", style: { marginTop: "16px" }, hidden: true },
    h("div", { class: "card-head" }, h("h2", {}, "Files on the drive"), h("div", { class: "actions" }, scopeBox, refresh)),
    h("p", { class: "hint" }, "Read-only view: names, sizes and dates. Files can't be opened, changed or deleted here."),
    crumbs,
    h("table", { class: "table browse-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", { class: "num" }, "Size"), h("th", {}, "Modified"))), tbody),
    foot);
  refresh.addEventListener("click", () => load());

  async function load() {
    try {
      const r = await api.browseDrive(state.scope, state.path);
      state.loaded = true;
      state.canDrive = r.can_browse_drive;
      draw(r);
    } catch (err) {
      if (err.status === 403 && state.scope === "drive") { state.scope = "meridian"; state.path = ""; return load(); }
      fill(tbody, h("tr", {}, h("td", { colspan: 3, class: "empty" }, errText(err))));
      fill(foot);
    }
  }

  function drawScope() {
    const btn = (id, label) => h("button", { type: "button", class: "seg-btn" + (state.scope === id ? " on" : ""),
      "aria-pressed": String(state.scope === id), onclick: () => go(id, "") }, label);
    fill(scopeBox, state.canDrive ? [btn("meridian", "Meridian folder"), btn("drive", "Whole drive")] : null);
  }

  function draw(r) {
    drawScope();
    const rootLabel = state.scope === "drive" ? "Drive" : "meridian";
    const parts = r.path ? r.path.split("/") : [];
    fill(crumbs,
      h("button", { type: "button", class: "crumb", onclick: () => go(state.scope, "") }, rootLabel),
      parts.map((p, i) => [h("span", { class: "crumb-sep" }, "/"),
        i === parts.length - 1 ? h("span", { class: "crumb current" }, p)
          : h("button", { type: "button", class: "crumb", onclick: () => go(state.scope, parts.slice(0, i + 1).join("/")) }, p)]));
    if (!r.exists) {
      fill(tbody, h("tr", {}, h("td", { colspan: 3, class: "empty" }, "Meridian hasn't written anything to this drive yet.")));
      fill(foot);
      return;
    }
    const rows = [];
    if (parts.length) {
      rows.push(h("tr", { class: "clickable", onclick: () => go(state.scope, parts.slice(0, -1).join("/")) },
        h("td", {}, h("span", { class: "ico ico-up", "aria-hidden": "true" }), h("button", { type: "button", class: "link-btn" }, "Up one level")),
        h("td", {}), h("td", {})));
    }
    for (const e of r.entries) {
      const isDir = e.type === "dir";
      const open = () => go(state.scope, (r.path ? r.path + "/" : "") + e.name);
      rows.push(h("tr", { class: isDir ? "clickable" : null, onclick: isDir ? open : null },
        h("td", {}, h("span", { class: "ico ico-" + e.type, "aria-hidden": "true" }),
          isDir ? h("button", { type: "button", class: "link-btn" }, e.name) : h("span", {}, e.name),
          e.type === "link" ? h("span", { class: "muted" }, " (link, not followed)") : null),
        h("td", { class: "num" }, e.size === null ? "" : fmtBytes(e.size)),
        h("td", {}, new Date(e.modified * 1000).toLocaleString())));
    }
    if (!r.entries.length) rows.push(h("tr", {}, h("td", { colspan: 3, class: "empty" }, "This folder is empty.")));
    fill(tbody, rows);
    const dirs = r.entries.filter((e) => e.type === "dir").length;
    fill(foot, `${dirs} folder${dirs === 1 ? "" : "s"} · ${r.entries.length - dirs} file${r.entries.length - dirs === 1 ? "" : "s"} · `,
      `${fmtBytes(r.files_bytes)} in files directly in this folder`,
      r.truncated ? h("strong", {}, ` · showing the first ${r.entries.length} of ${r.total} entries`) : null);
  }

  function go(scope, path, scroll = false) {
    state.scope = scope;
    state.path = path;
    load();
    if (scroll) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  return {
    el,
    go,
    setMounted(m) {
      el.hidden = !m;
      if (m && !state.loaded) load();
      if (!m) state.loaded = false;
    },
  };
}
