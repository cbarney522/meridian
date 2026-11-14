// Developer › Debug log: the reader's startup diagnostics (rw_dir/diag.txt) and the end of
// rw_dir/log/meridian.log, so nobody has to dig through the reader's debug-log download.
import { api, h, fill } from "../api.js";
import { errText } from "../ui.js";

export function render(root) {
  const diag = h("pre", { class: "logbox" }, "Loading…");
  const logBox = h("pre", { class: "logbox" });
  const listener = h("pre", { class: "logbox small" });
  const info = h("p", { class: "subtitle" });
  const refresh = h("button", { type: "button", class: "btn" }, "Refresh");
  const follow = h("input", { type: "checkbox", checked: true });

  root.append(
    h("div", { class: "toolbar" }, h("div", {}, h("h1", {}, "Debug log"), info),
      h("div", { class: "actions" }, h("label", { class: "field-check" }, follow, h("span", {}, "Auto-refresh")), refresh)),
    h("section", { class: "card" }, h("h2", {}, "Startup diagnostics (diag.txt)"),
      h("p", { class: "hint" }, "Written by the CAP's start script at every boot: what this reader lets the CAP access."), diag),
    h("section", { class: "card", style: { marginTop: "16px" } }, h("h2", {}, "Server log (last 64 KB of meridian.log)"), logBox),
    h("section", { class: "card", style: { marginTop: "16px" } }, h("h2", {}, "Badge listener"), listener),
  );

  async function load() {
    try {
      const d = await api.diagnostics();
      fill(info, `Meridian ${d.app_version} · data in ${d.data_dir}`);
      diag.textContent = d.diag;
      const atBottom = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 20;
      logBox.textContent = d.log || "(empty)";
      if (atBottom) logBox.scrollTop = logBox.scrollHeight;
      listener.textContent = d.badge_listener ? JSON.stringify(d.badge_listener, null, 2) : "(not running)";
    } catch (err) {
      diag.textContent = "Can't load diagnostics: " + errText(err);
    }
  }
  refresh.addEventListener("click", load);
  load();
  const timer = setInterval(() => { if (follow.checked) load(); }, 5000);
  return () => clearInterval(timer);
}
