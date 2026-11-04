// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The controls of a table row that are used now and then, behind one button.
//
// A guest carries up to ten of them, and laid side by side they made the VMs
// table wider than any window: the last ones sat behind a horizontal scroll,
// and in the narrow column the undeclared guests are listed in, three buttons
// stacked into a row five lines tall. The row keeps the controls an operator
// reaches for, and the others open from a `⋯` at its end.
//
// The menu stays a child of the cell it belongs to and is only drawn above
// the page, with fixed coordinates. That keeps it inside the card, so that
// `Kept.hold` disables its items with the rest of the panel's controls while
// the panel shows a kept answer.
const RowMenu = (function () {
  let open = null;

  function close() {
    if (open) {
      open.menu.hidden = true;
      open.toggle.setAttribute("aria-expanded", "false");
      open = null;
    }
  }

  function place(toggle, menu) {
    const anchor = toggle.getBoundingClientRect();
    const size = menu.getBoundingClientRect();
    // Under the button, right edges aligned, and turned upwards where the
    // window has no room left below it.
    const left = Math.max(8, Math.min(anchor.right - size.width, window.innerWidth - size.width - 8));
    const below = anchor.bottom + 4;
    const top =
      below + size.height > window.innerHeight - 8
        ? Math.max(8, anchor.top - size.height - 4)
        : below;
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }

  // `items` are the controls themselves, built by the page with their own
  // listeners: a click on one runs it, and closes the menu.
  function build(items, label) {
    const box = document.createElement("span");
    box.className = "row-menu";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "secondary row-menu-toggle";
    toggle.textContent = "⋯";
    toggle.title = label || "More";
    toggle.setAttribute("aria-label", label || "More");
    toggle.setAttribute("aria-haspopup", "menu");
    toggle.setAttribute("aria-expanded", "false");
    const menu = document.createElement("div");
    menu.className = "check-menu row-menu-list";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    items.forEach((item) => {
      item.setAttribute("role", "menuitem");
      item.classList.remove("secondary");
      item.addEventListener("click", close);
      menu.append(item);
    });
    toggle.addEventListener("click", () => {
      const was = open && open.menu === menu;
      close();
      if (was) {
        return;
      }
      menu.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      place(toggle, menu);
      open = { toggle, menu };
      const first = menu.querySelector("button:not([disabled]), a");
      if (first) {
        first.focus({ preventScroll: true });
      }
    });
    box.append(toggle, menu);
    return box;
  }

  document.addEventListener("mousedown", (event) => {
    if (open && !open.menu.contains(event.target) && !open.toggle.contains(event.target)) {
      close();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && open) {
      const toggle = open.toggle;
      close();
      toggle.focus();
    }
  });
  // Fixed coordinates are the window's, so a scroll would leave the menu
  // floating away from its row.
  window.addEventListener("scroll", close, true);
  window.addEventListener("resize", close);

  return { build, close };
})();
