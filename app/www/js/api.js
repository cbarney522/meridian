// Thin wrapper around the Meridian REST API. Relative URLs only, so the same code works
// against the mock server (localhost:8080) and the reader (https://reader:8443).

export class ApiError extends Error {
  constructor(status, body) {
    super(body && body.error ? body.error : `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request(method, path, body) {
  const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const api = {
  session: () => request("GET", "api/session"),
  menu: () => request("GET", "api/menu"),
  status: () => request("GET", "api/status"),
  routeCheck: (route) => request("GET", "api/route-check?route=" + encodeURIComponent(route)),
  setRole: (role) => request("POST", "api/dev/role", { role }),
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
