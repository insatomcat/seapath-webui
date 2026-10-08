// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The mark in the top bar that says a run is going, and leads to it.
//
// A run belongs to the service: the window over the page can be closed, the
// page left, and another operator can have launched it from another desk. The
// bar is the one place every page shares, so that is where a run in the
// background is said.
//
// The document arrives with the mark already drawn or already hidden, from
// what the service knew when it served the page. From there this file asks for
// the newest run of the history, which is the one going if any is: a run takes
// the one lock before it is recorded, so nothing newer can appear while it
// goes. That is one small file read on this node and reaches no other machine,
// which is why it runs on its own timer while the automatic reading of the
// panels waits to be switched on.

const RunGoing = (function () {
  const link = document.getElementById("run-going");
  // Often while a run is going, since the mark going out is how an operator on
  // another page learns it ended. Seldom otherwise: it only has to notice a
  // run somebody else launched.
  const GOING_MS = 3000;
  const IDLE_MS = 15000;
  let timer = null;
  // Counts what was drawn from a launch, so an answer asked for before the
  // launch cannot hide the mark the launch just lit.
  let drawn = 0;

  function draw(run) {
    if (!run) {
      link.hidden = true;
      link.dataset.run = "";
      return;
    }
    const what = run.playbook_id
      ? ": " + run.playbook_id + (run.check ? " (preview)" : "")
      : "";
    link.href = "runs?run=" + encodeURIComponent(run.id);
    link.title = "A run is going" + what + ". Open it.";
    link.dataset.run = run.id;
    link.hidden = false;
  }

  function going() {
    return !link.hidden;
  }

  async function read() {
    const asked = drawn;
    let newest = null;
    try {
      newest = (await API.get("/runs?limit=1"))[0] || null;
    } catch (failure) {
      // The mark keeps what it last knew. A node that does not answer says so
      // on the page under it, and a session that ended is sent to sign in by
      // the call itself.
      return;
    }
    if (asked !== drawn) {
      return;
    }
    const unfinished =
      newest && ["pending", "running"].includes(newest.state) ? newest : null;
    draw(unfinished);
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(tick, going() ? GOING_MS : IDLE_MS);
  }

  // A tab nobody is looking at asks nothing, and reads once when it is looked
  // at again.
  async function tick() {
    if (!document.hidden) {
      await read();
    }
    schedule();
  }

  // A page that has just launched a run says so, and the mark is lit by the
  // click that launched it.
  function saw(runId) {
    if (!link) {
      return;
    }
    drawn += 1;
    draw({ id: runId });
    schedule();
  }

  if (link) {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        tick();
      }
    });
    schedule();
  }

  return { saw };
})();
