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

  function quadletFile(container) {
    const node = document.createElement("td");
    const path = document.createElement("code");
    path.textContent = container.src;
    node.append(path);
    if (container.file && !container.file.found) {
      node.className = "missing";
      const missing = document.createElement("span");
      missing.textContent = " (nothing here holds it)";
      node.append(missing);
    }
    return node;
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

  function acts(container, host, running) {
    const node = document.createElement("td");
    if (!canAct || !container.actionable) {
      return node;
    }
    node.append(actButton(container, host, running));
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
          cell(resource.node || "the cluster chooses"),
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

  async function refresh(fresh) {
    view = await API.get(API.reading("/containers", fresh));
    mode = view.mode;
    renderContainers(view);
    renderUndeclared(view);
    fillScopes(view);
  }

  element("add").addEventListener("click", () => showAdd(true));
  element("add-cancel").addEventListener("click", () => showAdd(false));
  element("add-go").addEventListener("click", declare);
  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  async function start() {
    const { me } = await Chrome.load();
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
    await refresh();
    // Declaring one is a commit, which is an administrator's act like every
    // other write in this service.
    element("add").hidden = !Chrome.isAdmin(me);
  }

  start().catch((failure) => {
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
