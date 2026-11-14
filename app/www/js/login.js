// Sign-in page. On success the server sets a session cookie and we go back to the page the
// user was trying to reach (passed as login.html#<route>).
const $ = (id) => document.getElementById(id);

async function info() {
  try {
    const r = await fetch("api/login-info", { headers: { Accept: "application/json" } });
    const d = await r.json();
    $("login-reader").textContent = d.reader;
    $("login-foot").textContent = `Meridian ${d.app_version}`;
  } catch { /* the form still works */ }
}

function target() {
  const next = decodeURIComponent(location.hash.replace(/^#/, ""));
  return next.startsWith("/") ? "./#" + next : "./";       // only in-app routes, never another site
}

async function submit(e) {
  e.preventDefault();
  const err = $("login-error"), btn = $("login-btn");
  err.hidden = true;
  const user = $("user").value.trim(), password = $("password").value;
  if (!user || !password) {
    err.textContent = "Enter your user name and password.";
    err.hidden = false;
    return;
  }
  btn.disabled = true;
  btn.textContent = "Signing in…";
  try {
    const res = await fetch("api/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ user, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      location.replace(target());
      return;
    }
    err.textContent = (data.details && data.details.join(" ")) || "Sign-in failed.";
    err.hidden = false;
    $("password").value = "";
    $("password").focus();
  } catch {
    err.textContent = "Can't reach the reader.";
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign in";
  }
}

$("login-form").addEventListener("submit", submit);
$("user").focus();
info();
