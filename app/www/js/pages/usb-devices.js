// Hardware › USB › Devices
// Every USB device on the reader, shown as the physical tree (reader port → hub → device),
// with a role assigned per port. Roles are what rules and drivers refer to ("badge_in",
// "io_board"…), so swapping a failed badge reader for an identical one needs no reconfiguration.
import { api, h, fill } from "../api.js";

const STATUS = {
  ok:         ["ok",   "OK"],
  mismatch:   ["bad",  "Wrong device"],
  changed:    ["warn", "Different device"],
  missing:    ["bad",  "Missing"],
  unassigned: ["neutral", "Unassigned"],
  ignored:    ["neutral", "Ignored"],
  hub:        ["",     ""],
};

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("hardware.edit");
  const dev = ctx.session.dev_mode;
  const state = { data: null, pending: new Map(), saving: false };

  const summary = h("div", { class: "chips" });
  const refreshBtn = h("button", { type: "button", class: "btn" }, "Rescan");
  const tbody = h("tbody");
  const toast = h("div", { class: "toast", role: "status", "aria-live": "polite", hidden: true });
  const devBox = h("div", { class: "dev-box", hidden: true });
  const saveBar = h("div", { class: "savebar", hidden: true });

  root.append(
    h("h1", {}, "USB devices"),
    h("p", { class: "subtitle" },
      "Devices are identified by the physical port they're plugged into. ",
      canEdit ? "Give each port a role; if a different kind of device turns up there, it's flagged."
              : "You can view assignments; changing them needs the hardware.edit permission."),
    h("div", { class: "toolbar" }, summary, refreshBtn),
    toast,
    h("div", { class: "card table-card" },
      h("table", { class: "table usb-table" },
        h("thead", {}, h("tr", {},
          ["Port", "Device", "Type", "VID:PID", "Linux device", "Speed / power", "Role", "Status"].map((t) => h("th", {}, t)),
          dev ? h("th", {}, h("span", { class: "visually-hidden" }, "Simulate")) : null)),
        tbody)),
    devBox,
    saveBar,
  );

  refreshBtn.addEventListener("click", () => load());

  async function load() {
    try {
      state.data = await api.usbDevices();
      draw();
    } catch (err) {
      tbody.replaceChildren(h("tr", {}, h("td", { colspan: 9, class: "empty" }, "Can't read USB devices: ", String(err.message))));
    }
  }

  function roleOf(row) { return state.pending.has(row.port) ? state.pending.get(row.port) : row.role; }

  function draw() {
    const d = state.data;
    const s = d.summary;
    fill(summary, 
      chip("Devices", s.devices), chip("Hubs", s.hubs), chip("Assigned", s.assigned),
      chip("Problems", s.problems, s.problems ? "bad" : ""), chip("Unassigned", s.unassigned),
      chip("Requested power", `${s.power_ma} mA`));
    const roles = d.roles;
    tbody.replaceChildren(...d.devices.map((r) => row(r, roles)));
    drawSaveBar();
    drawDevBox();
  }

  function row(r, roles) {
    const [cls, label] = STATUS[r.status] || ["", r.status];
    const changedHere = state.pending.has(r.port);
    return h("tr", { class: (r.status === "missing" ? "row-off " : "") + (changedHere ? "row-dirty" : "") },
      h("td", { class: "port-cell" },
        h("div", { style: { paddingLeft: `${r.depth * 18}px` } },
          h("span", { class: "port" }, r.depth ? h("span", { class: "tree", "aria-hidden": "true" }, "└ ") : null, r.port),
          r.location && !r.location.startsWith("via ") ? h("div", { class: "sub port-label" }, r.location) : null)),
      h("td", {},
        h("div", { class: "dev-name" }, r.product || (r.status === "missing" ? "(not detected)" : "Unknown device")),
        h("div", { class: "sub" }, [r.manufacturer, r.serial ? `S/N ${r.serial}` : null].filter(Boolean).join(" · "))),
      h("td", {}, r.type_label ? h("span", { class: "type type-" + r.type }, r.type_label) : "—"),
      h("td", { class: "mono" }, r.vid ? `${r.vid}:${r.pid}` : "—"),
      h("td", {},
        r.nodes.length ? r.nodes.map((n) => h("div", { class: "mono" }, n)) : h("span", { class: "muted" }, "—"),
        r.drivers.length ? h("div", { class: "sub" }, "driver: " + r.drivers.join(", ")) : null),
      h("td", { class: "nowrap" }, r.speed_mbps ? `${r.speed_mbps} Mb/s` : "—",
        r.speed_mbps ? h("div", { class: "sub" }, [speedName(r.speed_mbps), r.max_power_ma ? `${r.max_power_ma} mA` : null].filter(Boolean).join(" · ")) : null),
      h("td", {}, r.type === "hub" ? h("span", { class: "muted" }, "—") : roleControl(r, roles)),
      h("td", {}, label ? h("span", { class: "pill " + cls, title: r.status_detail || "" }, label) : null,
        r.status_detail ? h("div", { class: "sub" }, r.status_detail) : null),
      dev ? h("td", {}, r.status === "missing" ? null :
        h("button", { type: "button", class: "btn btn-small", title: "Mock server only",
                      onclick: () => toggle(r.port) }, "Unplug")) : null,
    );
  }

  function roleControl(r, roles) {
    const current = roleOf(r);
    if (!canEdit) {
      const role = roles.find((x) => x.id === current);
      return role ? role.label : h("span", { class: "muted" }, "—");
    }
    const fits = roles.filter((x) => !r.type || x.id === "ignore" || x.expects.includes(r.type));
    const others = roles.filter((x) => !fits.includes(x));
    const opt = (x) => h("option", { value: x.id, selected: x.id === current }, x.label);
    const sel = h("select", { class: "role-select", "aria-label": `Role for port ${r.port}` },
      h("option", { value: "", selected: !current }, "— Unassigned —"),
      fits.length ? h("optgroup", { label: r.type ? "Suits this device" : "Roles" }, fits.map(opt)) : null,
      others.length ? h("optgroup", { label: "Other roles (wrong device type)" }, others.map(opt)) : null);
    sel.addEventListener("change", () => {
      const v = sel.value || null;
      if (v === (r.role || null)) state.pending.delete(r.port);
      else state.pending.set(r.port, v);
      draw();
    });
    return sel;
  }

  function wanted() {
    const out = {};
    for (const r of state.data.devices) { const role = roleOf(r); if (role && r.type !== "hub") out[r.port] = role; }
    return out;
  }

  function localErrors() {
    const errs = [], seen = {};
    for (const [port, role] of Object.entries(wanted())) {
      const def = state.data.roles.find((x) => x.id === role);
      if (def && def.single) {
        if (seen[role]) errs.push(`"${def.label}" is assigned to both ${seen[role]} and ${port}.`);
        seen[role] = port;
      }
    }
    return errs;
  }

  function drawSaveBar() {
    const n = state.pending.size;
    saveBar.hidden = !n;
    if (!n) return;
    const errs = localErrors();
    fill(saveBar, 
      h("span", {}, `${n} unsaved change${n > 1 ? "s" : ""}`),
      errs.length ? h("span", { class: "savebar-err" }, errs.join(" ")) : null,
      h("span", { class: "spacer" }),
      h("button", { type: "button", class: "btn", onclick: () => { state.pending.clear(); draw(); } }, "Discard"),
      h("button", { type: "button", class: "btn btn-primary", disabled: state.saving || errs.length > 0, onclick: save },
        state.saving ? "Saving…" : "Save"));
  }

  async function save() {
    state.saving = true; drawSaveBar();
    try {
      await api.saveUsbAssignments(state.data.revision, wanted());
      state.pending.clear();
      notify("Assignments saved.");
      await load();
    } catch (err) {
      if (err.status === 409) notify("Someone else changed the assignments. Reloaded their version; re-apply your changes.", true);
      else notify("Not saved: " + ((err.body && err.body.details) ? err.body.details.join(" ") : err.message), true);
      if (err.status === 409) { state.pending.clear(); await load(); }
    } finally {
      state.saving = false; drawSaveBar();
    }
  }

  function drawDevBox() {
    const list = (state.data.dev_unplugged || []);
    devBox.hidden = !dev || !list.length;
    if (devBox.hidden) return;
    fill(devBox, h("strong", {}, "Mock server — simulated unplugged: "),
      list.map((u) => h("button", { type: "button", class: "btn btn-small", onclick: () => toggle(u.port) },
        `Plug in ${u.port}${u.product ? " (" + u.product + ")" : ""}`)));
  }

  async function toggle(port) {
    try { await api.devUsbToggle(port); } catch (err) { notify(String(err.message), true); }
  }

  let toastTimer = 0;
  function notify(text, bad = false) {
    toast.textContent = text;
    toast.className = "toast" + (bad ? " bad" : "");
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 6000);
  }

  // Hot-plug: the reader pushes usb_attached / usb_detached; unsaved edits are kept.
  const unsub = ctx.events.subscribe((ev) => {
    if (ev.replay) return;
    if (ev.type !== "usb_attached" && ev.type !== "usb_detached") return;
    notify(`${ev.type === "usb_attached" ? "Connected" : "Disconnected"}: ${ev.product || "device"} on port ${ev.port}`,
           ev.type === "usb_detached");
    load();
  });

  load();
  return () => { unsub(); clearTimeout(toastTimer); };
}

function chip(label, value, cls = "") {
  return h("span", { class: "chip static " + cls }, label, h("span", { class: "chip-n" }, value));
}

function speedName(mbps) {
  const n = Number(mbps);
  return n >= 5000 ? "SuperSpeed" : n >= 480 ? "High speed" : n >= 12 ? "Full speed" : "Low speed";
}
