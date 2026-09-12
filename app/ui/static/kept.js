// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// What this browser last read, painted while the next reading is in flight.
//
// A navigation cannot be made free. The document is one round trip to the node,
// the answers its panels are drawn from are another, and on a machine reached
// through an ssh tunnel each of those is tens of milliseconds before the node
// has done anything; the readings that fan out to the other machines then take
// as long as the slowest of them answers, and a machine that is off costs the
// whole timeout. That is the price of the first load of a page, and it is a
// price worth paying once.
//
// Paying it on every hop is what made this UI unpleasant to move around in. An
// operator who looks at the cluster, opens the VMs page and comes back waits
// twice for an answer they were reading a moment ago.
//
// So a panel is drawn from what this browser last read, before anything is
// asked, and the reading that follows redraws it. Three rules keep that honest,
// and they are the whole of the design:
//
//   - A panel showing a kept answer says so, with its age, until the reading
//     lands. What is on screen is then never presented as current.
//   - Nothing can be acted on from a kept answer. The controls of the panel are
//     held until the reading lands, because a resource may have moved and an
//     operator must not migrate a guest off a node that has already failed over.
//   - It is this browser's memory of what it drew, and never a cache in front of
//     the service. Every reading still reaches the API, `fresh=1` still reaches
//     the machines, and nothing here answers a question on the service's behalf.
//
// Kept in `sessionStorage`, which is this tab: a page opened in a new tab is a
// first load, and closing the tab forgets everything. Keyed per node, because
// two nodes reached through two ssh tunnels are one origin to the browser, and
// per release, because a payload kept by one version and drawn by another is a
// render dying on a field that moved.

const Kept = (function () {
  const version = document.querySelector('meta[name="version"]');
  const RELEASE = version ? version.content : "";

  function key(name) {
    const cookie = document.querySelector('meta[name="csrf-cookie"]');
    return "seapath-kept-" + (cookie ? cookie.content : "") + "-" + name;
  }

  // A browser can refuse storage outright, in a private window or under a
  // policy, and it can refuse a write because the quota is full. Refusing costs
  // nothing but the first load an operator would have had anyway.
  function stored(name) {
    try {
      return JSON.parse(sessionStorage.getItem(key(name)) || "null");
    } catch (error) {
      return null;
    }
  }

  function keep(name, payload) {
    try {
      sessionStorage.setItem(
        key(name),
        JSON.stringify({ at: Date.now(), release: RELEASE, payload })
      );
    } catch (error) {
      /* The panel was drawn. Only the memory of it is lost. */
    }
  }

  // Draws the panel from what was kept, and says how old it is in milliseconds.
  // `null` means nothing was kept, or what was kept cannot be drawn, and the
  // caller then does what it always did: show its spinner and wait.
  //
  // The render is guarded. A payload this release cannot draw is dropped rather
  // than allowed to kill the page script, which would leave a page that renders
  // and then does nothing at all.
  function paint(name, render) {
    const held = stored(name);
    if (!held || held.release !== RELEASE || typeof held.at !== "number") {
      return null;
    }
    try {
      render(held.payload);
    } catch (error) {
      forget(name);
      return null;
    }
    return Math.max(0, Date.now() - held.at);
  }

  // What was kept, for a page that draws several answers in one order and wants
  // to replay that order rather than one render at a time. `paint` is the shape
  // for a single panel; this is the shape for a page. The caller owns the guard
  // in this form, because it is the one that knows how far its own drawing got.
  function held(name) {
    const kept = stored(name);
    if (!kept || kept.release !== RELEASE || typeof kept.at !== "number") {
      return null;
    }
    return { at: kept.at, payload: kept.payload };
  }

  function forget(name) {
    try {
      sessionStorage.removeItem(key(name));
    } catch (error) {
      /* Nothing was stored either. */
    }
  }

  function age(milliseconds) {
    const seconds = Math.round(milliseconds / 1000);
    if (seconds < 60) {
      return seconds <= 1 ? "a second ago" : seconds + " seconds ago";
    }
    const minutes = Math.round(seconds / 60);
    return minutes === 1 ? "a minute ago" : minutes + " minutes ago";
  }

  // What the panel says while it shows a kept answer: where it came from, how
  // old it is, and that a reading is on its way. Written into the element the
  // panel would have had its spinner in, which the render that follows hides.
  function rereading(ids, milliseconds) {
    ids.forEach((id) => {
      const line = document.getElementById(id);
      if (!line) {
        return;
      }
      line.textContent =
        "What this browser last read, " +
        age(milliseconds) +
        ". Reading again.";
      line.hidden = false;
    });
  }

  // The controls of a panel showing a kept answer, held until the reading lands.
  // Every act of this service names a machine and most of them restart something
  // under a running VM, and the row this one was aimed at may have moved since
  // the answer on screen was taken.
  //
  // The reread controls are left alone: asking for the reading is the one thing
  // an operator may do to a panel in this state, and `Reread` disables them
  // itself while their own request is in flight.
  let holding = [];

  const CONTROLS = "button, input, select, textarea, a.button-link";

  function hold(targets) {
    targets.forEach((target) => {
      const panel =
        typeof target === "string" ? document.getElementById(target) : target;
      if (!panel) {
        return;
      }
      // The panel's own controls, and the panel itself when it is one: a page
      // whose acts are single buttons rather than rows names the buttons, and
      // one that draws rows names the card around them. Asking for the
      // descendants of a button finds nothing, which is how the writes on the
      // Inventory page were left live by the first version of this.
      const controls = [...panel.querySelectorAll(CONTROLS)];
      if (panel.matches(CONTROLS)) {
        controls.push(panel);
      }
      controls.forEach((control) => {
        if (control.classList.contains("reread") || control.disabled) {
          return;
        }
        control.disabled = true;
        holding.push(control);
      });
    });
  }

  // The reading landed. A control the render replaced is gone from the document
  // and re-enabling it changes nothing, which is why this is safe to call
  // whatever each panel rebuilt.
  function release() {
    holding.forEach((control) => {
      control.disabled = false;
    });
    holding = [];
  }

  return { paint, held, keep, forget, rereading, hold, release, age };
})();
