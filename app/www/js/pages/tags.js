// Operations › Tags › Tags in field: every tag the reader currently sees, by zone, live.
import { api, h, fill } from "../api.js";
import { errText } from "../ui.js";

export function render(root) {
  const state = { data: null, filter: "", zone: "" };
  const head = h("div");
  const chips = h("div", { class: "chips", style: { margin: "12px 0" } });
  const tbody = h("tbody");
  const search = h("input", { type: "search", placeholder: "Filter by EPC…", class: "mono", spellcheck: "false", "aria-label": "Filter by EPC" });
  search.addEventListener("input", () => { state.filter = search.value.trim().toUpperCase(); draw(); });

  root.append(
    h("div", { class: "toolbar" }, h("div", {}, h("h1", {}, "Tags in field"),
      h("p", { class: "subtitle" }, "Tags the reader can see right now, by zone. Updates every second.")), search),
    head, chips,
    h("div", { class: "card table-card" },
      h("table", { class: "table tags-table" },
        h("thead", {}, h("tr", {}, ["EPC", "Zone", "Antenna", "Signal", "Reads", "Here for", "Last read"].map((x, i) =>
          h("th", { class: i >= 2 ? "num" : null }, h("span", { class: "th-plain" }, x))))),
        tbody)));

  async function load() {
    try { state.data = (await api.rfid()).status; draw(); }
    catch (err) { fill(head, h("div", { class: "notice bad" }, "Can't read tags: ", errText(err))); }
  }

  function draw() {
    const s = state.data;
    fill(head, h("div", { class: "statline" },
      s.running ? h("span", { class: "pill ok" }, `Inventory running: ${s.active_preset || "?"}`)
        : h("span", { class: "pill neutral" }, "Inventory stopped"),
      h("span", {}, h("b", {}, String(s.tags_in_field)), " tags"),
      h("span", {}, h("b", {}, String(s.read_rate)), " reads/s"),
      !s.stream_connected ? h("span", { class: "pill bad", title: s.stream_error || "" }, "Tag stream not connected") : null,
      !s.running ? h("a", { href: "#/hardware/rfid" }, "Start inventory") : null));
    const zones = Object.entries(s.zones);
    fill(chips,
      chip("All", s.tags_in_field, ""),
      zones.map(([z, n]) => chip(z, n, z)));
    const rows = s.tags.filter((t) => (!state.zone || t.zone === state.zone) && (!state.filter || t.epc.includes(state.filter)));
    fill(tbody, rows.length ? rows.map((t) => h("tr", {},
      h("td", { class: "mono" }, t.epc),
      h("td", {}, t.zone),
      h("td", { class: "num" }, String(t.antenna)),
      h("td", { class: "num" }, t.rssi_dbm === null ? "—" : `${t.rssi_dbm} dBm`),
      h("td", { class: "num" }, String(t.reads)),
      h("td", { class: "num" }, dur(t.last_seen - t.first_seen)),
      h("td", { class: "num" }, t.seen_ago_s < 1 ? "now" : `${Math.round(t.seen_ago_s)} s ago`)))
      : h("tr", {}, h("td", { colspan: 7, class: "empty" },
        !s.running ? "Inventory isn't running." : s.tags.length ? "No tags match the filter." : "No tags in the field.")));
  }

  function chip(label, n, zone) {
    const b = h("button", { type: "button", class: "chip" + (state.zone === zone ? " on" : "") }, label, h("span", { class: "chip-n" }, String(n)));
    b.addEventListener("click", () => { state.zone = zone; draw(); });
    return b;
  }

  load();
  const timer = setInterval(load, 1000);
  return () => clearInterval(timer);
}

function dur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
