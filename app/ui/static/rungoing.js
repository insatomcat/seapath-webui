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
// the run that is going and the ones queued behind it. That is a few small file
// reads on this node and reaches no other machine, which is why it runs on its
// own timer while the automatic reading of the panels waits to be switched on.

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
  // The runs the mark last stood for, so a page holding a list of runs learns
  // when one of them ended or a new one began, without a timer of its own.
  let standing = "";

  function announce(run, queued) {
    const ids = (run ? [run.id] : []).concat(queued.map((entry) => entry.id));
    const key = ids.join(" ");
    if (key === standing) {
      return;
    }
    standing = key;
    document.dispatchEvent(new CustomEvent("runs-moved", { detail: { ids } }));
  }

  function draw(run, queued) {
    queued = queued || [];
    if (!run && queued.length) {
      // The queue is waiting on a collection installation, which holds the
      // same lock and has no record to lead to.
      run = queued[0];
      queued = queued.slice(1);
    }
    announce(run, queued);
    if (!run) {
      link.hidden = true;
      link.dataset.run = "";
      return;
    }
    const what = run.playbook_id
      ? ": " + run.playbook_id + (run.check ? " (preview)" : "")
      : "";
    link.href = "runs?run=" + encodeURIComponent(run.id);
    const behind = queued.length
      ? ", and " + queued.length + " queued behind it"
      : "";
    link.title =
      (run.state === "queued" ? "A run is queued" : "A run is going") +
      what +
      behind +
      ". Open it.";
    link.dataset.run = run.id;
    link.hidden = false;
  }

  function going() {
    return !link.hidden;
  }

  async function read() {
    const asked = drawn;
    let queue = null;
    try {
      queue = await API.get("/runs/queue");
    } catch (failure) {
      // The mark keeps what it last knew. A node that does not answer says so
      // on the page under it, and a session that ended is sent to sign in by
      // the call itself.
      return;
    }
    if (asked !== drawn) {
      return;
    }
    draw(queue.going, queue.queued);
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

  return { saw, going };
})();
