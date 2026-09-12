// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The run an action just launched, followed over the page it was launched
// from.
//
// Almost everything this service does to a machine is a playbook, and
// launching one used to be a navigation: the page the operator was working on
// was replaced by the Runs page, and reading the result of their own action
// meant finding the way back to the page, the view, the card and sometimes the
// row they had left. On a page whose panels are read again on arrival that is
// several clicks and a wait to return to where they already were.
//
// Closing the window stops watching and does nothing else. The run belongs to
// the service rather than to this browser: it keeps going, it is in the
// history, and the Runs page has all of it. That is what makes the window
// ignorable, which is the point of showing it here rather than going there.

const RunWatch = (function () {
  const state = { runId: null, source: null, done: null };

  function element(id) {
    return document.getElementById(id);
  }

  // Opening is synchronous, and the record follows. A window that waited for
  // the first fetch would appear a beat after the confirmation it replaces,
  // over a page that looked, for that beat, as if nothing had been launched.
  //
  // `onDone` is for a page with something of its own to read again when the run
  // ends that the panel controls do not cover: the Real time page lists the
  // measurements it launched, and that list is what the operator stayed for.
  function open(runId, onDone) {
    stop();
    state.runId = runId;
    state.done = onDone || null;

    element("run-watch-stream").replaceChildren();
    element("run-watch-play").textContent = "";
    element("run-watch-task").textContent = "";
    element("run-watch-title").textContent = "Launched";
    element("run-watch-message").hidden = true;
    element("run-watch-cancel").hidden = true;
    const badge = element("run-watch-state");
    badge.className = RunStream.stateClass("running");
    badge.textContent = "running";
    element("run-watch-open").href = "runs?run=" + encodeURIComponent(runId);
    element("run-watch").hidden = false;

    state.source = RunStream.follow(runId, {
      onEvent: (payload) => {
        RunStream.append(element("run-watch-stream"), payload);
        if (payload.kind === "play") {
          element("run-watch-play").textContent = payload.play;
        }
        if (payload.kind === "task") {
          element("run-watch-task").textContent = payload.task;
        }
      },
      onEnd: () => {
        state.source = null;
        finish(runId);
      },
      onLost: () => {
        state.source = null;
        finish(runId);
      },
    });
    describe(runId);
  }

  // What the record says, which is everything the stream does not carry: which
  // playbook this is, whether it was a preview, and the sentence a run that
  // could not start leaves behind.
  async function describe(runId) {
    let record = null;
    try {
      record = await API.get("/runs/" + encodeURIComponent(runId));
    } catch (failure) {
      // The run is launched either way, and the stream above is drawing it.
      // Only the description is missing, and the window says so rather than
      // staying on a title that would never be filled.
      say(failure.message + " The Runs page has the whole record.");
      return;
    }
    // A second run opened from the same page while this answer was in flight.
    if (state.runId !== runId) {
      return;
    }
    element("run-watch-title").textContent =
      record.playbook_id + (record.check ? " (preview)" : "");
    const badge = element("run-watch-state");
    badge.className = RunStream.stateClass(record.state);
    badge.textContent = record.state;
    if (record.message) {
      say(record.message);
    }

    const finished = ["success", "failed", "cancelled", "interrupted"].includes(
      record.state
    );
    const me = Chrome.current();
    const cancel = element("run-watch-cancel");
    cancel.hidden = finished || !(me && Chrome.isAdmin(me));
    cancel.onclick = async () => {
      cancel.disabled = true;
      try {
        await API.post("/runs/" + encodeURIComponent(runId) + "/cancel");
      } catch (failure) {
        say(failure.message);
      } finally {
        cancel.disabled = false;
      }
    };
  }

  function say(message) {
    const banner = element("run-watch-message");
    banner.textContent = message;
    banner.hidden = false;
  }

  // The run is over. Its final state is on the record, and the panels of the
  // page underneath describe machines this run may have just changed: the
  // operator stayed to the end precisely to see that. Reading them again here
  // is what replaces the navigation back to the page and the reading it would
  // have done on arrival.
  //
  // Only when the window is still open. An operator who closed it said they
  // were not watching, and a fan out to every machine of the inventory on
  // nobody's behalf is the thing D37 is careful about.
  async function finish(runId) {
    const watching = !element("run-watch").hidden && state.runId === runId;
    if (!watching) {
      return;
    }
    await describe(runId);
    await Reread.readAgain();
    if (state.done) {
      await state.done();
    }
  }

  function stop() {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
  }

  function close() {
    stop();
    state.runId = null;
    state.done = null;
    element("run-watch").hidden = true;
  }

  element("run-watch-close").addEventListener("click", close);

  return { open };
})();
