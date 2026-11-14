// Dashboard: reader health, RFID activity, zones, active session, live event feed.
import { api, h } from "../api.js";

const EVENT_LABEL = {
  badge_scan: "Badge", tag_enter: "Arrived", tag_exit: "Left",
  session_closed: "Session closed", gpi_change: "Input", rule_fired: "Rule fired",
  usb_attached: "USB attached", usb_detached: "USB removed", storage_changed: "Storage",
  mqtt_state: "MQTT", identify: "Identify", badge_raw: "Scan", data_imported: "Data",
};
const MAX_EVENTS = 40;

export function render(root, ctx) {
  const canAbout = ctx.session.permissions.includes("system.view");
  const cards = h("div", { class: "grid" });
  const feed = h("ul", { class: "events" }, h("li", { class: "empty" }, "Waiting for events…"));
  root.append(
    h("h1", {}, "Dashboard"),
    h("p", { class: "subtitle" }, "Live status of this reader"),
    cards,
    h("div", { class: "card", style: { marginTop: "16px" } }, h("h2", {}, "Live events"), feed),
  );

  async function refresh() {
    try { drawCards(cards, await api.status(), canAbout); }
    catch (err) { cards.replaceChildren(h("div", { class: "notice bad" }, "Status unavailable: ", String(err.message))); }
  }

  let first = true;
  const unsub = ctx.events.subscribe((ev) => {
    const row = eventRow(ev);
    if (!row) return;
    if (first) { feed.replaceChildren(); first = false; }
    feed.prepend(row);
    while (feed.children.length > MAX_EVENTS) feed.lastChild.remove();
    if (!ev.replay && ev.type !== "gpi_change") refresh();   // counts and session changed
  }, { history: true });

  refresh();
  const timer = setInterval(refresh, 5000);
  return () => { clearInterval(timer); unsub(); };   // called when leaving the page
}

function drawCards(root, s, canAbout) {
  const r = s.reader, f = s.rfid;
  const rfidOn = f.available !== false, mqttOn = s.mqtt.available !== false;
  const maxZone = Math.max(1, ...Object.values(f.zones));
  const v = (x) => (x === null || x === undefined ? h("span", { class: "muted" }, "—") : x);
  const notYet = (what) => h("p", { class: "muted" }, `${what} isn't running in this build yet.`);
  const apiNote = r.api_ok === false
    ? h("p", { class: "hint" }, "Impinj details unavailable",
        canAbout ? [" — ", h("a", { href: "#/system/about" }, "set the reader login")] : null, ".")
    : null;
  root.replaceChildren(
    h("section", { class: "card" }, h("h2", {}, "Reader"),
      h("dl", { class: "kv" },
        h("dt", {}, "Name"), h("dd", {}, v(r.name)),
        h("dt", {}, "Model"), h("dd", {}, v(r.description)),
        h("dt", {}, "Serial"), h("dd", {}, v(r.serial)),
        h("dt", {}, "Firmware"), h("dd", {}, v(r.firmware)),
        h("dt", {}, "Region"), h("dd", {}, v(r.region)),
        h("dt", {}, "Interface"), h("dd", {}, v(r.rfid_interface)),
        h("dt", {}, "Meridian"), h("dd", {}, v(r.app_version)),
        h("dt", {}, "Profile"), h("dd", {}, v(r.profile)),
        h("dt", {}, "Uptime"), h("dd", {}, uptime(r.uptime_s)),
        h("dt", {}, "Clock"), h("dd", {}, r.ntp_synced === null || r.ntp_synced === undefined
          ? h("span", { class: "pill neutral" }, "Unknown") : pill(r.ntp_synced, "NTP synced", "Not synced")),
        h("dt", {}, "Temperature"), h("dd", {}, r.temp_c === null || r.temp_c === undefined ? v(null) : `${r.temp_c} °C`)),
      apiNote),
    h("section", { class: "card" }, h("h2", {}, "Network"),
      h("dl", { class: "kv" },
        h("dt", {}, "IPv4"), h("dd", {}, v(r.ipv4)),
        h("dt", {}, "IPv6"), h("dd", {}, v(r.ipv6)),
        h("dt", {}, "MAC"), h("dd", {}, v(r.mac)),
        h("dt", {}, "Reader time"), h("dd", {}, r.time ? `${clock(r.time.slice(11, 19), r.clock_24h)} ${r.timezone || ""}` : v(null)))),
    h("section", { class: "card" }, h("h2", {}, "RFID"),
      rfidOn ? [
        h("div", { class: "big" }, f.tags_in_field, h("small", {}, "tags in field")),
        h("div", { class: "big", style: { marginTop: "8px" } }, f.read_rate, h("small", {}, "reads / s")),
        h("p", {}, pill(f.running, "Inventory running", "Stopped"))] : notYet("The RFID engine")),
    h("section", { class: "card" }, h("h2", {}, "Zones"),
      Object.keys(f.zones).length
        ? h("div", { class: "bars" }, Object.entries(f.zones).map(([z, n]) =>
            h("div", { class: "bar-row" }, h("span", {}, z),
              h("div", { class: "bar-track" }, h("div", { class: "bar-fill", style: { width: `${(n / maxZone) * 100}%` } })),
              h("span", { class: "n" }, n))))
        : h("p", { class: "muted" }, "No zones configured.")),
    h("section", { class: "card" }, h("h2", {}, "Delivery & devices"),
      h("dl", { class: "kv" },
        h("dt", {}, "MQTT"), h("dd", {}, !mqttOn ? h("span", { class: "pill neutral" }, "Not in this build")
          : s.mqtt.enabled === false ? h("a", { href: "#/integrations/mqtt", class: "pill neutral" }, "Not set up")
          : h("span", { title: s.mqtt.error || s.mqtt.broker || "" }, pill(s.mqtt.connected, "Connected", "Disconnected"))),
        h("dt", {}, "Outbox"), h("dd", {}, !mqttOn || s.mqtt.enabled === false ? v(null) : s.mqtt.outbox_depth === 0 ? pill(true, "Empty") : h("span", { class: "pill warn" }, `${s.mqtt.outbox_depth} queued`)),
        h("dt", {}, "USB drive"), h("dd", {}, pill(s.usb.drive, s.usb.free_mb ? `${(s.usb.free_mb / 1024).toFixed(1)} GB free` : "Present", "Missing")),
        h("dt", {}, "Badge in"), h("dd", {}, pill(s.usb.badge_in, "Present", "Missing")),
        h("dt", {}, "Badge out"), h("dd", {}, pill(s.usb.badge_out, "Present", "Missing")))),
    h("section", { class: "card wide" }, h("h2", {}, "Active session"),
      s.session
        ? [h("div", { class: "big" }, s.session.user, h("small", {}, `badge ${s.session.badge}`)),
           h("dl", { class: "kv", style: { marginTop: "8px" } },
             h("dt", {}, "Removed"), h("dd", {}, s.session.out.length ? s.session.out.join(", ") : "—"),
             h("dt", {}, "Returned"), h("dd", {}, s.session.in.length ? s.session.in.join(", ") : "—"))]
        : h("p", { class: "empty" }, rfidOn ? "No one badged in" : "Sessions start once the RFID engine is running.")),
  );
}

