// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The Containers page: a quadlet the inventory uploads, the systemd unit it
// becomes on each machine, and the Pacemaker resource holding it where the
// cluster was told to.
//
// The row shape follows the act rather than the object, which is the one
// decision worth stating. A container Pacemaker holds is one row: starting it
// is one call, and the cluster picks the node. A container nothing holds is a
// row per machine, because it is a unit on each of them and stopping it on one
// says nothing about the others.
//
// One act writes: declaring a container. It is two requests behind one button,
// the file and the commit, and no run. Uploading a quadlet to the machines is
// the prerequisites playbook, which restarts a great deal more than a
// container, so this page names it and the operator launches it from the page
// that spells out what it disturbs.

(function () {
  let canAct = false;
  let mode = "standalone";
  let view = null;

  function element(id) {
    return document.getElementById(id);
  }

  function showBanner(message) {
    const banner = element("banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  function cell(text, className) {
    const node = document.createElement("td");
    node.textContent = text;
    if (className) {
      node.className = className;
    }
    return node;
  }

  function clear(node) {
    node.replaceChildren();
    return node;
  }

  function row(parent, cells) {
    const line = document.createElement("tr");
    cells.forEach((item) => line.append(item));
    parent.append(line);
    return line;
  }

  function dotted(status, words, title) {
    const node = document.createElement("td");
    const box = document.createElement("span");
    const dot = document.createElement("span");
    const text = document.createElement("span");
    dot.className = "dot status-" + status;
    text.textContent = words;
    box.className = "legend-item";
    if (title) {
      box.title = title;
    }
    box.append(dot, text);
    node.append(box);
    return node;
  }

  // The name, with the kind beside it where it is anything but a container.
  // A `.volume` or a `.network` is pulled in by the container that uses it, so
  // the page shows it and offers no button on it.
  function named(container) {
    const node = document.createElement("td");
    const name = document.createElement("span");
    name.textContent = container.name;
    node.append(name);
    if (container.kind !== ".container") {
      const kind = document.createElement("code");
      kind.textContent = container.kind;
      kind.className = "assumed";
      node.append(" ", kind);
    }
    if ((container.warnings || []).length) {
      const flag = document.createElement("span");
      flag.className = "missing";
      flag.textContent = " !";
      flag.title = container.warnings.join("\n\n");
      node.append(flag);
    }
    return node;
  }

  // Who starts and stops it, which decides everything else on the row.
  function managedBy(container) {
    if (container.managed === "pacemaker") {
      return dotted(
        "info",
        "Pacemaker",
        "The cluster holds a resource for this unit, so it decides which " +
          "member runs it and restarts it where it fails."
      );
    }
    return dotted(
      "unknown",
      "systemd",
      "No Pacemaker resource holds this unit, so it is an ordinary systemd " +
        "unit on each machine the inventory sends it to."
    );
  }

  // What the unit is doing on one machine, read from its own node_exporter.
  function unitState(unit) {
    if (!unit) {
      return dotted("unknown", "not read");
    }
    if (!unit.reachable) {
      return dotted("warning", "no answer", unit.error);
    }
    if (!unit.known) {
      return dotted(
        "unknown",
        "no unit yet",
        "This machine's exporter does not publish the unit, so the file has " +
          "not been uploaded there or systemd has not read it yet. The run " +
          "named below is what does both."
      );
    }
    if (unit.failed) {
      return dotted("warning", "failed");
    }
    if (unit.active) {
      return dotted(
        "ok",
        "active",
        unit.started_at ? "Started " + unit.started_at : ""
      );
    }
    return dotted("unknown", unit.state);
  }

  // What the cluster says about the resource, for the containers it holds.
  function resourceState(resource) {
    if (!resource) {
      return dotted("unknown", "no resource yet");
    }
    if (resource.failed) {
      return dotted(
        "warning",
        "failed" + (resource.fail_count ? ", " + resource.fail_count + " times" : "")
      );
    }
    if (resource.role === "started") {
      return dotted("ok", "running");
    }
    return dotted("unknown", resource.role || resource.state || "known");
  }

  // The file, named the way the machines will name it. What was here was the
  // `src` the inventory keeps it under, which answers a question about the
  // control machine: what an operator looking at a container wants is the name
  // they will find by listing /etc/containers/systemd, and the path is on
  // hover for the day the two have to be told apart.
  //
  // It opens the file where this node holds one. A quadlet is a dozen lines
  // and every question the rest of the row raises is answered in them: which
  // image, which ports, and whether an [Install] section is about to start the
  // container behind Pacemaker's back.
  function quadletFile(container) {
    const node = document.createElement("td");
    const missing = container.file && !container.file.found;
    const where = container.dest + ", uploaded from " + container.src + ".";

    if (container.readable) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "quadlet-open";
      open.textContent = container.file_name;
      open.title = where + " Opens it.";
      open.addEventListener("click", () => openQuadlet(container));
      node.append(open);
      return node;
    }

    const name = document.createElement("code");
    name.textContent = container.file_name;
    node.append(name);
    if (missing) {
      node.className = "missing";
      const said = document.createElement("span");
      said.textContent = " (nothing here holds it)";
      node.append(said);
      node.title = where + " Nothing this node holds answers to it.";
      return node;
    }
    node.title = where + " The file itself is not one this page can show.";
    return node;
  }

  // The file as the inventory carries it, read only. Editing it is the
  // Inventory page, where a write is a commit with a diff and the validation
  // that belongs to one.
  async function openQuadlet(container) {
    const text = element("quadlet-text");
    const error = element("quadlet-error");
    element("quadlet-title").textContent = container.file_name;
    element("quadlet-note").textContent =
      "Uploaded to " +
      (container.hosts || []).join(", ") +
      " as " +
      container.dest +
      ", and kept in the inventory as " +
      container.src +
      ". The Inventory page is where it is edited.";
    text.hidden = true;
    text.textContent = "";
    error.hidden = true;
    element("quadlet-loading").hidden = false;
    element("quadlet").hidden = false;
    try {
      const file = await API.get(
        "/containers/" + encodeURIComponent(container.name) + "/file"
      );
      text.textContent = file.content;
      text.hidden = false;
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      element("quadlet-loading").hidden = true;
    }
  }

  function scope(container) {
    const node = document.createElement("td");
    const text = document.createElement("code");
    text.textContent = container.scope_name;
    node.append(text);
    node.title =
      container.scope_kind === "group"
        ? "The upload entry is written on this group, so every machine of it " +
          "receives the quadlet."
        : "The upload entry is written on this machine alone.";
    return node;
  }

  // Where a container Pacemaker holds is running, and what put it there. One
  // cell, because the second answers the question the first raises: a member
  // is either the cluster's own choice or somebody's move, and the constraint
  // is the only thing that says which.
  //
  // Three states and no fourth. A container has no `preferred_host`: the
  // inventory says which machines receive the quadlet and never which member
  // runs it, so there is no declared placement to hold the constraint against
  // and the whole reading is the constraint and the node.
  function placedCell(container) {
    const resource = container.resource || {};
    const node = document.createElement("td");
    node.className = "placed";
    if (!resource.node) {
      node.textContent = "the cluster chooses";
      node.title =
        "Pacemaker holds a resource for this container and is not running " +
        "it anywhere at the moment.";
      return node;
    }
    const name = document.createElement("span");
    name.className = "placement " + container.placement;
    name.textContent = resource.node;
    name.title = explainPlacement(container, resource.node);
    node.append(name);
    return node;
  }

  // The colour says which of the three states the placement is in, and the
  // sentence behind it says what to do about that one.
  function explainPlacement(container, where) {
    const held = container.constraint;
    if (container.pinned) {
      return (
        container.pinned +
        " pins this container to that machine: it runs there or nowhere. " +
        "Nothing short of rebuilding the resource removes that rule, so " +
        "neither Move nor Return is offered here."
      );
    }
    if (!held) {
      return (
        "No constraint holds " +
        container.name +
        ", so the cluster places it and moves it where a member fails. Move " +
        "writes a constraint naming a machine."
      );
    }
    if (held.node !== where) {
      return (
        held.id +
        " names " +
        held.node +
        ", and the container is running on " +
        where +
        ". A placement carries an infinite score, so the cluster put it here " +
        "only because " +
        held.node +
        " could not take it: offline, in standby, or the container failed " +
        "there. Return removes the constraint and gives the placement back " +
        "to the cluster."
      );
    }
    return (
      held.id +
      " holds " +
      container.name +
      " on " +
      held.node +
      ", so the cluster is not free to place it. Return removes the " +
      "constraint."
    );
  }

  function actButton(container, host, running) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = running ? "Stop" : "Start";
    button.addEventListener("click", () =>
      confirmAct(container, host, running ? "stop" : "start")
    );
    return button;
  }

  function action(label, onclick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = label;
    button.addEventListener("click", onclick);
    return button;
  }

  function acts(container, host, running) {
    const node = document.createElement("td");
    node.className = "acts";
    if (!canAct || !container.actionable) {
      return node;
    }
    node.append(actButton(container, host, running));
    // Placement, on the containers the cluster places. It is the Cluster
    // page's pair of acts on the Cluster page's own object: the resource
    // holding this unit is a Pacemaker resource like any other, so the two
    // buttons call `/cluster/resources/{id}` rather than growing a second
    // route that would write the same constraint by another name.
    if (container.managed === "pacemaker" && container.resource) {
      if ((container.destinations || []).length) {
        node.append(" ", action("Move", () => confirmMove(container)));
      }
      if (container.constraint) {
        node.append(" ", action("Return", () => confirmReturn(container)));
      }
    }
    return node;
  }

  function renderContainers(payload) {
    const body = clear(element("container-rows"));
    const containers = payload.containers || [];

    element("loading").hidden = true;
    element("container-table").hidden = containers.length === 0;
    element("empty").textContent = payload.note || "";
    element("empty").hidden = !payload.note;
    element("runtime-note").textContent = payload.runtime_note || "";
    element("placement-key").hidden = !containers.some(
      (container) => container.managed === "pacemaker"
    );

    const lead = element("lead");
    const warnings = payload.warnings || [];
    lead.textContent = warnings.join(" ");
    lead.hidden = warnings.length === 0;

    containers.forEach((container) => {
      if (container.managed === "pacemaker") {
        // One row: the act is the cluster's and the node is its answer.
        const resource = container.resource || {};
        row(body, [
          named(container),
          managedBy(container),
          placedCell(container),
          resourceState(container.resource),
          quadletFile(container),
          scope(container),
          acts(container, "", resource.role === "started"),
        ]);
        return;
      }
      // One row per machine: the same quadlet is a unit on each of them, and
      // each has its own state and its own button.
      (container.units || []).forEach((unit) => {
        row(body, [
          named(container),
          managedBy(container),
          cell(unit.host),
          unitState(unit),
          quadletFile(container),
          scope(container),
          acts(container, unit.host, unit.active),
        ]);
      });
    });
  }

  function renderUndeclared(payload) {
    const undeclared = payload.undeclared || [];
    element("undeclared-card").hidden = undeclared.length === 0;
    const body = clear(element("undeclared-rows"));
    undeclared.forEach((resource) => {
      row(body, [
        cell(resource.id),
        cell(resource.agent.replace(/^systemd:/, "")),
        cell(resource.node || ""),
        resourceState(resource),
      ]);
    });
  }

  // Stopping a container stops what it was serving, and on these machines that
  // is a substation function. The confirmation names it, names the machine
  // where there is one, and says what the act does.
  function disruption(container, host, action) {
    if (container.managed === "pacemaker") {
      return action === "stop"
        ? "Sets the resource's target role to Stopped, so Pacemaker stops it " +
            "wherever it is running and leaves it down until it is started " +
            "again, a node failure included. Whatever it was serving stops " +
            "with it."
        : "Clears the resource's target role, so Pacemaker starts it and " +
            "chooses the node. Which member it lands on is the cluster's " +
            "decision.";
    }
    return action === "stop"
      ? "Stops the container on " +
          host +
          ", and whatever it was serving stops with it. The other machines " +
          "this quadlet is uploaded to are untouched."
      : "Starts the unit podman's generator wrote from the quadlet, on " +
          host +
          ". It comes up with whatever the file on that machine says, which " +
          "is the version the last convergence uploaded.";
  }

  // `choose` is the destination a move needs. The node is part of the act, so
  // it is picked in the window that names the disruption rather than in a
  // control on the row an operator could leave set from last time.
  function confirm({ title, body, note, label, choose, act }) {
    element("confirm-title").textContent = title;
    element("confirm-disruption").textContent = body;
    element("confirm-note").textContent = note || "";
    element("confirm-note").hidden = !note;
    element("confirm-error").hidden = true;

    const picker = element("confirm-node");
    element("confirm-choice").hidden = !choose;
    if (choose) {
      element("confirm-choice-label").textContent = choose.label;
      clear(picker);
      choose.options.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        picker.append(option);
      });
    }

    const go = element("confirm-go");
    go.textContent = label;
    go.disabled = false;
    go.onclick = async () => {
      go.disabled = true;
      go.setAttribute("aria-busy", "true");
      try {
        await act(choose ? picker.value : undefined);
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

  // Moving a container is the Cluster page's act on the Cluster page's object:
  // `crm resource move` writes the `cli-prefer` constraint, and nothing about
  // the container is special about it. What it costs is a stop and a start,
  // because a container does not migrate: podman has no live migration and
  // Pacemaker's systemd agent stops the unit on one member and starts it on
  // the other.
  function confirmMove(container) {
    const held = container.constraint;
    const resource = container.resource || {};
    confirm({
      title: "Move " + container.name,
      body:
        "Writes the cli-prefer constraint naming the machine, and the cluster " +
        "then runs the container there. It is stopped on " +
        (resource.node || "the member running it") +
        " and started on the machine chosen here, so whatever it was serving " +
        "stops in between: a container has no live migration.",
      note: held
        ? held.id +
          " already holds it on " +
          held.node +
          ", and this replaces that rule. Return gives the placement back to " +
          "the cluster."
        : "The constraint stays until Return removes it, and while it is " +
          "there the cluster places this container where it says rather than " +
          "where it would choose.",
      choose: { label: "Run it on", options: container.destinations },
      label: "Move",
      act: async (node) => {
        const started = await API.post(
          "/cluster/resources/" + encodeURIComponent(resource.id) + "/move",
          { node }
        );
        RunWatch.open(started.run_id);
      },
    });
  }

  // And its inverse. `crm resource clear` removes the bans with the
  // preference, and a ban is how a container is kept off a machine, so the
  // confirmation names the ones it is about to take with it.
  function confirmReturn(container) {
    const held = container.constraint;
    const resource = container.resource || {};
    confirm({
      title: "Return " + container.name + " to the cluster",
      body:
        "Removes " +
        (held ? held.id : "the cli-prefer constraint") +
        ", so Pacemaker places this container by its own rules again. It may " +
        "move it as a result, at the same cost the move had: the container " +
        "is stopped where it runs and started where the cluster puts it.",
      note:
        "The inventory declares no placement for a container, so nothing is " +
        "written back: `upload_extra_files` says which machines receive the " +
        "quadlet and the cluster decides which of them runs it.",
      label: "Return",
      act: async () => {
        const started = await API.post(
          "/cluster/resources/" + encodeURIComponent(resource.id) + "/clear"
        );
        RunWatch.open(started.run_id);
      },
    });
  }

  function confirmAct(container, host, action) {
    const verb = action === "stop" ? "Stop" : "Start";
    confirm({
      title:
        verb + " " + container.name + (host ? " on " + host : " on the cluster"),
      body: disruption(container, host, action),
      note:
        action === "stop" && container.managed !== "pacemaker"
          ? "systemd starts it again at the next boot if the quadlet carries " +
            "an [Install] section. Switching a container off for good is a " +
            "change to the inventory."
          : "",
      label: verb + " it",
      act: async () => {
        const query = host ? "?host=" + encodeURIComponent(host) : "";
        const started = await API.post(
          "/containers/" + encodeURIComponent(container.name) + "/" + action + query
        );
        RunWatch.open(started.run_id);
      },
    });
  }

  // Declaring one

  function steps(names) {
    const list = element("add-steps");
    list.replaceChildren();
    names.forEach((name) => {
      const item = document.createElement("li");
      item.textContent = name;
      list.append(item);
    });
    list.hidden = false;
    return {
      at(index, className, text) {
        const item = list.children[index];
        item.className = className;
        if (text) {
          item.textContent = text;
        }
      },
    };
  }

  function fillScopes(payload) {
    const select = clear(element("add-scope"));
    (payload.scopes || []).forEach((item) => {
      const label =
        (item.kind === "group" ? "group " : "machine ") +
        item.name +
        " (" +
        item.machines.join(", ") +
        ")";
      const option = new Option(label, item.kind + ":" + item.name);
      if (!item.available) {
        // Refused before the operator picks it rather than after, with the
        // reason under the pointer.
        option.disabled = true;
        option.title = item.reason;
        option.text = label + " - unavailable";
      }
      select.append(option);
    });
    const cluster = mode === "cluster";
    element("add-pacemaker-row").hidden = !cluster;
    element("add-pacemaker-help").hidden = !cluster;
  }

  function showAdd(open) {
    element("add-modal").hidden = !open;
    if (open) {
      element("add-error").hidden = true;
      element("add-next").hidden = true;
      element("add-steps").hidden = true;
      element("add-name").focus();
    }
  }

  async function declare() {
    const name = element("add-name").value.trim();
    const file = element("add-file").files[0];
    const chosen = element("add-scope").value;
    const error = element("add-error");
    error.hidden = true;

    if (!name || !file || !chosen) {
      error.textContent =
        "A container needs a name, a quadlet file and somewhere to upload it.";
      error.hidden = false;
      return;
    }

    // The name the entry will carry. `.j2` is kept, because the upload role
    // renders a template and copies anything else, and the destination is the
    // same file either way.
    const suffix = file.name.endsWith(".j2") ? ".container.j2" : ".container";
    const path = "files/" + name + suffix;
    const [kind, scopeName] = chosen.split(":");
    const pacemaker = element("add-pacemaker").checked && mode === "cluster";

    const progress = steps([
      "Committing " + path,
      "Declaring " + name + " in the inventory",
    ]);
    const go = element("add-go");
    go.disabled = true;
    go.setAttribute("aria-busy", "true");
    try {
      progress.at(0, "doing");
      await API.upload("/inventory/files/" + path, file);
      progress.at(0, "done");

      progress.at(1, "doing");
      const declared = await API.post("/containers", {
        name,
        scope_kind: kind,
        scope_name: scopeName,
        src: "../" + path,
        pacemaker,
      });
      progress.at(1, "done");

      // The runs that make it so, named rather than launched. Both are wide
      // acts on live machines, and the page that describes what they disturb
      // is the one that launches them.
      const next = element("add-next");
      next.textContent =
        "Declared as " +
        declared.commit.slice(0, 12) +
        ". It reaches the machines on the next run of " +
        declared.playbook +
        (declared.cluster_playbook
          ? ", and Pacemaker takes it over on the next run of " +
            declared.cluster_playbook
          : "") +
        ", launched from the Deployment page.";
      next.hidden = false;
      await refresh();
    } catch (failure) {
      const doing = [...element("add-steps").children].findIndex(
        (item) => item.className === "doing"
      );
      if (doing !== -1) {
        progress.at(doing, "failed");
      }
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      go.disabled = false;
      go.removeAttribute("aria-busy");
    }
  }

  const KEPT = "containers";

  function draw(answer) {
    view = answer;
    mode = answer.mode;
    renderContainers(answer);
    renderUndeclared(answer);
    fillScopes(answer);
  }

  async function refresh(fresh, pending) {
    draw(await (pending || API.get(API.reading("/containers", fresh))));
    Kept.keep(KEPT, view);
    Kept.release();
  }

  element("add").addEventListener("click", () => showAdd(true));
  element("add-cancel").addEventListener("click", () => showAdd(false));
  element("add-go").addEventListener("click", declare);
  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });
  element("quadlet-close").addEventListener("click", () => {
    element("quadlet").hidden = true;
  });

  async function start() {
    const me = Chrome.current();
    // Starting a container changes no desired state, so it is the operator's
    // act, the way starting a guest is.
    canAct = me.role === "operator" || Chrome.isAdmin(me);
    // A reading that succeeds clears the failure the last one reported, and
    // one that fails leaves the table showing the answer it already had.
    Reread.attach(
      element("reread"),
      async (fresh) => {
        showBanner("");
        await refresh(fresh);
      },
      (failure) => showBanner(failure.message)
    );
    // The request leaves first, so the reading is in flight while this browser
    // paints the table it last drew. The controls in it are held until the
    // answer lands: a unit may have stopped since, and starting or stopping one
    // from a row that old is an act aimed at the wrong state. See D46.
    const pending = API.started("/containers");
    const age = Kept.paint(KEPT, draw);
    if (age !== null) {
      Kept.rereading(["loading"], age);
      Kept.hold(["card-containers", "undeclared-card"]);
    }
    await refresh(false, pending);
    // Declaring one is a commit, which is an administrator's act like every
    // other write in this service.
    element("add").hidden = !Chrome.isAdmin(me);
  }

  start().catch((failure) => {
    Kept.release();
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
