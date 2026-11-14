// Meridian app shell: session, role-filtered menu, hash router, breadcrumb, live event feed.
import { api, h } from "./api.js";
import { buildMenubar, markActive, indexMenu } from "./menu.js";
import * as dashboard from "./pages/dashboard.js";
import * as site from "./pages/site.js";
import * as usbDevices from "./pages/usb-devices.js";
import * as badgeReaders from "./pages/badge-readers.js";
import * as storagePage from "./pages/storage.js";
import * as debugLog from "./pages/debug-log.js";
import * as accounts from "./pages/accounts.js";
import * as about from "./pages/about.js";
import * as timeLocation from "./pages/time-location.js";
import * as mqttPage from "./pages/mqtt.js";
import * as rfidSetup from "./pages/rfid-setup.js";
import * as tagsPage from "./pages/tags.js";
import * as placeholder from "./pages/placeholder.js";

// Pages that have real implementations. Everything else in the menu gets the placeholder
// until it's built, so the whole navigation works from day one.
const PAGES = { "/dashboard": dashboard, "/site": site, "/hardware/usb": usbDevices,
                "/hardware/badges": badgeReaders, "/hardware/storage": storagePage,
                "/dev/debug-log": debugLog, "/system/accounts": accounts,
                "/system/about": about, "/system/time": timeLocation,
                "/integrations/mqtt": mqttPage, "/hardware/rfid": rfidSetup,
                "/operations/tags": tagsPage };

const $ = (id) => document.getElementById(id);
const ctx = { session: null, menuIndex: new Map(), events: null };
let cleanup = null;

// ---------- live events (one shared connection for every page)
class LiveFeed {
  constructor() { this.subs = new Set(); this.es = null; this.history = []; this.seen = new Set(); }
  start() {
    this.stop();
    this.es = new EventSource("api/events");
    const conn = $("conn"), label = conn.querySelector(".conn-label");
    this.es.onopen = () => { conn.className = "conn live"; label.textContent = "Live"; conn.title = "Live connection"; };
    this.es.onerror = () => { conn.className = "conn down"; label.textContent = "Reconnecting"; conn.title = "Reconnecting…"; };
    this.es.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch { return; }
      if (this.seen.has(ev.id)) return;              // already have it (e.g. replay after a reconnect)
      this.seen.add(ev.id);
      this.history.push(ev);
      if (this.history.length > 100) this.seen.delete(this.history.shift().id);
      this.subs.forEach((fn) => fn(ev));
    };
  }
  stop() {
    if (this.es) this.es.close();
    this.es = null;
    const conn = $("conn");
    conn.className = "conn"; conn.querySelector(".conn-label").textContent = "Offline";
  }
  // subscribe(fn, { history: true }) first replays what this browser already received
  subscribe(fn, opts = {}) {
    if (opts.history) this.history.forEach((ev) => fn({ ...ev, replay: true }));
    this.subs.add(fn);
    return () => this.subs.delete(fn);
  }
}
ctx.events = new LiveFeed();

// ---------- session + menu
async function loadSession() {
  const [session, menu] = await Promise.all([api.session(), api.menu()]);
  ctx.session = session;
  ctx.menus = menu.menus;
  ctx.menuIndex = indexMenu(menu.menus);

  $("reader-name").textContent = session.reader.name;
  $("role-badge").textContent = `${session.user} · ${session.role_label}`;
  $("role-badge").title = `Signed in as ${session.user} (${session.role_label})`;
  buildMenubar($("mainnav"), menu.menus);

  $("pw-strip").hidden = !session.must_change_password;

  const sw = $("dev-switch");
  if (session.dev_mode) {
    const sel = $("role-select");
    sel.replaceChildren(...session.roles.map((r) =>
      h("option", { value: r.id, selected: r.id === session.role, title: r.description }, r.label)));
    sw.hidden = false;
  } else sw.hidden = true;

  if (session.permissions.includes("dashboard.view")) ctx.events.start();
  else ctx.events.stop();
}

function firstRoute() {
  const first = ctx.menus[0];
  if (!first) return null;
  return first.route || first.groups[0].items[0].route;
}

