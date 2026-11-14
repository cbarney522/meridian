// System › Time & location: site identity, site time zone, coordinates (sunrise/sunset for rules),
// and the reader's NTP status. The reader's own clock stays on UTC (set by NTP); Meridian shows
// and schedules everything in the site time zone.
import { api, h, fill } from "../api.js";
import { toast, saveBar, errText } from "../ui.js";

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("system.edit");
  const state = { data: null, form: null, time: null, skew: 0, saving: false };
  const msg = toast();
  const clockCard = h("section", { class: "card" });
  const ntpCard = h("section", { class: "card wide" });
  const formBox = h("section", { class: "card wide" });
  const bar = saveBar({ onSave: save, onDiscard: () => { state.form = clone(state.data.settings); drawForm(); changed(); } });

  root.append(
    h("h1", {}, "Time & location"),
    h("p", { class: "subtitle" }, "Where this reader is and what time it is there. Rules, logs and the kiosk use the site time zone."),
    msg.el,
    h("div", { class: "grid grid-2" }, clockCard, formBox, ntpCard),
    bar.el);

  // ---------- data
  async function loadTime() {
    try {
      state.time = await api.time();
      state.skew = Date.parse(state.time.utc) - Date.now();
      drawClock(); drawNtp();
    } catch (err) { fill(clockCard, h("div", { class: "notice bad" }, "Can't read the reader's clock: ", errText(err))); }
  }
  async function loadSettings() {
    try {
      state.data = await api.siteSettings();
      state.form = clone(state.data.settings);
      drawForm(); changed();
    } catch (err) { fill(formBox, h("div", { class: "notice bad" }, "Can't load site settings: ", errText(err))); }
  }

  // ---------- clock
  const clockEls = { local: h("div", { class: "big clock" }), date: h("div", { class: "muted" }), utc: h("span") };
  function tick() {
    if (!state.time) return;
    const now = new Date(Date.now() + state.skew);
    const tz = state.time.timezone;
    const h24 = state.data ? state.data.settings.clock_24h : false;
    try {
      clockEls.local.textContent = now.toLocaleTimeString([], { timeZone: tz, hour12: !h24, hour: "numeric", minute: "2-digit", second: "2-digit" });
      clockEls.date.textContent = now.toLocaleDateString([], { timeZone: tz, weekday: "long", year: "numeric", month: "long", day: "numeric" });
    } catch { clockEls.local.textContent = now.toISOString().slice(11, 19); }
    clockEls.utc.textContent = now.toISOString().slice(0, 19).replace("T", " ") + " UTC";
  }
  const h24 = () => (state.data ? state.data.settings.clock_24h : false);
  function drawClock() {
    const t = state.time, n = t.ntp;
    const sync = !n.available ? h("span", { class: "pill neutral" }, "NTP status unknown")
      : n.tracking.synced ? h("span", { class: "pill ok" }, "Synchronized") : h("span", { class: "pill bad" }, "Not synchronized");
    const sun = t.sun;
    fill(clockCard,
      h("h2", {}, "Site time"),
      clockEls.local, clockEls.date,
      h("dl", { class: "kv", style: { marginTop: "12px" } },
        h("dt", {}, "Time zone"), h("dd", {}, `${t.timezone} (${t.tz_abbrev}, UTC${fmtOffset(t.utc_offset)})`),
        h("dt", {}, "Reader clock"), h("dd", {}, clockEls.utc),
        h("dt", {}, "Clock source"), h("dd", {}, sync),
        sun ? [h("dt", {}, "Sunrise"), h("dd", {}, fmtHM(sun.sunrise, h24())),
               h("dt", {}, "Sunset"), h("dd", {}, fmtHM(sun.sunset, h24())),
               h("dt", {}, "Daylight"), h("dd", {}, sun.day_length || (sun.sunrise ? "—" : "No sunrise/sunset today"))]
            : [h("dt", {}, "Sunrise / sunset"), h("dd", {}, h("span", { class: "muted" }, "Enter the site's coordinates"))]),
      t.tz_database ? null : h("div", { class: "notice warn", style: { marginTop: "12px" } },
        "No time-zone database found, so only UTC is available. Looked in: ", (t.tz_path || []).join(", ") || "(nowhere)", ". Rebuild the CAP on a machine with tzdata installed."));
    tick();
  }

  // ---------- NTP
  function drawNtp() {
    const n = state.time.ntp, tr = n.tracking;
    const ago = (s) => (s === null || s === undefined ? "never" : s < 120 ? `${s} s ago` : s < 7200 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`);
    fill(ntpCard,
      h("h2", {}, "Network time (NTP)"),
      !n.available ? h("p", { class: "muted" }, "Meridian can't read the reader's NTP status (chronyc not available).") : [
        h("dl", { class: "kv" },
          h("dt", {}, "Synchronized to"), h("dd", {}, tr.reference || "—", tr.stratum ? h("span", { class: "muted" }, ` · stratum ${tr.stratum}`) : null),
          h("dt", {}, "Clock offset"), h("dd", {}, `${fmtMs(tr.offset_ms)}`),
          h("dt", {}, "Last adjusted"), h("dd", {}, tr.ref_time ? ago(Math.round(Date.now() / 1000 + state.skew / 1000 - tr.ref_time)) : "—")),
        n.sources.length ? h("table", { class: "table simple", style: { marginTop: "12px" } },
          h("thead", {}, h("tr", {}, ["Server", "State", "Stratum", "Reachability", "Last reply", "Offset"].map((x, i) =>
            h("th", { class: i >= 2 ? "num" : null }, x)))),
          h("tbody", {}, n.sources.map((s) => h("tr", { class: s.selected ? "row-on" : null },
            h("td", {}, s.name), h("td", {}, h("span", { class: "pill " + (s.selected ? "ok" : s.reach ? "neutral" : "bad") }, s.state)),
            h("td", { class: "num" }, String(s.stratum)), h("td", { class: "num" }, `${Math.round(bits(s.reach) / 8 * 100)}%`),
            h("td", { class: "num" }, ago(s.last_rx_s)), h("td", { class: "num" }, fmtMs(s.offset_ms))))))
          : h("p", { class: "muted" }, "No NTP servers configured.")],
      h("p", { class: "hint", style: { marginTop: "12px" } },
        "The reader gets its NTP servers from DHCP. To use a specific server (for example the plant's own), set it in the ",
        "Impinj web UI's network settings or with RShell: ", h("code", {}, "config network ntp add <server>"),
        ". Meridian can't change it itself: CAPs don't have permission to change system settings."),
      state.time.reader_ntp_config ? h("details", {}, h("summary", {}, "NTP settings reported by the reader API"),
        h("pre", { class: "mono" }, JSON.stringify(state.time.reader_ntp_config, null, 2))) : null);
  }

  // ---------- settings form
  function drawForm() {
    const f = state.form, dis = !canEdit;
    const field = (label, control, help) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), control,
      help ? h("span", { class: "field-help" }, help) : null);
    const text = (key, attrs = {}) => {
      const el = h("input", { type: "text", value: f[key] ?? "", disabled: dis, spellcheck: "false", ...attrs });
      el.addEventListener("input", () => { f[key] = el.value; changed(); });
      return el;
    };
    // time zone: free text with a searchable list of every zone
    const zl = h("datalist", { id: "tz-list" }, state.data.zones.map((z) => h("option", { value: z })));
    const tz = h("input", { type: "text", list: "tz-list", value: f.timezone, disabled: dis, spellcheck: "false", autocomplete: "off" });
    tz.addEventListener("input", () => { f.timezone = tz.value.trim(); changed(); });
    const common = h("div", { class: "chips" }, state.data.common_zones.slice(0, 6).map((z) =>
      h("button", { type: "button", class: "btn btn-small", disabled: dis, onclick: () => { tz.value = f.timezone = z; changed(); } }, z.split("/").pop().replace("_", " "))));
    const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const useBrowser = browserTz && browserTz !== f.timezone && !dis
      ? h("button", { type: "button", class: "btn btn-small", onclick: () => { tz.value = f.timezone = browserTz; changed(); } }, `Use this computer's (${browserTz})`) : null;

    const fmt = h("div", {}, [[false, "12-hour (2:30 PM)"], [true, "24-hour (14:30)"]].map(([v, l]) => {
      const r = h("input", { type: "radio", name: "clockfmt", checked: f.clock_24h === v, disabled: dis });
      r.addEventListener("change", () => { f.clock_24h = v; changed(); tick(); });
      return h("label", { class: "radio" }, r, h("span", {}, l));
    }));

    const lat = text("latitude", { inputmode: "decimal", placeholder: "e.g. 34.0656" });
    const lon = text("longitude", { inputmode: "decimal", placeholder: "e.g. -84.6769" });
    // pasting "34.0656, -84.6769" (as copied from a map) into either box fills both
    for (const el of [lat, lon]) el.addEventListener("paste", (e) => {
      const m = (e.clipboardData.getData("text") || "").match(/(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)/);
      if (!m) return;
      e.preventDefault();
      lat.value = f.latitude = m[1]; lon.value = f.longitude = m[2]; changed(); drawMapLink();
    });
    lat.addEventListener("input", drawMapLink); lon.addEventListener("input", drawMapLink);
    const geoMsg = h("span", { class: "field-help" });
    const geo = dis || !navigator.geolocation ? null : h("button", { type: "button", class: "btn btn-small" }, "Use this device's location");
    if (geo) geo.addEventListener("click", () => {
      geoMsg.textContent = "Asking the browser…";
      navigator.geolocation.getCurrentPosition((p) => {
        lat.value = f.latitude = p.coords.latitude.toFixed(6); lon.value = f.longitude = p.coords.longitude.toFixed(6);
        geoMsg.textContent = `Accurate to about ${Math.round(p.coords.accuracy)} m. Only useful if this device is at the site.`;
        changed(); drawMapLink();
      }, (e) => {
        geoMsg.textContent = e.code === 1 ? "Location blocked. Browsers often refuse it on self-signed HTTPS; copy the coordinates from a map instead."
          : "Couldn't get a location from this device.";
      }, { enableHighAccuracy: true, timeout: 10000 });
    });
    const mapLink = h("span");
    function drawMapLink() {
      const a = parseFloat(lat.value), o = parseFloat(lon.value);
      fill(mapLink, Number.isFinite(a) && Number.isFinite(o)
        ? h("a", { href: `https://www.openstreetmap.org/?mlat=${a}&mlon=${o}#map=17/${a}/${o}`, target: "_blank", rel: "noopener noreferrer" }, "Check on a map ↗")
        : null);
    }
    drawMapLink();

    fill(formBox,
      h("h2", {}, "Site"),
      h("div", { class: "form-grid" },
        field("Site ID", text("site_id", { maxlength: 40, class: "mono", placeholder: "plant-atl" }),
          "Short name shared by every reader at this site. Lowercase, digits and dashes; used in MQTT topics and the Site page."),
        field("Reader name", text("site_name", { maxlength: 80, placeholder: "Crib 2 portal" }), "Shown in the top bar and on the Site page."),
        field("Location", text("location", { maxlength: 120, placeholder: "Building 2, tool crib, east door" }), "Where in the building this reader is."),
        field("Time zone", tz, null),
        h("div", { class: "field" }, h("span", { class: "field-label" }, "Quick pick"), common, useBrowser),
        h("div", { class: "field" }, h("span", { class: "field-label" }, "Clock format"), fmt),
        field("Latitude", lat, "Decimal degrees, north positive. Paste “lat, long” from a map into either box."),
        field("Longitude", lon, "Decimal degrees, west negative."),
        h("div", { class: "field" }, h("span", { class: "field-label" }, " "), h("div", { class: "actions" }, geo, mapLink), geoMsg)),
      zl,
      h("p", { class: "hint" }, "Coordinates give rules today's sunrise and sunset (for example, turn the entry light on at dusk)."));
  }

  function changed() {
    if (!state.data) return;
    const dirty = JSON.stringify(normalize(state.form)) !== JSON.stringify(normalize(state.data.settings));
    bar.update({ dirty, errors: localErrors(state.form), saving: state.saving });
  }
  async function save() {
    state.saving = true; changed();
    try {
      await api.saveSiteSettings(state.data.revision, normalize(state.form));
      msg.show("Site settings saved.");
      await loadSettings(); await loadTime();
    } catch (err) {
      msg.show("Couldn't save: " + errText(err), true);
      if (err.status === 409) await loadSettings();
    } finally { state.saving = false; changed(); }
  }

  loadSettings().then(loadTime);
  const t1 = setInterval(tick, 1000), t2 = setInterval(loadTime, 30000);
  return () => { clearInterval(t1); clearInterval(t2); msg.dispose(); };
}

