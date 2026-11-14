// Site: every Meridian reader at this site, from their MQTT heartbeats.
// Click a reader to open its own Meridian UI in a new tab.
import { api, h } from "../api.js";

const COLUMNS = [
  { key: "status",      label: "Status" },
  { key: "name",        label: "Reader" },
  { key: "location",    label: "Location" },
  { key: "profile",     label: "Profile" },
  { key: "app_version", label: "Version" },
  { key: "tags",        label: "Tags", num: true },
  { key: "outbox",      label: "Outbox", num: true },
  { key: "last_seen",   label: "Last seen", num: true },
  { key: "alerts",      label: "Alerts" },
];
const ALERT_TEXT = { offline: "Offline", usb_missing: "USB drive missing", outbox_backlog: "Outbox backlog" };
const FILTERS = [
  ["all", "All"], ["online", "Online"], ["offline", "Offline"], ["attention", "Needs attention"], ["outdated", "Out of date"],
];

export function render(root) {
  const state = { data: null, filter: "all", query: "", sort: "status", dir: 1 };

  const title = h("h1", {}, "Site");
  const subtitle = h("p", { class: "subtitle" }, "Loading…");
  const chips = h("div", { class: "chips", role: "group", "aria-label": "Filter readers" });
  const search = h("input", { type: "search", class: "search", placeholder: "Search name, location, serial, IP…",
                              "aria-label": "Search readers" });
  const thead = h("thead");
  const tbody = h("tbody");
  const table = h("table", { class: "table" }, thead, tbody);

  root.append(title, subtitle,
    h("div", { class: "toolbar" }, chips, search),
    h("div", { class: "card table-card" }, table));

  search.addEventListener("input", () => { state.query = search.value.trim().toLowerCase(); draw(); });

  async function load() {
    try { state.data = await api.siteReaders(); draw(); }
    catch (err) { tbody.replaceChildren(h("tr", {}, h("td", { colspan: COLUMNS.length + 1, class: "empty" },
      "Site list unavailable: ", String(err.message)))); }
  }

  function draw() {
    const d = state.data;
    if (!d) return;
    const rows = d.readers.map((r) => ({ ...r, outdated: isOutdated(r.app_version, d.latest_version) }));
    const counts = {
      all: rows.length,
      online: rows.filter((r) => r.online).length,
      offline: rows.filter((r) => !r.online).length,
      attention: rows.filter((r) => r.alerts.length).length,
      outdated: rows.filter((r) => r.outdated).length,
    };
    subtitle.textContent = `${d.site} · ${rows.length} readers · latest Meridian ${d.latest_version}`;

    chips.replaceChildren(...FILTERS.map(([id, label]) =>
      h("button", { type: "button", class: "chip" + (state.filter === id ? " on" : "") + (id === "offline" || id === "attention" ? " warnish" : ""),
                    "aria-pressed": String(state.filter === id),
                    onclick: () => { state.filter = id; draw(); } },
        label, h("span", { class: "chip-n" }, counts[id]))));

    thead.replaceChildren(h("tr", {}, COLUMNS.map((c) =>
      h("th", { class: c.num ? "num" : null,
                "aria-sort": state.sort === c.key ? (state.dir > 0 ? "ascending" : "descending") : "none" },
        h("button", { type: "button", class: "th-sort", onclick: () => {
            state.dir = state.sort === c.key ? -state.dir : 1; state.sort = c.key; draw(); } },
          c.label, h("span", { class: "sort-ind", "aria-hidden": "true" },
            state.sort === c.key ? (state.dir > 0 ? "▲" : "▼") : "")))),
      h("th", {}, h("span", { class: "visually-hidden" }, "Open"))));

    const q = state.query;
    const shown = rows
      .filter((r) => state.filter === "all"
        || (state.filter === "online" && r.online)
        || (state.filter === "offline" && !r.online)
        || (state.filter === "attention" && r.alerts.length)
        || (state.filter === "outdated" && r.outdated))
      .filter((r) => !q || [r.name, r.location, r.serial, r.ip, r.profile].some((v) => v.toLowerCase().includes(q)))
      .sort((a, b) => compare(a, b, state.sort) * state.dir);

    if (!shown.length) {
      tbody.replaceChildren(h("tr", {}, h("td", { colspan: COLUMNS.length + 1, class: "empty" }, "No readers match.")));
      return;
    }
    tbody.replaceChildren(...shown.map((r) => row(r, d.latest_version)));
  }

  load();
  const timer = setInterval(load, 10000);
  return () => clearInterval(timer);
}

function row(r) {
  const href = r.this_reader ? "#/dashboard" : r.url;
  const ext = r.this_reader ? {} : { target: "_blank", rel: "noopener noreferrer" };
  return h("tr", { class: r.online ? null : "row-off" },
    h("td", {}, h("span", { class: "status " + (r.online ? "on" : "off") }, r.online ? "Online" : "Offline")),
    h("td", {}, h("a", { href, ...ext, class: "reader-link" }, r.name),
      r.this_reader ? h("span", { class: "tag" }, "this reader") : null,
      h("div", { class: "sub" }, `${r.serial} · ${r.ip}`)),
    h("td", {}, r.location),
    h("td", {}, r.profile),
    h("td", {}, r.outdated ? h("span", { class: "pill warn", title: "Newer version available" }, r.app_version) : r.app_version,
      h("div", { class: "sub" }, `FW ${r.firmware}`)),
    h("td", { class: "num" }, r.tags_in_field ?? "—"),
    h("td", { class: "num" }, r.outbox_depth ? h("span", { class: r.outbox_depth > 100 ? "pill bad" : "pill warn" }, r.outbox_depth) : "0"),
    h("td", { class: "num" }, ago(r.last_seen_s)),
    h("td", {}, r.alerts.length ? r.alerts.map((a) => h("span", { class: "pill bad alert" }, ALERT_TEXT[a] || a)) : h("span", { class: "muted" }, "—")),
    h("td", { class: "open" }, h("a", { href, ...ext, class: "open-link",
      "aria-label": `Open ${r.name}` + (r.this_reader ? "" : " in a new tab") }, r.this_reader ? "Dashboard" : "Open ↗")),
  );
}

function compare(a, b, key) {
  switch (key) {
    case "status": return (b.alerts.length - a.alerts.length) || (a.online - b.online) || a.name.localeCompare(b.name);
    case "tags": return (a.tags_in_field ?? -1) - (b.tags_in_field ?? -1);
    case "outbox": return a.outbox_depth - b.outbox_depth;
    case "last_seen": return a.last_seen_s - b.last_seen_s;
    case "alerts": return a.alerts.length - b.alerts.length;
    case "app_version": return verCmp(a.app_version, b.app_version);
    case "name": return a.name.localeCompare(b.name, undefined, { numeric: true });
    default: return String(a[key]).localeCompare(String(b[key]), undefined, { numeric: true });
  }
}

function verParts(v) { return v.split("-")[0].split(".").map(Number); }
function verCmp(a, b) {
  const x = verParts(a), y = verParts(b);
  for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] - y[i];
  return 0;
}
function isOutdated(v, latest) { return !v.includes("-") && verCmp(v, latest) < 0; }   // dev builds aren't flagged

function ago(s) {
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
