// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// Which of the two palettes the page is drawn in, and the switch that says so.
//
// The choice is one of three: light, dark, or follow the system. Only the
// first two are stored, and an absent key is the third, so the browser's own
// preference stays the default and a cleared storage returns to it.
//
// The palette itself lives in the stylesheet. What is here is the resolution
// of the choice into an attribute on the root element, plus the one event the
// two panels that paint with their own colours listen for: the console, which
// hands a theme to xterm.js, and the real time charts, whose series are drawn
// into an SVG rather than styled by a rule.

const Theme = (function () {
  // Shared with the resolver inlined in the head of every page, which runs
  // before the first paint and cannot wait for this file.
  const KEY = "seapath-theme";
  const EVENT = "themechange";

  const system = window.matchMedia("(prefers-color-scheme: light)");

  // A browser can refuse storage outright, in a private window or under a
  // policy. Refusing is not an error here: the preference is a convenience,
  // and the system answers the question when it is unavailable.
  function stored() {
    try {
      const value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "system";
    } catch (error) {
      return "system";
    }
  }

  function remember(choice) {
    try {
      if (choice === "system") {
        localStorage.removeItem(KEY);
      } else {
        localStorage.setItem(KEY, choice);
      }
    } catch (error) {
      /* The page still changes. Only the memory of it is lost. */
    }
  }

  function resolve(choice) {
    if (choice === "light" || choice === "dark") {
      return choice;
    }
    return system.matches ? "light" : "dark";
  }

  // The palette in force, which is what a caller painting its own colours
  // needs: `light` or `dark`, never `system`.
  function current() {
    return document.documentElement.dataset.theme === "light" ? "light" : "dark";
  }

  function apply(choice) {
    const theme = resolve(choice);
    const changed = theme !== current();
    document.documentElement.dataset.theme = theme;
    markSwitch(choice);
    if (changed) {
      window.dispatchEvent(new CustomEvent(EVENT, { detail: { theme } }));
    }
  }

  function buttons() {
    return Array.from(document.querySelectorAll("#theme [data-theme-choice]"));
  }

  // A radio group holds one tab stop, and it is the checked option: arrowing
  // moves between the three, tabbing leaves the group. Without this every
  // glyph is a stop of its own on the way to the sign out button.
  function markSwitch(choice) {
    buttons().forEach((button) => {
      const checked = button.dataset.themeChoice === choice;
      button.setAttribute("aria-checked", String(checked));
      button.tabIndex = checked ? 0 : -1;
    });
  }

  function mount() {
    const group = document.getElementById("theme");
    if (!group) {
      return;
    }
    buttons().forEach((button) => {
      button.addEventListener("click", () => {
        const choice = button.dataset.themeChoice;
        remember(choice);
        apply(choice);
      });
    });
    group.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      if (!(event.key in step)) {
        return;
      }
      event.preventDefault();
      const all = buttons();
      const at = all.findIndex((button) => button.tabIndex === 0);
      const next = all[(at + step[event.key] + all.length) % all.length];
      const choice = next.dataset.themeChoice;
      remember(choice);
      apply(choice);
      next.focus();
    });
    apply(stored());
  }

  // The system flipping under a page that is following it. A laptop on a
  // schedule does this in the middle of a run.
  system.addEventListener("change", () => {
    if (stored() === "system") {
      apply("system");
    }
  });

  mount();

  return { current, EVENT };
})();
