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
//
// The same readings run on a timer when the switch in the top bar is on. That
// switch is the whole of the difference: an automatic reading is the manual
// one, taken by the same code, drawn into the same panel, reported the same
// way when it fails.
//
// The switch is a setting of this browser and stands in the bar of every page,
// including the ones whose panels show what this operator has just changed and
// have nothing to read again. Nothing is armed there: the timer waits for a
// panel to register, and on such a page none does.

const Reread = (function () {
  const KEY = "seapath-autorefresh";
  // Ten seconds. Long enough that a fan out to every machine of the inventory
  // is not what the machines are doing, short enough that a failover is seen
  // while the operator still has the page open.
  const PERIOD_MS = 10000;

  // Every control the page attached, in the order it attached them. The timer
  // reads them all; each one decides for itself whether it is due.
  const controls = [];
  let timer = null;
  let ticking = false;

  // A browser can refuse storage outright, in a private window or under a
  // policy. Refusing is not an error here: the switch still works, and the
  // page is left in the position the next load would have shown anyway.
  function stored() {
    try {
      return localStorage.getItem(KEY) === "on";
    } catch (error) {
      return false;
    }
  }

  function remember(on) {
    try {
      if (on) {
        localStorage.setItem(KEY, "on");
      } else {
        localStorage.removeItem(KEY);
      }
    } catch (error) {
      /* The timer still runs. Only the memory of it is lost. */
    }
  }

  function toggle() {
    return document.getElementById("autorefresh");
  }

  function on() {
    const button = toggle();
    return button !== null && button.getAttribute("aria-checked") === "true";
  }

  // `read` is the page's own loader, and it is expected to render what it
  // fetched. `onFailure` is the page's banner: a reading that failed leaves
  // the panel showing the last one that worked, which is the honest thing to
  // show, so the failure has to be said somewhere else.
  function attach(button, read, onFailure) {
    if (!button) {
      return;
    }
    const control = { button, running: false };
    control.run = async () => {
      if (control.running) {
        return;
      }
      control.running = true;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      try {
        await read();
      } catch (failure) {
        onFailure(failure);
      } finally {
        control.running = false;
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    };
    button.addEventListener("click", control.run);
    controls.push(control);
    // The timer starts with the first control that registers rather than at
    // load, so a page with nothing to read again never arms one. The switch
    // itself is in the bar on every page: it is one setting for this browser,
    // like the palette beside it, and a control that comes and goes as an
    // operator moves between pages is one they stop reaching for.
    if (on()) {
      start();
    }
  }

  // A panel in a view that is not open is not read. Only one of the cluster
  // page's three cards is on screen at a time, and reading the other two on a
  // timer would fan out to every machine of the inventory to redraw a table
  // nobody is looking at. `offsetParent` is null exactly when the control, or
  // a card above it, is hidden.
  function due(control) {
    return !control.running && control.button.offsetParent !== null;
  }

  // An open dialog holds the timer. Every one of these pages names the machine
  // it is about to disturb in a modal and waits for the operator to agree, and
  // the row that dialog was opened on is in the table underneath it. Redrawing
  // that table while the sentence is being read is the page moving under a
  // decision about a live substation hypervisor.
  function deciding() {
    return document.querySelector(".modal:not([hidden])") !== null;
  }

  // One round at a time. Hiding and showing a tab while a fan out is in flight
  // asks for a reading on the way back in, and two rounds walking the same
  // controls would each schedule a timer, both of which would then survive.
  async function tick() {
    if (ticking) {
      return;
    }
    ticking = true;
    try {
      await round();
    } finally {
      ticking = false;
    }
  }

  // Sequential rather than parallel. These readings fan out to every machine
  // of the inventory, and a page with two open panels asks the cluster for one
  // thing at a time.
  async function round() {
    for (const control of controls) {
      // Asked again each time round: a reading takes as long as the slowest
      // machine answers, and a dialog opened while one was in flight stops the
      // ones that would have followed it.
      if (deciding()) {
        return;
      }
      if (due(control)) {
        await control.run();
      }
    }
  }

  // Scheduled from the end of the reading and not from the start of it, so a
  // fan out that takes longer than the period cannot stack requests behind
  // itself on a cluster that is already slow to answer.
  function schedule() {
    timer = window.setTimeout(async () => {
      timer = null;
      if (!on()) {
        return;
      }
      if (!document.hidden) {
        await tick();
      }
      if (on()) {
        schedule();
      }
    }, PERIOD_MS);
  }

  function stop() {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  }

  function start() {
    // A page with no panel to read again arms nothing, whatever position the
    // switch is in. The setting is the browser's and holds for the pages that
    // do; a timer here would wake every ten seconds to walk an empty list.
    if (!controls.length) {
      return;
    }
    if (timer === null) {
      schedule();
    }
  }

  // A reading now, and the cycle counted from the end of it. This is what the
  // switch does when it is turned on and what coming back to a hidden tab
  // does: in both, the answer on screen is as old as the time since anyone
  // asked for it, and waiting ten more seconds to say so is the switch looking
  // like it did nothing.
  function readNow() {
    stop();
    tick().then(start, start);
  }

  function mount() {
    const button = toggle();
    if (!button) {
      return;
    }
    button.addEventListener("click", () => {
      const next = !on();
      button.setAttribute("aria-checked", String(next));
      remember(next);
      if (next) {
        readNow();
      } else {
        stop();
      }
    });
    // The position the operator left it in, on this browser. The timer waits
    // for the first control to register, which is what says this page has a
    // panel to read again.
    button.setAttribute("aria-checked", String(stored()));
  }

  // A hidden tab asks nothing. A browser left open overnight on the cluster
  // page would otherwise fan out to every machine of the inventory every ten
  // seconds until morning, on nobody's behalf. Coming back to the tab takes a
  // reading straight away, because the one on screen is as old as the time
  // spent away from it.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !on()) {
      return;
    }
    readNow();
  });

  mount();

  return { attach };
})();
