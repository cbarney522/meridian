// Hardware › USB › Storage
// The USB data drive: is it there and mounted, how full is it, what's using the space, how long
// data is kept. If it's missing, the reader buffers events in its small internal storage.
import { api, h, fill } from "../api.js";
import { toast, saveBar, fmtBytes, errText } from "../ui.js";

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("hardware.edit");
  const dev = ctx.session.dev_mode;
  const state = { data: null, form: null, saving: false };

  const msg = toast();
  const body = h("div");
  const settingsBox = h("div");
  const devBox = h("div", { class: "dev-box", hidden: !dev });
  const bar = saveBar({ onSave: save, onDiscard: () => { state.form = { ...state.data.settings }; drawSettings(); changed(); } });
  const dialog = formatDialog();

  root.append(
    h("h1", {}, "Data drive"),
    h("p", { class: "subtitle" }, "The USB drive that holds the event outbox, event history and logs."),
    msg.el, body,
    h("section", { class: "card", style: { marginTop: "16px" } }, h("h2", {}, "Retention & alerts"), settingsBox),
    devBox, bar.el, dialog.el,
  );

  async function load(resetForm = false) {
    try {
      const d = await api.storage();
      const first = !state.data;
      state.data = d;
      if (first || resetForm) { state.form = { ...d.settings }; drawSettings(); }
      draw();
    } catch (err) {
      fill(body, h("div", { class: "notice bad" }, "Can't read storage status: ", errText(err)));
    }
  }

  function draw() {
    const d = state.data, drv = d.drive, cap = drv.capacity;
    const notices = [];
    if (!d.assigned_port) {
      notices.push(h("div", { class: "notice bad" }, h("strong", {}, "No data drive configured. "),
        "No USB port has the Data drive role. Plug in a drive and assign it on ", h("a", { href: "#/hardware/usb" }, "Devices"), "."));
    } else if (!drv.present) {
      notices.push(h("div", { class: "notice bad" }, h("strong", {}, "Data drive missing. "),
        `Nothing detected on port ${drv.port}. Events are being buffered in internal storage (${fmtBytes(d.internal.free)} free) until a drive is back.`));
    } else if (!drv.mounted) {
      notices.push(h("div", { class: "notice warn" }, h("strong", {}, "Drive ejected. "),
        "It's safe to unplug. Events are being buffered in internal storage until it's mounted again."));
    }
    d.warnings.forEach((w) => notices.push(h("div", { class: "notice warn" }, w)));

    const pctUsed = cap ? (cap.used / cap.size) * 100 : 0;
    const alertAt = 100 - d.settings.alert_free_pct;
    const maxItem = Math.max(1, ...d.usage.map((u) => u.bytes));

    fill(body,
      notices.length ? h("div", { class: "notices" }, notices) : null,
      h("div", { class: "grid" },
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Drive"),
            drv.present ? h("span", { class: "pill " + (drv.mounted ? "ok" : "warn") }, drv.mounted ? "Mounted" : "Ejected")
                        : h("span", { class: "pill bad" }, "Not present")),
          drv.present ? h("dl", { class: "kv" },
            h("dt", {}, "Device"), h("dd", {}, [drv.manufacturer, drv.product].filter(Boolean).join(" ")),
            h("dt", {}, "Serial"), h("dd", { class: "mono" }, drv.serial || "—"),
            h("dt", {}, "Port"), h("dd", { class: "mono" }, drv.port),
            h("dt", {}, "Linux device"), h("dd", { class: "mono" }, drv.mount ? drv.mount.device : drv.node || "—"),
            h("dt", {}, "Mounted at"), h("dd", { class: "mono" }, drv.mount ? drv.mount.mount_point : "—"),
            h("dt", {}, "Filesystem"), h("dd", {}, drv.mount
              ? h("span", { class: "pill neutral" }, drv.mount.fs_type === "vfat" ? "FAT32 (vfat)" : drv.mount.fs_type) : "—"),
            h("dt", {}, "Write test"), h("dd", {}, drv.writable === undefined || drv.writable === null ? "—"
              : h("span", { class: "pill " + (drv.writable ? "ok" : "bad"), title: drv.write_error || "" },
                  drv.writable ? "Meridian can write" : "Not writable")))
            : h("p", { class: "muted" }, d.assigned_port ? `Expected on port ${d.assigned_port}.` : "No drive assigned.")),
        h("section", { class: "card" }, h("h2", {}, "Capacity"),
          cap ? [
            h("div", { class: "big" }, fmtBytes(cap.free), h("small", {}, `free of ${fmtBytes(cap.size)}`)),
            h("div", { class: "cap-track", title: `${pctUsed.toFixed(1)}% used` },
              h("div", { class: "cap-fill" + (pctUsed > alertAt ? " over" : ""), style: { width: `${Math.max(pctUsed, 0.5)}%` } }),
              h("div", { class: "cap-mark", style: { left: `${alertAt}%` }, title: `Alert below ${d.settings.alert_free_pct}% free` })),
            h("div", { class: "sub" }, `${pctUsed.toFixed(1)}% used · alert below ${d.settings.alert_free_pct}% free`)]
            : h("p", { class: "muted" }, "Not available while the drive isn't mounted.")),
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Internal storage"),
            d.internal.buffering ? h("span", { class: "pill warn" }, "Buffering events") : h("span", { class: "pill neutral" }, "Standby")),
          h("dl", { class: "kv" },
            h("dt", {}, "Location"), h("dd", { class: "mono" }, d.internal.path),
            h("dt", {}, "Free"), h("dd", {}, `${fmtBytes(d.internal.free)} of ${fmtBytes(d.internal.size)}`)),
          h("p", { class: "hint" }, "Small emergency buffer used only when the data drive is missing or ejected.")),
        cap ? h("section", { class: "card wide" }, h("h2", {}, "What's using the space"),
          h("div", { class: "bars usage-bars" }, d.usage.map((u) =>
            h("div", { class: "bar-row" },
              h("span", {}, u.label, h("div", { class: "sub mono" }, u.path)),
              h("div", { class: "bar-track" }, h("div", { class: "bar-fill", style: { width: `${(u.bytes / maxItem) * 100}%` } })),
              h("span", { class: "n" }, fmtBytes(u.bytes)))))) : null,
        drv.present ? h("section", { class: "card" }, h("h2", {}, "Removing the drive"),
          h("p", {}, drv.mount && drv.mount.sync
            ? "The reader mounts drives with 'sync' (every write goes straight to the drive), so unplugging is fairly safe. Best moment: when no badges are being scanned and no events are being written."
            : "Unplug only when the reader is idle."),
          (d.can_eject || d.can_format) && canEdit ? h("div", { class: "actions" },
            d.can_eject ? h("button", { type: "button", class: "btn", onclick: () => act(drv.mounted ? "eject" : "mount") }, drv.mounted ? "Eject safely" : "Mount") : null,
            d.can_format ? h("button", { type: "button", class: "btn btn-danger", onclick: () => dialog.open() }, "Format as ext4…") : null) : null) : null,
      ),
    );
    if (dev) fill(devBox, h("strong", {}, "Mock server: "),
      h("button", { type: "button", class: "btn btn-small", onclick: async () => { await api.devStorageFs(); load(); } },
        `Pretend the drive is ${d.dev_fs_type === "ext4" ? "FAT32 (vfat)" : "ext4"}`),
      h("span", {}, " · unplug it on ", h("a", { href: "#/hardware/usb" }, "Devices"), " to see the missing state"));
  }

  async function act(what, confirm) {
    try {
      await api.storageAction(what, confirm);
      msg.show({ eject: "Drive ejected. It's safe to unplug.", mount: "Drive mounted.", format: "Drive formatted as ext4 and mounted." }[what]);
      await load();
      return true;
    } catch (err) {
      msg.show("Failed: " + errText(err), true);
      return false;
    }
  }

  // ---------- settings
  function drawSettings() {
    const f = state.form, dis = !canEdit;
    const num = (key, label, min, max, unit, help) => {
      const el = h("input", { type: "number", min: String(min), max: String(max), value: f[key], disabled: dis });
      el.addEventListener("input", () => { f[key] = parseInt(el.value, 10); changed(); });
      return h("label", { class: "field" }, h("span", { class: "field-label" }, label),
        h("span", { class: "with-unit" }, el, h("span", {}, unit)), h("span", { class: "field-help" }, help));
    };
    fill(settingsBox, h("div", { class: "form-grid" },
      num("retention_days_events", "Keep event history", 1, 3650, "days", "Older events are deleted from the history database. Unsent events in the outbox are never deleted by this."),
      num("retention_days_logs", "Keep log files", 1, 3650, "days", "Older log files are deleted."),
      num("alert_free_pct", "Alert when free space is below", 1, 50, "%", "Raises a warning here, on the dashboard and in the site list.")));
  }

  function localErrors() {
    const f = state.form, e = [];
    const chk = (k, lo, hi, name) => { if (!Number.isInteger(f[k]) || f[k] < lo || f[k] > hi) e.push(`${name}: ${lo}–${hi}.`); };
    chk("retention_days_events", 1, 3650, "Event history");
    chk("retention_days_logs", 1, 3650, "Log files");
    chk("alert_free_pct", 1, 50, "Alert threshold");
    return e;
  }

  function changed() {
    const dirty = JSON.stringify(state.form) !== JSON.stringify(state.data.settings);
    bar.update({ dirty, errors: localErrors(), saving: state.saving, label: "Retention settings have unsaved changes" });
  }

  async function save() {
    state.saving = true; changed();
    try {
      await api.saveStorageSettings(state.data.revision, state.form);
      msg.show("Settings saved.");
      await load(true);
    } catch (err) {
      msg.show((err.status === 409 ? "Someone else saved first; their settings are loaded. " : "Not saved: ") + errText(err), true);
      if (err.status === 409) await load(true);
    } finally {
      state.saving = false; changed();
    }
  }

  // ---------- format confirmation dialog
  function formatDialog() {
    const input = h("input", { type: "text", class: "mono", autocomplete: "off", spellcheck: "false", "aria-label": "Type FORMAT to confirm" });
    const go = h("button", { type: "button", class: "btn btn-danger", disabled: true }, "Format drive");
    const el = h("dialog", { class: "dialog" },
      h("h2", {}, "Format the data drive?"),
      h("p", {}, "This erases ", h("strong", {}, "everything"), " on the drive, including events that haven't been sent yet and the event history. The drive is reformatted as ext4 and mounted again."),
      h("p", {}, "Type ", h("code", {}, "FORMAT"), " to confirm."),
      input,
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn", onclick: () => el.close() }, "Cancel"), go));
    input.addEventListener("input", () => { go.disabled = input.value !== "FORMAT"; });
    go.addEventListener("click", async () => { if (await act("format", input.value)) el.close(); });
    return { el, open() { input.value = ""; go.disabled = true; el.showModal(); input.focus(); } };
  }

  const unsub = ctx.events.subscribe((ev) => {
    if (["storage_changed", "usb_attached", "usb_detached"].includes(ev.type)) load();
  });
  load();
  const timer = setInterval(() => load(), 10000);
  return () => { unsub(); clearInterval(timer); msg.dispose(); if (dialog.el.open) dialog.el.close(); };
}
