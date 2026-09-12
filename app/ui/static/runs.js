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
      badge.className = RunStream.stateClass(record.state);
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

  // Ansible prints this only when profile_tasks is enabled. The numbers are in
  // the event stream either way, so the view answers "which step took the four
  // minutes" without a callback plugin and without parsing stdout.
  function renderTimings(durations) {
    const opener = element("timing-open");
    const rows = Object.entries(durations || {}).sort((a, b) => b[1] - a[1]);
    opener.hidden = !rows.length;
    if (!rows.length) {
      return;
    }
    const total = rows.reduce((sum, [, value]) => sum + value, 0);
    opener.textContent = "Where the time went";
    element("timing-note").textContent =
      rows.length + " tasks, " + RunStream.seconds(total) + " of task time.";

    const body = document.querySelector("#timing-table tbody");
    body.replaceChildren();
    rows.slice(0, 15).forEach(([task, value]) => {
      const row = document.createElement("tr");
      [task, RunStream.seconds(value)].forEach((text) => {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      });
      body.append(row);
    });
  }

  // The counts of every machine added up, which is the line an operator reads
  // to know whether the run did anything. Which machine is the question under
  // it, and the rows answer that one.
  function summariseHosts(entries) {
    const totals = { ok: 0, changed: 0, failed: 0, unreachable: 0 };
    entries.forEach(([, counts]) => {
      Object.keys(totals).forEach((key) => {
        totals[key] += counts[key] || 0;
      });
    });
    const machines = entries.length + (entries.length === 1 ? " machine" : " machines");
    const parts = [totals.ok + " ok", totals.changed + " changed"];
    if (totals.failed) {
      parts.push(totals.failed + " failed");
    }
    if (totals.unreachable) {
      parts.push(totals.unreachable + " unreachable");
    }
    return {
      text: machines + ", " + parts.join(", "),
      wrong: Boolean(totals.failed || totals.unreachable),
    };
  }

  function renderHosts(hosts) {
    const card = element("hosts-card");
    const entries = Object.entries(hosts || {});
    card.hidden = !entries.length;
    if (entries.length) {
      const summary = summariseHosts(entries);
      element("hosts-summary").textContent = summary.text;
      element("hosts-summary").className = summary.wrong ? "state-failed" : "";
      // A run that went wrong is read for which machine it went wrong on, so
      // that one arrives open. Only ever opened here, never shut: an operator
      // who unfolded it is left where they are as the counts keep coming.
      if (summary.wrong) {
        card.open = true;
      }
    }
    const body = document.querySelector("#hosts-table tbody");
    body.replaceChildren();
    entries.forEach(([host, counts]) => {
      const row = document.createElement("tr");
      // skipped is here because a run of sixteen tasks reporting five ok reads
      // as a truncated log until the eleven skipped ones are visible.
      [
        host,
        counts.ok,
        counts.changed,
        counts.skipped,
        counts.failed,
        counts.ignored,
        counts.unreachable,
      ].forEach((value, index) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (index === 4 && counts.failed) cell.className = "state-failed";
        if (index === 5 && counts.ignored) cell.className = "outcome-ignored";
        row.append(cell);
      });
      body.append(row);
    });
  }

  // What the run was given beside the inventory. An artefact leaves no trace
  // in `git log`, so this line is where "which image did this run push" is
  // answered, next to the commit and the collection it already records.
  function describeFiles(files) {
    if (!files || !files.length) {
      return "the inventory alone";
    }
    const artefacts = files.filter((file) => file.source === "artefacts");
    const bytes = files.reduce((total, file) => total + file.size, 0);
    const parts = [files.length - artefacts.length + " versioned"];
    if (artefacts.length) {
      parts.push(artefacts.length + " artefact" + (artefacts.length > 1 ? "s" : ""));
    }
    return parts.join(", ") + " (" + (bytes / (1024 * 1024)).toFixed(1) + " MB)";
  }

  // What the operator checked, groups and hosts together. Empty means the
  // playbook's own scope.
  function chosen(scope) {
    return (scope.groups || []).concat(scope.hosts || []);
  }

  // The scope a run was launched with, as one line.
  function describeScope(record) {
    const scope = record.scope || {};
    // Null where the playbook's patterns were not read, which is a different
    // answer from an empty list: one is unknown, the other is a run that
    // played nothing.
    const machines = record.machines;
    const named =
      machines === null || machines === undefined
        ? "every machine the playbook plays"
        : machines.join(", ") || "no machine of this inventory";
    const narrowed = chosen(scope);
    return narrowed.length ? narrowed.join(", ") + " (" + named + ")" : named;
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
      ["Files staged", describeFiles(record.files)],
      // What this run was aimed at, beside what it reached. The host table
      // below says who answered; this says who was asked, which is the only
      // way to tell a machine that failed from one the run never played.
      ["Machines", describeScope(record)],
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
    element("download-log").href = "api/v1/runs/" + record.id + "/log";
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
    // Folded again for the run being opened. Left as the previous one was, a
    // clean run inherited the unfolded table of the failure read before it.
    element("hosts-card").open = false;

    const record = renderRecord(await API.get("/runs/" + runId));
    await loadList();

    element("relaunch").onclick = () => confirmRelaunch(record);
    element("cancel").onclick = async () => {
      try {
        await API.post("/runs/" + runId + "/cancel");
      } catch (failure) {
        RunStream.append(element("stream"), {
          kind: "task",
          task: failure.message,
        });
      }
    };

    // The same lines the window over an action draws, from the same stream.
    state.source = RunStream.follow(runId, {
      onEvent: (payload) => {
        state.seen += 1;
        RunStream.append(element("stream"), payload);
      },
      onEnd: async (final) => {
        state.source = null;
        renderHosts(final.hosts);
        renderTimings(final.durations);
        renderRecord(await API.get("/runs/" + runId));
        await loadList();
      },
      onLost: () => {
        state.source = null;
      },
    });
  }

  // Relaunching asks once, like applying does. Typing the machine's name was
  // asked for here after the apply confirmation stopped asking, which put the
  // heavier friction on the lighter act: a relaunch converges again with the
  // same playbook, and converging again is how a failed run is recovered.
  function confirmRelaunch(record) {
    const modal = element("confirm");
    const go = element("confirm-go");

    element("confirm-title").textContent = "Relaunch " + record.playbook_id;
    element("confirm-disruption").textContent =
      "Relaunching is safe: the playbooks are idempotent, so converging again " +
      "is the recovery. It will run against this machine from the current " +
      "inventory, which may have changed since the run that failed." +
      (chosen(scope).length
        ? " Narrowed to " + chosen(scope).join(", ") + ", as the original run was."
        : "");

    // A relaunch repeats the run it relaunches, variables and scope included.
    // Dropping the variables silently would reboot a machine whose run was
    // launched with skip_reboot_setup, and would send cluster_remove_machine
    // off without the machine to remove. Dropping the scope would widen a run
    // that was narrowed to one machine on purpose.
    const variables = record.variables || {};
    const scope = record.scope || { groups: [], hosts: [] };
    const named = Object.keys(variables);
    const line = element("confirm-variables");
    line.textContent = named.length
      ? "Launched again with " +
        named.map((name) => name + " = " + variables[name]).join(", ") +
        ", as the original run was."
      : "";
    line.hidden = !named.length;

    element("confirm-error").hidden = true;
    go.disabled = false;
    go.onclick = async () => {
      go.disabled = true;
      go.setAttribute("aria-busy", "true");
      try {
        const started = await API.post("/runs", {
          playbook: record.playbook_id,
          check: record.check,
          variables,
          scope,
        });
        modal.hidden = true;
        await show(started.run_id);
      } catch (failure) {
        const error = element("confirm-error");
        error.textContent = failure.message;
        error.hidden = false;
        go.disabled = false;
      } finally {
        go.removeAttribute("aria-busy");
      }
    };
    modal.hidden = false;
  }

  element("timing-open").addEventListener("click", () => {
    element("timing-modal").hidden = false;
  });

  element("timing-close").addEventListener("click", () => {
    element("timing-modal").hidden = true;
  });

  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  async function start() {
    state.me = Chrome.current();
    const runs = await loadList();
    const requested = new URLSearchParams(window.location.search).get("run");
    const target = requested || (runs.length ? runs[0].id : null);
    if (target) {
      await show(target);
    }
  }

  start();
})();
