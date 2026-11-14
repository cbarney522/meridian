// System › About this reader: the facts the Impinj web UI shows, plus the reader-API login
// Meridian uses to read them.
import { api, h, fill } from "../api.js";
import { toast, errText } from "../ui.js";

export function render(root, ctx) {
  const msg = toast();
  const cards = h("div", { class: "grid" });
  const login = h("div");
  const canEdit = ctx.session.permissions.includes("system.edit");
  root.append(
    h("h1", {}, "About this reader"),
    h("p", { class: "subtitle" }, "Hardware, firmware and network details, the same ones the Impinj web UI shows."),
    msg.el, cards, login);

  async function load() {
    try { drawInfo(await api.readerInfo()); }
    catch (err) { fill(cards, h("div", { class: "notice bad" }, "Can't load reader details: ", errText(err))); }
  }

  function drawInfo(d) {
    const it = d.items || {}, n = d.net || {};
    const v = (x) => (x === null || x === undefined || x === "" ? h("span", { class: "muted" }, "—") : String(x));
    const kv = (rows) => h("dl", { class: "kv" }, rows.flatMap(([k, x]) => [h("dt", {}, k), h("dd", {}, v(x))]));
    fill(cards,
      d.api.ok ? null : h("div", { class: "notice warn wide" },
        "Meridian can't read the Impinj details yet: ", d.api.error || "not read yet", ". ",
        canEdit ? "Enter the reader's admin login below." : "Ask a site admin to enter the reader's admin login."),
      h("section", { class: "card" }, h("h2", {}, "Reader"), kv([
        ["Description", it.description], ["Hostname", d.hostname], ["Operating region", it.region],
        ["RFID interface", it.rfid_interface], ["Firmware", it.firmware], ["Meridian", d.app_version]])),
      h("section", { class: "card" }, h("h2", {}, "Model details"), kv([
        ["SKU", it.sku], ["HLA", it.hla], ["PCBA", it.pcba], ["Serial number", it.serial]])),
      h("section", { class: "card" }, h("h2", {}, "Network"), kv([
        ["Interface", n.interface], ["IPv4", n.ipv4], ["IPv6", n.ipv6], ["Hardware address", n.mac]])),
      h("section", { class: "card" }, h("h2", {}, "Time"), kv([
        ["Reader time", d.time ? `${d.time.slice(0, 10)} ${d.time.slice(11, 19)} ${d.timezone || ""}` : null],
        ["Uptime", uptime(d.uptime_s)],
        ["Details read", d.api.fetched ? new Date(d.api.fetched * 1000).toLocaleTimeString() : null]])),
    );
  }

  async function drawLogin() {
    if (!canEdit) return;
    let s;
    try { s = await api.readerApiSettings(); } catch (err) { return fill(login, h("div", { class: "notice bad" }, errText(err))); }
    const user = h("input", { type: "text", value: s.username, autocomplete: "off", maxlength: 64 });
    const pw = h("input", { type: "password", autocomplete: "new-password",
      placeholder: s.password_set ? "•••••••• (saved, leave blank to keep)" : "factory default “impinj” is tried until you save one" });
    const result = h("div");
    const field = (label, control, help) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), control,
      help ? h("span", { class: "field-help" }, help) : null);
    const test = h("button", { type: "button", class: "btn" }, "Test");
    const save = h("button", { type: "button", class: "btn btn-primary" }, "Save");
    const clear = s.password_set ? h("button", { type: "button", class: "btn" }, "Forget saved password") : null;

    test.addEventListener("click", async () => {
      fill(result, h("p", { class: "muted" }, "Testing…"));
      try {
        const r = await api.testReaderApi({ username: user.value.trim(), password: pw.value });
        fill(result, r.ok ? h("div", { class: "notice ok" }, `Login works. Serial ${r.items.serial || "?"}, firmware ${r.items.firmware || "?"}.`)
                          : h("div", { class: "notice bad" }, r.error || "Login failed."));
      } catch (err) { fill(result, h("div", { class: "notice bad" }, errText(err))); }
    });
    const put = async (body, text) => {
      try {
        await api.saveReaderApiSettings({ revision: s.revision, username: user.value.trim(), ...body });
        msg.show(text);
        setTimeout(load, 1500);          // the server re-reads the reader right away
        fill(login); drawLogin();
      } catch (err) { msg.show(errText(err), true); }
    };
    save.addEventListener("click", () => put({ password: pw.value }, "Reader login saved."));
    if (clear) clear.addEventListener("click", () => put({ clear_password: true }, "Saved password removed."));

    fill(login, h("section", { class: "card", style: { marginTop: "16px", maxWidth: "560px" } },
      h("h2", {}, "Reader API login"),
      h("p", { class: "hint" }, "The region, interface, firmware and model details come from the reader's own REST API, ",
        "which needs the same login as the Impinj web UI. It's stored on this reader only and never shown again."),
      field("User name", user), field("Password", pw),
      h("div", { class: "actions" }, test, save, clear), result));
  }

  load(); drawLogin();
  const timer = setInterval(load, 15000);
  return () => { clearInterval(timer); msg.dispose(); };
}

function uptime(s) {
  if (!s && s !== 0) return null;
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d}d ${hr}h ${m}m`;
}
