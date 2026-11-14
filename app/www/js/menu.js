// Top menu bar: dropdowns open on hover (mouse) AND on click/tap, with keyboard support.
// Two levels only; the second level is a grouped panel, so there are no cascading flyouts
// and no diagonal-mouse problem.
import { h } from "./api.js";

const OPEN_DELAY = 150;   // ms of hover before a closed menu opens (avoids accidental opens)
const CLOSE_DELAY = 300;  // ms after the pointer leaves before the menu closes (forgives sloppy mousing)

export function buildMenubar(nav, menus) {
  nav.replaceChildren();
  const bar = h("ul", { class: "menubar", role: "menubar" });
  let openLi = null, openTimer = 0, closeTimer = 0;

  const tops = [];

  function open(li, how) {
    if (openLi && openLi !== li) close(openLi);
    if (!li.querySelector(".mb-panel")) return;
    li.classList.add("open");
    li._openedBy = how;
    li.querySelector(".mb-top").setAttribute("aria-expanded", "true");
    openLi = li;
    keepOnScreen(li.querySelector(".mb-panel"));
  }
  function close(li) {
    if (!li) return;
    li.classList.remove("open");
    const top = li.querySelector(".mb-top");
    if (top.hasAttribute("aria-expanded")) top.setAttribute("aria-expanded", "false");
    if (openLi === li) openLi = null;
  }
  function closeAll() { clearTimeout(openTimer); clearTimeout(closeTimer); close(openLi); }
  function links(li) { return [...li.querySelectorAll(".mb-link")]; }

  menus.forEach((m, idx) => {
    const li = h("li", { class: "mb-item", role: "none", "data-menu": m.id });
    let top;
    if (m.route) {
      top = h("a", { class: "mb-top", role: "menuitem", href: "#" + m.route, "data-route": m.route }, m.label);
      li.append(top);
    } else {
      const panelId = "mb-panel-" + m.id;
      top = h("button", { class: "mb-top", role: "menuitem", type: "button",
                          "aria-haspopup": "true", "aria-expanded": "false", "aria-controls": panelId },
              m.label, h("span", { class: "mb-caret", "aria-hidden": "true" }));
      const panel = h("div", { class: "mb-panel", role: "menu", id: panelId, "aria-label": m.label },
        m.groups.map(g => h("div", { class: "mb-group", role: "group", "aria-label": g.label },
          h("div", { class: "mb-group-label", "aria-hidden": "true" }, g.label),
          g.items.map(it => h("a", { class: "mb-link", role: "menuitem", href: "#" + it.route,
                                     "data-route": it.route, tabindex: "-1" }, it.label)))));
      li.append(top, panel);

      // --- mouse hover (pointerType check keeps touch from triggering hover logic)
      li.addEventListener("pointerenter", e => {
        if (e.pointerType !== "mouse") return;
        clearTimeout(closeTimer);
        if (openLi && openLi !== li) open(li, "hover");             // already browsing: switch instantly
        else if (!openLi) openTimer = setTimeout(() => open(li, "hover"), OPEN_DELAY);
      });
      li.addEventListener("pointerleave", e => {
        if (e.pointerType !== "mouse") return;
        clearTimeout(openTimer);
        closeTimer = setTimeout(() => close(li), CLOSE_DELAY);
      });

      // --- click / tap toggles. If hover just opened it, a click "pins" it instead of closing.
      top.addEventListener("click", e => {
        clearTimeout(openTimer);
        if (openLi === li) {
          if (li._openedBy === "hover" && e.pointerType === "mouse") { li._openedBy = "click"; return; }
          close(li);
        } else {
          open(li, "click");
        }
      });

      // --- keyboard inside the panel
      panel.addEventListener("keydown", e => {
        const ls = links(li), i = ls.indexOf(document.activeElement);
        if (e.key === "ArrowDown") { e.preventDefault(); ls[(i + 1) % ls.length].focus(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); ls[(i - 1 + ls.length) % ls.length].focus(); }
        else if (e.key === "Escape") { e.preventDefault(); close(li); top.focus(); }
        else if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); moveTop(idx, e.key === "ArrowRight" ? 1 : -1, true); }
        else if (e.key === "Home") { e.preventDefault(); ls[0].focus(); }
        else if (e.key === "End") { e.preventDefault(); ls[ls.length - 1].focus(); }
      });
    }

    // --- keyboard on the top-level item
    top.addEventListener("keydown", e => {
      if (e.key === "ArrowRight") { e.preventDefault(); moveTop(idx, 1, !!openLi); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); moveTop(idx, -1, !!openLi); }
      else if (e.key === "ArrowDown" && !m.route) { e.preventDefault(); open(li, "key"); links(li)[0]?.focus(); }
      else if (e.key === "Escape") { closeAll(); }
    });

    tops.push(li);
    bar.append(li);
  });

  function moveTop(idx, dir, openIt) {
    const next = tops[(idx + dir + tops.length) % tops.length];
    closeAll();
    const t = next.querySelector(".mb-top");
    t.focus();
    if (openIt && next.querySelector(".mb-panel")) { open(next, "key"); links(next)[0]?.focus(); }
  }

  // choosing a page closes the menu
  bar.addEventListener("click", e => { if (e.target.closest(".mb-link")) closeAll(); });
  // click/tap outside, or focus leaving the bar, closes it
  document.addEventListener("pointerdown", e => { if (!nav.contains(e.target)) closeAll(); });
  nav.addEventListener("focusout", e => { if (!nav.contains(e.relatedTarget)) closeAll(); });

  nav.append(bar);
}

// Keep a dropdown inside the window when its menu is near the right edge.
function keepOnScreen(panel) {
  panel.style.left = "0px";
  const r = panel.getBoundingClientRect();
  const over = r.right - (window.innerWidth - 8);
  if (over > 0) panel.style.left = -over + "px";
}

// Mark the current page in the bar and in its dropdown.
export function markActive(nav, route) {
  nav.querySelectorAll(".mb-item").forEach(li => {
    const hit = [...li.querySelectorAll("[data-route]")].some(a => a.dataset.route === route);
    li.classList.toggle("active", hit);
  });
  nav.querySelectorAll("[data-route]").forEach(a => {
    if (a.dataset.route === route) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

// Flatten the menu into route -> {label, trail} for breadcrumbs and lookups.
export function indexMenu(menus) {
  const idx = new Map();
  for (const m of menus) {
    if (m.route) idx.set(m.route, { label: m.label, trail: [m.label] });
    for (const g of m.groups || []) for (const it of g.items)
      idx.set(it.route, { label: it.label, trail: [m.label, g.label, it.label] });
  }
  return idx;
}
