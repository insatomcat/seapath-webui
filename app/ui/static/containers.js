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
  let canWrite = false;
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

  // The files as the inventory carries them, read only. Editing them is the
  // Inventory page, where a write is a commit with a diff and the validation
  // that belongs to one.
  //
  // One tab per file, because a container is rarely one: a pod names its
  // networks and its containers, and a workload carries the settings its RBD
  // image holds. A file this node cannot show says why in its own tab, so the
  // one a site has not uploaded yet does not hide the others.
  async function openQuadlet(container) {
    const tabs = clear(element("quadlet-files"));
    element("quadlet-title").textContent = container.name;
    element("quadlet-note").textContent = "";
    element("quadlet-text").hidden = true;
    element("quadlet-file-error").hidden = true;
    element("quadlet-error").hidden = true;
    tabs.hidden = true;
    element("quadlet-loading").hidden = false;
    element("quadlet").hidden = false;
    try {
      const answer = await API.get(
        "/containers/" + encodeURIComponent(container.name) + "/files"
      );
      const files = answer.files || [];
      const buttons = files.map((file, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.setAttribute("role", "tab");
        button.textContent = file.file_name;
        button.addEventListener("click", () => {
          buttons.forEach((other) => other.setAttribute("aria-pressed", "false"));
          button.setAttribute("aria-pressed", "true");
          showQuadlet(container, file);
        });
        tabs.append(button);
        return button;
      });
      // A single file needs no tab to be told apart from the others.
      tabs.hidden = files.length < 2;
      if (buttons.length) {
        buttons[0].click();
      }
    } catch (failure) {
      element("quadlet-error").textContent = failure.message;
      element("quadlet-error").hidden = false;
    } finally {
      element("quadlet-loading").hidden = true;
    }
  }

  function showQuadlet(container, file) {
    const text = element("quadlet-text");
    const error = element("quadlet-file-error");
    element("quadlet-note").textContent =
      (file.on_rbd
        ? "Written on the RBD image of " + container.name + " as " + file.dest
        : "Uploaded to " + (container.hosts || []).join(", ") + " as " + file.dest) +
      ", and kept in the inventory as " +
      file.src +
      ". The Inventory page is where it is edited.";
    text.textContent = file.content || "";
    text.hidden = Boolean(file.error);
    error.textContent = file.error || "";
    error.hidden = !file.error;
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
  // is either the cluster's own choice, the entry's `preferred_host`, or
  // somebody's move, and only the constraints held against the entry say
  // which.
  //
  // The states are the VMs page's, computed by the service: the entry, the
  // rule in force and the node the resource is on are the three things held
  // against each other.
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

  // The colour says which state the placement is in, and the sentence behind
  // it says what to do about that one. The rule in force is the move's
  // `cli-prefer` where there is one, since its infinite score overrides the
  // deployment's `prefer-`, whose score of 100 the cluster only weighs.
  function explainPlacement(container, where) {
    const name = container.name;
    const declared = container.preferred_host || "";
    const moved = container.constraint;
    const preference = container.preference;
    const held = moved || preference;
    if (container.pinned) {
      return (
        container.pinned +
        " pins this container to that machine: it runs there or nowhere. " +
        "Nothing short of rebuilding the resource removes that rule, so " +
        "neither Move nor Return is offered here."
      );
    }
    if (held && held.node !== where) {
      const why = moved
        ? ". A move carries an infinite score, so the cluster put it here " +
          "only because " +
          held.node +
          " could not take it: offline, in standby, or the container failed " +
          "there."
        : ". The deployment wrote it with a score of 100, which the cluster " +
          "weighs against the rest of its rules, so " +
          held.node +
          " could not take it or another rule outweighed it.";
      return (
        held.id + " names " + held.node + ", and " + name +
        " is running on " + where + why + returnSentence(container)
      );
    }
    if (!held && !declared) {
      return (
        "No constraint holds " + name + ", and its inventory entry declares " +
        "no preferred_host, so the cluster places it. Move writes a " +
        "constraint naming a machine."
      );
    }
    if (!held) {
      return (
        "No constraint holds " + name + ", and its inventory entry declares " +
        "preferred_host: " + declared + ". A deployment run of " +
        (container.playbook || "the workloads") +
        " writes that preference to the cluster."
      );
    }
    if (held.node === declared) {
      return (
        held.id + ". The cluster keeps " + name + " on " + held.node +
        ", which is the preferred_host its inventory entry declares." +
        (moved ? returnSentence(container) : "")
      );
    }
    const entry = declared
      ? ", and its inventory entry declares preferred_host: " + declared + "."
      : ", and its inventory entry declares no preferred_host.";
    const cause = moved
      ? " A move from this page writes that constraint, and so does crm " +
        "resource move typed on a machine." + returnSentence(container)
      : " The rule is what the last deployment wrote, and a deployment run " +
        "writes the entry's value again.";
    return (
      held.id + ". The cluster keeps " + name + " on " + held.node + entry + cause
    );
  }

  // What Return leaves behind: `crm resource clear` removes the move's
  // `cli-prefer` and never the deployment's `prefer-`, so the container goes
  // back to the preference the cluster carries, or to no preference at all.
  function returnSentence(container) {
    const preference = container.preference;
    if (!container.constraint) {
      return "";
    }
    return preference
      ? " Return removes " + container.constraint.id + " and leaves " +
          preference.id + ", so the cluster leans to " + preference.node +
          " again."
      : " Return removes " + container.constraint.id + " and leaves the " +
          "placement to the cluster.";
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
      if (running) {
        node.append(" ", action("Restart", () => confirmRestart(container)));
      }
      if ((container.destinations || []).length) {
        node.append(" ", action("Move", () => confirmMove(container)));
      }
      if (container.constraint) {
        node.append(" ", action("Return", () => confirmReturn(container)));
      }
    }
    if (canWrite && container.values_editable) {
      node.append(" ", action("Values", () => openValues(container)));
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
      note: container.preference
        ? container.preference.id +
          " stays: a deployment wrote it from preferred_host: " +
          container.preference.node +
          ", and a clear leaves it, so the cluster leans back to that machine."
        : container.preferred_host
          ? "The inventory entry declares preferred_host: " +
            container.preferred_host +
            ", and the cluster carries no rule for it yet: a deployment run " +
            "writes it."
          : "The inventory entry declares no preferred_host, so nothing " +
            "is written back and the cluster decides which member runs it.",
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

  // A restart is how a changed quadlet, image or site value reaches a running
  // workload, and it stops what the workload serves until it is up again.
  function confirmRestart(container) {
    confirm({
      title: "Restart " + container.name + " on the cluster",
      body:
        "Stops the resource where it runs and starts it again, which is how a " +
        "changed quadlet, image or site value takes effect. Whatever it " +
        "serves is down in between, for as long as the workload takes to " +
        "stop and to start.",
      label: "Restart it",
      act: async () => {
        const started = await API.post(
          "/containers/" + encodeURIComponent(container.name) + "/restart"
        );
        RunWatch.open(started.run_id);
      },
    });
  }

  // Site values, as a delivery's values.yaml describes them. The form is
  // built from the fields rather than written per workload, and the service
  // checks each value against its format again before anything is committed.

  function valueForm(holder, fields) {
    holder.replaceChildren();
    fields.forEach((field) => {
      const id = holder.id + "-" + field.key;
      const label = document.createElement("label");
      label.htmlFor = id;
      label.textContent = field.key;
      let input;
      if (field.format === "boolean") {
        input = document.createElement("select");
        ["true", "false"].forEach((value) => input.append(new Option(value, value)));
      } else {
        input = document.createElement("input");
        input.type = field.format === "integer" ? "number" : "text";
        input.autocomplete = "off";
        input.spellcheck = false;
        if (field.example !== null && field.example !== undefined) {
          input.placeholder = String(field.example);
        }
      }
      input.id = id;
      input.dataset.key = field.key;
      const initial =
        field.current !== null && field.current !== undefined
          ? field.current
          : field.has_default
            ? field.default
            : "";
      input.value = initial === null || initial === undefined ? "" : String(initial);
      const help = document.createElement("p");
      help.className = "help";
      help.id = id + "-help";
      help.textContent =
        field.description +
        " (" +
        field.format +
        (field.has_default ? ", " + String(field.default) + " when empty" : "") +
        ")";
      holder.append(label, input, help);
    });
  }

  function formValues(holder) {
    const found = {};
    holder.querySelectorAll("[data-key]").forEach((input) => {
      found[input.dataset.key] = input.value;
    });
    return found;
  }

  // A refusal names each value it refused, beside the field it is about.
  function showRefusal(holder, error, failure) {
    const refused = (failure.detail && failure.detail.refused) || {};
    holder.querySelectorAll("[data-key]").forEach((input) => {
      const reason = refused[input.dataset.key];
      input.setAttribute("aria-invalid", reason ? "true" : "false");
      const help = element(input.id + "-help");
      if (reason) {
        help.dataset.reason = reason;
        help.textContent = input.dataset.key + " " + reason;
      }
    });
    error.textContent = failure.message;
    error.hidden = false;
  }

  // Installing a delivery

  let staged = null;

  function showDelivery(open) {
    element("delivery").hidden = !open;
    if (!open) {
      if (staged && !staged.installed) {
        API.del("/containers/deliveries/" + staged.id).catch(() => {});
      }
      staged = null;
      return;
    }
    staged = null;
    element("delivery-file").value = "";
    element("delivery-pick").hidden = false;
    element("delivery-summary").hidden = true;
    element("delivery-findings").hidden = true;
    element("delivery-error").hidden = true;
    element("delivery-next").hidden = true;
    element("delivery-check").hidden = false;
    element("delivery-go").hidden = true;
  }

  async function checkDelivery() {
    const file = element("delivery-file").files[0];
    const error = element("delivery-error");
    error.hidden = true;
    if (!file) {
      error.textContent = "Choose the archive the supplier sent.";
      error.hidden = false;
      return;
    }
    const check = element("delivery-check");
    check.disabled = true;
    element("delivery-loading").hidden = false;
    try {
      staged = await API.upload("/containers/deliveries", file, {}, "POST");
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      return;
    } finally {
      check.disabled = false;
      element("delivery-loading").hidden = true;
    }
    const findings = clear(element("delivery-findings"));
    (staged.findings || []).forEach((finding) => {
      const item = document.createElement("li");
      item.textContent = finding;
      findings.append(item);
    });
    findings.hidden = findings.children.length === 0;
    if (!findings.hidden) {
      return;
    }
    element("delivery-pick").hidden = true;
    element("delivery-check").hidden = true;
    element("delivery-summary").hidden = false;
    element("delivery-what").textContent =
      (staged.update ? "Updates " : "Installs ") +
      staged.name +
      " from " +
      staged.version +
      ": " +
      staged.images.join(", ") +
      ", " +
      staged.quadlets.length +
      " quadlets" +
      (staged.files.length ? ", " + staged.files.length + " files on its RBD image" : "") +
      ".";
    element("delivery-readme").textContent = staged.readme || "";
    element("delivery-readme-box").hidden = !staged.readme;
    valueForm(element("delivery-values"), staged.values || []);
    element("delivery-go").hidden = false;
  }

  async function installDelivery() {
    const holder = element("delivery-values");
    const error = element("delivery-error");
    error.hidden = true;
    const go = element("delivery-go");
    go.disabled = true;
    try {
      const installed = await API.post(
        "/containers/deliveries/" + staged.id + "/install",
        { values: formValues(holder) }
      );
      staged.installed = true;
      go.hidden = true;
      const next = element("delivery-next");
      next.textContent =
        (installed.commit
          ? "Committed as " + installed.commit.slice(0, 12) + ". "
          : "Nothing changed. ") +
        "It reaches the machines on the next run of " +
        installed.playbook +
        ", launched from the Deployment page" +
        (staged.update ? ", and the running workload once it is restarted." : ".");
      next.hidden = false;
      await refresh(true);
    } catch (failure) {
      showRefusal(holder, error, failure);
    } finally {
      go.disabled = false;
    }
  }

  // The site values of an installed workload

  let editing = null;

  async function openValues(container) {
    editing = container.name;
    element("values-title").textContent = "Site values of " + container.name;
    element("values-error").hidden = true;
    element("values-next").hidden = true;
    element("values").hidden = false;
    try {
      const answer = await API.get(
        "/containers/" + encodeURIComponent(container.name) + "/values"
      );
      valueForm(element("values-form"), answer.values || []);
    } catch (failure) {
      element("values-error").textContent = failure.message;
      element("values-error").hidden = false;
    }
  }

  async function saveValues() {
    const holder = element("values-form");
    const error = element("values-error");
    error.hidden = true;
    const go = element("values-go");
    go.disabled = true;
    try {
      const saved = await API.put(
        "/containers/" + encodeURIComponent(editing) + "/values",
        { values: formValues(holder) }
      );
      const next = element("values-next");
      next.textContent = saved.commit
        ? "Committed as " +
          saved.commit.slice(0, 12) +
          ". Run deploy_containers_cluster from the Deployment page, then " +
          "restart " +
          editing +
          " here."
        : "Nothing changed.";
      next.hidden = false;
      await refresh(true);
    } catch (failure) {
      showRefusal(holder, error, failure);
    } finally {
      go.disabled = false;
    }
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
    showScope();
  }

  // A workload Pacemaker runs goes on every hypervisor of the cluster, so
  // there is no machine to choose for it.
  function showScope() {
    element("add-scope-block").hidden =
      element("add-pacemaker").checked && mode === "cluster";
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
    const pacemaker = element("add-pacemaker").checked && mode === "cluster";
    const chosen = pacemaker
      ? "group:cluster_machines"
      : element("add-scope").value;
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

      // The run that makes it so, named rather than launched. It is a wide
      // act on live machines, and the page that describes what it disturbs
      // is the one that launches it.
      const next = element("add-next");
      next.textContent =
        "Declared as " +
        declared.commit.slice(0, 12) +
        ". It reaches the machines on the next run of " +
        declared.playbook +
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

  element("install").addEventListener("click", () => showDelivery(true));
  element("delivery-cancel").addEventListener("click", () => showDelivery(false));
  element("delivery-check").addEventListener("click", checkDelivery);
  element("delivery-go").addEventListener("click", installDelivery);
  element("values-cancel").addEventListener("click", () => {
    element("values").hidden = true;
  });
  element("values-go").addEventListener("click", saveValues);
  element("add").addEventListener("click", () => showAdd(true));
  element("add-cancel").addEventListener("click", () => showAdd(false));
  element("add-go").addEventListener("click", declare);
  element("add-pacemaker").addEventListener("change", showScope);
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
    canWrite = Chrome.isAdmin(me);
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
    // A delivery is a workload Pacemaker runs, which a standalone machine has
    // no cluster for.
    element("install").hidden = !Chrome.isAdmin(me) || mode !== "cluster";
  }

  start().catch((failure) => {
    Kept.release();
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