function normalize(f) {
  const num = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
  return { ...f, site_id: (f.site_id || "").trim().toLowerCase(), latitude: num(f.latitude), longitude: num(f.longitude) };
}
function localErrors(f) {
  const e = [], n = normalize(f);
  if (n.site_id && !/^[a-z0-9][a-z0-9-]{0,39}$/.test(n.site_id)) e.push("Site ID: lowercase letters, digits and dashes.");
  if (n.latitude !== null && !(n.latitude >= -90 && n.latitude <= 90)) e.push("Latitude must be −90 to 90.");
  if (n.longitude !== null && !(n.longitude >= -180 && n.longitude <= 180)) e.push("Longitude must be −180 to 180.");
  if ((n.latitude === null) !== (n.longitude === null)) e.push("Enter both latitude and longitude, or neither.");
  return e;
}
function clone(o) { return JSON.parse(JSON.stringify(o)); }
function bits(n) { let c = 0; for (; n; n >>= 1) c += n & 1; return c; }
function fmtMs(ms) { return ms === null || ms === undefined ? "—" : Math.abs(ms) < 1 ? `${(ms * 1000).toFixed(0)} µs` : `${ms.toFixed(1)} ms`; }
function fmtOffset(z) { return z ? `${z.slice(0, 3)}:${z.slice(3)}` : ""; }
function fmtHM(hm, h24) {
  if (!hm) return "—";
  if (h24) return hm;
  const [H, M] = hm.split(":").map(Number);
  return `${H % 12 || 12}:${String(M).padStart(2, "0")} ${H < 12 ? "AM" : "PM"}`;
}
