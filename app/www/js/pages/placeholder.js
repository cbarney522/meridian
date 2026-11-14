// Stand-in for pages that aren't built yet. Shows where you are and which permission
// guards the page, so navigation and role filtering can be tested end to end.
import { h } from "../api.js";

export function render(root, ctx) {
  root.append(
    h("h1", {}, ctx.title),
    h("p", { class: "subtitle" }, ctx.trail.slice(0, -1).join(" › ")),
    h("div", { class: "notice" },
      h("p", {}, "This page isn't built yet."),
      h("p", {}, "Route ", h("code", {}, ctx.path), " · requires ", h("code", {}, ctx.perm || "?"),
        " · you are viewing as ", h("strong", {}, ctx.session.role_label), ".")),
  );
}
