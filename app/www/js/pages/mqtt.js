// Integrations › MQTT: broker connection, the topics this reader uses, and live traffic.
import { api, h, fill } from "../api.js";
import { toast, saveBar, errText } from "../ui.js";

const EXAMPLES = [
  ["ping", { id: "1" }, "Replies on the ack topic. The quickest end-to-end check."],
  ["identify", { id: "2", args: { seconds: 10 } }, "Shows on the Dashboard's live events (flashes a light once outputs exist)."],
  ["status", { id: "3" }, "Replies with reader health, USB and MQTT status."],
  ["info", { id: "4" }, "Replies with model, serial, firmware, region and network details."],
];

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("integrations.edit");
  const state = { data: null, form: null, saving: false };
  const msg = toast();
  const statusCard = h("section", { class: "card" });
  const formCard = h("section", { class: "card" });
  const topicsCard = h("section", { class: "card full" });
  const trafficCard = h("section", { class: "card full" });
  const eventsCard = h("section", { class: "card full" });
  let eventsDrawn = false;
  const bar = saveBar({ onSave: save, onDiscard: () => { state.form = formFrom(state.data.settings); drawForm(); changed(); } });

  root.append(
    h("h1", {}, "MQTT"),
    h("p", { class: "subtitle" }, "Connects this reader to the site's MQTT broker: it publishes its status and events, and takes commands."),
    msg.el,
    h("div", { class: "grid grid-2" }, statusCard, formCard, topicsCard, eventsCard, trafficCard),
    bar.el);

  async function load(rebuildForm) {
    try {
      const d = await api.mqtt();
      state.data = d;
      if (rebuildForm || !state.form) { state.form = formFrom(d.settings); drawForm(); changed(); }
      drawStatus(); drawTopics(); drawTraffic();
      if (!eventsDrawn) { drawEvents(); eventsDrawn = true; }   // static; don't collapse open samples every 3 s
    } catch (err) { fill(statusCard, h("div", { class: "notice bad" }, "Can't load MQTT settings: ", errText(err))); }
  }

  // ---------- status
  function drawStatus() {
    const s = state.data.status;
    const pill = !s.enabled ? h("span", { class: "pill neutral" }, "Off")
      : s.connected ? h("span", { class: "pill ok" }, "Connected") : h("span", { class: "pill bad" }, "Not connected");
    const kv = [["State", pill], ["Broker", s.broker ? `${s.broker}${s.tls ? " (TLS)" : ""}` : null],
      ["Connected since", s.since ? new Date(s.since * 1000).toLocaleString() : null],
      ["Problem", s.error ? h("span", { class: "bad-text" }, s.error) : null],
      ["Sent / received", `${s.sent} / ${s.received}`],
      ["Waiting to send", s.queued ? h("span", { class: "pill warn" }, `${s.queued} queued`) : "0"],
      ["Dropped (queue full)", s.dropped ? h("span", { class: "pill bad" }, String(s.dropped)) : "0"]];
    fill(statusCard, h("h2", {}, "Connection"),
      h("dl", { class: "kv" }, kv.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v ?? h("span", { class: "muted" }, "—"))])),
      !s.enabled ? h("p", { class: "hint", style: { marginTop: "10px" } }, canEdit
        ? "Enter the broker details, use Test, then tick Enabled and save." : "Not set up yet. Ask a site admin.") : null);
  }

  // ---------- settings form
  function drawForm() {
    const f = state.form, dis = !canEdit;
    const field = (label, control, help) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), control,
      help ? h("span", { class: "field-help" }, help) : null);
    const input = (key, attrs = {}) => {
      const el = h("input", { type: "text", value: f[key] ?? "", disabled: dis, spellcheck: "false", ...attrs });
      el.addEventListener("input", () => { f[key] = el.value; changed(); });
      return el;
    };
    const check = (key, label, after) => {
      const el = h("input", { type: "checkbox", checked: !!f[key], disabled: dis });
      el.addEventListener("change", () => { f[key] = el.checked; if (after) after(el.checked); changed(); });
      return h("label", { class: "radio" }, el, h("span", {}, label));
    };
    const port = input("port", { inputmode: "numeric", maxlength: 5 });
    const pw = h("input", { type: "password", autocomplete: "new-password", disabled: dis,
      placeholder: state.data.settings.password_set ? "•••••••• (saved, leave blank to keep)" : "" });
    pw.addEventListener("input", () => { f.password = pw.value; changed(); });
    const ca = h("textarea", { rows: 4, disabled: dis, class: "mono", spellcheck: "false",
      placeholder: "-----BEGIN CERTIFICATE-----\n(only for a broker with a private CA)" }, f.ca_pem || "");
    ca.addEventListener("input", () => { f.ca_pem = ca.value; changed(); });
    const qos = h("select", { disabled: dis },
      h("option", { value: "1", selected: f.qos === 1 }, "1 — at least once (recommended)"),
      h("option", { value: "0", selected: f.qos === 0 }, "0 — at most once"));
    qos.addEventListener("change", () => { f.qos = Number(qos.value); changed(); });
    const tlsBox = h("div", { hidden: !f.tls },
      check("verify_cert", "Verify the broker's certificate"),
      field("CA certificate (PEM)", ca, "Leave empty for brokers with a public certificate."));
    const result = h("div");
    const test = h("button", { type: "button", class: "btn", disabled: dis }, "Test connection");
    test.addEventListener("click", async () => {
      fill(result, h("p", { class: "muted" }, "Connecting…"));
      try {
        const r = await api.testMqtt(toBody(f));
        fill(result, r.ok ? h("div", { class: "notice ok" }, `Connected in ${r.ms} ms. Nothing was saved yet.`)
                          : h("div", { class: "notice bad" }, "Couldn't connect: ", r.error));
      } catch (err) { fill(result, h("div", { class: "notice bad" }, errText(err))); }
    });
    const clear = state.data.settings.password_set && canEdit
      ? h("button", { type: "button", class: "btn btn-small", onclick: () => { f.clear_password = true; pw.value = ""; pw.placeholder = "(will be removed when you save)"; changed(); } }, "Forget saved password") : null;

    fill(formCard, h("h2", {}, "Broker"),
      check("enabled", "Enabled: keep this reader connected"),
      h("div", { class: "form-grid", style: { marginTop: "10px" } },
        field("Broker address", input("host", { placeholder: "192.168.0.50 or broker.plant.local" }), null),
        field("Port", port, "1883 plain, 8883 with TLS."),
        h("div", { class: "field" }, check("tls", "Use TLS (encrypted)", (on) => {
          tlsBox.hidden = !on;
          if (on && f.port === "1883") { f.port = "8883"; port.value = "8883"; }
          if (!on && f.port === "8883") { f.port = "1883"; port.value = "1883"; }
        }), tlsBox),
        field("User name", input("username", { autocomplete: "off", placeholder: "e.g. the reader's serial number" }), null),
        h("div", { class: "field" }, h("span", { class: "field-label" }, "Password"), pw, clear),
        field("Topic prefix", input("prefix", { class: "mono" }), "First level of every topic."),
        field("Heartbeat (seconds)", input("heartbeat_s", { inputmode: "numeric", maxlength: 4 }), "How often the status topic is refreshed."),
        field("Quality of service", qos, null)),
      h("div", { class: "actions", style: { marginTop: "12px" } }, test), result);
  }

  // ---------- topics + examples
  function drawTopics() {
    const t = state.data.status.topics;
    if (!t) return fill(topicsCard, h("h2", {}, "Topics"), h("p", { class: "muted" }, "Shown once the broker is set up."));
    const copy = (text) => h("button", { type: "button", class: "btn btn-small", title: "Copy",
      onclick: () => navigator.clipboard?.writeText(text).then(() => msg.show("Copied."), () => msg.show("Copy isn't allowed here; select the text instead.", true)) }, "Copy");
    const row = (label, topic, note) => h("tr", {}, h("td", {}, label), h("td", { class: "mono" }, topic), h("td", {}, note), h("td", {}, copy(topic)));
    const cmd = (name) => t.commands.replace("<name>", name);
    fill(topicsCard, h("h2", {}, "Topics"),
      h("table", { class: "table simple" },
        h("thead", {}, h("tr", {}, ["", "Topic", "", ""].map((x) => h("th", {}, x)))),
        h("tbody", {},
          row("Watch everything", t.subscribe_all, "Subscribe to this in MQTT Explorer."),
          row("Status", t.status, "Retained. “online” with details; “offline” if the reader drops off."),
          row("Events", t.events, "USB attached/removed, badge scans, …"),
          row("Commands", t.commands, "Publish here to command this reader."),
          row("All readers", t.broadcast, "Commands for every reader at the site."),
          row("Replies", t.acks, "One reply per command, matched by id."),
          row("Client ID", t.client_id, "Give the reader a broker login with this name or its serial."))),
      h("h3", { style: { marginTop: "16px" } }, "Try a command"),
      h("table", { class: "table simple" },
        h("thead", {}, h("tr", {}, ["Publish to", "Payload", "", ""].map((x) => h("th", {}, x)))),
        h("tbody", {}, EXAMPLES.map(([name, body, note]) => h("tr", {},
          h("td", { class: "mono" }, cmd(name)), h("td", { class: "mono" }, JSON.stringify(body)), h("td", {}, note),
          h("td", {}, copy(JSON.stringify(body))))))),
      h("p", { class: "hint", style: { marginTop: "10px" } },
        "Mosquitto access control that limits each reader to its own topics (user name = reader serial): ",
        h("code", {}, `pattern readwrite ${state.data.settings.prefix}/+/%u/#`), " and ",
        h("code", {}, `pattern read ${state.data.settings.prefix}/+/all/#`), "."));
  }

  // ---------- event types
  function drawEvents() {
    const list = state.data.event_types || [];
    const t = state.data.status.topics;
    const topicFor = (type) => (t ? t.events.replace("<type>", type) : `…/evt/${type}`);
    const built = list.filter((e) => e.built).length;
    fill(eventsCard, h("h2", {}, "Event types"),
      h("p", { class: "hint" }, `Each event is published on its own topic under evt/. This build produces ${built} of the ${list.length} types; `,
        "the others arrive with the RFID engine, I/O and access control. A type appears in MQTT Explorer's tree the first time it happens."),
      h("table", { class: "table simple events-table" },
        h("thead", {}, h("tr", {}, ["Type", "", "Meaning", "Sample payload"].map((x) => h("th", {}, x)))),
        h("tbody", {}, list.map((e) => h("tr", { class: e.built ? null : "row-off" },
          h("td", {}, h("div", { class: "mono" }, e.type), h("div", { class: "sub" }, e.label)),
          h("td", {}, e.built ? h("span", { class: "pill ok" }, "In this build") : h("span", { class: "pill neutral" }, "Planned")),
          h("td", {}, e.meaning, h("div", { class: "sub mono" }, topicFor(e.type))),
          h("td", {}, h("details", {}, h("summary", {}, "Show"), h("pre", { class: "mono sample" }, JSON.stringify(e.sample, null, 2)))))))));
  }

  // ---------- traffic
  function drawTraffic() {
    const rows = state.data.status.traffic;
    fill(trafficCard, h("h2", {}, "Recent traffic"),
      rows.length ? h("table", { class: "table simple traffic" },
        h("thead", {}, h("tr", {}, ["Time", "", "Topic", "Payload"].map((x) => h("th", {}, x)))),
        h("tbody", {}, rows.map((m) => h("tr", {},
          h("td", { class: "mono" }, m.ts),
          h("td", {}, h("span", { class: "pill " + (m.dir === "in" ? "warn" : "neutral") }, m.dir === "in" ? "in" : "out")),
          h("td", { class: "mono" }, m.topic.split("/").slice(3).join("/") || m.topic),
          h("td", { class: "mono payload" }, m.payload)))))
        : h("p", { class: "muted" }, state.data.status.connected ? "Nothing sent or received yet." : "No traffic while disconnected."));
  }

  // ---------- save
  function changed() {
    if (!state.data) return;
    const dirty = JSON.stringify(toBody(state.form)) !== JSON.stringify(toBody(formFrom(state.data.settings))) || !!state.form.password || !!state.form.clear_password;
    bar.update({ dirty, errors: localErrors(state.form), saving: state.saving });
  }
  async function save() {
    state.saving = true; changed();
    try {
      await api.saveMqtt(state.data.revision, toBody(state.form));
      msg.show(state.form.enabled ? "Saved. Connecting…" : "Saved.");
      await load(true);
      setTimeout(() => load(false), 2500);
    } catch (err) {
      msg.show("Couldn't save: " + errText(err), true);
      if (err.status === 409) await load(true);
    } finally { state.saving = false; changed(); }
  }

  load(true);
  const timer = setInterval(() => load(false), 3000);
  return () => { clearInterval(timer); msg.dispose(); };
}

function formFrom(s) {
  return { enabled: s.enabled, host: s.host, port: String(s.port), tls: s.tls, verify_cert: s.verify_cert, ca_pem: s.ca_pem,
           username: s.username, password: "", prefix: s.prefix, heartbeat_s: String(s.heartbeat_s), qos: s.qos };
}
function toBody(f) {
  const b = { ...f, host: (f.host || "").trim(), port: Number(f.port), heartbeat_s: Number(f.heartbeat_s) };
  if (!b.password) delete b.password;
  if (!b.clear_password) delete b.clear_password;
  return b;
}
function localErrors(f) {
  const e = [], b = toBody(f);
  if (b.enabled && !b.host) e.push("Enter the broker address.");
  if (!(b.port >= 1 && b.port <= 65535)) e.push("Port must be 1–65535.");
  if (!(b.heartbeat_s >= 10 && b.heartbeat_s <= 3600)) e.push("Heartbeat must be 10–3600 s.");
  if (!/^[A-Za-z0-9_-]+(\/[A-Za-z0-9_-]+)*$/.test((b.prefix || "").replace(/^\/+|\/+$/g, ""))) e.push("Topic prefix: letters, digits, - and _.");
  return e;
}