function eventRow(ev) {
  const today = new Date().toISOString().slice(0, 10);
  const time = (ev.ts.slice(0, 10) === today ? "" : ev.ts.slice(5, 10) + " ") + ev.ts.slice(11);
  let cls = ev.type, text, label = EVENT_LABEL[ev.type] || ev.type;
  switch (ev.type) {
    case "badge_scan":
      if (ev.result === "granted") text = `${ev.user} (badge ${ev.badge}) — access granted`;
      else { cls = "denied"; text = `Badge ${ev.badge} — denied (${ev.result.replace("denied_", "")})`; }
      break;
    case "tag_exit": {
      const what = ev.tool || ev.epc, who = ev.user ? ` by ${ev.user}` : "";
      if (ev.reason === "moved") label = "Moved";
      text = ev.reason === "moved" ? `${what} moved from ${ev.zone} to ${ev.to_zone}${who}`
        : `${what} left ${ev.zone}${who}${ev.dwell_s !== undefined ? ` after ${fmtDwell(ev.dwell_s)}` : ""}`;
      break;
    }
    case "tag_enter":
      if (ev.from_zone) return null;          // a move: the matching tag_exit line already says it
      text = `${ev.tool || ev.epc} arrived in ${ev.zone}${ev.user ? ` by ${ev.user}` : ""}`;
      break;
    case "session_closed": text = `${ev.user}: ${ev.removed} removed, ${ev.returned} returned`; break;
    case "gpi_change": text = `${ev.input} ${ev.state}`; break;
    case "rule_fired": text = `${ev.rule} — ${ev.detail}`; break;
    case "usb_attached": case "usb_detached": text = `${ev.product || "Device"} on port ${ev.port}`; break;
    case "badge_raw":
      if (ev.result && ev.result.ok) text = `Badge ${ev.result.badge_id} on ${ev.role ? ev.role.replace("_", " ") : "port " + ev.port}`;
      else { cls = "denied"; text = `Unreadable scan on ${ev.role ? ev.role.replace("_", " ") : "port " + ev.port}${ev.result && ev.result.error ? ` (${ev.result.error})` : ""}`; }
      break;
    case "data_imported": text = `${ev.user || "Someone"} imported ${Number(ev.rows).toLocaleString()} rows into ${ev.dataset}`; break;
    case "mqtt_state": cls = ev.connected ? "mqtt_state" : "denied"; text = ev.connected ? "Connected to the broker" : "Lost the broker connection"; break;
    case "identify": text = `Identify requested${ev.source ? " over " + ev.source.toUpperCase() : ""} (${ev.seconds} s)`; break;
    case "storage_changed": text = `Data drive ${ev.action === "eject" ? "ejected" : ev.action === "mount" ? "mounted" : "formatted"}`; break;
    default: text = JSON.stringify(ev);
  }
  return h("li", {}, h("time", {}, time), h("span", { class: "etype " + cls }, label), h("span", {}, text));
}

function fmtDwell(s) {
  s = Math.round(s);
  return s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
function clock(hms, h24) {
  if (h24) return hms;
  const [H, M, S] = hms.split(":").map(Number);
  return `${H % 12 || 12}:${String(M).padStart(2, "0")}:${String(S).padStart(2, "0")} ${H < 12 ? "AM" : "PM"}`;
}
function pill(ok, yes, no = yes) { return h("span", { class: "pill " + (ok ? "ok" : "bad") }, ok ? yes : no); }
function uptime(s) {
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d}d ${hr}h ${m}m`;
}
