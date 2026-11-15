// Integrations › HTTP / webhooks: POST each event as signed JSON to one or more URLs, with a
// durable queue, retries, a Send test button and a delivery log.
import { api, h, fill } from "../api.js";
import { toast, saveBar, errText } from "../ui.js";

const MAX = 5;

export function render(root, ctx) {
  const canEdit = ctx.session.permissions.includes("integrations.edit");
  const state = { data: null, form: null, saving: false, log: [], logFilter: "", open: new Set() };
  const msg = toast();
  const epCard = h("section", { class: "card full" });
  const logCard = h("section", { class: "card full" });
  const helpCard = h("section", { class: "card full" });
  const bar = saveBar({ onSave: save, onDiscard: () => { state.form = formFrom(state.data.settings); drawEndpoints(); changed(); } });

  root.append(
    h("h1", {}, "HTTP / webhooks"),
    h("p", { class: "subtitle" }, "Sends each event, as it happens, to web addresses you choose: an ERP, a middleware platform or your own script. ",
      "Nothing is lost while a receiver is down: events wait here and are retried."),
    msg.el,
    h("div", { class: "grid grid-2" }, epCard, logCard, helpCard),
    bar.el);

  // ---------- data
  async function load(rebuildForm) {
    try {
      const d = await api.webhooks();
      state.data = d;
      if (rebuildForm || !state.form) { state.form = formFrom(d.settings); drawEndpoints(); changed(); }
      else drawStatuses();
      await loadLog();
    } catch (err) { fill(epCard, h("div", { class: "notice bad" }, "Can't load webhook settings: ", errText(err))); }
  }
  async function loadLog() {
    try {
      state.log = (await api.webhookDeliveries(state.logFilter)).deliveries;
      drawLog();
    } catch (err) { fill(logCard, h("div", { class: "notice bad" }, "Can't load the delivery log: ", errText(err))); }
  }

  // ---------- endpoints
  const statusSlots = {};
  function drawEndpoints() {
    const f = state.form, dis = !canEdit;
    const types = state.data.event_types || [];
    const saved = Object.fromEntries(state.data.settings.endpoints.map((e) => [e.id, e]));
    for (const k of Object.keys(statusSlots)) delete statusSlots[k];

    const cards = f.endpoints.map((ep, idx) => {
      const field = (label, control, help) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), control,
        help ? h("span", { class: "field-help" }, help) : null);
      const input = (key, attrs = {}) => {
        const el = h("input", { type: "text", value: ep[key] ?? "", disabled: dis, spellcheck: "false", ...attrs });
        el.addEventListener("input", () => { ep[key] = el.value; changed(); if (key === "url") verifyBox.hidden = !isHttps(ep.url); });
        return el;
      };
      const check = (key, label) => {
        const el = h("input", { type: "checkbox", checked: !!ep[key], disabled: dis });
        el.addEventListener("change", () => { ep[key] = el.checked; changed(); });
        return h("label", { class: "radio" }, el, h("span", {}, label));
      };
      const verifyBox = h("div", { hidden: !isHttps(ep.url) }, check("verify_cert", "Verify the receiver's certificate (untick for a self-signed one)"));

      // event types: All, or a list
      const all = ep.events.includes("*");
      const allBox = h("input", { type: "checkbox", checked: all, disabled: dis });
      const list = h("div", { class: "event-checks", hidden: all }, types.map((t) => {
        const c = h("input", { type: "checkbox", checked: ep.events.includes(t.type), disabled: dis });
        c.addEventListener("change", () => {
          ep.events = c.checked ? [...new Set([...ep.events, t.type])] : ep.events.filter((x) => x !== t.type);
          changed();
        });
        return h("label", { class: "radio", title: t.meaning }, c, h("span", { class: t.built ? null : "muted" },
          h("span", { class: "mono" }, t.type), t.built ? "" : " (planned)"));
      }));
      allBox.addEventListener("change", () => {
        ep.events = allBox.checked ? ["*"] : [];
        list.hidden = allBox.checked;
        drawEndpoints(); changed();
      });

      // secret
      const s = saved[ep.id];
      const secretRow = !s ? h("p", { class: "hint" }, "A signing secret is created when you save.")
        : s.secret === null ? h("p", { class: "hint" }, "Signing secret: set (visible to people who can edit integrations).")
        : h("div", { class: "field" }, h("span", { class: "field-label" }, "Signing secret"),
            h("div", { class: "copy-cell" },
              h("button", { type: "button", class: "btn btn-small", onclick: () => copy(s.secret) }, "Copy"),
              h("span", { class: "mono" }, ep.regenerate_secret ? "(a new secret will be created when you save)" : s.secret)),
            canEdit ? check("regenerate_secret", "Create a new secret when I save (the receiver must be updated)") : null,
            h("span", { class: "field-help" }, "Give this to whoever runs the receiver so it can check each request really came from this reader."));

      // actions
      const result = h("div");
      const dirtyEp = () => !s || JSON.stringify(toBody(ep)) !== JSON.stringify(toBody(formFrom({ endpoints: [s] }).endpoints[0]));
      const test = h("button", { type: "button", class: "btn", disabled: dis }, "Send test");
      test.addEventListener("click", async () => {
        if (dirtyEp()) return fill(result, h("div", { class: "notice warn" }, "Save first: the test uses the saved settings."));
        fill(result, h("p", { class: "muted" }, "Sending…"));
        try {
          const r = await api.testWebhook(ep.id);
          fill(result, r.ok
            ? h("div", { class: "notice ok" }, `Delivered: HTTP ${r.code} in ${r.ms} ms.`, r.response ? h("div", { class: "sub mono" }, r.response.slice(0, 200)) : null)
            : h("div", { class: "notice bad" }, "Not delivered: ", r.error, r.response ? h("div", { class: "sub mono" }, r.response.slice(0, 200)) : null));
          loadLog();
        } catch (err) { fill(result, h("div", { class: "notice bad" }, errText(err))); }
      });
      const remove = h("button", { type: "button", class: "btn btn-small btn-danger", disabled: dis, onclick: () => {
        f.endpoints.splice(idx, 1); drawEndpoints(); changed();
      } }, "Remove");
      const slot = h("div", { class: "statline" });
      statusSlots[ep.id] = slot;

      return h("div", { class: "endpoint" },
        h("div", { class: "card-head" }, h("h3", {}, ep.name || `Endpoint ${idx + 1}`), slot),
        check("enabled", "Enabled: send events to this address"),
        h("div", { class: "form-grid", style: { marginTop: "10px" } },
          field("Name", input("name", { maxlength: 60, placeholder: "e.g. SAP via middleware" }), null),
          field("URL", input("url", { maxlength: 500, class: "mono", placeholder: "https://erp.example.com/meridian/events" }),
            "Receives a POST with a JSON body for each event."),
          field("Timeout (seconds)", input("timeout_s", { inputmode: "numeric", maxlength: 2, class: "num-input" }), "2–30. How long to wait for an answer."),
          h("div", { class: "field" }, h("span", { class: "field-label" }, " "), verifyBox)),
        h("div", { class: "field" }, h("span", { class: "field-label" }, "Events to send"),
          h("label", { class: "radio" }, allBox, h("span", {}, "All events, including types added in later versions")), list),
        secretRow,
        h("div", { class: "actions", style: { marginTop: "12px" } }, test, remove), result);
    });

    const add = f.endpoints.length < MAX && canEdit
      ? h("button", { type: "button", class: "btn", onclick: () => {
          f.endpoints.push({ id: "", name: "", url: "", enabled: true, events: ["*"], verify_cert: true, timeout_s: "10" });
          drawEndpoints(); changed();
        } }, "Add endpoint") : null;
    fill(epCard, h("h2", {}, "Endpoints"),
      cards.length ? cards : h("p", { class: "muted" }, canEdit
        ? "No endpoints yet. Add one, save, then use Send test." : "No endpoints set up. Ask a site admin."),
      h("div", { class: "actions", style: { marginTop: "12px" } }, add),
      state.data.status.dropped ? h("div", { class: "notice bad" }, `${state.data.status.dropped} events were dropped because a queue was full (10,000 waiting).`) : null);
    drawStatuses();
  }

  function drawStatuses() {
    for (const [id, slot] of Object.entries(statusSlots)) {
      const st = state.data.status.endpoints[id];
      if (!st) { fill(slot, h("span", { class: "pill neutral" }, "Not saved yet")); continue; }
      const pill = { ok: h("span", { class: "pill ok" }, "Delivering"), failing: h("span", { class: "pill bad" }, "Failing"),
        idle: h("span", { class: "pill neutral" }, "Nothing sent yet"), off: h("span", { class: "pill neutral" }, "Off") }[st.state];
      fill(slot, pill,
        h("span", { class: "muted" }, ` ${st.delivered} delivered · ${st.pending} waiting · ${st.failed} failed`),
        st.last_ok_at ? h("span", { class: "muted" }, ` · last OK ${new Date(st.last_ok_at * 1000).toLocaleTimeString()}`) : null,
        st.last_error ? h("div", { class: "bad-text" }, st.last_error,
          st.retry_in_s !== null ? ` · retrying in ${fmtWait(st.retry_in_s)} ` : " ",
          canEdit && st.pending ? h("button", { type: "button", class: "btn btn-small", onclick: async () => {
            try { await api.retryWebhook(id); msg.show("Retrying now."); setTimeout(() => load(false), 1500); }
            catch (err) { msg.show(errText(err), true); }
          } }, "Retry now") : null) : null);
    }
  }

  // ---------- delivery log
  function drawLog() {
    const eps = state.data.settings.endpoints;
    const sel = h("select", { "aria-label": "Endpoint" }, h("option", { value: "" }, "All endpoints"),
      eps.map((e) => h("option", { value: e.id, selected: state.logFilter === e.id }, e.name)));
    sel.addEventListener("change", () => { state.logFilter = sel.value; loadLog(); });
    const pill = (s) => h("span", { class: "pill " + ({ delivered: "ok", pending: "warn", failed: "bad" }[s] || "neutral") },
      { delivered: "Delivered", pending: "Waiting", failed: "Failed" }[s] || s);
    const rows = state.log.flatMap((d) => {
      const open = state.open.has(d.id);
      const tr = h("tr", { class: "clickable", onclick: () => { open ? state.open.delete(d.id) : state.open.add(d.id); drawLog(); } },
        h("td", { class: "mono nowrap" }, new Date((d.last_at || d.created) * 1000).toLocaleString()),
        h("td", {}, d.endpoint),
        h("td", { class: "mono" }, d.type),
        h("td", {}, pill(d.status)),
        h("td", { class: "num" }, String(d.attempts)),
        h("td", { class: "mono" }, d.last_code ? `${d.last_code}` : "—", d.last_ms !== null ? h("span", { class: "muted" }, ` ${d.last_ms} ms`) : null),
        h("td", { class: "bad-text" }, d.status === "delivered" ? "" : d.last_error || ""));
      if (!open) return [tr];
      return [tr, h("tr", { class: "detail" }, h("td", { colspan: 7 },
        h("div", { class: "sub" }, "Event id ", h("span", { class: "mono" }, d.event_id),
          d.status === "pending" && d.next_at ? ` · next try ${new Date(d.next_at * 1000).toLocaleTimeString()}` : ""),
        h("pre", { class: "mono sample" }, pretty(d.body)),
        d.response ? [h("div", { class: "sub" }, "Receiver answered:"), h("pre", { class: "mono sample" }, d.response)] : null,
        canEdit && d.type !== "test" && d.status !== "pending" ? h("button", { type: "button", class: "btn btn-small", onclick: async (e) => {
          e.stopPropagation();
          try { await api.resendWebhook(d.id); msg.show("Queued again with the same event id."); setTimeout(loadLog, 1200); }
          catch (err) { msg.show(errText(err), true); }
        } }, "Resend") : null))];
    });
    fill(logCard, h("div", { class: "card-head" }, h("h2", {}, "Delivery log"), sel),
      rows.length ? h("div", { class: "table-scroll" }, h("table", { class: "table simple" },
        h("thead", {}, h("tr", {}, ["Time", "Endpoint", "Event", "Status", "Tries", "Answer", "Problem"].map((x) => h("th", {}, x)))),
        h("tbody", {}, rows)))
        : h("p", { class: "muted" }, "Nothing sent yet. Deliveries are kept for 7 days."),
      h("p", { class: "hint" }, "Click a row to see exactly what was sent. Newest first; the last 100 shown."));
  }

  // ---------- help for receivers (static)
  fill(helpCard, h("h2", {}, "What a receiver gets"),
    h("p", {}, "One HTTPS POST per event, with a JSON body like this:"),
    h("pre", { class: "mono sample" }, JSON.stringify({
      id: "evt_3f9c0a1b2c4d5e6f7a8b", type: "badge", time: "2026-11-15T21:20:05Z",
      reader: { serial: "37025501450", crib: 1204, site: "plant-atl", name: "Crib 2 portal" },
      data: { port: "1-1.1.1", role: "badge_in", raw: "I105:0010442", ts: "2026-11-15T16:20:05" } }, null, 2)),
    h("table", { class: "table simple" },
      h("thead", {}, h("tr", {}, h("th", {}, "Header"), h("th", {}, "Meaning"))),
      h("tbody", {},
        [["X-Meridian-Event", "The event type, same as type in the body."],
         ["X-Meridian-Delivery", "The event id. A resend or retry keeps it, so a receiver can ignore one it already has."],
         ["X-Meridian-Timestamp", "Unix time the request was sent."],
         ["X-Meridian-Signature", "sha256= then the HMAC-SHA256, in hex, of timestamp + \".\" + body, keyed with the endpoint's secret."]]
          .map(([k, v]) => h("tr", {}, h("td", { class: "mono" }, k), h("td", {}, v))))),
    h("p", { class: "hint", style: { marginTop: "10px" } },
      "Answer with any 2xx status to accept. Anything else, no answer, or a redirect is retried after 30 s, 2 min, 10 min, 30 min, then hourly, ",
      "for up to 24 hours. While a receiver is failing its later events wait, so they arrive in order. ",
      "For a quick test on your PC, run ", h("code", {}, "python dev/webhook_receiver.py --secret <secret>"), " from the Meridian repo ",
      "and use http://<your PC's IP>:8765/ as the URL."));

  // ---------- save
  function changed() {
    if (!state.data) return;
    const dirty = JSON.stringify(state.form.endpoints.map(toBody)) !== JSON.stringify(formFrom(state.data.settings).endpoints.map(toBody));
    bar.update({ dirty, errors: localErrors(state.form), saving: state.saving });
  }
  async function save() {
    state.saving = true; changed();
    try {
      await api.saveWebhooks(state.data.revision, { endpoints: state.form.endpoints.map(toBody) });
      msg.show("Saved.");
      await load(true);
    } catch (err) {
      msg.show("Couldn't save: " + errText(err), true);
      if (err.status === 409) await load(true);
    } finally { state.saving = false; changed(); }
  }
  function copy(text) {
    navigator.clipboard?.writeText(text).then(() => msg.show("Copied."), () => msg.show("Copy isn't allowed here; select the text instead.", true));
  }

  load(true);
  const timer = setInterval(() => load(false), 3000);
  return () => { clearInterval(timer); msg.dispose(); };
}

