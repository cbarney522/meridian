// Hardware › RFID › Inventory & zones: which reader preset runs, antenna → zone names, and how
// raw reads become "arrived" / "left" events.
import { api, h, fill } from "../api.js";
import { toast, saveBar, errText } from "../ui.js";

const PORTS = [1, 2, 3, 4];          // R700 antenna ports

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("rfid.edit");
  const state = { data: null, form: null, saving: false, busy: false };
  const msg = toast();
  const invCard = h("section", { class: "card" });
  const zonesCard = h("section", { class: "card" });
  const trackCard = h("section", { class: "card full" });
  const bar = saveBar({ onSave: save, onDiscard: () => { state.form = clone(state.data.settings); drawForms(); changed(); } });

  root.append(
    h("h1", {}, "Inventory & zones"),
    h("p", { class: "subtitle" }, "Meridian runs one of the reader's saved inventory presets, names each antenna's zone, and reports tags arriving and leaving. ",
      "Watch the result on ", h("a", { href: "#/operations/tags" }, "Tags in field"), "."),
    msg.el,
    h("div", { class: "grid grid-2" }, invCard, zonesCard, trackCard),
    bar.el);

  async function load(refresh = false, rebuild = false) {
    try {
      const d = await api.rfid(refresh);
      state.data = d;
      if (rebuild || !state.form) { state.form = clone(d.settings); drawForms(); changed(); }
      drawInventory(); drawZoneCounts();
    } catch (err) { fill(invCard, h("div", { class: "notice bad" }, "Can't load RFID settings: ", errText(err))); }
  }

  // ---------- inventory (live state + start/stop)
  function drawInventory() {
    const s = state.data.status, f = state.form;
    const statePill = s.reader_error && !s.reader_status ? h("span", { class: "pill bad" }, "Reader API unavailable")
      : s.running ? h("span", { class: "pill ok" }, `Running: ${s.active_preset || "?"}`) : h("span", { class: "pill neutral" }, "Stopped");
    const presets = state.data.presets;
    const sel = h("select", { disabled: !canEdit, "aria-label": "Preset" },
      h("option", { value: "", selected: !f.preset }, presets.length ? "Choose a preset…" : "No presets found"),
      presets.map((p) => h("option", { value: p, selected: f.preset === p }, p)),
      f.preset && !presets.includes(f.preset) ? h("option", { value: f.preset, selected: true }, `${f.preset} (not on the reader)`) : null);
    sel.addEventListener("change", () => { f.preset = sel.value; changed(); });
    const auto = h("input", { type: "checkbox", checked: f.auto_start, disabled: !canEdit });
    auto.addEventListener("change", () => { f.auto_start = auto.checked; changed(); });
    const start = h("button", { type: "button", class: "btn btn-primary", disabled: !canEdit || state.busy || !f.preset }, s.running ? "Restart" : "Start inventory");
    const stop = h("button", { type: "button", class: "btn", disabled: !canEdit || state.busy || !s.running }, "Stop");
    start.addEventListener("click", () => act(() => api.rfidStart(f.preset), `Inventory started with “${f.preset}”.`));
    stop.addEventListener("click", () => act(() => api.rfidStop(), "Inventory stopped."));
    const refresh = h("button", { type: "button", class: "btn btn-small", disabled: state.busy }, "Refresh list");
    refresh.addEventListener("click", () => load(true));
    const impinjUi = `https://${location.hostname}/`;
    fill(invCard, h("h2", {}, "Inventory"),
      h("dl", { class: "kv" },
        h("dt", {}, "State"), h("dd", {}, statePill),
        h("dt", {}, "Tag stream"), h("dd", {}, s.stream_connected ? h("span", { class: "pill ok" }, "Connected")
          : h("span", { class: "pill bad", title: s.stream_error || "" }, "Not connected")),
        h("dt", {}, "Reads / s"), h("dd", {}, String(s.read_rate)),
        h("dt", {}, "Tags in field"), h("dd", {}, String(s.tags_in_field))),
      s.reader_error ? h("div", { class: "notice warn", style: { marginTop: "10px" } }, s.reader_error,
        /401|403|login/i.test(s.reader_error) ? [" — check the reader login on ", h("a", { href: "#/system/about" }, "About this reader"), "."] : null) : null,
      h("div", { class: "field", style: { marginTop: "12px" } }, h("span", { class: "field-label" }, "Preset"),
        h("div", { class: "actions" }, sel, refresh),
        h("span", { class: "field-help" }, "Presets are created and tuned (antennas, power, search mode) in the ",
          h("a", { href: impinjUi, target: "_blank", rel: "noopener noreferrer" }, "Impinj web UI ↗"),
          " under Profile Presets. Use a reporting interval of 0 so every read is reported.")),
      h("label", { class: "radio", style: { marginTop: "8px" } }, auto, h("span", {}, "Start this preset automatically when the reader starts")),
      h("div", { class: "actions", style: { marginTop: "12px" } }, start, stop));
  }

  async function act(fn, ok) {
    state.busy = true; drawInventory();
    try { await fn(); msg.show(ok); }
    catch (err) { msg.show(errText(err), true); }
    finally { state.busy = false; await load(); }
  }

  // ---------- zones
  const zoneInputs = {}, zoneCounts = {};
  function drawForms() {
    const f = state.form, dis = !canEdit;
    fill(zonesCard, h("h2", {}, "Zones"),
      h("p", { class: "hint" }, "Name what each antenna covers. Antennas sharing a name form one zone (e.g. two antennas on one drawer)."),
      h("table", { class: "table simple" },
        h("thead", {}, h("tr", {}, ["Antenna", "Zone name", "Tags now"].map((x, i) => h("th", { class: i === 2 ? "num" : null }, x)))),
        h("tbody", {}, PORTS.map((p) => {
          const inp = zoneInputs[p] = h("input", { type: "text", value: f.zones[String(p)] || "", disabled: dis, maxlength: 32,
            placeholder: `Antenna ${p}`, "aria-label": `Zone for antenna ${p}` });
          inp.addEventListener("input", () => { f.zones[String(p)] = inp.value; changed(); });
          zoneCounts[p] = h("td", { class: "num" }, "—");
          return h("tr", {}, h("td", {}, `Port ${p}`), h("td", { class: "field" }, inp), zoneCounts[p]);
        }))),
      check("ignore_unzoned", "Ignore antennas without a zone name"));

    const num = (key, label, help, attrs = {}) => {
      const el = h("input", { type: "text", inputmode: "decimal", value: f[key] ?? "", disabled: dis, ...attrs });
      el.addEventListener("input", () => { f[key] = el.value; changed(); });
      return h("label", { class: "field" }, h("span", { class: "field-label" }, label), el, h("span", { class: "field-help" }, help));
    };
    fill(trackCard, h("h2", {}, "Arrived / left detection"),
      h("div", { class: "form-grid" },
        num("enter_reads", "Reads to confirm arrival", "A tag counts as arrived after this many reads. Filters out stray reads from passing tags."),
        num("exit_timeout_s", "Gone after (seconds)", "A tag not read for this long has left. Raise it if tags at the edge of the field flicker in and out."),
        num("move_quiet_s", "Move delay (seconds)", "A tag read by another zone's antenna only moves there once its old zone hasn't read it for this long."),
        num("rssi_min_dbm", "Minimum signal (dBm)", "Ignore weaker reads, e.g. -65, to keep tags in neighbouring areas out. Empty = no limit.", { placeholder: "no limit" }),
        num("epc_prefix", "Only EPCs starting with", "Hex, e.g. E280 or your company prefix. Empty = all tags.", { class: "mono", placeholder: "all tags" })));
  }
  function check(key, label) {
    const el = h("input", { type: "checkbox", checked: !!state.form[key], disabled: !canEdit });
    el.addEventListener("change", () => { state.form[key] = el.checked; changed(); });
    return h("label", { class: "radio", style: { marginTop: "10px" } }, el, h("span", {}, label));
  }
  function drawZoneCounts() {
    const byPort = {};
    for (const t of state.data.status.tags) byPort[t.antenna] = (byPort[t.antenna] || 0) + 1;
    for (const p of PORTS) if (zoneCounts[p]) zoneCounts[p].textContent = String(byPort[p] || 0);
  }

  // ---------- save
  function changed() {
    if (!state.data) return;
    const dirty = JSON.stringify(norm(state.form)) !== JSON.stringify(norm(state.data.settings));
    bar.update({ dirty, errors: localErrors(state.form), saving: state.saving });
    if (state.data) drawInventory();
  }
  async function save() {
    state.saving = true; changed();
    try {
      await api.saveRfid(state.data.revision, norm(state.form));
      msg.show("Saved. Zone names and detection settings apply immediately.");
      await load(false, true);
    } catch (err) {
      msg.show("Couldn't save: " + errText(err), true);
      if (err.status === 409) await load(false, true);
    } finally { state.saving = false; changed(); }
  }

  load(true, true);
  const timer = setInterval(() => load(), 2000);
  return () => { clearInterval(timer); msg.dispose(); };
}

