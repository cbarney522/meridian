// Thin wrapper around the Meridian REST API. Relative URLs only, so the same code works
// against the mock server (localhost:8080) and the reader (https://reader:8443).

export class ApiError extends Error {
  constructor(status, body) {
    super(body && body.details ? body.details.join(" ") : body && body.error ? body.error : `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request(method, path, body, raw) {
  const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (raw) {                                  // file upload: send the file as-is
    opts.headers["Content-Type"] = raw.type || "text/csv";
    if (raw.name) opts.headers["X-File-Name"] = encodeURIComponent(raw.name);
    opts.body = raw.body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {                   // signed out or session expired: go to the sign-in page
    const back = location.hash.replace(/^#/, "");
    location.replace("login.html" + (back ? "#" + encodeURIComponent(back) : ""));
    throw new ApiError(401, { error: "not_signed_in" });
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const api = {
  session: () => request("GET", "api/session"),
  menu: () => request("GET", "api/menu"),
  status: () => request("GET", "api/status"),
  siteReaders: () => request("GET", "api/site/readers"),
  usbDevices: () => request("GET", "api/usb/devices"),
  saveUsbAssignments: (revision, assignments) => request("PUT", "api/usb/assignments", { revision, assignments }),
  devUsbToggle: (port) => request("POST", "api/dev/usb/toggle", { port }),
  badges: () => request("GET", "api/badges"),
  saveBadgeSettings: (revision, settings) => request("PUT", "api/badges/settings", { revision, settings }),
  previewBadge: (raw, settings) => request("POST", "api/badges/preview", { raw, settings }),
  devSwipe: (port, garbled = false) => request("POST", "api/dev/badge/swipe", { port, garbled }),
  storage: () => request("GET", "api/storage"),
  readerInfo: () => request("GET", "api/reader/info"),
  time: () => request("GET", "api/time"),
  mqtt: () => request("GET", "api/mqtt"),
  dataTables: () => request("GET", "api/data/tables"),
  dataRows: (id, params) => request("GET", `api/data/tables/${encodeURIComponent(id)}/rows?` + new URLSearchParams(params)),
  dataPreview: (id, file) => request("POST", `api/data/tables/${encodeURIComponent(id)}/preview`, undefined, { body: file, name: file.name, type: "text/csv" }),
  dataImport: (id, file) => request("POST", `api/data/tables/${encodeURIComponent(id)}/import`, undefined, { body: file, name: file.name, type: "text/csv" }),
  dataClear: (id) => request("DELETE", `api/data/tables/${encodeURIComponent(id)}`),
  sqliteObjects: () => request("GET", "api/data/sqlite"),
  sqliteRows: (name, params) => request("GET", `api/data/sqlite/${encodeURIComponent(name)}/rows?` + new URLSearchParams(params)),
  sqliteSchema: (name) => request("GET", `api/data/sqlite/${encodeURIComponent(name)}/schema`),
  sqliteQuery: (sql) => request("POST", "api/data/sqlite/query", { sql }),
  sqliteIntegrity: () => request("POST", "api/data/sqlite/integrity", {}),
  sqliteExportUrl: (name) => `api/data/sqlite/${encodeURIComponent(name)}/export.csv`,
  rfid: (refresh) => request("GET", "api/rfid" + (refresh ? "?refresh=1" : "")),
  saveRfid: (revision, settings) => request("PUT", "api/rfid", { revision, settings }),
  rfidStart: (preset) => request("POST", "api/rfid/start", { preset }),
  rfidStop: () => request("POST", "api/rfid/stop", {}),
  rfidPreset: (id) => request("GET", "api/rfid/preset?id=" + encodeURIComponent(id)),
  saveMqtt: (revision, settings) => request("PUT", "api/mqtt", { revision, settings }),
  testMqtt: (settings) => request("POST", "api/mqtt/test", { settings }),
  netStatus: () => request("GET", "api/net/status"),
  netChecks: () => request("POST", "api/net/checks", {}),
  netPort: (host, port, tls) => request("POST", "api/net/port", { host, port, tls }),
  netDns: (name) => request("POST", "api/net/dns", { name }),
  netPing: (host) => request("POST", "api/net/ping", { host }),
  webhooks: () => request("GET", "api/webhooks"),
  saveWebhooks: (revision, settings) => request("PUT", "api/webhooks", { revision, settings }),
  webhookDeliveries: (endpoint) => request("GET", "api/webhooks/deliveries" + (endpoint ? `?endpoint=${encodeURIComponent(endpoint)}` : "")),
  testWebhook: (endpoint) => request("POST", "api/webhooks/test", { endpoint }),
  resendWebhook: (delivery) => request("POST", "api/webhooks/resend", { delivery }),
  retryWebhook: (endpoint) => request("POST", "api/webhooks/retry", { endpoint }),
  siteSettings: () => request("GET", "api/site-settings"),
  saveSiteSettings: (revision, settings) => request("PUT", "api/site-settings", { revision, settings }),
  readerApiSettings: () => request("GET", "api/reader/api-settings"),
  saveReaderApiSettings: (body) => request("PUT", "api/reader/api-settings", body),
  testReaderApi: (body) => request("POST", "api/reader/api-test", body),
  browseDrive: (scope, path) => request("GET", `api/storage/browse?scope=${encodeURIComponent(scope)}&path=${encodeURIComponent(path)}`),
  saveStorageSettings: (revision, settings) => request("PUT", "api/storage/settings", { revision, settings }),
  storageAction: (action, confirm) => request("POST", "api/storage/" + action, confirm ? { confirm } : {}),
  devStorageFs: () => request("POST", "api/dev/storage/fs", {}),
  diagnostics: () => request("GET", "api/dev/diagnostics"),
  routeCheck: (route) => request("GET", "api/route-check?route=" + encodeURIComponent(route)),
  setRole: (role) => request("POST", "api/dev/role", { role }),
  changePassword: (current, next) => request("POST", "api/account/password", { current, new: next }),
  logout: () => request("POST", "api/logout", {}),
  accounts: () => request("GET", "api/accounts"),
  createAccount: (acct) => request("POST", "api/accounts", acct),
  updateAccount: (user, changes) => request("PUT", "api/accounts/" + encodeURIComponent(user), changes),
  resetAccountPassword: (user) => request("POST", "api/accounts/" + encodeURIComponent(user) + "/reset-password", {}),
  deleteAccount: (user) => request("DELETE", "api/accounts/" + encodeURIComponent(user)),
};

// Small DOM builder. Text always goes through textContent, never innerHTML,
// so data from the reader (tag names, user names, PO numbers) can't inject markup.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v); // CSSOM: allowed by our CSP, unlike style="" attributes
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Replace an element's children, with the same rules as h(): arrays are flattened and
// null/undefined/false are skipped (plain replaceChildren() would print "null").
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
  return el;
}
