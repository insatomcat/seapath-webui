// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The VMs page: what the inventory declares, and what the cluster is doing
// with it, side by side.
//
// The two halves are labelled rather than merged, because they are two
// different kinds of truth. The definition columns are the desired state, held
// in git, changed by a commit. The state and node columns are Pacemaker's
// report of this moment, which no commit here can change and which a
// convergence does not describe.
//
// One act writes: adding a VM. It is four requests behind one button, the two
// uploads, the commit and the run, and each one says where it got to, because
// two of them move a file that can be very large and a lone spinner cannot
// tell an upload from a hung request.
//
// Underneath it is the ordinary path, and it stays ordinary: the image lands
// in the artefacts, the XML is committed with the inventory, the guest is a
// commit in the `VMs` group, and the guest is created by the upstream
// playbook. What D30 settles is that the operator is not made to walk it.

(function () {
  // Filled once the session and the reading are in: which role is signed in
  // decides whether the acting buttons exist at all, and the mode decides what
  // the stop confirmation has to warn about.
  let canAct = false;
  let mode = "standalone";

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

  function row(parent, cells) {
    const line = document.createElement("tr");
    cells.forEach((item) => line.append(item));
    parent.append(line);
    return line;
  }

  // A dot and its words in one cell, the way the cluster page reads a
  // resource. The wording is the operator's question rather than the
  // exporter's vocabulary: "running on node2" over "role started".
  function state(guest) {
    const node = document.createElement("td");
    const box = document.createElement("span");
    const dot = document.createElement("span");
    const resource = guest.resource;
    let status = "absent";
    let words = "not deployed";

    if (resource) {
      if (resource.failed) {
        status = "warning";
        words = "failed";
      } else if (resource.role === "started") {
        status = "ok";
        words = "running";
      } else {
        status = "unknown";
        words = resource.role || resource.state || "known";
      }
      if (resource.fail_count) {
        words += ", " + resource.fail_count + " failures";
      }
    }

    dot.className = "dot status-" + status;
    const text = document.createElement("span");
    text.textContent = words;
    box.className = "legend-item";
    box.append(dot, text);
    node.append(box);
    return node;
  }

  // A file the guest names, and whether a deployment would find it. A missing
  // one is the failure worth catching here: with `any_errors_fatal`, a copy
  // that cannot find its source ends the run on every host at once.
  function file(guest, value) {
    if (!value) {
      return cell("");
    }
    const reference = (guest.files || []).find((item) => item.value === value);
    const node = document.createElement("td");
    const path = document.createElement("code");
    path.textContent = value;
    node.append(path);
    if (reference && !reference.found) {
      // The same colour the Inventory page gives a file it does not hold, for
      // the same reason: the run stops at the task that copies it.
      node.className = "missing";
      const missing = document.createElement("span");
      missing.textContent = " (nothing here holds it)";
      node.append(missing);
    }
    return node;
  }

  // What the next deployment run does to this guest. `force` is the one worth
  // a column: the roles destroy and recreate a guest that carries it, so a run
  // an operator reads as "converge my VMs" reinstalls that one.
  function ondeploy(guest) {
    const words = [];
    if (guest.force) {
      words.push("recreated");
    }
    if (!guest.enable) {
      words.push("left stopped");
    }
    return cell(words.join(", ") || "left alone", guest.force ? "recreated" : "");
  }

  // Starting and stopping. The button offered is the one that changes
  // something: a guest Pacemaker reports as started is offered a stop, one it
  // reports as stopped a start, and a guest nothing reports at all neither,
  // because there is no domain to act on until it has been deployed.
  function acts(guest) {
    const cell = document.createElement("td");
    if (!canAct || !guest.resource) {
      return cell;
    }
    const running = guest.resource.role === "started";
    cell.append(actButton(guest.name, running ? "stop" : "start"));
    return cell;
  }

  function actButton(name, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = action === "stop" ? "Stop" : "Start";
    button.addEventListener("click", () => confirmAct(name, action));
    return button;
  }

  // Stopping a guest stops what it was serving, and on these machines that is
  // a substation function. The confirmation names the guest and says what the
  // act does, the way an apply names the machines it disturbs.
  const DISRUPTION = {
    start:
      "Starts the guest. In a cluster this asks Pacemaker to run it and " +
      "Pacemaker chooses the node, which is not necessarily the one it last " +
      "ran on.",
    stop:
      "Stops the guest, and whatever it was serving stops with it. In a " +
      "cluster the resource is disabled as well as stopped, so Pacemaker " +
      "leaves it down until it is started again, a node failure included.",
  };

  function confirmAct(name, action) {
    const verb = action === "stop" ? "Stop" : "Start";
    element("confirm-title").textContent = verb + " " + name;
    element("confirm-disruption").textContent = DISRUPTION[action];
    element("confirm-note").hidden = action !== "stop" || mode === "cluster";
    element("confirm-note").textContent =
      "This machine has no Pacemaker, so the guest is asked to shut down " +
      "through ACPI. One that ignores ACPI keeps running.";
    element("confirm-error").hidden = true;

    const go = element("confirm-go");
    go.textContent = verb;
    go.disabled = false;
    go.onclick = async () => {
      go.disabled = true;
      go.setAttribute("aria-busy", "true");
      try {
        const started = await API.post(
          "/vms/" + encodeURIComponent(name) + "/" + action
        );
        window.location.assign("runs?run=" + encodeURIComponent(started.run_id));
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

  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  function renderGuests(view) {
    element("loading").hidden = true;

    const lead = element("lead");
    lead.textContent = view.playbook
      ? "Declared in the inventory, deployed by " + view.playbook + "."
      : "";
    lead.hidden = !view.playbook;

    mode = view.mode;
    element("runtime-note").textContent = view.runtime_note;
    element("empty").textContent = view.note;
    element("empty").hidden = !view.note;

    const rows = element("guest-rows");
    rows.replaceChildren();
    (view.guests || []).forEach((guest) => {
      row(rows, [
        cell(guest.name),
        state(guest),
        cell(guest.resource ? guest.resource.node : ""),
        file(guest, guest.vm_disk),
        file(guest, guest.vm_template || guest.xml_path),
        ondeploy(guest),
        acts(guest),
      ]);
    });
    element("guest-table").hidden = !(view.guests || []).length;
  }

  function renderUndeclared(view) {
    const resources = view.undeclared || [];
    element("undeclared-card").hidden = !resources.length;
    const rows = element("undeclared-rows");
    rows.replaceChildren();
    resources.forEach((resource) => {
      row(rows, [
        cell(resource.id),
        cell(resource.node),
        cell(resource.role),
        cell(resource.failed ? "failed" : resource.state),
        // Acted on like a declared guest: it is the one most likely to need
        // stopping, since a convergence will not touch it and nothing else
        // here can reach it.
        acts({ name: resource.id, resource }),
      ]);
    });
  }

  // Adding a VM

  // One line per act, so a ten minute upload is legible while it happens.
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

  function showAdd(open) {
    element("add-card").hidden = !open;
    element("add").hidden = open;
    if (open) {
      element("add-error").hidden = true;
      element("add-steps").hidden = true;
      element("add-name").focus();
    }
  }

  // The role reads a `.j2` as a template and renders it per guest, and takes
  // anything else as the XML itself. The extension is the only thing that
  // says which, so the page reads it rather than asking the operator to.
  function xmlVariable(filename) {
    return filename.endsWith(".j2") ? "vm_template" : "xml_path";
  }

  // The name under which a file is stored, which is also the path the entry
  // writes. `files/` is where the reference resolver expects it and where the
  // reference inventories put it.
  function stored(name, filename) {
    const dot = filename.indexOf(".");
    const suffix = dot === -1 ? "" : filename.slice(dot);
    return "files/" + name + suffix;
  }

  async function addGuest() {
    const name = element("add-name").value.trim();
    const disk = element("add-disk").files[0];
    const xml = element("add-xml").files[0];
    const error = element("add-error");
    error.hidden = true;

    if (!name || !disk || !xml) {
      error.textContent =
        "A VM needs a name, a disk image and a libvirt XML. All three are " +
        "what the deployment run is given.";
      error.hidden = false;
      return;
    }

    const diskPath = stored(name, disk.name);
    const xmlPath = stored(name, xml.name);
    const progress = steps([
      "Uploading " + diskPath,
      "Committing " + xmlPath,
      "Declaring " + name + " in the inventory",
      "Launching the deployment",
    ]);

    const go = element("add-go");
    go.disabled = true;
    go.setAttribute("aria-busy", "true");
    try {
      progress.at(0, "doing");
      await API.upload("/inventory/artefacts/" + diskPath, disk);
      progress.at(0, "done");

      progress.at(1, "doing");
      await API.upload("/inventory/files/" + xmlPath, xml);
      progress.at(1, "done");

      progress.at(2, "doing");
      const declaration = { name, vm_disk: "../" + diskPath, enable: true };
      declaration[xmlVariable(xml.name)] = "../" + xmlPath;
      const declared = await API.post("/vms", declaration);
      progress.at(2, "done");

      progress.at(3, "doing");
      const started = await API.post("/runs", { playbook: declared.playbook });
      progress.at(3, "done");
      window.location.assign("runs?run=" + encodeURIComponent(started.run_id));
    } catch (failure) {
      const doing = [...element("add-steps").children].findIndex(
        (item) => item.className === "doing"
      );
      if (doing !== -1) {
        progress.at(doing, "failed");
      }
      // What has already happened is on screen above the message, which is
      // the part that decides what to do next: a guest declared and a run
      // that would not start is deployed from the Deployment page, and
      // nothing has to be uploaded twice.
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      go.disabled = false;
      go.removeAttribute("aria-busy");
    }
  }

  element("add").addEventListener("click", () => showAdd(true));
  element("add-cancel").addEventListener("click", () => showAdd(false));
  element("add-go").addEventListener("click", addGuest);

  async function start() {
    const { me } = await Chrome.load();
    // Starting a guest changes no desired state, so it is the operator's act
    // rather than the administrator's, the way cancelling a run is.
    canAct = me.role === "operator" || Chrome.isAdmin(me);
    const view = await API.get("/vms");
    renderGuests(view);
    renderUndeclared(view);
    // Adding a VM commits the inventory and launches a run, which is an
    // administrator's act like every other write in this service.
    element("add").hidden = !Chrome.isAdmin(me);
  }

  start().catch((failure) => {
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
