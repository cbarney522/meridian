// System › Identity: the crib number (one crib per reader) and the names that identify this
// reader to people and to the master. Stored in the same "site" settings section as the time
// zone and coordinates, so both pages share one revision.
import { api, h, fill } from "../api.js";
import { toast, saveBar, errText } from "../ui.js";

const CRIB_RE = /^[1-9][0-9]{0,3}$/;

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("system.edit");
  const state = { data: null, form: null, saving: false };
  const msg = toast();
  const formBox = h("section", { class: "card wide" });
  const usedBox = h("section", { class: "card wide" });
  const bar = saveBar({ onSave: save, onDiscard: () => { state.form = clone(state.data.settings); draw(); changed(); } });

  root.append(
    h("h1", {}, "Identity"),
    h("p", { class: "subtitle" }, "Which crib this reader belongs to, and the names people and the master database see."),
    msg.el,
    h("div", { class: "grid grid-2" }, formBox, usedBox),
    bar.el);

  async function load() {
    try {
      state.data = await api.siteSettings();
      state.form = clone(state.data.settings);
      draw(); changed();
    } catch (err) { fill(formBox, h("div", { class: "notice bad" }, "Can't load identity settings: ", errText(err))); }
  }

  function draw() {
    const f = state.form, dis = !canEdit;
    const field = (label, control, help) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), control,
      help ? h("span", { class: "field-help" }, help) : null);
    const text = (key, attrs = {}) => {
      const el = h("input", { type: "text", value: f[key] ?? "", disabled: dis, spellcheck: "false", ...attrs });
      el.addEventListener("input", () => { f[key] = el.value; changed(); });
      return el;
    };
    const saved = state.data.settings.crib_number;
    fill(formBox,
      h("h2", {}, "This reader"),
      saved ? null : h("div", { class: "notice warn" },
        "No crib number yet. Every reader needs one before it reports to the master database."),
      h("div", { class: "form-grid" },
        field("Crib number", text("crib_number", { inputmode: "numeric", maxlength: 4, class: "mono", placeholder: "e.g. 1204" }),
          "1 to 9999, no leading zeros. One crib per reader."),
        field("Site ID", text("site_id", { maxlength: 40, class: "mono", placeholder: "plant-atl" }),
          "Short name shared by every reader at this site. Lowercase, digits and dashes; used in MQTT topics and the Site page."),
        field("Reader name", text("site_name", { maxlength: 80, placeholder: "Crib 2 portal" }), "Shown in the top bar and on the Site page."),
        field("Location", text("location", { maxlength: 120, placeholder: "Building 2, tool crib, east door" }), "Where in the building this reader is.")));
    fill(usedBox,
      h("h2", {}, "Where the crib number is used"),
      h("ul", {},
        h("li", {}, "The top bar of every page: ", h("strong", {}, saved ? `Crib ${saved}` : "not set")),
        h("li", {}, "The MQTT status message and every event sent to the broker (", h("code", {}, "crib"), " field)"),
        h("li", {}, "Requests to the master database, so it sends this crib's items, bins and tags")),
      h("p", { class: "hint" }, "Any number can be entered here. Once the master database sync is built, Meridian will check the number against the master and warn on this page if another crib already uses it."));
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
      msg.show("Identity saved. Reload the page to see the new name in the top bar.");
      await load();
    } catch (err) {
      msg.show("Couldn't save: " + errText(err), true);
      if (err.status === 409) await load();
    } finally { state.saving = false; changed(); }
  }

  load();
  return () => msg.dispose();
}

function normalize(f) {
  const c = String(f.crib_number ?? "").trim();
  return { ...f, crib_number: c === "" ? null : CRIB_RE.test(c) ? Number(c) : c,
           site_id: (f.site_id || "").trim().toLowerCase() };
}
function localErrors(f) {
  const e = [], n = normalize(f);
  if (typeof n.crib_number === "string") e.push("Crib number: 1 to 9999, digits only, no leading zeros.");
  if (n.site_id && !/^[a-z0-9][a-z0-9-]{0,39}$/.test(n.site_id)) e.push("Site ID: lowercase letters, digits and dashes.");
  return e;
}
function clone(o) { return JSON.parse(JSON.stringify(o)); }
