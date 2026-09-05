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
// Nothing on this page writes. A guest is defined on the Inventory page and
// deployed from the Deployment page.

(function () {
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

  function renderGuests(view) {
    element("loading").hidden = true;

    const lead = element("lead");
    lead.textContent = view.playbook
      ? "Declared in the inventory, deployed by " + view.playbook + "."
      : "";
    lead.hidden = !view.playbook;

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
      ]);
    });
  }

  async function start() {
    await Chrome.load();
    const view = await API.get("/vms");
    renderGuests(view);
    renderUndeclared(view);
  }

  start().catch((failure) => {
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
