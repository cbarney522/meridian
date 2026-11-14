// System › Accounts: who can sign in to this reader and with which role.
import { api, h, fill } from "../api.js";
import { toast, errText } from "../ui.js";

export function render(root) {
  const state = { data: null };
  const msg = toast();
  const tbody = h("tbody");
  const addBtn = h("button", { type: "button", class: "btn btn-primary" }, "Add account");

  root.append(
    h("div", { class: "toolbar" },
      h("div", {}, h("h1", {}, "Accounts"),
        h("p", { class: "subtitle" }, "People who can sign in to this reader's Meridian UI. Operators who only badge in at the kiosk don't need an account here.")),
      addBtn),
    msg.el,
    h("div", { class: "card table-card" },
      h("table", { class: "table accounts-table" },
        h("thead", {}, h("tr", {}, ["User", "Role", "Status", "Last sign-in", "Signed in now", ""].map((t) =>
          h("th", { class: t === "Signed in now" ? "num" : null }, t)))),
        tbody)),
    h("p", { class: "hint", style: { marginTop: "12px" } },
      "New accounts and reset passwords are temporary: the person must choose their own password at first sign-in. ",
      "Disabling, deleting or resetting an account signs that person out immediately."),
  );
  addBtn.addEventListener("click", () => addDialog());

  async function load() {
    try {
      state.data = await api.accounts();
      draw();
    } catch (err) {
      fill(tbody, h("tr", {}, h("td", { colspan: 6, class: "empty" }, "Can't load accounts: ", errText(err))));
    }
  }

  function roleLabel(id) { return (state.data.roles.find((r) => r.id === id) || { label: id }).label; }

  function draw() {
    const { accounts, roles } = state.data;
    fill(tbody, accounts.map((a) => {
      const canEdit = a.editable && !a.you;
      const roleCell = canEdit
        ? (() => {
            const sel = h("select", { class: "role-select", "aria-label": `Role for ${a.user}` },
              roles.filter((r) => r.assignable || r.id === a.role).map((r) =>
                h("option", { value: r.id, selected: r.id === a.role, disabled: !r.assignable, title: r.description }, r.label)));
            sel.addEventListener("change", () => update(a, { role: sel.value }, `${a.user} is now ${roleLabel(sel.value)}.`, () => { sel.value = a.role; }));
            return sel;
          })()
        : a.role_label;
      return h("tr", { class: a.disabled ? "row-off" : null },
        h("td", {}, h("div", { class: "dev-name" }, a.user, a.you ? h("span", { class: "tag" }, "you") : null),
          a.name ? h("div", { class: "sub" }, a.name) : null),
        h("td", {}, roleCell),
        h("td", {},
          a.disabled ? h("span", { class: "pill bad" }, "Disabled") : h("span", { class: "pill ok" }, "Active"),
          a.must_change && !a.disabled ? h("span", { class: "pill warn", style: { marginLeft: "6px" } }, "Must change password") : null),
        h("td", {}, a.last_login ? when(a.last_login) : h("span", { class: "muted" }, "Never")),
        h("td", { class: "num" }, a.sessions ? String(a.sessions) : h("span", { class: "muted" }, "—")),
        h("td", { class: "row-actions" }, canEdit ? [
          h("button", { type: "button", class: "btn btn-small", onclick: () => resetDialog(a) }, "Reset password"),
          h("button", { type: "button", class: "btn btn-small", onclick: () =>
            update(a, { disabled: !a.disabled }, `${a.user} ${a.disabled ? "enabled" : "disabled"}.`) }, a.disabled ? "Enable" : "Disable"),
          h("button", { type: "button", class: "btn btn-small btn-danger", onclick: () => deleteDialog(a) }, "Delete"),
        ] : a.you ? h("span", { class: "muted" }, "Use “Change password” for your own") : h("span", { class: "muted" }, "Developer account")),
      );
    }));
  }

  async function update(a, changes, okText, revert) {
    try {
      await api.updateAccount(a.user, changes);
      msg.show(okText);
    } catch (err) {
      msg.show(errText(err), true);
      if (revert) revert();
    }
    load();
  }

  // ---------- dialogs
  function dialog(title, ...content) {
    const dlg = h("dialog", { class: "dialog" }, h("h2", {}, title), ...content);
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
    return dlg;
  }

  function showPassword(dlg, user, pw, intro) {
    const box = h("input", { type: "text", class: "mono temp-pw", value: pw, readonly: true, "aria-label": "Temporary password" });
    const copy = h("button", { type: "button", class: "btn" }, "Copy");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(pw); copy.textContent = "Copied"; }
      catch { box.select(); copy.textContent = "Press Ctrl+C"; }
    });
    dlg.replaceChildren(h("h2", {}, "Temporary password"),
      h("p", {}, intro, " Give it to ", h("strong", {}, user), " securely. It's shown only once; they'll choose their own password at first sign-in."),
      h("div", { class: "pw-row" }, box, copy),
      h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn btn-primary", onclick: () => dlg.close() }, "Done")));
    box.select();
  }

  function addDialog() {
    const user = h("input", { type: "text", autocomplete: "off", spellcheck: "false", autocapitalize: "none", maxlength: "32" });
    const name = h("input", { type: "text", autocomplete: "off", maxlength: "64" });
    const role = h("select", {}, state.data.roles.filter((r) => r.assignable).map((r) =>
      h("option", { value: r.id, selected: r.id === "technician" }, r.label)));
    const roleHelp = h("span", { class: "field-help" });
    const showHelp = () => { roleHelp.textContent = (state.data.roles.find((r) => r.id === role.value) || {}).description || ""; };
    role.addEventListener("change", showHelp); showHelp();
    const gen = h("input", { type: "radio", name: "pwmode", checked: true });
    const own = h("input", { type: "radio", name: "pwmode" });
    const pw = h("input", { type: "password", autocomplete: "new-password", disabled: true, placeholder: "At least 8 characters" });
    [gen, own].forEach((r) => r.addEventListener("change", () => { pw.disabled = gen.checked; if (own.checked) pw.focus(); }));
    const err = h("p", { class: "bad-text", hidden: true });
    const go = h("button", { type: "button", class: "btn btn-primary" }, "Add account");
    const dlg = dialog("Add account",
      h("label", { class: "field" }, h("span", { class: "field-label" }, "User name"), user,
        h("span", { class: "field-help" }, "Lower-case letters, digits, '.', '_' or '-'. Used to sign in.")),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Display name (optional)"), name),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Role"), role, roleHelp),
      h("div", { class: "field" }, h("span", { class: "field-label" }, "Password"),
        h("label", { class: "radio" }, gen, " Generate a temporary password"),
        h("label", { class: "radio" }, own, " Set one now"), pw),
      err,
      h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), go));
    user.focus();
    go.addEventListener("click", async () => {
      err.hidden = true;
      try {
        const r = await api.createAccount({ user: user.value.trim(), name: name.value, role: role.value,
                                            password: own.checked ? pw.value : "" });
        load();
        if (r.temporary_password) showPassword(dlg, user.value.trim(), r.temporary_password, "Account created.");
        else { dlg.close(); msg.show(`Account ${user.value.trim()} created. They'll be asked to choose a new password at first sign-in.`); }
      } catch (e) { err.textContent = errText(e); err.hidden = false; }
    });
  }

  function resetDialog(a) {
    const go = h("button", { type: "button", class: "btn btn-primary" }, "Reset password");
    const err = h("p", { class: "bad-text", hidden: true });
    const dlg = dialog(`Reset password for ${a.user}?`,
      h("p", {}, "A new temporary password will be generated. ", a.sessions ? `${a.user} is signed in now and will be signed out.` : ""),
      err,
      h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), go));
    go.addEventListener("click", async () => {
      try {
        const r = await api.resetAccountPassword(a.user);
        load();
        showPassword(dlg, a.user, r.temporary_password, "Password reset.");
      } catch (e) { err.textContent = errText(e); err.hidden = false; }
    });
  }

  function deleteDialog(a) {
    const go = h("button", { type: "button", class: "btn btn-danger" }, "Delete account");
    const err = h("p", { class: "bad-text", hidden: true });
    const dlg = dialog(`Delete ${a.user}?`,
      h("p", {}, "The account is removed and can't sign in. ", a.sessions ? "They'll be signed out now. " : "",
        "To keep it for later, disable it instead."),
      err,
      h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), go));
    go.addEventListener("click", async () => {
      try {
        await api.deleteAccount(a.user);
        dlg.close();
        msg.show(`Account ${a.user} deleted.`);
        load();
      } catch (e) { err.textContent = errText(e); err.hidden = false; }
    });
  }

  load();
  return () => msg.dispose();
}

function when(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const mins = Math.round((Date.now() - d) / 60000);
  const rel = mins < 1 ? "just now" : mins < 60 ? `${mins} min ago` : mins < 1440 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} d ago`;
  return h("span", { title: d.toLocaleString() }, rel);
}
