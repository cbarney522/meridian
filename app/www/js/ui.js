// Small reusable UI pieces shared by pages: toast messages, the unsaved-changes bar,
// summary chips, byte formatting.
import { h, fill } from "./api.js";

export function toast() {
  const el = h("div", { class: "toast", role: "status", "aria-live": "polite", hidden: true });
  let timer = 0;
  return {
    el,
    show(text, bad = false) {
      el.textContent = text;
      el.className = "toast" + (bad ? " bad" : "");
      el.hidden = false;
      clearTimeout(timer);
      timer = setTimeout(() => { el.hidden = true; }, 6000);
    },
    dispose() { clearTimeout(timer); },
  };
}

// Sticky bar shown while a form has unsaved changes.
export function saveBar({ onSave, onDiscard }) {
  const el = h("div", { class: "savebar", hidden: true });
  return {
    el,
    update({ dirty, errors = [], saving = false, label }) {
      el.hidden = !dirty;
      if (!dirty) return;
      fill(el,
        h("span", {}, label || "Unsaved changes"),
        errors.length ? h("span", { class: "savebar-err" }, errors.join(" ")) : null,
        h("span", { class: "spacer" }),
        h("button", { type: "button", class: "btn", onclick: onDiscard }, "Discard"),
        h("button", { type: "button", class: "btn btn-primary", disabled: saving || errors.length > 0, onclick: onSave },
          saving ? "Saving…" : "Save"));
    },
  };
}

export function chip(label, value, cls = "") {
  return h("span", { class: "chip static " + cls }, label, h("span", { class: "chip-n" }, value));
}

export function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

// Error text from an ApiError, including server-side validation details.
export function errText(err) {
  return err.body && err.body.details ? err.body.details.join(" ") : (err.message || String(err));
}

// Show control characters in a raw scan so the technician can see what the reader really sent.
export function visible(raw) {
  return String(raw).replace(/[\x00-\x1f]/g, (c) => ({ "\t": "⇥", "\r": "↵", "\n": "↵" }[c] || "·"));
}
