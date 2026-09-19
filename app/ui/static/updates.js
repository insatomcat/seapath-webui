// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The Updates page: what an upgrade would bring to each machine, and the
// upgrade.
//
// Everything drawn here comes out of the run history. What a machine would
// install is what it answered to the last check, which is a run, and the page
// says how old that answer is. A machine updated since then is marked, rather
// than listed with packages it has already installed.
//
// An update is the upstream playbook followed by the check, in one run, so
// each machine is drawn as the update left it.
//
// The machine serving the page is updated alone, since a machine after it
// would never be reached. Its run cannot outlive its reboot: the playbook
// stops short of it, the check reads the machine, and the run schedules the
// reboot and ends. The machine finishes its update at boot. With a playbook
// that still finishes on the controller, it is not offered at all.

(function () {
  let canCheck = false;
  let canUpdate = false;
  let view = null;
  const selected = new Set();

  function element(id) {
    return document.getElementById(id);
  }

  function showBanner(message) {
    const banner = element("banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  function cell(content) {
    const node = document.createElement("td");
    if (content instanceof Node) {
      node.append(content);
    } else {
      node.textContent = content;
    }
    return node;
  }

  function span(text, className) {
    const node = document.createElement("span");
    node.textContent = text;
    if (className) {
      node.className = className;
    }
    return node;
  }

  function when(iso) {
    if (!iso) {
      return "";
    }
    return new Date(iso).toLocaleString();
  }

  function plural(count, one, many) {
    return count + " " + (count === 1 ? one : many);
  }

  function ended(state) {
    return {
      success: "succeeded",
      failed: "failed",
      interrupted: "was interrupted",
      cancelled: "was cancelled",
    }[state] || state;
  }

  function runLink(run, text) {
    const link = document.createElement("a");
    link.href = "runs?run=" + encodeURIComponent(run.id);
    link.textContent = text;
    return link;
  }

  // The lead says which check the table is drawn from, and whether a newer
  // one is going. A table without a date would read as the machines now.
  function renderLead() {
    const lead = element("lead");
    lead.replaceChildren();
    if (view.check && ["pending", "running"].includes(view.check.state)) {
      lead.append("A check is running, ");
      lead.append(runLink(view.check, "launched by " + view.check.launched_by));
      lead.append(". ");
    }
    if (view.checked) {
      lead.append(
        "Checked " + when(view.checked.started_at) + " by " +
          view.checked.launched_by + ": "
      );
      const run = runLink(view.checked, "the run " + ended(view.checked.state));
      run.className = RunStream.stateClass(view.checked.state);
      lead.append(run);
      lead.append(".");
    } else if (!view.check) {
      lead.append(
        "No check has been run yet. A check refreshes the package lists of " +
          "every machine and asks apt what an upgrade would do; it installs " +
          "nothing."
      );
    }
    if (view.updating) {
      lead.append(" An update is running, ");
      lead.append(runLink(view.updating, "launched by " + view.updating.launched_by));
      lead.append(".");
    }
  }

  function kernelCell(reading) {
    if (!reading || !reading.running_kernel) {
      return cell("");
    }
    const box = document.createElement("div");
    box.append(span(reading.running_kernel));
    if (reading.awaiting_reboot) {
      box.append(document.createElement("br"));
      box.append(span("reboots into " + reading.newest_kernel, "tag warn"));
    } else if (reading.kernel_pending) {
      box.append(document.createElement("br"));
      box.append(
        span("installed, not booted: " + reading.newest_kernel, "tag warn")
      );
    }
    return cell(box);
  }

  function pendingCell(machine) {
    const reading = machine.reading;
    if (machine.stale) {
      return cell(span("updated since this check, check again", "tag"));
    }
    if (!reading) {
      return cell(
        view.checked
          ? span("no answer to the last check", "state-failed")
          : ""
      );
    }
    if (reading.error) {
      return cell(span(reading.error, "state-failed"));
    }
    const simulation = reading.simulation;
    const upgrades = simulation.upgrades.length;
    const installs = simulation.installs.length;
    const removals = simulation.removals.length;
    const box = document.createElement("div");
    if (upgrades + installs + removals === 0) {
      box.append(span("up to date", "state-success"));
    } else {
      const parts = [];
      if (upgrades) {
        parts.push(plural(upgrades, "upgrade", "upgrades"));
      }
      if (installs) {
        parts.push(plural(installs, "new package", "new packages"));
      }
      if (removals) {
        parts.push(plural(removals, "removal", "removals"));
      }
      const open = document.createElement("button");
      open.type = "button";
      open.className = "inline-link";
      open.textContent = parts.join(", ");
      open.addEventListener("click", () => showPackages(machine));
      box.append(open);
      const kernel = simulation.upgrades
        .concat(simulation.installs)
        .some((item) => item.kernel);
      if (kernel) {
        box.append(" ");
        box.append(span("new kernel", "tag warn"));
      }
    }
    if (view.reboots_for_kernel && reboots(machine) && !reading.awaiting_reboot) {
      box.append(" ");
      box.append(span("reboots", "tag warn"));
    }
    // The room for the snapshot the update takes of root. The update refuses
    // too little before it touches anything, and this says so before the
    // run; it is as old as the check, so it warns rather than blocks.
    // An earlier update that rebooted the machine and was not finished by it.
    if (reading.unfinished) {
      box.append(document.createElement("br"));
      box.append(span(reading.unfinished, "state-failed"));
    }
    const room = reading.snapshot;
    if (room && room.note) {
      box.append(document.createElement("br"));
      box.append(span(room.note, room.enough ? "warning-text" : "state-failed"));
    }
    if (reading.refresh_error) {
      box.append(document.createElement("br"));
      box.append(
        span(
          "the package lists could not be refreshed: " + reading.refresh_error,
          "state-failed"
        )
      );
    }
    return cell(box);
  }

  // Whether an update would reboot the machine, as of the last check. The
  // playbook decides again when it runs, from the same two readings: a kernel
  // the upgrade installs, or one installed that the machine never booted.
  function reboots(machine) {
    const reading = machine.reading;
    if (!reading || reading.error || machine.stale) {
      return false;
    }
    return (
      reading.kernel_pending ||
      reading.simulation.upgrades
        .concat(reading.simulation.installs)
        .some((item) => item.kernel)
    );
  }

  function lastUpdateCell(machine) {
    const run = machine.last_update;
    if (!run) {
      return cell("");
    }
    const box = document.createElement("div");
    box.append(when(run.started_at) + " ");
    const link = runLink(run, run.state);
    link.className = RunStream.stateClass(run.state);
    box.append(link);
    return cell(box);
  }

  function selectCell(machine) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selected.has(machine.host);
    // This machine drives the run, and may reboot at its end.
    const refused = machine.this_node && !view.updates_itself;
    box.disabled = !canUpdate || refused || !updateAvailable();
    if (refused) {
      box.title = "Updated from another member: this machine drives the run.";
    } else if (machine.this_node) {
      box.title = view.reboots_for_kernel
        ? "Updated on its own, since it drives the run."
        : "Updated on its own: the run ends with its reboot.";
    }
    box.addEventListener("change", () => {
      if (box.checked && (!view.one_at_a_time || machine.this_node)) {
        // An older playbook reboots every machine it is sent to at once, and
        // this machine's run ends with its reboot.
        selected.clear();
        selected.add(machine.host);
        renderRows();
      } else if (box.checked) {
        const local = view.machines.find((other) => other.this_node);
        if (local && selected.delete(local.host)) {
          renderRows();
        }
        selected.add(machine.host);
      } else {
        selected.delete(machine.host);
      }
      renderActs();
    });
    return cell(box);
  }

  function renderRows() {
    const rows = element("rows");
    rows.replaceChildren();
    view.machines.forEach((machine) => {
      const line = document.createElement("tr");
      const name = document.createElement("div");
      name.append(span(machine.host));
      if (machine.this_node) {
        name.append(" ");
        name.append(span("this machine", "tag"));
      }
      line.append(
        selectCell(machine),
        cell(name),
        kernelCell(machine.reading),
        pendingCell(machine),
        lastUpdateCell(machine)
      );
      rows.append(line);
    });
  }

  // The reasons that stop an update of the machines ticked. An unreachable
  // machine stops a run of all of them and not one narrowed to the others, so
  // the server is left to decide that one when the run is launched.
  function refusals() {
    const update = view.update;
    if (!update) {
      return [];
    }
    return update.unmet.filter(
      (_, index) => update.unmet_codes[index] !== "peer_reachable"
    );
  }

  function updateAvailable() {
    return Boolean(view.update) && refusals().length === 0 && !view.updating;
  }

  function renderActs() {
    element("acts").hidden = !canUpdate;
    const go = element("update");
    go.disabled = selected.size === 0 || !updateAvailable();
    const others = view.machines.filter((machine) => !machine.this_node);
    const note = element("acts-note");
    if (view.updating) {
      note.textContent = "An update is already running.";
    } else if (!view.one_at_a_time) {
      note.textContent =
        "The collection this image ships updates every machine it is sent " +
        "to at once, without moving their guests first, so one machine is " +
        "updated per run.";
    } else if (view.this_host && !view.updates_itself && others.length === 0) {
      note.textContent =
        "This machine is the only one, and it drives the run, so it cannot " +
        "update itself with the playbook this image ships, which finishes " +
        "after the reboot. Run seapath_update_debian from a control machine.";
    } else if (view.this_host && !view.updates_itself) {
      note.textContent =
        view.this_host + " is updated from another member, since it drives " +
        "the run and the playbook this image ships finishes after the reboot.";
    } else if (view.this_host && others.length && view.reboots_for_kernel) {
      note.textContent =
        view.this_host + " is updated on its own, after the others, since " +
        "it drives the run. When it gets a new kernel, the run checks it, " +
        "schedules its reboot and ends, and it finishes its update itself " +
        "once its new system is up.";
    } else if (view.this_host && others.length) {
      note.textContent =
        view.this_host + " is updated on its own, after the others: the run " +
        "ends with its reboot, and it finishes its update itself once its " +
        "new system is up.";
    } else if (view.this_host && view.reboots_for_kernel) {
      note.textContent =
        "When this machine gets a new kernel, the run checks it, schedules " +
        "its reboot and ends, and the machine finishes its update itself " +
        "once its new system is up.";
    } else if (view.this_host) {
      note.textContent =
        "The run ends with the reboot of this machine, which finishes its " +
        "update itself once its new system is up. Check for updates " +
        "afterwards to see that it did.";
    } else {
      note.textContent = "";
    }
  }

  function renderUnavailable() {
    const box = element("unavailable");
    const reasons = refusals();
    box.textContent = reasons.join(" ");
    box.hidden = reasons.length === 0;
  }

  function draw(answer) {
    view = answer;
    // A machine the inventory no longer declares cannot stay ticked.
    const hosts = new Set(view.machines.map((machine) => machine.host));
    Array.from(selected).forEach((host) => {
      if (!hosts.has(host)) {
        selected.delete(host);
      }
    });
    element("loading").hidden = true;
    element("note").textContent = view.note || "";
    element("note").hidden = !view.note;
    element("table").hidden = view.machines.length === 0;
    element("check").hidden = !canCheck || view.machines.length === 0;
    renderLead();
    renderUnavailable();
    renderRows();
    renderActs();
  }

  function showPackages(machine) {
    const simulation = machine.reading.simulation;
    element("packages-host").textContent = machine.host;
    const rows = element("packages-rows");
    rows.replaceChildren();
    const add = (item, after, className) => {
      const line = document.createElement("tr");
      const name = document.createElement("div");
      name.append(span(item.name, className));
      if (item.kernel) {
        name.append(" ");
        name.append(span("kernel", "tag warn"));
      }
      line.append(
        cell(name),
        cell(item.current || ""),
        cell(after),
        cell(item.origin || "")
      );
      rows.append(line);
    };
    simulation.upgrades.forEach((item) => add(item, item.candidate));
    simulation.installs.forEach((item) => add(item, item.candidate + " (new)"));
    simulation.removals.forEach((item) => add(item, "removed", "state-failed"));
    const note = element("packages-note");
    note.textContent = machine.reading.refresh_error
      ? "The package lists could not be refreshed, so this is measured " +
        "against the lists the machine already had."
      : "";
    note.hidden = !note.textContent;
    element("packages-card").hidden = false;
    element("packages-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function confirm({ title, body, note, label, act }) {
    element("confirm-title").textContent = title;
    element("confirm-disruption").textContent = body;
    element("confirm-note").textContent = note || "";
    element("confirm-note").hidden = !note;
    element("confirm-error").hidden = true;

    const go = element("confirm-go");
    go.textContent = label;
    go.disabled = false;
    go.onclick = async () => {
      go.disabled = true;
      go.setAttribute("aria-busy", "true");
      try {
        await act();
        element("confirm").hidden = true;
      } catch (failure) {
        const error = element("confirm-error");
        error.textContent = failure.message;
        error.hidden = false;
        go.disabled = false;
      } finally {
        go.removeAttribute("aria-busy");
      }
    };
    element("confirm").hidden = false;
  }

  function confirmCheck() {
    confirm({
      title: "Check the machines for software updates",
      body:
        "Refreshes the package lists of " +
        view.machines.map((machine) => machine.host).join(", ") +
        " from the sources each one is configured with, and asks apt what " +
        "an upgrade would install, replace and remove. Nothing is installed " +
        "and nothing restarts.",
      label: "Check",
      act: async () => {
        RunWatch.open((await API.post("/software/check")).run_id, refresh);
      },
    });
  }

  function confirmUpdate() {
    const hosts = view.machines
      .map((machine) => machine.host)
      .filter((host) => selected.has(host));
    const entry = view.update.entry;
    const cramped = view.machines
      .filter((machine) => selected.has(machine.host))
      .filter((machine) =>
        machine.reading && machine.reading.snapshot &&
          !machine.reading.snapshot.enough && !machine.stale
      )
      .map((machine) => machine.host);
    // Only ever alone: the server refuses this machine beside others.
    const itself = hosts.includes(view.this_host);
    const chosen = view.machines.filter((machine) => selected.has(machine.host));
    const rebooting = chosen.filter(reboots).map((machine) => machine.host);
    const unknown = chosen
      .filter((machine) => !machine.reading || machine.reading.error || machine.stale)
      .map((machine) => machine.host);
    confirm({
      title: "Update " + hosts.join(", "),
      body: entry.disruption,
      note:
        (view.reboots_for_kernel
          ? (rebooting.length
              ? "At the last check, " + rebooting.join(", ") +
                (rebooting.length === 1
                  ? " gets a new kernel and reboots. "
                  : " get a new kernel and reboot. ")
              : unknown.length < chosen.length
                ? "At the last check, none of them gets a new kernel, and " +
                  "none reboots. "
                : "") +
            (unknown.length
              ? "No current check says whether " + unknown.join(", ") +
                (unknown.length === 1 ? " reboots. " : " reboot. ")
              : "")
          : "") +
        (itself && view.reboots_for_kernel
          ? "Should " + view.this_host + " reboot, the run checks it, " +
            "schedules the reboot and ends, and this page goes away a few " +
            "seconds later. "
          : "") +
        (itself && !view.reboots_for_kernel
          ? "This page goes away when " + view.this_host + " reboots, and " +
            "the run ends there without a final status, as it should. Once " +
            "the page is back, check for updates to see that the machine " +
            "finished its update. "
          : "") +
        (cramped.length
          ? "At the last check, " + cramped.join(", ") +
            " had no room for the snapshot, and the update stops there " +
            "before changing anything unless that was fixed since. "
          : "") + entry.notes,
      label: view.reboots_for_kernel
        ? (hosts.length === 1 ? "Update it" : "Update them")
        : (hosts.length === 1 ? "Update and reboot it" : "Update and reboot them"),
      act: async () => {
        const launched = await API.post("/software/update", { hosts });
        selected.clear();
        RunWatch.open(launched.run_id, refresh);
      },
    });
  }

  async function refresh() {
    draw(await API.get("/software"));
  }

  element("check").addEventListener("click", confirmCheck);
  element("update").addEventListener("click", confirmUpdate);
  element("packages-close").addEventListener("click", () => {
    element("packages-card").hidden = true;
  });
  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  async function start() {
    const me = Chrome.current();
    // A check installs nothing, so it is the operator's; an update installs
    // packages and reboots machines, so it is the administrator's.
    canCheck = me.role === "operator" || Chrome.isAdmin(me);
    canUpdate = Chrome.isAdmin(me);
    Reread.attach(
      element("reread"),
      async () => {
        showBanner("");
        await refresh();
      },
      (failure) => showBanner(failure.message)
    );
    await refresh();
  }

  start().catch((failure) => {
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
