"""System › Accounts API, shared by the reader server and the mock server.

    GET    /api/accounts                          list + roles the caller may assign
    POST   /api/accounts                          {user, name, role, password?}  (no password -> generated)
    PUT    /api/accounts/<user>                   {name?, role?, disabled?}
    POST   /api/accounts/<user>/reset-password    {password?}                    (no password -> generated)
    DELETE /api/accounts/<user>

The caller must hold accounts.manage (checked by the server before calling handle()).
Extra rules: only a Developer may assign the developer role or touch a Developer account;
nobody may disable, delete or change the role of their own account; at least one enabled
account able to manage accounts must remain (enforced in core.config).
"""
from urllib.parse import unquote

from core.config import AccountError, generate_password

PROTECTED_ROLE = "developer"


def handle(method, path, body, actor, store, access, sessions):
    """Returns (status, response_dict). `actor` is the signed-in account dict."""
    roles = set(access.roles)
    managers = {r for r in roles if access.can(r, "accounts.manage")}
    actor_dev = actor["role"] == PROTECTED_ROLE
    parts = [unquote(p) for p in path[len("/api/accounts"):].strip("/").split("/") if p]

    def assignable(role):
        return role in roles and (role != PROTECTED_ROLE or actor_dev)

    def target(user):
        rec = next((a for a in store.list_accounts() if a["user"] == user), None)
        if not rec:
            raise AccountError("No such account.", 404)
        if rec["role"] == PROTECTED_ROLE and not actor_dev and user != actor["user"]:
            raise AccountError("Only a Developer can change a Developer account.", 403)
        return rec

    try:
        if method == "GET" and not parts:
            accounts = []
            for a in store.list_accounts():
                accounts.append(dict(a, role_label=access.roles.get(a["role"], {}).get("label", a["role"]),
                                     sessions=sessions.count_user(a["user"]) if sessions else 0,
                                     you=a["user"] == actor["user"],
                                     editable=a["role"] != PROTECTED_ROLE or actor_dev or a["user"] == actor["user"]))
            return 200, {"accounts": accounts, "me": actor["user"],
                         "roles": [{"id": k, "label": v["label"], "description": v["description"],
                                    "assignable": assignable(k)} for k, v in access.roles.items()]}

        if method == "POST" and not parts:
            role = body.get("role")
            if not assignable(role):
                raise AccountError("You can't assign that role.", 403)
            generated = not body.get("password")
            pw = generate_password() if generated else body.get("password")
            store.create_account(body.get("user"), body.get("name"), role, pw, roles)
            return 201, {"ok": True, "temporary_password": pw if generated else None}

        if len(parts) == 1 and method == "PUT":
            user = parts[0]
            target(user)
            changes = {k: body[k] for k in ("name", "role", "disabled") if k in body}
            if "role" in changes and not assignable(changes["role"]):
                raise AccountError("You can't assign that role.", 403)
            if store.update_account(user, changes, roles, managers, actor["user"]) and sessions:
                sessions.drop_user(user)
            return 200, {"ok": True}

        if len(parts) == 2 and parts[1] == "reset-password" and method == "POST":
            user = parts[0]
            target(user)
            if user == actor["user"]:
                raise AccountError("Use “Change password” for your own account.")
            generated = not body.get("password")
            pw = store.reset_password(user, body.get("password") or "")
            if sessions:
                sessions.drop_user(user)
            return 200, {"ok": True, "temporary_password": pw if generated else None}

        if len(parts) == 1 and method == "DELETE":
            user = parts[0]
            target(user)
            store.delete_account(user, managers, actor["user"])
            if sessions:
                sessions.drop_user(user)
            return 200, {"ok": True}
    except AccountError as e:
        return e.status, {"error": "invalid" if e.status == 400 else "forbidden" if e.status == 403 else "not_found",
                          "details": [str(e)]}
    return 404, {"error": "not_found"}
