// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The run view. Its job is to turn an Ansible event stream into something an
// operator can read, and to be honest about how a run ended: `interrupted` is
// offered as "relaunch", never as a failure, because the playbooks are
// idempotent and converging again is the recovery.

(function () {
  const state = { me: null, current: null, source: null, seen: 0 };

  function element(id) {
    return document.getElementById(id);
  }

  function stateClass(name) {
    return "state state-" + name;
  }

  function fillList(target, pairs) {
    target.replaceChildren();
    pairs.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = value;
      target.append(term, definition);
    });
  }

  async function loadList() {
    const runs = await API.get("/runs?limit=50");
    const list = element("run-list");
    list.replaceChildren();

    if (!runs.length) {
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent =
        "No run yet. Apply a playbook from the configuration page.";
      list.append(empty);
      return runs;
    }

    runs.forEach((record) => {
      const item = document.createElement("li");
      item.className = "run" + (record.id === state.current ? " current" : "");

      const badge = document.createElement("span");
      badge.className = stateClass(record.state);
      badge.textContent = record.state;

      const title = document.createElement("span");
      title.textContent =
        record.playbook_id + (record.check ? " (preview)" : "");

      const when = document.createElement("span");
      when.className = "when";
      when.textContent = record.started_at
        ? new Date(record.started_at).toLocaleString()
        : "";

      item.append(badge, title, when);
      item.addEventListener("click", () => show(record.id));
      list.append(item);
    });
    return runs;
  }

  function appendEvent(payload) {
    const stream = element("stream");
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

  // The stats event carries one mapping per outcome, each keyed by host, which
  // is Ansible's shape rather than a reader's. Turned back into one line per
  // host, and the zeroes are kept: "failed=0" is the sentence an operator is
  // looking for.
  function recapLine(stats) {
    const outcomes = ["ok", "changed", "skipped", "failures", "dark", "rescued"];
    const labels = { failures: "failed", dark: "unreachable" };
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
            .filter((key) => key !== "rescued" || (stats[key] || {})[host])
            .map((key) => (labels[key] || key) + "=" + ((stats[key] || {})[host] || 0))
            .join(" ")
      )
      .join("   ");
  }

  function seconds(value) {
    return value >= 10 ? value.toFixed(0) + "s" : value.toFixed(1) + "s";
  }

  // Ansible prints this only when profile_tasks is enabled. The numbers are in
  // the event stream either way, so the view answers "which step took the four
  // minutes" without a callback plugin and without parsing stdout.
  function renderTimings(durations) {
    const card = element("timing-card");
    const rows = Object.entries(durations || {}).sort((a, b) => b[1] - a[1]);
    card.hidden = !rows.length;
    if (!rows.length) {
      return;
    }
    const total = rows.reduce((sum, [, value]) => sum + value, 0);
    card.querySelector("summary").textContent =
      "Where the time went  (" +
      rows.length +
      " tasks, " +
      seconds(total) +
      " of task time)";

    const body = card.querySelector("tbody");
    body.replaceChildren();
    rows.slice(0, 15).forEach(([task, value]) => {
      const row = document.createElement("tr");
      [task, seconds(value)].forEach((text) => {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      });
      body.append(row);
    });
  }

  function renderHosts(hosts) {
    const body = document.querySelector("#hosts-table tbody");
    body.replaceChildren();
    Object.entries(hosts || {}).forEach(([host, counts]) => {
      const row = document.createElement("tr");
      // skipped is here because a run of sixteen tasks reporting five ok reads
      // as a truncated log until the eleven skipped ones are visible.
      [
        host,
        counts.ok,
        counts.changed,
        counts.skipped,
        counts.failed,
        counts.unreachable,
      ].forEach((value, index) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (index === 4 && counts.failed) cell.className = "state-failed";
        row.append(cell);
      });
      body.append(row);
    });
  }

  function renderRecord(record) {
    element("run-detail").hidden = false;
    element("run-title").textContent =
      record.playbook_id + (record.check ? " (preview)" : "");

    fillList(element("run-summary"), [
      ["State", record.state],
      ["Playbook", record.playbook],
      ["Launched by", record.launched_by],
      ["Started", record.started_at ? new Date(record.started_at).toLocaleString() : ""],
      ["Inventory commit", (record.inventory_commit || "none").slice(0, 12)],
      ["Collection", record.collection_version],
      ["Command", (record.command || []).join(" ")],
    ]);

    const message = element("run-message");
    message.textContent = record.message || "";
    message.hidden = !record.message;

    renderHosts(record.progress.hosts);
    renderTimings(record.progress.durations);
    element("run-play").textContent = record.progress.play || "";
    element("run-task").textContent = record.progress.task || "";

    const finished = ["success", "failed", "cancelled", "interrupted"].includes(
      record.state
    );
    // An interrupted run is offered as a relaunch. It is the ordinary outcome
    // of a playbook that reboots the machine it runs from.
    element("relaunch").hidden = !(
      finished &&
      Chrome.isAdmin(state.me) &&
      record.state !== "success"
    );
    element("cancel").hidden = finished || !Chrome.isAdmin(state.me);
    element("download-log").href = "/api/v1/runs/" + record.id + "/log";
    return record;
  }

  async function show(runId) {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
    state.current = runId;
    state.seen = 0;
    element("stream").replaceChildren();

    const record = renderRecord(await API.get("/runs/" + runId));
    await loadList();

    element("relaunch").onclick = () => confirmRelaunch(record);
    element("cancel").onclick = async () => {
      try {
        await API.post("/runs/" + runId + "/cancel");
      } catch (failure) {
        appendEvent({ kind: "task", task: failure.message });
      }
    };

    // Resumable by index: a browser reconnecting after a reboot asks for what
    // it has not seen rather than replaying a whole convergence.
    const source = new EventSource(
      "/api/v1/runs/" + encodeURIComponent(runId) + "/events?offset=0"
    );
    state.source = source;
    source.onmessage = (message) => {
      state.seen += 1;
      appendEvent(JSON.parse(message.data));
    };
    source.addEventListener("state", async (message) => {
      const final = JSON.parse(message.data);
      renderHosts(final.hosts);
      renderTimings(final.durations);
      source.close();
      state.source = null;
      renderRecord(await API.get("/runs/" + runId));
      await loadList();
    });
    source.onerror = () => {
      // The stream ends when the run does, and it also ends when the machine
      // running it goes away. Both look the same here, and the record is the
      // source of truth either way.
      source.close();
      state.source = null;
    };
  }

  function confirmRelaunch(record) {
    const modal = element("confirm");
    const input = element("confirm-input");
    const go = element("confirm-go");
    const node = document.getElementById("node-name").textContent;

    element("confirm-title").textContent = "Relaunch " + record.playbook_id;
    element("confirm-disruption").textContent =
      "Relaunching is safe: the playbooks are idempotent, so converging again " +
      "is the recovery. It will run against this machine from the current " +
      "inventory, which may have changed since the run that failed.";
    element("confirm-name").textContent = node;
    element("confirm-error").hidden = true;
    input.value = "";
    go.disabled = true;
    input.oninput = () => {
      go.disabled = input.value !== node;
    };
    go.onclick = async () => {
      go.disabled = true;
      try {
        const started = await API.post("/runs", {
          playbook: record.playbook_id,
          check: record.check,
        });
        modal.hidden = true;
        await show(started.run_id);
      } catch (failure) {
        const error = element("confirm-error");
        error.textContent = failure.message;
        error.hidden = false;
        go.disabled = false;
      }
    };
    modal.hidden = false;
  }

  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  async function start() {
    const chrome = await Chrome.load();
    state.me = chrome.me;
    const runs = await loadList();
    const requested = new URLSearchParams(window.location.search).get("run");
    const target = requested || (runs.length ? runs[0].id : null);
    if (target) {
      await show(target);
    }
  }

  start();
})();