// ---------- router
async function route() {
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  const page = $("page");
  let path = location.hash.replace(/^#/, "");

  if (!ctx.menus.length) {   // e.g. Operator: kiosk only
    renderCrumbs([]);
    page.replaceChildren(h("div", { class: "notice" },
      h("h1", {}, "No access to configuration"),
      h("p", {}, `The ${ctx.session.role_label} role uses the kiosk only. Ask a site admin if you need configuration access.`)));
    return;
  }
  if (!path) { location.replace("#" + firstRoute()); return; }

  markActive($("mainnav"), path);
  const entry = ctx.menuIndex.get(path);
  renderCrumbs(entry ? entry.trail : [path]);

  // The server decides, even if someone types a hidden page into the address bar.
  let perm = null;
  try {
    perm = (await api.routeCheck(path)).perm;
  } catch (err) {
    const forbidden = err.status === 403;
    page.replaceChildren(h("div", { class: "notice bad" },
      h("h1", {}, forbidden ? "No access" : "Page not found"),
      h("p", {}, forbidden
        ? ["Your role (", ctx.session.role_label, ") doesn't include ", h("code", {}, err.body?.needs || "this permission"), "."]
        : ["There's no page at ", h("code", {}, path), "."]),
      h("p", {}, h("a", { href: "#" + firstRoute() }, "Go to your start page"))));
    return;
  }

  const mod = PAGES[path] || placeholder;
  page.replaceChildren();
  cleanup = mod.render(page, { ...ctx, path, perm, title: entry ? entry.label : path, trail: entry ? entry.trail : [] }) || null;
  page.focus({ preventScroll: true });
}

function renderCrumbs(trail) {
  $("breadcrumb").replaceChildren(...trail.map((t) => h("li", {}, t)));
}

// ---------- boot
async function boot() {
  try {
    await loadSession();
  } catch (err) {
    if (err.status === 401) return;            // already on the way to the sign-in page
    $("page").replaceChildren(h("div", { class: "notice bad" },
      h("h1", {}, "Can't reach the reader"),
      h("p", {}, String(err.message || err))));
    return;
  }
  $("role-select").addEventListener("change", async (e) => {
    await api.setRole(e.target.value);   // mock server only
    await loadSession();
    route();
  });
  $("pw-open").addEventListener("click", openPasswordDialog);
  $("signout").addEventListener("click", async () => {
    ctx.events.stop();
    try { await api.logout(); } catch { /* going to the sign-in page either way */ }
    location.replace("login.html");
  });
  window.addEventListener("hashchange", route);
  route();
}

// Change-password dialog. You stay signed in; other sessions for your account are signed out.
function openPasswordDialog() {
  const cur = h("input", { type: "password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", autocomplete: "new-password" });
  const again = h("input", { type: "password", autocomplete: "new-password" });
  const err = h("p", { class: "bad-text", hidden: true });
  const go = h("button", { type: "button", class: "btn btn-primary" }, "Change password");
  const dlg = h("dialog", { class: "dialog" },
    h("h2", {}, "Change password"),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Current password"), cur),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "New password"), nw,
      h("span", { class: "field-help" }, "At least 8 characters.")),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "New password again"), again),
    err,
    h("div", { class: "dialog-actions" },
      h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), go));
  dlg.addEventListener("close", () => dlg.remove());
  go.addEventListener("click", async () => {
    err.hidden = true;
    if (nw.value !== again.value) { err.textContent = "The new passwords don't match."; err.hidden = false; return; }
    try {
      await api.changePassword(cur.value, nw.value);
      dlg.replaceChildren(h("h2", {}, "Password changed"),
        h("p", {}, "You're still signed in here. Any other sessions for your account have been signed out."),
        h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn btn-primary", onclick: () => dlg.close() }, "Done")));
      $("pw-strip").hidden = true;
    } catch (e) {
      err.textContent = e.message; err.hidden = false;
    }
  });
  document.body.append(dlg);
  dlg.showModal();
  cur.focus();
}

boot();
