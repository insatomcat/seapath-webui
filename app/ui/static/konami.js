// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The Konami code, typed on any signed in page, reveals the Bookmarks tab and
// opens it. Whether the tab is shown is a setting of this browser, read by the
// head of every page before the first paint, so it stays in the bar from one
// page to the next without blinking in.

const Konami = (function () {
  // Shared with the resolver inlined in the head of every page.
  const KEY = "seapath-bookmarks-tab";
  const SEQUENCE = [
    "ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown",
    "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight",
    "b", "a",
  ];
  let position = 0;

  function show(shown) {
    try {
      if (shown) {
        localStorage.setItem(KEY, "on");
      } else {
        localStorage.removeItem(KEY);
      }
    } catch (error) {
      /* The tab still changes on this page. Only the memory of it is lost. */
    }
    document.documentElement.dataset.bookmarks = shown ? "on" : "off";
  }

  // A key typed into a field, or into the console's terminal, belongs to
  // what it was typed into: arrows there move a cursor or walk a history.
  function typing(target) {
    return (
      target instanceof Element &&
      (target.closest("input, textarea, select, [contenteditable], .xterm") !==
        null)
    );
  }

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey || typing(event.target)) {
      return;
    }
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    if (key === SEQUENCE[position]) {
      position += 1;
    } else {
      position = key === SEQUENCE[0] ? 1 : 0;
    }
    if (position === SEQUENCE.length) {
      position = 0;
      show(true);
      window.location.href = "bookmarks";
    }
  });

  return { show };
})();
