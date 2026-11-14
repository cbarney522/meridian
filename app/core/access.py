"""Role, permission and menu logic shared by the mock server and (later) the real CAP server.

Standard library only. Nothing here is mock-specific: the real server.py should import
this module unchanged, so the rules the UI is designed against are the rules the reader enforces.
"""
import json
import os

DEFS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "defs")


def _load(name):
    with open(os.path.join(DEFS_DIR, name), encoding="utf-8") as f:
        return json.load(f)


class Access:
    def __init__(self):
        self.reload()

    def reload(self):
        roles_doc = _load("roles.json")
        self.all_permissions = roles_doc["permissions"]
        self.roles = roles_doc["roles"]
        self.menu = _load("menu.json")["menus"]
        # route -> required permission, used to guard pages server-side
        self.route_perms = {}
        for top in self.menu:
            if "route" in top:
                self.route_perms[top["route"]] = top["perm"]
            for group in top.get("groups", []):
                for item in group["items"]:
                    self.route_perms[item["route"]] = item["perm"]

    def permissions_for(self, role):
        r = self.roles.get(role)
        if r is None:
            return set()
        perms = r["permissions"]
        if "*" in perms:
            return set(self.all_permissions)
        return {p for p in perms if p in self.all_permissions}

    def can(self, role, perm):
        return perm in self.permissions_for(role)

    def can_route(self, role, route):
        perm = self.route_perms.get(route)
        return perm is not None and self.can(role, perm)

    def menu_for(self, role):
        """Return the menu with everything the role can't see removed.
        Empty groups and empty top-level menus are dropped entirely (hidden, not greyed)."""
        perms = self.permissions_for(role)
        out = []
        for top in self.menu:
            if "route" in top:
                if top["perm"] in perms:
                    out.append({k: top[k] for k in ("id", "label", "route")})
                continue
            groups = []
            for group in top.get("groups", []):
                items = [{k: i[k] for k in ("id", "label", "route")}
                         for i in group["items"] if i["perm"] in perms]
                if items:
                    groups.append({"label": group["label"], "items": items})
            if groups:
                out.append({"id": top["id"], "label": top["label"], "groups": groups})
        return out
