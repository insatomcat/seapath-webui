// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// Reading a panel again, without a navigation.
//
// The panels this is attached to report what machines are doing at this
// moment, and the answer ages while an operator reads it. The way to a fresh
// one was reloading the page, which refetches every panel of it, sends the
// view bar back through its placeholders, the open panel back to its spinner,
// and loses the scroll position. On the cluster page that is three requests
// fanned out to every machine of the inventory to see one table again.
//
// The control asks the endpoint its own panel is drawn from and hands the
// answer to the render function the first load used. Nothing on screen moves
// until the whole reading is in hand, and the panel is then redrawn in one
// pass, so the swap is a single frame rather than an empty table filling up.
//
// One reading at a time per control: the button is disabled for as long as its
// request is in flight, so a run of clicks cannot leave two answers racing to
// draw the same table.

const Reread = (function () {
  // `read` is the page's own loader, and it is expected to render what it
  // fetched. `onFailure` is the page's banner: a reading that failed leaves
  // the panel showing the last one that worked, which is the honest thing to
  // show, so the failure has to be said somewhere else.
  function attach(button, read, onFailure) {
    if (!button) {
      return;
    }
    let running = false;
    button.addEventListener("click", async () => {
      if (running) {
        return;
      }
      running = true;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      try {
        await read();
      } catch (failure) {
        onFailure(failure);
      } finally {
        running = false;
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    });
  }

  return { attach };
})();
