// Hardware › RFID › Inventory & zones: which reader preset runs, antenna → zone names, and how
// raw reads become "arrived" / "left" events.
import { api, h, fill } from "../api.js";
import { toast, saveBar, errText } from "../ui.js";

const PORTS = [1, 2, 3, 4];          // R700 antenna ports
const PATTERN = "pattern:";          // Preset values naming a firing pattern

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("rfid.edit");
  const state = { data: null, form: null, saving: false, busy: false };
  const msg = toast();
  const invCard = h("section", { class: "card" });
  const zonesCard = h("section", { class: "card" });
  const trackCard = h("section", { class: "card full" });
  const patternsCard = h("section", { class: "card full" });
  let editing = null;          // { index: -1 for new, draft: {...} } while the pattern editor is open
  const bar = saveBar({ onSave: save, onDiscard: () => { state.form = clone(state.data.settings); buildInventory(); drawForms(); changed(); } });

  root.append(
    h("h1", {}, "Inventory & zones"),
    h("p", { class: "subtitle" }, "Meridian runs one of the reader's saved inventory presets, names each antenna's zone, and reports tags arriving and leaving. ",
      "Watch the result on ", h("a", { href: "#/operations/tags" }, "Tags in field"), "."),
    msg.el,
    h("div", { class: "grid grid-2" }, invCard, zonesCard, patternsCard, trackCard),
    bar.el);

  async function load(refresh = false, rebuild = false) {
    try {
      const d = await api.rfid(refresh);
      state.data = d;
      if (rebuild || !state.form) { state.form = clone(d.settings); buildInventory(); drawForms(); changed(); showPresetInfo(); }
      if (refresh) { for (const k of Object.keys(presetCache)) delete presetCache[k]; showPresetInfo(); }
      drawInventory(); drawZoneCounts();
    } catch (err) { fill(invCard, h("div", { class: "notice bad" }, "Can't load RFID settings: ", errText(err))); }
  }

  // ---------- inventory (live state + start/stop)
  // Built once; the 2-second refresh only updates the values and buttons in place, so an open
  // dropdown or a focused control is never replaced under the user's pointer.
  const inv = {};
  function buildInventory() {
    const f = state.form;
    inv.state = h("dd"); inv.stream = h("dd"); inv.rate = h("dd"); inv.count = h("dd");
    inv.stepDt = h("dt", {}, "Step"); inv.step = h("dd");
    inv.error = h("div");
    inv.sel = h("select", { disabled: !canEdit, "aria-label": "Preset" });
    inv.sel.addEventListener("change", () => { f.preset = inv.sel.value; changed(); showPresetInfo(); });
    inv.info = h("div", { class: "preset-info" });
    inv.presetsKey = null;
    const auto = h("input", { type: "checkbox", checked: f.auto_start, disabled: !canEdit });
    auto.addEventListener("change", () => { f.auto_start = auto.checked; changed(); });
    inv.start = h("button", { type: "button", class: "btn btn-primary" });
    inv.stop = h("button", { type: "button", class: "btn" }, "Stop");
    inv.start.addEventListener("click", () => {
      const sel = state.form.preset, pat = (state.form.patterns || []).find((p) => PATTERN + p.id === sel);
      if (pat && !(state.data.settings.patterns || []).some((p) => p.id === pat.id)) {
        return msg.show("Save the page first: this firing pattern hasn't been saved yet.", true);
      }
      act(() => api.rfidStart(sel), pat ? `Firing pattern “${pat.name}” started.` : `Inventory started with “${sel}”.`);
    });
    inv.stop.addEventListener("click", () => act(() => api.rfidStop(), "Inventory stopped."));
    inv.refresh = h("button", { type: "button", class: "btn btn-small" }, "Refresh list");
    inv.refresh.addEventListener("click", () => load(true));
    const impinjUi = `https://${location.hostname}/`;
    fill(invCard, h("h2", {}, "Inventory"),
      h("dl", { class: "kv" },
        h("dt", {}, "State"), inv.state, inv.stepDt, inv.step, h("dt", {}, "Tag stream"), inv.stream,
        h("dt", {}, "Reads / s"), inv.rate, h("dt", {}, "Tags in field"), inv.count),
      inv.error,
      h("div", { class: "field", style: { marginTop: "12px" } }, h("span", { class: "field-label" }, "Preset"),
        h("div", { class: "actions" }, inv.sel, inv.refresh), inv.info,
        h("span", { class: "field-help" }, "Presets are created and tuned (antennas, power, search mode) in the ",
          h("a", { href: impinjUi, target: "_blank", rel: "noopener noreferrer" }, "Impinj web UI ↗"),
          " under Profile Presets. Any reporting interval works: Meridian adapts its arrival and departure timing to each preset.")),
      h("label", { class: "radio", style: { marginTop: "8px" } }, auto, h("span", {}, "Start this preset automatically when the reader starts")),
      h("div", { class: "actions", style: { marginTop: "12px" } }, inv.start, inv.stop));
  }

  // what the selected preset (or each preset of a pattern) does, read from the reader
  const presetCache = {};
  async function showPresetInfo() {
    const f = state.form, sel = f.preset || "";
    const pat = (f.patterns || []).find((p) => PATTERN + p.id === sel);
    const ids = pat ? [...new Set(pat.steps.map((st) => st.preset))] : sel ? [sel] : [];
    if (!ids.length) return fill(inv.info);
    fill(inv.info, h("span", { class: "muted" }, "Reading preset settings…"));
    const infos = await Promise.all(ids.map(async (id) => {
      if (!presetCache[id]) {
        try { presetCache[id] = await api.rfidPreset(id); } catch (err) { presetCache[id] = { id, error: errText(err) }; }
      }
      return presetCache[id];
    }));
    if ((state.form.preset || "") !== sel) return;          // selection changed meanwhile
    fill(inv.info, infos.map((i) => presetLine(i, ids.length > 1)));
  }
  function presetLine(i, named) {
    const label = named ? h("b", {}, i.id, ": ") : null;
    if (i.error || !i.summary) return h("div", { class: "preset-line" }, label, h("span", { class: "muted" }, "Couldn't read this preset's settings", i.error ? ` (${i.error})` : "", "."));
    const sm = i.summary, ri = sm.report_interval_s || 0;
    const ants = sm.antennas.length ? `Antennas ${sm.antennas.join(", ")}` : "No antennas listed";
    const power = Object.keys(sm.power_dbm).length ? ` · ${[...new Set(Object.values(sm.power_dbm))].map((d) => d + " dBm").join(" / ")}` : "";
    const zoned = Object.keys(state.form.zones || {}).filter((p) => (state.form.zones[p] || "").trim());
    const noZone = state.form.ignore_unzoned && sm.antennas.length && !sm.antennas.some((a) => zoned.includes(String(a)));
    return h("div", { class: "preset-line" }, label, ants, power, sm.search_mode ? ` · ${sm.search_mode}` : "", " · ",
      ri > 0 ? h("span", { class: "warn-text" }, `reports each tag once every ${ri} s`) : "reports every read",
      ri > 0 ? h("div", { class: "muted" }, `Meridian adapts while it runs: a tag counts as arrived after 1 report, and gone after ${2 * ri + 2} s without one.`) : null,
      noZone ? h("div", { class: "bad-text" }, "None of these antennas has a zone name, and “Ignore antennas without a zone name” is ticked, so every read is ignored.") : null,
      h("details", {}, h("summary", {}, "Settings as the reader reports them"), h("pre", { class: "mono sample" }, JSON.stringify(i.raw, null, 2))));
  }

  function drawInventory() {
    if (!inv.sel) return;
    const s = state.data.status, f = state.form;
    const pat = s.pattern;
    fill(inv.state, s.reader_error && !s.reader_status ? h("span", { class: "pill bad" }, "Reader API unavailable")
      : pat ? h("span", { class: "pill ok" }, `Running pattern: ${pat.name}`)
      : s.running ? h("span", { class: "pill ok" }, `Running: ${s.active_preset || "?"}`) : h("span", { class: "pill neutral" }, "Stopped"));
    inv.stepDt.hidden = inv.step.hidden = !pat;
    if (pat) {
      fill(inv.step, `${pat.step} of ${pat.steps}: `, h("b", {}, pat.preset), " · ",
        pat.phase === "pause" ? `pausing, ${Math.ceil(pat.remaining_s)} s` : `${Math.ceil(pat.remaining_s)} s left`,
        pat.continuous ? ` · cycle ${pat.cycle}` : "",
        pat.error ? h("div", { class: "bad-text" }, pat.error) : null);
    }
    fill(inv.stream, s.stream_connected ? h("span", { class: "pill ok" }, "Connected")
      : h("span", { class: "pill bad", title: s.stream_error || "" }, "Not connected"));
    inv.rate.textContent = String(s.read_rate);
    inv.count.textContent = String(s.tags_in_field);
    fill(inv.error, s.reader_error ? h("div", { class: "notice warn", style: { marginTop: "10px" } }, s.reader_error,
      /401|403|login/i.test(s.reader_error) ? [" — check the reader login on ", h("a", { href: "#/system/about" }, "About this reader"), "."] : null) : null);
    // rebuild the options only when the list (or the chosen preset) actually changed, and never while it's open/focused
    const presets = state.data.presets, patterns = f.patterns || [];
    const key = JSON.stringify([presets, f.preset, patterns.map((p) => [p.id, p.name])]);
    if (key !== inv.presetsKey && document.activeElement !== inv.sel) {
      inv.presetsKey = key;
      const known = presets.includes(f.preset) || patterns.some((p) => PATTERN + p.id === f.preset);
      fill(inv.sel,
        h("option", { value: "", selected: !f.preset }, presets.length || patterns.length ? "Choose a preset or pattern…" : "No presets found"),
        h("optgroup", { label: "Reader presets" }, presets.map((p) => h("option", { value: p, selected: f.preset === p }, p))),
        patterns.length ? h("optgroup", { label: "Firing patterns" },
          patterns.map((p) => h("option", { value: PATTERN + p.id, selected: f.preset === PATTERN + p.id }, `${p.name} (${p.steps.length} presets)`))) : null,
        f.preset && !known ? h("option", { value: f.preset, selected: true }, `${f.preset.replace(PATTERN, "")} (not found)`) : null);
    }
    const isPattern = (f.preset || "").startsWith(PATTERN);
    const same = isPattern ? (pat && PATTERN + pat.id === f.preset) : (!pat && s.running && s.active_preset === f.preset);
    inv.start.textContent = same ? "Restart" : isPattern ? "Start pattern" : s.running ? "Switch to this preset" : "Start inventory";
    inv.start.disabled = !canEdit || state.busy || !f.preset;
    inv.stop.disabled = !canEdit || state.busy || !s.running;
    inv.refresh.disabled = state.busy;
    const eff = s.effective;
    if (inv.effective && eff) {
      const adj = eff.report_interval_s > 0 || eff.exit_timeout_s !== Number(state.data.settings.exit_timeout_s);
      fill(inv.effective, "In effect now: arrived after ", h("b", {}, String(eff.enter_reads)), eff.enter_reads === 1 ? " read" : " reads",
        ", gone after ", h("b", {}, `${eff.exit_timeout_s} s`), adj ? " (adjusted for the running preset or pattern)." : ".");
    }
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
    editing = null;
    drawPatterns();
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
    inv.effective = h("p", { class: "hint" });
    fill(trackCard, h("h2", {}, "Arrived / left detection"), inv.effective,
      h("div", { class: "form-grid" },
        num("enter_reads", "Reads to confirm arrival", "A tag counts as arrived after this many reads. Filters out stray reads from passing tags."),
        num("exit_timeout_s", "Gone after (seconds)", "A tag not read for this long has left. Raise it if tags at the edge of the field flicker in and out."),
        num("move_quiet_s", "Move delay (seconds)", "A tag read by another zone's antenna only moves there once its old zone hasn't read it for this long."),
        num("rssi_min_dbm", "Minimum signal (dBm)", "Ignore weaker reads, e.g. -65, to keep tags in neighbouring areas out. Empty = no limit.", { placeholder: "no limit" }),
        num("epc_prefix", "Only EPCs starting with", "Hex, e.g. E280 or your company prefix. Empty = all tags.", { class: "mono", placeholder: "all tags" })));
  }
  // ---------- firing patterns
  function drawPatterns() {
    const f = state.form, list = f.patterns || [];
    const summary = (p) => p.steps.map((st, i) => [
      h("span", { class: "step-chip" }, st.preset || "?", h("span", { class: "muted" }, ` ${fmtS(st.seconds)}`)),
      st.pause_s > 0 ? h("span", { class: "muted" }, ` pause ${fmtS(st.pause_s)} `) : " ",
      i < p.steps.length - 1 ? h("span", { class: "muted" }, "→ ") : p.continuous ? h("span", { class: "muted" }, "↻ repeat") : null]).flat();
    fill(patternsCard, h("h2", {}, "Inventory presets: firing patterns"),
      h("p", { class: "hint" }, "A firing pattern runs two or more reader presets one after another, each for a set time, ",
        "with an optional pause after each. Choose it in the Preset list above like any preset."),
      list.length ? h("table", { class: "table simple" },
        h("thead", {}, h("tr", {}, ["Name", "Sequence", "Runs", "One cycle", ""].map((x) => h("th", {}, x)))),
        h("tbody", {}, list.map((p, i) => h("tr", {},
          h("td", {}, h("b", {}, p.name)),
          h("td", {}, summary(p)),
          h("td", {}, p.continuous ? "Continuously" : "Once"),
          h("td", {}, fmtS(p.steps.reduce((a, st) => a + Number(st.seconds || 0) + Number(st.pause_s || 0), 0))),
          h("td", { class: "actions-cell" }, canEdit && !editing ? [
            h("button", { type: "button", class: "btn btn-small", onclick: () => openEditor(i) }, "Edit"),
            h("button", { type: "button", class: "btn btn-small", onclick: () => removePattern(i) }, "Delete")] : null)))))
        : h("p", { class: "muted" }, "No firing patterns yet."),
      editing ? editor() : canEdit ? h("button", { type: "button", class: "btn", style: { marginTop: "12px" }, onclick: () => openEditor(-1) }, "+ Add firing pattern") : null);
  }

  function openEditor(index) {
    const src = index >= 0 ? state.form.patterns[index] : null;
    editing = { index, errors: [], draft: src ? clone(src) : { id: "", name: "", continuous: false,
      steps: [{ preset: "", seconds: 10, pause_s: 0 }, { preset: "", seconds: 10, pause_s: 0 }] } };
    drawPatterns();
  }

  function editor() {
    const d = editing.draft, presets = state.data.presets;
    const name = h("input", { type: "text", value: d.name, maxlength: 40, placeholder: "e.g. Portal then drawers", "aria-label": "Pattern name" });
    name.addEventListener("input", () => { d.name = name.value; });
    const cycle = h("span", { class: "muted" });
    const updateCycle = () => { cycle.textContent = `One cycle: ${fmtS(d.steps.reduce((a, st) => a + (Number(st.seconds) || 0) + (Number(st.pause_s) || 0), 0))}`; };
    const rows = d.steps.map((st, i) => {
      const sel = h("select", { "aria-label": `Preset for step ${i + 1}` },
        h("option", { value: "", selected: !st.preset }, "Choose a preset…"),
        presets.map((p) => h("option", { value: p, selected: st.preset === p }, p)),
        st.preset && !presets.includes(st.preset) ? h("option", { value: st.preset, selected: true }, `${st.preset} (not on the reader)`) : null);
      sel.addEventListener("change", () => { st.preset = sel.value; });
      const secs = h("input", { type: "text", inputmode: "decimal", value: st.seconds, class: "num-input", "aria-label": `Seconds for step ${i + 1}` });
      secs.addEventListener("input", () => { st.seconds = secs.value; updateCycle(); });
      const pause = h("input", { type: "text", inputmode: "decimal", value: st.pause_s, class: "num-input", "aria-label": `Pause after step ${i + 1}` });
      pause.addEventListener("input", () => { st.pause_s = pause.value; updateCycle(); });
      const rm = h("button", { type: "button", class: "btn btn-small", disabled: d.steps.length <= 2, title: "A pattern needs at least two presets",
        onclick: () => { d.steps.splice(i, 1); drawPatterns(); } }, "Remove");
      return h("tr", {}, h("td", {}, String(i + 1)), h("td", {}, sel), h("td", {}, secs), h("td", {}, pause), h("td", {}, rm));
    });
    const cont = h("input", { type: "checkbox", checked: d.continuous });
    cont.addEventListener("change", () => { d.continuous = cont.checked; });
    updateCycle();
    return h("div", { class: "pattern-editor" },
      h("h3", {}, editing.index >= 0 ? `Edit “${state.form.patterns[editing.index].name}”` : "New firing pattern"),
      h("label", { class: "field", style: { maxWidth: "360px" } }, h("span", { class: "field-label" }, "Name"), name),
      h("table", { class: "table simple", style: { marginTop: "8px" } },
        h("thead", {}, h("tr", {}, ["Step", "Preset", "Scan for (s)", "Pause after (s)", ""].map((x) => h("th", {}, x)))),
        h("tbody", {}, rows)),
      h("div", { class: "actions", style: { marginTop: "8px" } },
        h("button", { type: "button", class: "btn btn-small", disabled: d.steps.length >= 20,
          onclick: () => { d.steps.push({ preset: "", seconds: 10, pause_s: 0 }); drawPatterns(); } }, "+ Add another preset"),
        cycle),
      h("label", { class: "radio", style: { marginTop: "10px" } }, cont, h("span", {}, "Run continuously (repeat until stopped)")),
      editing.errors.length ? h("div", { class: "notice bad", style: { marginTop: "10px" } }, h("ul", {}, editing.errors.map((e) => h("li", {}, e)))) : null,
      h("div", { class: "actions", style: { marginTop: "12px" } },
        h("button", { type: "button", class: "btn btn-primary", onclick: applyEditor }, "Done"),
        h("button", { type: "button", class: "btn", onclick: () => { editing = null; drawPatterns(); } }, "Cancel")),
      h("p", { class: "hint" }, "Done adds it to the page; click Save at the bottom to keep it."));
  }

  function applyEditor() {
    const d = editing.draft, list = state.form.patterns || (state.form.patterns = []);
    const errs = [];
    d.name = (d.name || "").trim();
    if (!d.name) errs.push("Give the pattern a name.");
    if (list.some((p, i) => i !== editing.index && p.name.toLowerCase() === d.name.toLowerCase())) errs.push(`There's already a pattern called “${d.name}”.`);
    if (d.steps.length < 2) errs.push("Add at least two presets.");
    d.steps.forEach((st, i) => {
      if (!st.preset) errs.push(`Step ${i + 1}: choose a preset.`);
      const sec = Number(st.seconds), pause = Number(st.pause_s || 0);
      if (!(sec >= 1 && sec <= 3600)) errs.push(`Step ${i + 1}: scan time must be 1–3600 seconds.`);
      if (!(pause >= 0 && pause <= 600)) errs.push(`Step ${i + 1}: pause must be 0–600 seconds.`);
    });
    if (errs.length) { editing.errors = errs; return drawPatterns(); }
    d.steps = d.steps.map((st) => ({ preset: st.preset, seconds: Number(st.seconds), pause_s: Number(st.pause_s || 0) }));
    if (!d.id) {
      const base = d.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "pattern";
      let id = base, n = 2;
      while (list.some((p) => p.id === id)) id = `${base}-${n++}`;
      d.id = id;
    }
    if (editing.index >= 0) list[editing.index] = d; else list.push(d);
    editing = null;
    drawPatterns(); changed();
    msg.show("Firing pattern added to the page. Click Save to keep it.");
  }

  function removePattern(i) {
    const p = state.form.patterns[i];
    if (!confirm(`Delete the firing pattern “${p.name}”?`)) return;
    state.form.patterns.splice(i, 1);
    if (state.form.preset === PATTERN + p.id) state.form.preset = "";
    drawPatterns(); changed();
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
function fmtS(n) { n = Number(n) || 0; return n >= 60 ? `${Math.floor(n / 60)} min${n % 60 ? ` ${Math.round(n % 60)} s` : ""}` : `${+n.toFixed(1)} s`; }
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
