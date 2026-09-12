// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// One run, turned into lines an operator reads, and the connection those lines
// arrive on.
//
// Two views show a run: the Runs page, which is the history and the whole
// record, and the window that follows the run an action just launched, over
// the page it was launched from. They show the same log because they run the
// same code. Two renders of one event stream would be two sets of bugs, and
// the one an operator only sees for the length of a convergence is the one
// nobody would notice going wrong.

const RunStream = (function () {
  function seconds(value) {
    return value >= 10 ? value.toFixed(0) + "s" : value.toFixed(1) + "s";
  }

  function stateClass(name) {
    return "state state-" + name;
  }

  // The stats event carries one mapping per outcome, each keyed by host, which
  // is Ansible's shape rather than a reader's. Turned back into one line per
  // host, and the zeroes are kept: "failed=0" is the sentence an operator is
  // looking for.
  function recapLine(stats) {
    const outcomes = [
      "ok",
      "changed",
      "skipped",
      "failures",
      "dark",
      "rescued",
      "ignored",
    ];
    const labels = { failures: "failed", dark: "unreachable" };
    // Ansible prints these two only when they happened, and so does this. A
    // recap ending in "rescued=0 ignored=0" on every run trains an operator to
    // stop reading the end of the line.
    const whenNonZero = ["rescued", "ignored"];
    const hosts = [
      ...new Set(outcomes.flatMap((key) => Object.keys(stats[key] || {}))),
    ].sort();
    if (!hosts.length) {
      return "no host was reached";
    }
    return hosts
      .map(
        (host) =>
          host +
          " " +
          outcomes
            .filter(
              (key) => !whenNonZero.includes(key) || (stats[key] || {})[host]
            )
            .map((key) => (labels[key] || key) + "=" + ((stats[key] || {})[host] || 0))
            .join(" ")
      )
      .join("   ");
  }

  // One event, appended to a stream element that then follows its own tail.
  function append(stream, payload) {
    const line = document.createElement("div");
    line.className = "stream-line stream-" + payload.kind;

    if (payload.kind === "play") {
      line.textContent = "PLAY  " + payload.play;
    } else if (payload.kind === "task") {
      line.textContent = "TASK  " + payload.task;
    } else if (payload.kind === "result") {
      line.textContent =
        payload.outcome.padEnd(12) +
        payload.host +
        "  " +
        (payload.task || "") +
        (payload.seconds ? "  " + seconds(payload.seconds) : "");
      line.classList.add("outcome-" + payload.outcome);
      // A failure says why, and a debug task shows what it printed. Those are
      // the two reasons to look at a result rather than at a counter.
      [payload.message, payload.output].forEach((text) => {
        if (!text) {
          return;
        }
        const detail = document.createElement("div");
        detail.className = "stream-reason";
        detail.textContent = text;
        line.append(detail);
      });
    } else if (payload.kind === "stats") {
      // Ansible's own recap. Printing the word and dropping the numbers left a
      // heading with nothing under it at the end of every run.
      line.textContent = "RECAP  " + recapLine(payload.stats);
    } else {
      return;
    }

    stream.append(line);
    stream.scrollTop = stream.scrollHeight;
  }

  // The run's events, from the beginning, and the end of it.
  //
  // Resumable by index: a browser reconnecting after a reboot asks for what it
  // has not seen rather than replaying a whole convergence.
  //
  // `onEnd` is the run's final state, which arrives as an event of its own.
  // `onLost` is the same connection ending without one, which is the machine
  // running it going away: the record is the source of truth either way, so
  // what a view does about it is its own business.
  function follow(runId, handlers) {
    const source = new EventSource(
      "api/v1/runs/" + encodeURIComponent(runId) + "/events?offset=0"
    );
    source.onmessage = (message) => {
      handlers.onEvent(JSON.parse(message.data));
    };
    source.addEventListener("state", (message) => {
      source.close();
      handlers.onEnd(JSON.parse(message.data));
    });
    source.onerror = () => {
      source.close();
      if (handlers.onLost) {
        handlers.onLost();
      }
    };
    return source;
  }

  return { append, follow, recapLine, seconds, stateClass };
})();
