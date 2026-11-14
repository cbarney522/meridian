// Hardware › USB › Badge readers
// Badge readers look like keyboards to the reader, so which port is "Badge in" is decided on the
// Devices page (or here, with the swipe-to-identify helper). This page shows each reader's health,
// a live scan test, and how raw scans are turned into badge IDs.
import { api, h, fill } from "../api.js";
import { toast, saveBar, errText, visible } from "../ui.js";

const MAX_SCANS = 20;
const ROLE_LABEL = { badge_in: "Badge in", badge_out: "Badge out" };

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("hardware.edit");
  const dev = ctx.session.dev_mode;
  const state = { data: null, form: null, saving: false, scans: [] };

  const msg = toast();
  const cards = h("div", { class: "grid grid-2" });
  const scanList = h("ul", { class: "scans" });
  const formBox = h("div");
  const devBox = h("div", { class: "dev-box", hidden: true });
  const bar = saveBar({ onSave: save, onDiscard: () => { state.form = clone(state.data.settings); drawForm(); } });

  root.append(
    h("h1", {}, "Badge readers"),
    h("p", { class: "subtitle" }, "Badge readers appear to the reader as keyboards. Assign their ports on ",
      h("a", { href: "#/hardware/usb" }, "Devices"), "; test them and set up how scans become badge IDs here."),
    msg.el,
    cards,
    h("section", { class: "card", style: { marginTop: "16px" } },
      h("h2", {}, "Live scan test"),
      h("p", { class: "hint" }, "Swipe a badge on any reader. Each scan shows the port it came from, exactly what the reader typed, and the badge ID it becomes.",
        canEdit ? " Swiping on an unassigned keyboard-type device lets you make it the in or out reader." : ""),
      scanList),
    h("section", { class: "card", style: { marginTop: "16px" } },
      h("h2", {}, "Scan handling"),
      formBox),
    devBox,
    bar.el,
  );

  async function load(keepForm = true) {
    try {
      const d = await api.badges();
      const prev = state.data ? JSON.stringify(state.data.settings) : null;
      const dirty = state.form && state.data && JSON.stringify(state.form) !== prev;
      state.data = d;
      // Only rebuild the form when it must: first load, after save/conflict, or when the saved
      // settings changed and there are no local edits. Rebuilding would steal focus mid-typing.
      if (!keepForm || prev === null || (!dirty && prev !== JSON.stringify(d.settings))) {
        state.form = clone(d.settings);
        drawForm();
      }
      drawCards();
      drawScans();
      drawDev();
    } catch (err) {
      fill(cards, h("div", { class: "notice bad" }, "Can't load badge readers: ", errText(err)));
    }
  }

  // ---------- reader cards
  function drawCards() {
    fill(cards, state.data.listener === "disabled"
      ? h("div", { class: "notice warn" }, "Badge reading is turned off on this reader (server started with --no-badges).") : null,
      state.data.readers.map((r) => {
      const statusPill = {
        ok: ["ok", "Connected"], missing: ["bad", "Missing"], unassigned: ["neutral", "Not assigned"],
        mismatch: ["bad", "Wrong device"], changed: ["warn", "Different device"],
      }[r.status] || ["neutral", r.status];
      return h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, ROLE_LABEL[r.role]), h("span", { class: "pill " + statusPill[0] }, statusPill[1])),
        r.assigned
          ? h("dl", { class: "kv" },
              h("dt", {}, "Port"), h("dd", { class: "mono" }, r.port),
              h("dt", {}, "Device"), h("dd", {}, r.product || "—"),
              h("dt", {}, "Input"), h("dd", { class: "mono" }, r.nodes.find((n) => n.includes("event")) || "—"),
              h("dt", {}, "Last scan"), h("dd", {}, r.last_scan
                ? [h("span", { class: "mono" }, visible(r.last_scan.raw)), " → ", h("strong", {}, r.last_scan.badge_id || "—"),
                   " ", h("span", { class: "pill " + (r.last_scan.ok ? "ok" : "bad") }, r.last_scan.ok ? "valid" : "invalid"),
                   h("div", { class: "sub" }, r.last_scan.ts.replace("T", " "))]
                : h("span", { class: "muted" }, "No scans yet")),
              h("dt", {}, "Today"), h("dd", {}, `${r.scans_today} scans`,
                r.errors_today ? h("span", { class: "pill bad", style: { marginLeft: "8px" } }, `${r.errors_today} invalid`) : null),
              r.listener_error ? [h("dt", {}, "Problem"), h("dd", { class: "bad-text" }, r.listener_error)] : null)
          : h("p", { class: "muted" }, "No port is assigned as ", ROLE_LABEL[r.role].toLowerCase(), ". Assign one on ",
              h("a", { href: "#/hardware/usb" }, "Devices"), canEdit ? ", or swipe a badge on the reader below to identify it." : "."));
    }));
  }

  // ---------- live scans
  function drawScans() {
    if (!state.scans.length) {
      fill(scanList, h("li", { class: "empty" }, "Waiting for a swipe…"));
      return;
    }
    fill(scanList, state.scans.map((s) => {
      const isReader = s.role === "badge_in" || s.role === "badge_out";
      const r = s.result;
      return h("li", { class: "scan" },
        h("time", {}, s.ts.slice(11)),
        h("div", {},
          h("div", {}, h("span", { class: "mono" }, s.port), " ",
            isReader ? h("strong", {}, ROLE_LABEL[s.role]) : h("span", { class: "muted" }, s.role ? `${s.role} (not a badge reader)` : "unassigned keyboard")),
          h("div", { class: "sub" }, s.product || "")),
        h("div", {}, h("div", { class: "mono raw" }, visible(s.raw)),
          h("div", { class: "sub" }, r.steps.length ? r.steps.join(" → ") : "no changes")),
        h("div", { class: "scan-id" }, "→ ", h("strong", {}, r.badge_id || "—"), " ",
          h("span", { class: "pill " + (r.ok ? "ok" : "bad"), title: r.error || "" }, r.ok ? "valid" : "invalid"),
          r.error ? h("div", { class: "sub bad-text" }, r.error) : null),
        h("div", { class: "scan-actions" }, canEdit && !isReader
          ? ["badge_in", "badge_out"].map((role) =>
              h("button", { type: "button", class: "btn btn-small", onclick: () => assign(s.port, role) }, `Use as ${ROLE_LABEL[role]}`))
          : null));
    }));
  }

  async function assign(port, role) {
    try {
      const usb = await api.usbDevices();
      const map = {};
      for (const r of usb.devices) if (r.role && r.type !== "hub") map[r.port] = r.role;
      for (const p of Object.keys(map)) if (map[p] === role) delete map[p];     // role moves to the new port
      map[port] = role;
      await api.saveUsbAssignments(usb.revision, map);
      msg.show(`Port ${port} is now the ${ROLE_LABEL[role].toLowerCase()} reader.`);
      state.scans.forEach((s) => { if (s.port === port) s.role = role; else if (s.role === role) s.role = null; });
      load();
    } catch (err) {
      msg.show("Couldn't assign: " + errText(err), true);
    }
  }

  // ---------- settings form
  const preview = { raw: "I105:0010442", out: h("div", { class: "preview-out" }), timer: 0 };

  function drawForm() {
    const f = state.form;
    const dis = !canEdit;
    const field = (label, control, help) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), control,
      help ? h("span", { class: "field-help" }, help) : null);
    const text = (key, opts = {}) => {
      const val = Array.isArray(f[key]) ? f[key].join(", ") : f[key];
      const el = h("input", { type: "text", value: val, disabled: dis, class: opts.mono ? "mono" : null, spellcheck: "false", ...opts.attrs });
      el.addEventListener("input", () => {
        f[key] = Array.isArray(state.data.settings[key]) ? el.value.split(",").map((x) => x.trim()).filter(Boolean) : el.value;
        changed();
      });
      return el;
    };
    const select = (key, options) => {
      const el = h("select", { disabled: dis }, options.map(([v, l]) => h("option", { value: v, selected: f[key] === v }, l)));
      el.addEventListener("change", () => { f[key] = el.value; changed(); drawForm(); });
      return el;
    };
    const zeros = h("input", { type: "checkbox", checked: f.strip_leading_zeros, disabled: dis });
    zeros.addEventListener("change", () => { f.strip_leading_zeros = zeros.checked; changed(); });
    const timeout = h("input", { type: "number", min: "20", max: "2000", step: "10", value: f.timeout_ms, disabled: dis });
    timeout.addEventListener("input", () => { f.timeout_ms = parseInt(timeout.value, 10) || 0; changed(); });
    const sample = h("input", { type: "text", value: preview.raw, class: "mono", spellcheck: "false", "aria-label": "Sample scan" });
    sample.addEventListener("input", () => { preview.raw = sample.value; runPreview(); });

    fill(formBox,
      h("div", { class: "form-grid" },
        field("Remove prefixes", text("strip_prefixes"), "Comma-separated, e.g. I, O if the readers are programmed to tag in/out scans."),
        field("Remove suffixes", text("strip_suffixes"), "Comma-separated. Leave empty if none."),
        field("Facility / card separator", text("separator", { attrs: { maxlength: "3" } }), "Character between facility code and card number in the raw scan, e.g. ':'"),
        field("Badge ID is", select("output", [["card", "Card number only"], ["facility_card", "Facility + card"], ["raw", "Raw scan, unchanged"]]),
          "Must match how badges are stored in the master user list."),
        h("label", { class: "field field-check" }, zeros, h("span", {}, "Remove leading zeros")),
        field("Validation pattern", text("pattern", { mono: true }), "Regular expression the final ID must match. Invalid scans are rejected."),
        field("End of scan", select("end_of_scan", [["enter", "Reader sends Enter"], ["timeout", "Pause in typing"]]), null),
        f.end_of_scan === "timeout" ? field("Pause length (ms)", timeout, "20–2000. Gap that ends a scan when the reader sends no Enter.") : null),
      h("div", { class: "preview" },
        h("div", { class: "field-label" }, "Try it"),
        h("div", { class: "preview-row" }, sample, preview.out)));
    runPreview(true);
  }

  function runPreview(now = false) {
    clearTimeout(preview.timer);
    preview.timer = setTimeout(async () => {
      try {
        const r = await api.previewBadge(preview.raw, state.form);
        fill(preview.out, "→ ", h("strong", {}, r.badge_id || "—"), " ",
          h("span", { class: "pill " + (r.ok ? "ok" : "bad") }, r.ok ? "valid" : "invalid"),
          h("div", { class: "sub" }, r.error || (r.steps.length ? r.steps.join(" → ") : "no changes")));
      } catch (err) {
        fill(preview.out, h("span", { class: "bad-text" }, errText(err)));
      }
    }, now ? 0 : 250);
  }

  function changed() {
    const dirty = JSON.stringify(state.form) !== JSON.stringify(state.data.settings);
    bar.update({ dirty, saving: state.saving, label: "Scan handling has unsaved changes" });
    runPreview();
  }

  async function save() {
    state.saving = true; changed();
    try {
      await api.saveBadgeSettings(state.data.revision, state.form);
      msg.show("Scan handling saved. New scans use these settings.");
      await load(false);
    } catch (err) {
      msg.show((err.status === 409 ? "Someone else saved first; their settings are loaded. " : "Not saved: ") + errText(err), true);
      if (err.status === 409) await load(false);
    } finally {
      state.saving = false; changed();
    }
  }

  // ---------- mock-only swipe buttons
  function drawDev() {
    if (!dev) return;
    const ports = [
      ...state.data.readers.filter((r) => r.status === "ok").map((r) => ({ port: r.port, label: ROLE_LABEL[r.role] })),
      ...state.data.other_keyboards.map((k) => ({ port: k.port, label: k.product })),
    ];
    devBox.hidden = !ports.length;
    fill(devBox, h("strong", {}, "Mock server — simulate a swipe: "),
      ports.map((p) => h("button", { type: "button", class: "btn btn-small", onclick: () => api.devSwipe(p.port) }, `${p.port} (${p.label})`)),
      ports[0] ? h("button", { type: "button", class: "btn btn-small", onclick: () => api.devSwipe(ports[0].port, true) }, "Misread on " + ports[0].port) : null);
  }

  const unsub = ctx.events.subscribe((ev) => {
    if (ev.type === "badge_raw") {
      state.scans.unshift(ev);
      state.scans.length = Math.min(state.scans.length, MAX_SCANS);
      drawScans();
      load();
    } else if (ev.type === "usb_attached" || ev.type === "usb_detached") {
      load();
    }
  });

  load(false);
  return () => { unsub(); msg.dispose(); clearTimeout(preview.timer); };
}

function clone(o) { return JSON.parse(JSON.stringify(o)); }