function clone(o) { return JSON.parse(JSON.stringify(o)); }
function norm(f) {
  const zones = {};
  for (const [p, n] of Object.entries(f.zones || {})) if ((n || "").trim()) zones[p] = n.trim();
  const num = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
  return { ...f, zones, enter_reads: num(f.enter_reads), exit_timeout_s: num(f.exit_timeout_s), move_quiet_s: num(f.move_quiet_s),
           rssi_min_dbm: num(f.rssi_min_dbm), epc_prefix: (f.epc_prefix || "").trim().toUpperCase() };
}
function localErrors(f) {
  const e = [], n = norm(f);
  if (n.auto_start && !n.preset) e.push("Pick a preset to start automatically.");
  if (!(n.enter_reads >= 1 && n.enter_reads <= 50)) e.push("Reads to confirm: 1–50.");
  if (!(n.exit_timeout_s >= 1 && n.exit_timeout_s <= 600)) e.push("Gone after: 1–600 s.");
  if (!(n.move_quiet_s >= 0 && n.move_quiet_s <= 30)) e.push("Move delay: 0–30 s.");
  if (n.rssi_min_dbm !== null && !(n.rssi_min_dbm >= -100 && n.rssi_min_dbm <= 0)) e.push("Minimum signal: -100 to 0 dBm.");
  if (!/^[0-9A-F]*$/.test(n.epc_prefix)) e.push("EPC prefix: hex digits only.");
  return e;
}
