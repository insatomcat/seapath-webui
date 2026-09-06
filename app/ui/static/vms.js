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
  let canWrite = false;
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

  // Which playbook creates this guest, and whether the file said so or the
  // page worked it out from the one deployment the file describes. The
  // difference matters: a guess the file has not made is one an operator may
  // want to make explicit.
  function deployedBy(guest) {
    const cell = document.createElement("td");
    const text = document.createElement("span");
    text.textContent = guest.deployment;
    cell.append(text);
    if (!guest.declared) {
      cell.title =
        "This inventory has one flat VMs group, so every guest is deployed " +
        "by the playbook its mode calls for.";
      text.className = "assumed";
    }
    return cell;
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

    // What libvirt says, for a guest Pacemaker does not answer for. That is
    // every guest on a standalone machine, and it is the only reading those
    // have: they have no Pacemaker resource at all.
    if (!resource && guest.domain) {
      status = guest.domain.running ? "ok" : "unknown";
      words = guest.domain.state;
      box.title =
        "Read from libvirt-exporter on " +
        guest.domain.host +
        ". This guest has no Pacemaker resource, so nothing else here " +
        "reports it.";
    } else if (!resource && guest.deployment !== "cluster") {
      words = "not reported";
      box.title =
        "This page reads Pacemaker and libvirt-exporter, and neither " +
        "reported this guest. Its machine may not run the exporter, or may " +
        "not have answered.";
    }

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
    // Something has to have reported the guest before it can be acted on: a
    // name nothing answers for is a guest that has not been deployed, and
    // starting one is a deployment run rather than a button here.
    if (!canAct || !(guest.resource || guest.domain)) {
      return cell;
    }
    const running = guest.resource
      ? guest.resource.role === "started"
      : guest.domain.running;
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
    reconfigure:
      "Stops the guest, removes its Pacemaker resource and creates it again " +
      "from the metadata. That is what makes a metadata change take effect, " +
      "and it is an outage: Pacemaker reads those keys only when it creates " +
      "the resource, so there is no way to apply one without the guest going " +
      "down and coming back.",
    start:
      "Starts the guest. In a cluster this asks Pacemaker to run it and " +
      "Pacemaker chooses the node, which is not necessarily the one it last " +
      "ran on.",
    stop:
      "Stops the guest, and whatever it was serving stops with it. In a " +
      "cluster the resource is disabled as well as stopped, so Pacemaker " +
      "leaves it down until it is started again, a node failure included.",
  };

  // One window for every act that cannot be undone by clicking again. It names
  // the thing and says what happens, the way an apply names the machines it
  // disturbs, and the caller says what to do when the operator agrees.
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

  function confirmAct(name, action) {
    const verb = { stop: "Stop", start: "Start", reconfigure: "Apply" }[action];
    confirm({
      title: verb + " " + name,
      body: DISRUPTION[action],
      note:
        action === "stop" && mode !== "cluster"
          ? "This machine has no Pacemaker, so the guest is asked to shut " +
            "down through ACPI. One that ignores ACPI keeps running."
          : "",
      label: verb,
      act: async () => {
        const started = await API.post(
          "/vms/" + encodeURIComponent(name) + "/" + action
        );
        window.location.assign("runs?run=" + encodeURIComponent(started.run_id));
      },
    });
  }

  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  // The metadata of one image, in a modal. A value here can be a whole libvirt
  // domain: `vm_manager` stores the running XML under `xml` and the template
  // it was built from under `_base_xml`, so a table of these under the guest
  // list would push everything else off the screen.
  //
  // Read from Ceph as the request is served, so the window opens filled.
  let openGuest = null;

  function metaButton(name, deployment) {
    const cell = document.createElement("td");
    if (deployment !== "cluster") {
      // The metadata is on an RBD image, and a standalone machine has no Ceph
      // to hold one. Saying nothing here beats a button that always fails.
      return cell;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = "Metadata";
    button.addEventListener("click", () => openMetadata(name));
    cell.append(button);
    return cell;
  }

  async function openMetadata(name) {
    openGuest = name;
    element("meta-title").textContent = "Metadata of " + name;
    element("meta-error").hidden = true;
    element("meta").hidden = false;
    await loadMetadata();
  }

  async function loadMetadata() {
    element("meta-loading").hidden = false;
    try {
      render(
        await API.get("/vms/" + encodeURIComponent(openGuest) + "/metadata")
      );
    } catch (failure) {
      showMetaError(failure.message);
    } finally {
      element("meta-loading").hidden = true;
    }
  }

  function showMetaError(message) {
    const error = element("meta-error");
    error.textContent = message;
    error.hidden = !message;
  }

  function render(view) {
    showMetaError("");
    element("meta-lead").textContent = view.image;

    const rows = clear(element("meta-rows"));
    const entries = Object.entries(view.entries || {}).sort();
    entries.forEach(([key, value]) => {
      row(rows, [metaKey(key), metaValue(value), metaActions(key, value)]);
    });
    element("meta-table").hidden = !entries.length;
    element("meta-empty").hidden = Boolean(entries.length);
    element("meta-empty").textContent =
      "This image carries no metadata. A guest declared and never deployed " +
      "has no image at all, and reads as this.";

    // The outage that applies a change is offered only when the image actually
    // moved, which is what reading it before and after is for.
    const changes = view.changes || [];
    element("meta-pending").hidden = !changes.length;
    element("meta-pending-note").textContent = changes.length
      ? changes.map((change) => change.key).join(", ") +
        " changed on the image. Pacemaker reads these keys when it creates " +
        "the resource, so the guest is still running with the old ones."
      : "";
  }

  // A key SEAPATH itself reads starts with an underscore. A site's own label
  // does not, and changes nothing about how the guest runs.
  function metaKey(key) {
    const cell = document.createElement("td");
    cell.className = "meta-key";
    const code = document.createElement("code");
    code.textContent = key;
    if (key.startsWith("_")) {
      code.className = "reserved";
    }
    cell.append(code);
    return cell;
  }

  function metaValue(value) {
    const cell = document.createElement("td");
    cell.className = "meta-value";
    const block = document.createElement("pre");
    block.textContent = value;
    cell.append(block);
    return cell;
  }

  function metaActions(key, value) {
    const cell = document.createElement("td");
    cell.className = "meta-actions";
    if (!canWrite) {
      return cell;
    }
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "secondary";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => openEditor(key, value));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => confirmRemove(key));

    cell.append(edit, remove);
    return cell;
  }

  // Removing a key is a write to the image and nothing here puts back what it
  // took away, so it is asked before it happens. What the key is decides how
  // much the sentence has to say.
  function confirmRemove(key) {
    confirm({
      title: "Remove " + key + " from " + openGuest,
      body:
        "The key is deleted from the image. The guest keeps running and keeps " +
        "the configuration it started with, because Pacemaker reads these " +
        "keys when it creates the resource.",
      note: removalNote(key),
      label: "Remove it",
      act: () => write(key, null),
    });
  }

  function removalNote(key) {
    if (key === "xml" || key === "_base_xml") {
      return (
        key +
        " holds the libvirt domain this guest was built from. vm_manager " +
        "reads it to clone the guest and to tell two UUIDs apart, so removing " +
        "it takes those away with it."
      );
    }
    if (key.startsWith("_")) {
      return (
        "SEAPATH reads this key. Without it the guest takes vm_manager's own " +
        "default the next time its resource is created."
      );
    }
    return "";
  }

  // The editor, wide and tall, because two of these keys hold a libvirt domain
  // each and editing one in a three line box is how a closing tag goes
  // missing.
  function openEditor(key, value) {
    element("edit-title").textContent = key
      ? "Edit " + key + " on " + openGuest
      : "Add a key on " + openGuest;
    element("edit-key").value = key || "";
    element("edit-key").readOnly = Boolean(key);
    element("edit-key-help").textContent = key
      ? "The key this value is stored under. Rename it by removing this one " +
        "and adding another."
      : "Letters, digits, underscore, dot and dash. A key starting with an " +
        "underscore is one SEAPATH itself reads.";
    element("edit-value").value = value || "";
    element("edit-error").hidden = true;
    element("edit").hidden = false;
    element(key ? "edit-value" : "edit-key").focus();
  }

  async function write(key, value) {
    element("meta-loading").hidden = false;
    try {
      render(
        await API.put("/vms/" + encodeURIComponent(openGuest) + "/metadata", {
          key,
          value,
        })
      );
      return true;
    } catch (failure) {
      showMetaError(failure.message);
      throw failure;
    } finally {
      element("meta-loading").hidden = true;
    }
  }

  element("meta-close").addEventListener("click", () => {
    element("meta").hidden = true;
    openGuest = null;
  });

  element("meta-refresh").addEventListener("click", loadMetadata);
  element("meta-add").addEventListener("click", () => openEditor("", ""));
  element("meta-apply").addEventListener("click", () => {
    element("meta").hidden = true;
    confirmAct(openGuest, "reconfigure");
  });

  element("edit-cancel").addEventListener("click", () => {
    element("edit").hidden = true;
  });

  element("edit-go").addEventListener("click", async () => {
    const key = element("edit-key").value.trim();
    const error = element("edit-error");
    if (!key) {
      error.textContent = "A key is needed: it is what rbd stores the value under.";
      error.hidden = false;
      return;
    }
    const go = element("edit-go");
    go.disabled = true;
    go.setAttribute("aria-busy", "true");
    try {
      await write(key, element("edit-value").value);
      element("edit").hidden = true;
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      go.disabled = false;
      go.removeAttribute("aria-busy");
    }
  });

  function renderGuests(view) {
    element("loading").hidden = true;

    const lead = element("lead");
    lead.textContent = view.playbook
      ? "Declared in the inventory, deployed by " + view.playbook + "."
      : "";
    lead.hidden = !view.playbook;

    mode = view.mode;
    // What one `VMs` group cannot say about a file that declares both a
    // cluster and a machine outside it. Said where the guests are listed,
    // because that is where the reading would otherwise be trusted.
    showBanner((view.warnings || []).join(" "));
    element("runtime-note").textContent = view.runtime_note;
    element("empty").textContent = view.note;
    element("empty").hidden = !view.note;

    const rows = element("guest-rows");
    rows.replaceChildren();
    (view.guests || []).forEach((guest) => {
      row(rows, [
        cell(guest.name),
        deployedBy(guest),
        state(guest),
        cell(
          guest.resource
            ? guest.resource.node
            : guest.domain
              ? guest.domain.host
              : ""
        ),
        file(guest, guest.vm_disk),
        file(guest, guest.vm_template || guest.xml_path),
        ondeploy(guest),
        acts(guest),
        metaButton(guest.name, guest.deployment),
      ]);
    });
    element("guest-table").hidden = !(view.guests || []).length;
  }

  function renderUndeclared(view) {
    const resources = view.undeclared || [];
    const domains = view.undeclared_domains || [];
    element("undeclared-card").hidden = !(resources.length || domains.length);
    const rows = element("undeclared-rows");
    rows.replaceChildren();
    // A domain a machine runs and no inventory declares, on a machine
    // Pacemaker does not answer for. Same finding, other reading.
    domains.forEach((domain) => {
      row(rows, [
        cell(domain.name),
        cell(domain.host),
        cell(domain.state),
        cell(domain.running ? "running" : "stopped"),
        acts({ name: domain.name, domain }),
        metaButton(domain.name, "standalone"),
      ]);
    });
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
        // Pacemaker reported it, so it is a cluster guest whatever the
        // inventory failed to say about it.
        metaButton(resource.id, "cluster"),
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

  // Placement, priority, migration and the disk bus are Pacemaker's and
  // vm_manager's, so they are offered where those exist. The pinning profile
  // is not: `deploy_vms_standalone` writes it to /etc/seapath/alloc.d and the
  // same hook reads it there, so the section itself is shown in both modes.
  // Applied again whenever the deployment changes, because an inventory with
  // machines of both kinds asks the question in the form itself.
  function gateDeployment() {
    const cluster = chosenDeployment() === "cluster";
    document.querySelectorAll("#add-modal [data-cluster]").forEach((node) => {
      node.hidden = !cluster;
    });
    document.querySelectorAll("#add-modal [data-standalone]").forEach((node) => {
      node.hidden = cluster;
    });
    // These two answer to a choice made inside the section as well as to the
    // mode, so their own rule is applied after the mode has spoken: the node
    // list belongs to a placement that was asked for, and the migration
    // settings to a migration that was allowed.
    element("add-host").hidden = !cluster || !element("add-placement").value;
    element("add-migration").hidden =
      !cluster || !element("add-live-migration").checked;
    element("add-more-summary").textContent = cluster
      ? "Placement, migration and real time"
      : "Real time";
    element("add-enable-label").textContent = cluster
      ? "Add it to the cluster once it is created"
      : "Start it once it is created";
  }

  function showAdd(open) {
    element("add-modal").hidden = !open;
    gateDeployment();
    if (open) {
      element("add-error").hidden = true;
      element("add-steps").hidden = true;
      element("add-name").focus();
    }
  }

  // The machines a guest may be placed on and the guests it may be kept
  // beside, both filled from the reading the page already has.
  function fillChoices(view) {
    // The deployments this inventory has machines for. Offered only where
    // there are two, because a file with one has already answered.
    const where = clear(element("add-deployment"));
    (view.deployments || []).forEach((name) => {
      where.append(new Option(name, name));
    });
    element("add-where").hidden = (view.deployments || []).length < 2;

    const hosts = clear(element("add-host"));
    (view.machines || []).forEach((name) => {
      hosts.append(new Option(name, name));
    });
    const beside = clear(element("add-colocated"));
    (view.guests || []).forEach((guest) => {
      beside.append(new Option(guest.name, guest.name));
    });
  }

  element("add-deployment").addEventListener("change", gateDeployment);

  element("add-placement").addEventListener("change", (event) => {
    element("add-host").hidden = !event.target.value;
  });

  element("add-live-migration").addEventListener("change", (event) => {
    element("add-migration").hidden = !event.target.checked;
  });

  // What the form holds beyond the three files, which is what
  // `cluster_vm create` is given and what the image's metadata then carries.
  function declaration() {
    const placement = element("add-placement").value;
    const chosen = [...element("add-colocated").selectedOptions].map(
      (option) => option.value
    );
    const number = (id) => {
      const raw = element(id).value.trim();
      return raw === "" ? null : Number(raw);
    };
    const fields = {
      enable: element("add-enable").checked,
      vm_pinning_profile: element("add-profile").value.trim() || null,
    };
    if (chosenDeployment() !== "cluster") {
      return Object.assign(fields, {
        autostart: element("add-autostart").checked,
        // The role extracts a gzipped raw image before defining the domain,
        // and the extension is the only thing that says which kind it is. The
        // same rule as the `.j2` one on the XML above.
        disk_extract: gzipped,
      });
    }
    fields.nostart = element("add-nostart").checked;
    if (placement) {
      fields[placement] = element("add-host").value;
    }
    return Object.assign(fields, {
      priority: number("add-priority"),
      live_migration: element("add-live-migration").checked,
      migrate_to_timeout: number("add-migrate-timeout"),
      migration_downtime: number("add-downtime"),
      disk_bus: element("add-bus").value || null,
      colocated_vms: chosen,
      strong_colocation: element("add-strong").checked,
    });
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

  // Set from the disk the operator picked, and read by `declaration()`.
  let gzipped = false;

  // Where the guest being added will be created. The file's own mode when it
  // describes one kind of machine, and the operator's choice when it has both.
  function chosenDeployment() {
    const field = element("add-deployment");
    return element("add-where").hidden ? mode : field.value;
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

    gzipped = disk.name.endsWith(".gz");
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
      const entry = Object.assign(
        { name, vm_disk: "../" + diskPath, deployment: chosenDeployment() },
        declaration()
      );
      entry[xmlVariable(xml.name)] = "../" + xmlPath;
      const declared = await API.post("/vms", entry);
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
    // Changing what a guest is configured with is an administrator's act, the
    // way every other write in this service is.
    canWrite = Chrome.isAdmin(me);
    const view = await API.get("/vms");
    renderGuests(view);
    renderUndeclared(view);
    fillChoices(view);
    // Adding a VM commits the inventory and launches a run, which is an
    // administrator's act like every other write in this service.
    element("add").hidden = !Chrome.isAdmin(me);
  }

  start().catch((failure) => {
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
