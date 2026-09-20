// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The tables, on a phone.
//
// The panels and the windows of this UI fold down to a phone screen on their
// own, and their contents did not: a guest carries nine columns, a Pacemaker
// resource seven, and a `by-path` disk name alone is wider than the screen.
// Each of those tables scrolled sideways inside its own card, which is a
// reading that hides half of what it holds behind a gesture nothing announces.
//
// So a table too wide for the screen is drawn as a list of records instead:
// one line per column, the column's own header in front of the value, the
// controls of the row together at the end of it. The stylesheet does the
// drawing, under the `stacked` class; this file decides which tables get it
// and carries each column's header down to the cells under it, because every
// table on these pages is filled by its page's script and the cells are
// written without the heading they belong to.
//
// Measured rather than declared: a table of two columns reads perfectly well
// as a table on a phone, and turning it into six lines of labelled values
// would only make the page longer. The measurement is the honest question,
// "does this overflow the card it is in", asked of the table as a table.
(function () {
  // The same width the stylesheet folds the page at. A rem in a media query is
  // the browser's initial font size and never this document's, which is why
  // the number here is the stylesheet's and not 78rem.
  const PHONE = "(max-width: 49.6rem)";
  const phone = window.matchMedia(PHONE);

  // What each frame was last measured at. A stacked table is taller than the
  // table it replaces, so the frame around it resizes when this file writes
  // the class, and a resize observer that acted on that would take the class
  // off, restore the height, and put it back forever. Only a change of width
  // is a reason to measure again.
  const widths = new WeakMap();
  const watched = new WeakSet();

  const resize = new ResizeObserver((entries) => {
    const moved = entries.filter((entry) => {
      const width = entry.contentRect.width;
      if (widths.get(entry.target) === width) {
        return false;
      }
      widths.set(entry.target, width);
      return true;
    });
    if (moved.length) {
      schedule();
    }
  });

  // The column headings, in the order the cells of a row are written. A header
  // cell the page has hidden is a column its rows do not carry either, so it is
  // left out of the count: the Creation column of the guest list appears and
  // disappears with the guests on screen, and a label that counted it would be
  // one column out on every row.
  function headings(table) {
    const head = table.tHead;
    const row = head && head.rows[head.rows.length - 1];
    if (!row) {
      return null;
    }
    return Array.from(row.cells)
      .filter((cell) => !cell.hidden)
      .map((cell) => cell.textContent.trim());
  }

  function label(table) {
    const names = headings(table);
    if (!names) {
      return false;
    }
    Array.from(table.tBodies).forEach((body) => {
      Array.from(body.rows).forEach((row) => {
        Array.from(row.cells).forEach((cell, index) => {
          const name = names[index];
          // The columns with no heading of their own are the controls of the
          // row. They carry no label and the stylesheet groups them.
          if (name) {
            cell.dataset.label = name;
          } else {
            delete cell.dataset.label;
          }
        });
      });
    });
    return true;
  }

  // Does this table fit the card it is in, drawn as a table? Asked with the
  // class off, because a stacked table fits by construction and would answer
  // yes forever.
  function fit(table) {
    const frame = table.parentElement;
    if (!frame) {
      return;
    }
    table.classList.remove("stacked");
    if (!watched.has(frame)) {
      watched.add(frame);
      resize.observe(frame);
    }
    // A table in a closed panel has no width to measure. The frame taking one
    // when the panel opens is a resize, which brings it back here.
    if (!phone.matches || !frame.clientWidth) {
      return;
    }
    if (frame.scrollWidth > frame.clientWidth + 1 && label(table)) {
      table.classList.add("stacked");
      widths.set(frame, frame.getBoundingClientRect().width);
    }
  }

  let pending = null;

  function schedule() {
    if (pending !== null) {
      return;
    }
    pending = requestAnimationFrame(() => {
      pending = null;
      document.querySelectorAll("table").forEach(fit);
    });
  }

  // Was a table part of this? The document is watched whole, and most of what
  // happens in it has nothing to do with a table: a run streams its events
  // into the window over the page, several lines a second, and measuring every
  // table on the page against every one of them is a layout on each frame for
  // an answer that cannot have changed.
  function tables(records) {
    return records.some((record) => {
      if (record.target.closest && record.target.closest("table")) {
        return true;
      }
      return Array.from(record.addedNodes).some(
        (node) =>
          node.matches &&
          (node.matches("table") || node.querySelector("table") !== null)
      );
    });
  }

  // Every table here is filled, emptied and refilled by its page's script,
  // long after this file has run: a row added to the guest list is a row with
  // no labels on it. Watching the document is what keeps this out of the
  // fourteen page scripts, none of which has any business knowing how a phone
  // draws a table.
  new MutationObserver((records) => {
    if (tables(records)) {
      schedule();
    }
  }).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  // Turning the phone over.
  phone.addEventListener("change", schedule);

  schedule();
})();
