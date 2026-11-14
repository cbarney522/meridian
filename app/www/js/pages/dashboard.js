// Dashboard: reader health, RFID activity, zones, active session, live event feed.
import { api, h } from "../api.js";

const EVENT_LABEL = {
  badge_scan: "Badge", tag_enter: "Returned", tag_exit: "Removed",
  session_closed: "Session closed", gpi_change: "Input", rule_fired: "Rule fired",
  usb_attached: "USB attached", usb_detached: "USB removed", storage_changed: "Storage",
};
const MAX_EVENTS = 40;

export function render(root, ctx) {
  const cards = h("div", { class: "grid" });
  const feed = h("ul", { class: "events" }, h("li", { class: "empty" }, "Waiting for events…"));
  root.append(
    h("h1", {}, "Dashboard"),
    h("p", { class: "subtitle" }, "Live status of this reader"),
    cards,
    h("div", { class: "card", style: { marginTop: "16px" } }, h("h2", {}, "Live events"), feed),
  );

  async function refresh() {
    try { drawCards(cards, await api.status()); }
    catch (err) { cards.replaceChildren(h("div", { class: "notice bad" }, "Status unavailable: ", String(err.message))); }
  }

  let first = true;
  const unsub = ctx.events.subscribe((ev) => {
    if (ev.type === "badge_raw") return;          // low-level; shown on Hardware › USB › Badge readers
    if (first) { feed.replaceChildren(); first = false; }
    feed.prepend(eventRow(ev));
    while (feed.children.length > MAX_EVENTS) feed.lastChild.remove();
    if (ev.type !== "gpi_change") refresh();   // counts and session changed
  });

  refresh();
  const timer = setInterval(refresh, 5000);
  return () => { clearInterval(timer); unsub(); };   // called when leaving the page
}

function drawCards(root, s) {
  const r = s.reader, f = s.rfid;
  const maxZone = Math.max(1, ...Object.values(f.zones));
  root.replaceChildren(
    h("section", { class: "card" }, h("h2", {}, "Reader"),
      h("dl", { class: "kv" },
        h("dt", {}, "Name"), h("dd", {}, r.name),
        h("dt", {}, "Serial"), h("dd", {}, r.serial),
        h("dt", {}, "Firmware"), h("dd", {}, r.firmware),
        h("dt", {}, "Meridian"), h("dd", {}, r.app_version),
        h("dt", {}, "Profile"), h("dd", {}, r.profile),
        h("dt", {}, "Uptime"), h("dd", {}, uptime(r.uptime_s)),
        h("dt", {}, "Clock"), h("dd", {}, pill(r.ntp_synced, "NTP synced", "Not synced")),
        h("dt", {}, "Temperature"), h("dd", {}, `${r.temp_c} °C`))),
    h("section", { class: "card" }, h("h2", {}, "RFID"),
      h("div", { class: "big" }, f.tags_in_field, h("small", {}, "tags in field")),
      h("div", { class: "big", style: { marginTop: "8px" } }, f.read_rate, h("small", {}, "reads / s")),
      h("p", {}, pill(f.running, "Inventory running", "Stopped"))),
    h("section", { class: "card" }, h("h2", {}, "Zones"),
      h("div", { class: "bars" }, Object.entries(f.zones).map(([z, n]) =>
        h("div", { class: "bar-row" }, h("span", {}, z),
          h("div", { class: "bar-track" }, h("div", { class: "bar-fill", style: { width: `${(n / maxZone) * 100}%` } })),
          h("span", { class: "n" }, n))))),
    h("section", { class: "card" }, h("h2", {}, "Delivery & devices"),
      h("dl", { class: "kv" },
        h("dt", {}, "MQTT"), h("dd", {}, pill(s.mqtt.connected, "Connected", "Disconnected")),
        h("dt", {}, "Outbox"), h("dd", {}, s.mqtt.outbox_depth === 0 ? pill(true, "Empty") : h("span", { class: "pill warn" }, `${s.mqtt.outbox_depth} queued`)),
        h("dt", {}, "USB drive"), h("dd", {}, pill(s.usb.drive, `${(s.usb.free_mb / 1024).toFixed(1)} GB free`, "Missing")),
        h("dt", {}, "Badge in"), h("dd", {}, pill(s.usb.badge_in, "Present", "Missing")),
        h("dt", {}, "Badge out"), h("dd", {}, pill(s.usb.badge_out, "Present", "Missing")))),
    h("section", { class: "card wide" }, h("h2", {}, "Active session"),
      s.session
        ? [h("div", { class: "big" }, s.session.user, h("small", {}, `badge ${s.session.badge}`)),
           h("dl", { class: "kv", style: { marginTop: "8px" } },
             h("dt", {}, "Removed"), h("dd", {}, s.session.out.length ? s.session.out.join(", ") : "—"),
             h("dt", {}, "Returned"), h("dd", {}, s.session.in.length ? s.session.in.join(", ") : "—"))]
        : h("p", { class: "empty" }, "No one badged in")),
  );
}

function eventRow(ev) {
  const time = ev.ts.slice(11);
  let cls = ev.type, text;
  switch (ev.type) {
    case "badge_scan":
      if (ev.result === "granted") text = `${ev.user} (badge ${ev.badge}) — access granted`;
      else { cls = "denied"; text = `Badge ${ev.badge} — denied (${ev.result.replace("denied_", "")})`; }
      break;
    case "tag_exit": text = `${ev.tool} from ${ev.zone} by ${ev.user}`; break;
    case "tag_enter": text = `${ev.tool} to ${ev.zone} by ${ev.user}`; break;
    case "session_closed": text = `${ev.user}: ${ev.removed} removed, ${ev.returned} returned`; break;
    case "gpi_change": text = `${ev.input} ${ev.state}`; break;
    case "rule_fired": text = `${ev.rule} — ${ev.detail}`; break;
    case "usb_attached": case "usb_detached": text = `${ev.product || "Device"} on port ${ev.port}`; break;
    case "storage_changed": text = `Data drive ${ev.action === "eject" ? "ejected" : ev.action === "mount" ? "mounted" : "formatted"}`; break;
    default: text = JSON.stringify(ev);
  }
  return h("li", {}, h("time", {}, time), h("span", { class: "etype " + cls }, EVENT_LABEL[ev.type] || ev.type), h("span", {}, text));
}

function pill(ok, yes, no = yes) { return h("span", { class: "pill " + (ok ? "ok" : "bad") }, ok ? yes : no); }
function uptime(s) {
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d}d ${hr}h ${m}m`;
}
