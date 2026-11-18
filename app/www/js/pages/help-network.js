// Help › Troubleshooting › Network tools: what the reader's network looks like (link, address,
// DHCP, gateway, DNS) and tests run from the reader itself (quick check, port, DNS, ping).
import { api, h, fill } from "../api.js";
import { toast, errText } from "../ui.js";

const PILL = { pass: ["ok", "Pass"], warn: ["warn", "Check"], fail: ["bad", "Fail"], info: ["neutral", "Info"] };

export function render(root) {
  const msg = toast();
  const state = { st: null, portResults: [], lastChecks: null };
  const quickCard = h("section", { class: "card full" });
  const netCard = h("section", { class: "card full" });
  const portCard = h("section", { class: "card full" });
  const dnsCard = h("section", { class: "card" });
  const pingCard = h("section", { class: "card" });

  root.append(
    h("h1", {}, "Network tools"),
    h("p", { class: "subtitle" }, "Tests run from this reader, so they show what the reader itself can reach. ",
      "Use them when MQTT, webhooks or the reader's web pages aren't working."),
    msg.el,
    h("div", { class: "grid grid-2" }, quickCard, netCard, portCard, dnsCard, pingCard));

  // ---------- quick check
  const runBtn = h("button", { type: "button", class: "btn btn-primary" }, "Run quick check");
  const copyBtn = h("button", { type: "button", class: "btn", hidden: true }, "Copy results");
  const quickOut = h("div");
  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true; copyBtn.hidden = true;
    fill(quickOut, h("p", { class: "muted" }, "Checking… this can take up to 30 seconds."));
    try {
      const r = await api.netChecks();
      state.lastChecks = r.checks;
      fill(quickOut, h("table", { class: "table simple checks" }, h("tbody", {}, r.checks.map((c) => h("tr", {},
        h("td", { class: "nowrap" }, h("span", { class: "pill " + PILL[c.status][0] }, PILL[c.status][1])),
        h("td", { class: "nowrap" }, h("strong", {}, c.name)),
        h("td", {}, c.detail))))));
      copyBtn.hidden = false;
    } catch (err) { fill(quickOut, h("div", { class: "notice bad" }, errText(err))); }
    finally { runBtn.disabled = false; }
  });
  copyBtn.addEventListener("click", () => {
    const st = state.st, lines = [`Meridian network check, ${new Date().toLocaleString()}`, st ? `Host ${st.hostname}` : ""];
    for (const c of state.lastChecks || []) lines.push(`[${PILL[c.status][1].toUpperCase()}] ${c.name}: ${c.detail}`);
    copy(lines.filter(Boolean).join("\n"));
  });
  fill(quickCard, h("div", { class: "card-head" }, h("h2", {}, "Quick check"), h("div", { class: "actions" }, copyBtn, runBtn)),
    h("p", { class: "hint" }, "Checks, in order: the cable and link, the IP address and DHCP, the default gateway, DNS, ",
      "then every server this reader is set up to talk to (the MQTT broker and enabled webhook receivers), and time sync."),
    quickOut);

  // ---------- this reader's network
  async function loadStatus() {
    try {
      state.st = await api.netStatus();
      drawStatus();
      drawPort();
    } catch (err) { fill(netCard, h("h2", {}, "This reader's network"), h("div", { class: "notice bad" }, errText(err))); }
  }
  function drawStatus() {
    const st = state.st, d = st.dhcp;
    const cls = { dhcp: "ok", dhcp_likely: "ok", static: "neutral", unknown: "warn", apipa: "bad", none: "bad" }[d.verdict];
    const title = { dhcp: "DHCP lease", dhcp_likely: "DHCP (very likely)", static: "Static address", unknown: "Can't tell",
      apipa: "No DHCP lease", none: "No address" }[d.verdict];
    const lease = d.lease;
    fill(netCard,
      h("div", { class: "card-head" }, h("h2", {}, "This reader's network"),
        h("button", { type: "button", class: "btn btn-small", onclick: loadStatus }, "Refresh")),
      h("div", { class: "notice " + cls }, h("strong", {}, title + ". "), d.message,
        lease ? h("dl", { class: "kv", style: { marginTop: "8px" } },
          kv("DHCP server", lease.server), kv("Router", lease.router),
          kv("Obtained", lease.obtained ? new Date(lease.obtained * 1000).toLocaleString() : null),
          kv("Expires", lease.expire ? new Date(lease.expire * 1000).toLocaleString() : null),
          kv("Lease record", lease.source)) : null,
        h("details", { style: { marginTop: "8px" } }, h("summary", {}, "How this was decided"),
          h("ul", {}, d.evidence.map((e) => h("li", { class: "mono" }, e))))),
      h("div", { class: "table-scroll" }, h("table", { class: "table simple" },
        h("thead", {}, h("tr", {}, ["Interface", "Link", "IPv4", "MAC", "IPv6"].map((x) => h("th", {}, x)))),
        h("tbody", {}, st.interfaces.map((i) => h("tr", { class: i.name === st.interface ? null : "muted" },
          h("td", { class: "mono" }, i.name, i.name === st.interface ? h("span", { class: "muted" }, " (main)") : null),
          h("td", {}, i.carrier ? h("span", { class: "pill ok" }, "Up") : h("span", { class: "pill bad" }, "No link"),
            i.speed_mbps ? ` ${i.speed_mbps} Mb/s` : "", i.duplex && i.duplex !== "unknown" && i.carrier ? ` ${i.duplex} duplex` : ""),
          h("td", { class: "mono" }, i.ipv4 ? `${i.ipv4}/${i.prefix}` : "—"),
          h("td", { class: "mono" }, i.mac || "—"),
          h("td", { class: "mono" }, i.ipv6.filter((a) => a.scope === "global").map((a) => a.address).join(", ") || "—"))))) ),
      h("dl", { class: "kv", style: { marginTop: "10px" } },
        kv("Host name", st.hostname),
        kv("Default gateway", st.gateways.length ? st.gateways.map((g) => `${g.gateway} (${g.interface})`).join(", ") : "none"),
        kv("DNS servers", st.dns.servers.join(", ") || "none"),
        st.dns.search.length ? kv("Search domains", st.dns.search.join(" ")) : null));
  }

  // ---------- port test
  const host = h("input", { type: "text", class: "mono", spellcheck: "false", placeholder: "broker.example.com or 192.168.0.20", list: "net-targets" });
  const port = h("input", { type: "text", class: "mono num-input", inputmode: "numeric", maxlength: 5, value: "1883" });
  const tls = h("input", { type: "checkbox" });
  const testBtn = h("button", { type: "button", class: "btn btn-primary" }, "Test");
  const portOut = h("div");
  const targetsList = h("datalist", { id: "net-targets" });
  testBtn.addEventListener("click", runPort);
  for (const el of [host, port]) el.addEventListener("keydown", (e) => { if (e.key === "Enter") runPort(); });
  async function runPort() {
    testBtn.disabled = true;
    try {
      const r = await api.netPort(host.value, port.value, tls.checked);
      state.portResults.unshift(r);
      state.portResults.length = Math.min(state.portResults.length, 10);
      drawPortResults();
    } catch (err) { msg.show(errText(err), true); }
    finally { testBtn.disabled = false; }
  }
  function drawPort() {
    const st = state.st;
    fill(targetsList, (st.targets || []).map((t) => h("option", { value: t.host }, t.label)));
    if (!host.value && st.targets?.length) {
      host.value = st.targets[0].host; port.value = String(st.targets[0].port); tls.checked = st.targets[0].tls;
    }
    const chips = h("div", { class: "chips" },
      (st.common_ports || []).map((c) => h("button", { type: "button", class: "chip", onclick: () => { port.value = String(c.port); tls.checked = c.tls; } },
        `${c.port} ${c.label}`)),
      (st.targets || []).map((t) => h("button", { type: "button", class: "chip", title: "Use this reader's setting", onclick: () => {
        host.value = t.host; port.value = String(t.port); tls.checked = t.tls;
      } }, t.label)));
    fill(portCard, h("h2", {}, "Port test"),
      h("p", { class: "hint" }, "Opens a connection from the reader to a server, e.g. your MQTT broker on 1883 or 8883. ",
        "It shows whether the network and firewalls let the reader through and whether something is listening; it sends no data."),
      targetsList,
      h("div", { class: "form-grid" },
        h("label", { class: "field" }, h("span", { class: "field-label" }, "Host"), host),
        h("label", { class: "field" }, h("span", { class: "field-label" }, "Port"), port),
        h("div", { class: "field" }, h("span", { class: "field-label" }, " "),
          h("label", { class: "radio" }, tls, h("span", {}, "Also check TLS (for 8883, 443)")))),
      h("div", { class: "field" }, h("span", { class: "field-label" }, "Shortcuts"), chips),
      h("div", { class: "actions", style: { marginTop: "10px" } }, testBtn),
      portOut);
    drawPortResults();
  }
  function drawPortResults() {
    fill(portOut, state.portResults.map((r) => {
      const cls = !r.ok ? "bad" : r.trusted === false ? "warn" : "ok";
      const c = r.certificate;
      return h("div", { class: "notice " + cls, style: { marginTop: "10px" } },
        h("strong", {}, `${r.host}:${r.port}${r.tls ? " (TLS)" : ""} `), r.message,
        h("div", { class: "sub mono" }, [
          r.addresses ? `resolved: ${r.addresses.map((a) => a.address).join(", ")}${r.dns_ms !== undefined ? ` (${r.dns_ms} ms)` : ""}` : null,
          r.connect_ms !== undefined ? `connect ${r.connect_ms} ms` : null,
          r.tls_ms !== undefined ? `TLS ${r.tls_version} ${r.tls_ms} ms` : null].filter(Boolean).join(" · ")),
        c ? h("dl", { class: "kv", style: { marginTop: "6px" } },
          kv("Certificate", c.subject), kv("Issued by", c.issuer),
          kv("Expires", c.not_after ? `${c.not_after}${c.expires_in_days !== undefined ? ` (${c.expires_in_days} days)` : ""}` : null),
          c.san?.length ? kv("Names", c.san.join(", ")) : null,
          r.cert_problem ? kv("Problem", r.cert_problem) : null) : null);
    }));
  }

  // ---------- DNS lookup
  const dnsName = h("input", { type: "text", class: "mono", spellcheck: "false", placeholder: "time.nist.gov" });
  const dnsBtn = h("button", { type: "button", class: "btn" }, "Look up");
  const dnsOut = h("div");
  const runDns = async () => {
    dnsBtn.disabled = true;
    try {
      const r = await api.netDns(dnsName.value);
      fill(dnsOut, h("div", { class: "notice " + (r.ok ? "ok" : "bad"), style: { marginTop: "10px" } }, r.message,
        r.servers?.length ? h("div", { class: "sub" }, "Asked: ", r.servers.join(", ")) : null));
    } catch (err) { fill(dnsOut, h("div", { class: "notice bad" }, errText(err))); }
    finally { dnsBtn.disabled = false; }
  };
  dnsBtn.addEventListener("click", runDns);
  dnsName.addEventListener("keydown", (e) => { if (e.key === "Enter") runDns(); });
  fill(dnsCard, h("h2", {}, "DNS lookup"),
    h("p", { class: "hint" }, "Turns a host name into an address using the reader's DNS servers."),
    h("div", { class: "inline-test" }, h("label", { class: "field" }, dnsName), dnsBtn), dnsOut);

  // ---------- ping
  const pingHost = h("input", { type: "text", class: "mono", spellcheck: "false", placeholder: "192.168.0.1" });
  const pingBtn = h("button", { type: "button", class: "btn" }, "Ping");
  const pingOut = h("div");
  const runPing = async () => {
    pingBtn.disabled = true;
    fill(pingOut, h("p", { class: "muted" }, "Pinging…"));
    try {
      const r = await api.netPing(pingHost.value);
      fill(pingOut, h("div", { class: "notice " + (r.ok ? "ok" : r.available ? "warn" : "neutral"), style: { marginTop: "10px" } }, r.message,
        r.output ? h("details", {}, h("summary", {}, "Full output"), h("pre", { class: "mono sample" }, r.output)) : null));
    } catch (err) { fill(pingOut, h("div", { class: "notice bad" }, errText(err))); }
    finally { pingBtn.disabled = false; }
  };
  pingBtn.addEventListener("click", runPing);
  pingHost.addEventListener("keydown", (e) => { if (e.key === "Enter") runPing(); });
  fill(pingCard, h("h2", {}, "Ping"),
    h("p", { class: "hint" }, "Sends 4 pings. Many servers and firewalls ignore ping, so no reply doesn't always mean a problem: the port test is the better check."),
    h("div", { class: "inline-test" }, h("label", { class: "field" }, pingHost), pingBtn), pingOut);

  function copy(text) {
    navigator.clipboard?.writeText(text).then(() => msg.show("Copied. Paste it into an email or support ticket."),
      () => msg.show("Copy isn't allowed here; select the text instead.", true));
  }

  loadStatus();
  return () => msg.dispose();
}

function kv(k, v) {
  return v === null || v === undefined || v === "" ? null : [h("dt", {}, k), h("dd", { class: "mono" }, String(v))];
}