function formFrom(s) {
  return { endpoints: (s.endpoints || []).map((e) => ({ id: e.id, name: e.name, url: e.url, enabled: e.enabled,
    events: [...e.events], verify_cert: e.verify_cert, timeout_s: String(e.timeout_s), regenerate_secret: false })) };
}
function toBody(e) {
  const b = { id: e.id, name: (e.name || "").trim(), url: (e.url || "").trim(), enabled: !!e.enabled, events: [...e.events].sort(),
              verify_cert: !!e.verify_cert, timeout_s: Number(e.timeout_s) };
  if (e.regenerate_secret) b.regenerate_secret = true;
  return b;
}
function localErrors(f) {
  const e = [];
  f.endpoints.forEach((ep, i) => {
    const n = ep.name || `Endpoint ${i + 1}`, b = toBody(ep);
    if (!/^https?:\/\/\S+$/i.test(b.url)) e.push(`${n}: enter a full http:// or https:// address.`);
    if (!b.events.length) e.push(`${n}: choose at least one event type.`);
    if (!(b.timeout_s >= 2 && b.timeout_s <= 30)) e.push(`${n}: timeout must be 2–30 s.`);
  });
  return e;
}
function isHttps(u) { return /^https:/i.test((u || "").trim()); }
function pretty(s) { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; } }
function fmtWait(s) { return s < 60 ? `${s} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`; }
