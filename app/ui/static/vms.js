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
//
// Move and Return call `/cluster/resources/{name}`, which is deliberate rather
// than a slip: what is being placed is a Pacemaker resource, `vm_manager`
// names it after the guest, and one act with a button on two pages beats two
// endpoints doing the same thing. See D34.

(function () {
  // Filled once the session and the reading are in: which role is signed in
  // decides whether the acting buttons exist at all, and the mode decides what
  // the stop confirmation has to warn about.
  let canAct = false;

  // Where a move may send a guest, as the last reading answered it: the
  // machines the inventory allows, held against the members the cluster
  // reports as online and out of standby. Empty means no move is offered.
  let placementNodes = [];
  let canWrite = false;
  let mode = "standalone";

  // Which guests the tables list: `all`, `cluster` or `standalone`, by the
  // playbook that deploys them. Kept in the browser, per viewer, because it
  // is a way of reading the page rather than anything about the guests.
  const FILTER_KEY = "seapath-webui.vms.filter";
  let filter = "all";
  try {
    filter = localStorage.getItem(FILTER_KEY) || "all";
  } catch (ignored) {
    filter = "all";
  }
  // The last reading, so a change of filter redraws without asking the
  // cluster again.
  let lastView = null;
  // Which guests have a VNC display, by name, once their definitions were read.
  let displays = null;

  // Whether the file declares both kinds of guest, which is the only case the
  // filter has anything to choose between. Otherwise it is not offered, and
  // a choice this browser kept from another inventory narrows nothing: it
  // would hide every guest behind a control that is not there.
  function split(view) {
    return ((view && view.deployments) || []).length > 1;
  }

  function shown(deployment) {
    return !split(lastView) || filter === "all" || filter === deployment;
  }

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
        (guest.domain.description ? guest.domain.description + ". " : "") +
        "Read from libvirt-exporter on " +
        guest.domain.host +
        ". This guest has no Pacemaker resource, so nothing else here " +
        "reports it.";
    } else if (guest.disabled) {
      // Taken out of the cluster: Ceph holds its disk, Pacemaker holds
      // nothing. Grey rather than red, because somebody asked for it.
      words = "out of the cluster";
      status = "unknown";
      box.title =
        "Ceph holds this guest's disk and Pacemaker has no resource for it, " +
        "which is what Disable leaves. Enable puts it back, and a deployment " +
        "run leaves it alone.";
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

  // Binary units, as libvirt and `qemu-img` print them: a disk created as
  // 50G reads back as 50 GiB rather than 53.7 GB.
  function bytes(value) {
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let index = 0;
    let scaled = value;
    while (scaled >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    const digits = scaled >= 100 || Number.isInteger(scaled) || index === 0 ? 0 : 1;
    return scaled.toFixed(digits) + " " + units[index];
  }

  // What the domain is sized at, as the machine running it reports it: the
  // vCPUs, the memory, and the disks summed, each disk on hover. Nothing for
  // a guest no machine reports, and no disk for one that is shut off, whose
  // block devices the exporter does not read.
  function specsCell(domain) {
    const node = document.createElement("td");
    if (!domain) {
      return node;
    }
    const parts = [];
    if (domain.vcpus) {
      parts.push(domain.vcpus + " vCPU");
    }
    if (domain.maximum_memory_bytes) {
      parts.push(bytes(domain.maximum_memory_bytes));
    }
    const disks = domain.disks || [];
    if (disks.length) {
      const total = disks.reduce((sum, disk) => sum + disk.capacity_bytes, 0);
      parts.push(bytes(total));
    }
    node.textContent = parts.join(" · ");
    node.className = "specs";
    node.title =
      (disks.length
        ? "Disks: " +
          disks.map((disk) => disk.device + " " + bytes(disk.capacity_bytes)).join(", ")
        : domain.running
          ? "Its disks were not reported on this reading, which a busy " +
            "exporter does. Re-read the guests to ask again."
          : "The disks of a guest that is not running are not reported.") +
      "\nRead from libvirt-exporter on " + domain.host + ".";
    return node;
  }

  // The address the entry gives the guest, and where that address comes from.
  // Read off the inventory: a guest publishes no exporter, so what it is
  // answering on right now is not something this page knows, and the cell
  // says which of the two statements the address is.
  function addressCell(guest) {
    const node = document.createElement("td");
    if (!guest.ansible_host) {
      // No address declared, which a guest on DHCP and a guest nobody reaches
      // inside are alike. Guessing which would be the page inventing a fact.
      return node;
    }
    const address = document.createElement("code");
    address.textContent = guest.ansible_host;
    node.append(address);
    if (guest.seeded) {
      const origin = document.createElement("span");
      origin.className = "legend";
      origin.textContent = " given by the seed";
      node.append(origin);
    }
    // A shell inside the guest, at this address, over the connection a run
    // makes. Offered only where the node has a host key to check it against,
    // which is also the reason a guest just declared shows no button yet.
    if (Console.offers(guest.name)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = "Console";
      button.title = "Open a shell in " + guest.name + " at " + guest.ansible_host;
      button.addEventListener("click", () => Console.open(guest.name));
      node.append(" ", button);
    }
    return node;
  }

  // How the guest is created: the image and the XML its entry names, for as
  // long as it names them. The deployment run that creates the guest takes
  // those lines out when it ends, and the column has nothing left to say
  // about that guest. What is left of the creation once the recipe is gone,
  // the files it was made from, is on the guest's own row: see `nameCell`.
  function creationCell(guest) {
    const node = document.createElement("td");
    if ((guest.creation || []).length || guest.force) {
      [guest.vm_disk, guest.vm_template || guest.xml_path]
        .filter(Boolean)
        .forEach((value) => node.append(file(guest, value)));
      // `force` is why these lines stay: both roles skip their creation block
      // for a guest the hypervisor already has, unless the entry carries it,
      // and then they destroy the guest and make it again from this recipe.
      // So a run an operator reads as "converge my VMs" reinstalls this one
      // and whatever it had written is gone. Said where the recipe is,
      // because the recipe is what the run reads back.
      if (guest.force) {
        const warning = document.createElement("div");
        warning.className = "recreated";
        warning.textContent = "force: a run recreates it";
        warning.title =
          "This entry carries force: true, so the next deployment run " +
          "destroys " +
          guest.name +
          " and creates it again from these files. Everything written inside " +
          "the guest since it was created is lost.";
        node.append(warning);
      }
      return node;
    }
    return node;
  }

  // The guest's name, and where the files it was created from are still held
  // here, the offer to delete them. A mark on the row rather than a column of
  // its own: the offer stands on one row at a time, for as long as nobody
  // takes it, and a column open for it is width every other row pays.
  function nameCell(guest) {
    const node = document.createElement("td");
    node.append(guest.name);
    const sources = guest.sources || [];
    if (!(canWrite && sources.length)) {
      return node;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "leftover";
    const label =
      "Delete the files " +
      guest.name +
      " was created from, still held here: " +
      sources.map((source) => source.path).join(", ");
    button.title = label;
    button.setAttribute("aria-label", label);
    button.append(struckFiles());
    button.addEventListener("click", () => confirmDeleteSources(guest));
    node.append(" ", button);
    return node;
  }

  // A sheet of paper with a stroke through it: files that are still on this
  // node and nothing reads any more. Stroked in `currentColor`, like the
  // glyphs of the top bar, so one drawing works on both grounds.
  function struckFiles() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    ["M4 2.4h4.4L11.8 5.8v7.8H4z", "M8.4 2.4v3.4h3.4", "M2.6 13.4 13.4 2.6"].forEach(
      (drawing) => {
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", drawing);
        svg.append(path);
      }
    );
    return svg;
  }

  // A file the guest names, and whether a deployment would find it. A missing
  // one is the failure worth catching here: with `any_errors_fatal`, a copy
  // that cannot find its source ends the run on every host at once.
  function file(guest, value) {
    const reference = (guest.files || []).find((item) => item.value === value);
    const line = document.createElement("div");
    const path = document.createElement("code");
    path.textContent = value;
    line.append(path);
    if (reference && !reference.found) {
      // The same colour the Inventory page gives a file it does not hold, for
      // the same reason: the run stops at the task that copies it.
      line.className = "missing";
      const missing = document.createElement("span");
      missing.textContent = " (nothing here holds it)";
      line.append(missing);
    }
    return line;
  }

  // Starting and stopping. The button offered is the one that changes
  // something: a guest Pacemaker reports as started is offered a stop, one it
  // reports as stopped a start, and a guest nothing reports at all neither,
  // because there is no domain to act on until it has been deployed.
  function acts(guest) {
    const cell = runtimeActs(guest);
    // The guest's serial console, for the moment it no longer answers on its
    // address. Offered once something reports the guest, since before that
    // there is no domain whose serial port could be read.
    if (Console.permitted() && (guest.resource || guest.domain)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = "Serial console";
      button.title =
        "Open " +
        guest.name +
        "'s serial console with " +
        (guest.deployment === "cluster" ? "vm-mgr console" : "virsh console");
      button.addEventListener("click", () => Console.openSerial(guest.name));
      cell.append(cell.childNodes.length ? " " : "", button);
    }
    // Its screen, the one way into a Windows guest whose network is down.
    // Offered when the guest's definition has a VNC display: the XML Ceph
    // holds for a cluster guest, the domain its machine runs for a standalone
    // one. A definition nobody could read offers nothing.
    if (Graphic.permitted() && (guest.resource || guest.domain) && hasDisplay(guest)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = "Graphic console";
      button.title = "Open " + guest.name + "'s screen, from its VNC display";
      button.addEventListener("click", () => Graphic.open(guest.name));
      cell.append(cell.childNodes.length ? " " : "", button);
    }
    // What the cluster printed about this guest, on every machine at once.
    // The question the Logs page was built for is a guest that will not
    // migrate, and the answer is on the machine it left as much as on the one
    // it would not reach, so the link asks all of them.
    cell.append(cell.childNodes.length ? " " : "", logsLink(guest.name));
    return cell;
  }

  // A regular expression, so a name is escaped before it becomes one. Guest
  // names carry dots, and a dot is every character until it is escaped.
  function quoted(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function logsLink(guest) {
    const link = document.createElement("a");
    link.className = "act-link";
    link.href =
      "logs?scope=guests&minutes=60&grep=" + encodeURIComponent(quoted(guest));
    link.textContent = "Logs";
    link.title =
      "libvirt, Pacemaker and Corosync on every machine, over the last hour, " +
      "narrowed to " + guest;
    return link;
  }

  function hasDisplay(guest) {
    return displays !== null && displays[guest.name] === true;
  }

  function runtimeActs(guest) {
    const cell = document.createElement("td");
    cell.className = "acts";
    if (!canAct) {
      return cell;
    }
    // Out of the cluster, with its disk still in Ceph: what is left is
    // putting it back, or deleting it for good. Deleting writes the inventory
    // and destroys the images, so it is the administrator's.
    if (guest.disabled) {
      cell.append(actButton(guest.name, "enable"));
      if (canWrite) {
        cell.append(" ", actButton(guest.name, "delete"));
      }
      return cell;
    }
    // Something has to have reported the guest before it can be acted on: a
    // name nothing answers for is a guest that has not been deployed, and
    // starting one is a deployment run rather than a button here.
    if (!(guest.resource || guest.domain)) {
      return cell;
    }
    const running = guest.resource
      ? guest.resource.role === "started"
      : guest.domain.running;
    cell.append(actButton(guest.name, running ? "stop" : "start"));
    // Taking it out of the cluster, offered wherever Pacemaker holds it: a
    // stop keeps the resource and a node failure or a Start brings the guest
    // back, while this removes the resource and leaves the disk.
    if (guest.resource) {
      cell.append(" ", actButton(guest.name, "disable"));
    }
    return cell;
  }

  // Where the guest is, and what is holding it there. One cell, because the
  // second answers a question the first raises: a guest running on a node its
  // entry never named is either Pacemaker's own choice or somebody's move, and
  // the constraint is the only thing that says which.
  //
  // The comparison is the whole point. `preferred_host` and a move here write
  // the same `cli-prefer` object, so the CIB cannot say who asked for it; held
  // against the entry it can say whether anybody declared it.
  //
  // Three things are held against each other and not two: the entry, the
  // constraint, and the machine the guest is running on at this moment. A
  // colour that compared the first two alone called a guest green while it was
  // running somewhere else entirely, which is a reading an operator opens this
  // page to get.
  //
  // The answer used to be a badge spelling that sentence out beside every node
  // name, which made this column the widest in the table for a reading that is
  // three states. The colour of the node name carries the three, and the
  // sentence is on hover, where the half worth reading is what to do about it.
  function nodeCell(guest) {
    const where = guest.resource
      ? guest.resource.node
      : guest.domain
        ? guest.domain.host
        : "";
    const box = document.createElement("td");
    box.className = "placed";
    if (!where) {
      return box;
    }
    const name = document.createElement("span");
    name.textContent = where;
    // A pinned guest is a fifth state that the other four do not describe:
    // `pinned_host` is a rule of its own rather than a placement, so it is
    // neither something the cluster chose nor something a move or a return
    // can change, and the row offers neither.
    const pin = pinOf(guest);
    if (pin) {
      name.className = "placement pinned";
      name.title = explainPin(guest.name, pin);
      box.append(name);
      return box;
    }
    const held = preferenceOf(guest);
    const declared = guest.preferred_host || "";
    name.className = "placement " + placementState(held, declared, where);
    name.title = explain(guest.name, held, declared, where);
    box.append(name);
    return box;
  }

  function explainPin(name, pin) {
    return (
      pin.id +
      ". The inventory entry pins " +
      name +
      " to " +
      pin.node +
      " with pinned_host, so it runs there or nowhere and the cluster never " +
      "moves it. Moving it means changing pinned_host on its entry and " +
      "deploying it again."
    );
  }

  // The four states the colour says, and the only thing it says. An entry that
  // declares a placement the cluster does not hold is the same finding as a
  // cluster holding one the entry never named: the two disagree, and which way
  // round is in the sentence rather than in the colour.
  //
  // The fourth is the cluster failing to honour a rule it carries, and it
  // comes first because it outranks every disagreement about where the guest
  // belongs. `crm resource move` writes an infinite score, so a guest running
  // anywhere but the machine its own constraint names means that machine could
  // not take it: it is down, in standby, or the guest failed there.
  function placementState(held, declared, where) {
    if (held && held.node !== where) {
      return "displaced";
    }
    if (!held) {
      return declared ? "adrift" : "free";
    }
    return held.node === declared ? "kept" : "adrift";
  }

  // The whole reading as a sentence, on hover, because the colour says which
  // of the three states a placement is in and nothing more. The subject of
  // every one of them is the placement and never the guest: "declared" alone
  // would read as whether the inventory has the guest at all, which is a
  // different question this page also answers.
  function explain(name, held, declared, where) {
    // The cluster is running the guest away from an infinite preference, which
    // is a finding about a machine rather than a drift between two records.
    // What the entry says is still worth a clause, because it decides what a
    // return would put back.
    if (held && held.node !== where) {
      const unheld =
        held.id +
        " names " +
        held.node +
        ", and " +
        name +
        " is running on " +
        where +
        ". A placement carries an infinite score, so the cluster put the " +
        "guest elsewhere only because " +
        held.node +
        " could not take it: offline, in standby, or the guest failed there.";
      if (declared === held.node) {
        return (
          unheld +
          " The constraint and the inventory entry agree on " +
          held.node +
          ", so there is nothing here to put back: the guest returns on its " +
          "own once that machine can take it again."
        );
      }
      if (declared) {
        return (
          unheld +
          " The inventory entry declares " +
          declared +
          ", and Return writes that placement back."
        );
      }
      return (
        unheld +
        " The inventory entry declares no placement at all, and Return " +
        "removes the constraint and leaves the placement to Pacemaker."
      );
    }
    if (!held && !declared) {
      return (
        "No constraint holds " +
        name +
        ", and its inventory entry declares no placement, so the cluster " +
        "places it."
      );
    }
    if (!held) {
      return (
        "No constraint holds " +
        name +
        ", and its inventory entry declares " +
        declared +
        ". A deployment run writes that placement to the cluster."
      );
    }
    if (declared === held.node) {
      return (
        held.id +
        ". The cluster keeps " +
        name +
        " on " +
        held.node +
        ", which is the placement its inventory entry declares."
      );
    }
    const cause =
      " A move from this page writes the same constraint preferred_host " +
      "writes, and so does crm resource move typed on a machine, so the " +
      "cluster cannot say which of the two asked for it.";
    if (declared) {
      return (
        held.id +
        ". The cluster keeps " +
        name +
        " on " +
        held.node +
        ", and its inventory entry declares " +
        declared +
        "." +
        cause +
        " Return writes " +
        declared +
        " back."
      );
    }
    return (
      held.id +
      ". The cluster keeps " +
      name +
      " on " +
      held.node +
      ", and its inventory entry declares no placement at all." +
      cause +
      " Return removes the constraint and leaves the placement to Pacemaker."
    );
  }

  // The `cli-prefer` constraint, which is what a move writes and what
  // `preferred_host` writes: the same object, and telling them apart is the
  // page's job rather than the cluster's. `pin-` is `pinned_host`, a different
  // rule that a return does not remove.
  function preferenceOf(guest) {
    return (guest.constraints || []).find((item) =>
      item.id.startsWith("cli-prefer-")
    );
  }

  function pinOf(guest) {
    return (guest.constraints || []).find((item) => item.id.startsWith("pin-"));
  }

  // `cli-ban-<resource>-on-<node>` is what an observer leaves: `vm_manager`
  // bans a guest from a machine that declares itself one. `crm resource clear`
  // removes those along with the preference, which is a side effect of the
  // return rather than anything it was asked for, so the confirmation names
  // them and says what puts them back.
  function bansOf(guest) {
    return (guest.constraints || []).filter((item) =>
      item.id.startsWith("cli-ban-")
    );
  }

  // Moving a guest, and giving its placement back. Offered on a guest
  // Pacemaker holds and nowhere else: a standalone guest has no cluster to
  // hear a constraint, and a pinned one runs where it is pinned or nowhere,
  // which is a decision its inventory entry made.
  function placement(guest) {
    const box = document.createElement("td");
    // Move and Return, side by side rather than stacked: two buttons on two
    // lines made every row of the table as tall as this one.
    box.className = "acts";
    if (!canAct || !guest.resource || pinOf(guest)) {
      return box;
    }
    // Pacemaker refuses to move a resource to the node it is already active
    // on, so that node is not a destination and a guest with nowhere else to
    // go is offered no Move. Keeping a guest where it is is `preferred_host`
    // on its entry, which is a placement rather than a move.
    const options = placementNodes.filter((node) => node !== guest.resource.node);
    if (options.length) {
      const move = document.createElement("button");
      move.type = "button";
      move.className = "secondary";
      move.textContent = "Move";
      move.addEventListener("click", () => confirmMove(guest, options));
      box.append(move);
    }
    // Return is the inverse of a move, so it is offered where there is a move
    // to undo: a constraint naming a machine the entry does not name. Where
    // the two already agree the run would clear the constraint and write the
    // same one straight back, moving nothing and changing nothing, at the cost
    // of a run and a line of the audit trail. Offered there it also made the
    // green state unreadable, since a button on a row is a claim that the row
    // has something to put right.
    const held = preferenceOf(guest);
    if (held && held.node !== (guest.preferred_host || "")) {
      const back = document.createElement("button");
      back.type = "button";
      back.className = "secondary";
      back.textContent = "Return";
      back.addEventListener("click", () => confirmReturn(guest));
      box.append(box.childNodes.length ? " " : "", back);
    }
    return box;
  }

  function confirmMove(guest, options) {
    const held = preferenceOf(guest);
    confirm({
      title: "Move " + guest.name,
      body:
        "Writes the cli-prefer constraint that names the node, which is the " +
        "same object preferred_host produces and written by the same command. " +
        "With live_migration on this guest's image Pacemaker migrates the " +
        "domain and it keeps running; without it the guest is stopped where " +
        "it is and started on the other node, and whatever it was serving " +
        "stops in between.",
      note: held
        ? held.id + " already holds it on " + held.node + ", and this " +
          "replaces it. Return puts back what the inventory declares. To keep " +
          "the guest where it is instead, declare preferred_host on its " +
          "inventory entry: that is a placement rather than a move, and it " +
          "disturbs nothing."
        : "The constraint stays until Return removes it or the guest's " +
          "Pacemaker resource is rebuilt, and while it is there it overrides " +
          "the placement the inventory declares.",
      choose: {
        label: "Run it on",
        options,
      },
      label: "Move",
      act: async (node) => {
        const started = await API.post(
          "/cluster/resources/" + encodeURIComponent(guest.name) + "/move",
          { node }
        );
        RunWatch.open(started.run_id);
      },
    });
  }

  function confirmReturn(guest) {
    const held = preferenceOf(guest);
    // The bans belong in the disruption rather than in the aside under it:
    // `crm resource clear` takes them with the preference, so removing them is
    // part of what the button does even though nobody asked for it.
    const bans = bansOf(guest);
    confirm({
      title: "Return " + guest.name + " to the cluster",
      body:
        "Removes " +
        (held ? held.id : "the cli-prefer constraint") +
        (guest.preferred_host
          ? ", and writes back preferred_host: " + guest.preferred_host + "."
          : ", and the inventory declares no placement to write back, so " +
            "Pacemaker places the guest by its own rules.") +
        " It may move as a result, at the same cost the move had." +
        (bans.length
          ? " crm resource clear takes the bans on this guest with it: " +
            bans.map((item) => item.id).join(", ") +
            ". A ban is how a guest is kept off an observer, and the next " +
            "deployment run is what writes it again."
          : ""),
      note:
        "What is written back is the inventory's preferred_host. Where that " +
        "differs from _preferred_host on the guest's image, the metadata " +
        "window is where the difference is settled.",
      label: "Return",
      act: async () => {
        const started = await API.post(
          "/cluster/resources/" + encodeURIComponent(guest.name) + "/clear"
        );
        RunWatch.open(started.run_id);
      },
    });
  }

  function actButton(name, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = VERBS[action];
    button.addEventListener("click", () => confirmAct(name, action));
    return button;
  }

  // Stopping a guest stops what it was serving, and on these machines that is
  // a substation function. The confirmation names the guest and says what the
  // act does, the way an apply names the machines it disturbs.
  const VERBS = {
    stop: "Stop",
    start: "Start",
    reconfigure: "Apply",
    disable: "Disable",
    enable: "Enable",
    delete: "Delete",
  };

  const DISRUPTION = {
    disable:
      "Stops the guest and removes its Pacemaker resource, so the cluster " +
      "no longer runs it, restarts it or moves it. Its disk image, the " +
      "metadata on it and its inventory entry all stay: Enable puts it back " +
      "as it was, and a deployment run leaves it alone.",
    delete:
      "Takes the guest's entry out of the inventory, as a commit, then " +
      "deletes its disk image, every other image of its RBD group and the " +
      "metadata on them from Ceph, as a run. The entry goes first so that no " +
      "deployment run creates the guest again. The disk image file and the " +
      "libvirt XML the entry named stay where they are, since another guest " +
      "may use them.",
    enable:
      "Creates the guest's Pacemaker resource again from the metadata on its " +
      "image, and Pacemaker starts it on the node it chooses.",
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
  function confirm({ title, body, note, label, choose, act }) {
    element("confirm-title").textContent = title;
    element("confirm-disruption").textContent = body;
    element("confirm-note").textContent = note || "";
    element("confirm-note").hidden = !note;
    element("confirm-error").hidden = true;

    // The destination a move needs, picked in the window that names the
    // disruption: the node is part of the act, so it is read at the moment the
    // act is confirmed rather than from a control left set on the row.
    const picker = element("confirm-node");
    element("confirm-choice").hidden = !choose;
    if (choose) {
      element("confirm-choice-label").textContent = choose.label;
      picker.replaceChildren();
      choose.options.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        option.selected = name === choose.selected;
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

  function confirmAct(name, action) {
    const verb = VERBS[action];
    confirm({
      title:
        action === "disable"
          ? "Take " + name + " out of the cluster"
          : action === "enable"
            ? "Put " + name + " back in the cluster"
            : action === "delete"
              ? "Delete " + name + " and its disk"
              : verb + " " + name,
      body: DISRUPTION[action],
      note:
        action === "stop" && mode !== "cluster"
          ? "This machine has no Pacemaker, so the guest is asked to shut " +
            "down through ACPI. One that ignores ACPI keeps running."
          : action === "delete"
            ? "What the guest had written on its disk is lost, and nothing " +
              "here brings it back. Should the run fail, the images stay in " +
              "Ceph and reverting the commit on the Inventory page declares " +
              "the guest again."
            : "",
      label: action === "delete" ? "Delete " + name : verb,
      act: async () => {
        const started = await API.post(
          "/vms/" + encodeURIComponent(name) + "/" + action
        );
        RunWatch.open(started.run_id);
        if (action === "delete") {
          // The entry is already gone from the inventory, so the row goes now
          // rather than when somebody next rereads.
          refresh(true).catch((failure) => showBanner(failure.message));
        }
      },
    });
  }

  // No run and nothing on a machine: the guest runs from its own disk, which
  // the image was copied or imported into when it was created. What is said is
  // which files go and where from, since the image is the one that cannot be
  // taken back from the history.
  function confirmDeleteSources(guest) {
    const described = guest.sources.map(
      (source) =>
        source.path +
        (source.where === "artefacts"
          ? " from the artefacts"
          : " from the inventory folder, as a commit")
    );
    confirm({
      title: "Delete the files " + guest.name + " was created from",
      body:
        "Deletes " + described.join(" and ") + " on this node. " + guest.name +
        " keeps running from its own disk, and nothing on any machine changes.",
      note:
        "No other entry of the inventory names these files. An image deleted " +
        "here is gone: creating this guest again means uploading it again.",
      label: "Delete",
      act: async () => {
        await API.post(
          "/vms/" + encodeURIComponent(guest.name) + "/delete-sources"
        );
        refresh(true).catch((failure) => showBanner(failure.message));
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

  function metaButton(name, deployment, guest) {
    const cell = document.createElement("td");
    if (deployment !== "cluster") {
      // The metadata is on an RBD image, and a standalone machine has no Ceph
      // to hold one. What stands in for it is the domain libvirt holds and the
      // profile the entry carries, for a guest the inventory declares.
      if (guest && canWrite) {
        standaloneButtons(cell, guest);
      }
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

  // A standalone guest's two windows. The domain is offered once a machine
  // reports it, since before that there is nothing for `virsh dumpxml` to
  // read; the profile is an entry of the inventory and always is.
  function standaloneButtons(cell, guest) {
    if (guest.domain) {
      const xml = document.createElement("button");
      xml.type = "button";
      xml.className = "secondary";
      xml.textContent = "XML";
      xml.title = "Edit " + guest.name + "'s libvirt domain, as virsh edit does";
      xml.addEventListener("click", () => DOMAIN.open(guest));
      cell.append(xml, " ");
    }
    const profile = document.createElement("button");
    profile.type = "button";
    profile.className = "secondary";
    profile.textContent = "Pinning profile";
    profile.title =
      "Edit seapath_alloc, written to /etc/seapath/alloc.d/" +
      guest.name + ".yaml";
    profile.addEventListener("click", () => PROFILE.open(guest));
    cell.append(profile);
  }

  // One editor, two subjects, and the same gesture for both: edit, Save, and
  // when something changed, say whether the guest is restarted to apply it.
  // Both are read when the guest starts from shut off. Then one run does all
  // of it, the restart at its end when asked for, and the window closes onto
  // the run, which goes on whether or not anybody keeps watching it.
  function guestEditor(id, { title, load, save }) {
    let guest = null;
    let original = "";

    function show(part) {
      element(id + "-edit").hidden = part !== "edit";
      element(id + "-ask").hidden = part !== "ask";
    }

    function fail(message) {
      const error = element(id + "-error");
      error.textContent = message;
      error.hidden = !message;
    }

    async function open(row) {
      guest = row.name;
      element(id + "-title").textContent = title(guest);
      element(id + "-lead").hidden = true;
      element(id + "-text").value = "";
      element(id + "-save").disabled = true;
      fail("");
      show("edit");
      element(id).hidden = false;
      element(id + "-loading").hidden = false;
      try {
        const loaded = await load(row);
        original = loaded.text;
        element(id + "-text").value = loaded.text;
        if (loaded.lead) {
          element(id + "-lead").textContent = loaded.lead;
          element(id + "-lead").hidden = false;
        }
        element(id + "-save").disabled = false;
        element(id + "-text").focus();
      } catch (failure) {
        fail(failure.message);
      } finally {
        element(id + "-loading").hidden = true;
      }
    }

    element(id + "-save").addEventListener("click", () => {
      fail("");
      if (element(id + "-text").value.trim() === original.trim()) {
        fail("Nothing changed, so there is nothing to apply.");
        return;
      }
      element(id + "-ask-text").textContent =
        guest + " reads this when it starts from shut off, and a reboot from " +
        "inside it is not that. Restarting it now shuts it down through ACPI " +
        "and starts it again at the end of the same run: whatever it serves " +
        "stops in between.";
      element(id + "-restart").textContent = "Apply and restart " + guest;
      show("ask");
    });

    async function apply(restart) {
      const buttons = [id + "-restart", id + "-later", id + "-back"].map(element);
      buttons.forEach((button) => (button.disabled = true));
      fail("");
      try {
        const answer = await save(guest, element(id + "-text").value, restart);
        if (!answer.run_id) {
          fail("The machine already holds exactly this, so nothing was run.");
          show("edit");
          return;
        }
        element(id).hidden = true;
        RunWatch.open(answer.run_id);
        refresh(true).catch((failure) => showBanner(failure.message));
      } catch (failure) {
        fail(failure.message);
      } finally {
        buttons.forEach((button) => (button.disabled = false));
      }
    }

    element(id + "-restart").addEventListener("click", () => apply(true));
    element(id + "-later").addEventListener("click", () => apply(false));
    element(id + "-back").addEventListener("click", () => show("edit"));
    element(id + "-cancel").addEventListener("click", () => {
      element(id).hidden = true;
    });

    return { open };
  }

  const DOMAIN = guestEditor("domain", {
    title: (name) => "Domain of " + name,
    load: async (row) => {
      const read = await API.get("/vms/" + encodeURIComponent(row.name) + "/xml");
      return {
        text: read.xml,
        lead: "The inactive definition " + read.host + " holds, read just now.",
      };
    },
    save: (name, xml, restart) =>
      API.put("/vms/" + encodeURIComponent(name) + "/xml", { xml, restart }),
  });

  const PROFILE = guestEditor("profile", {
    title: (name) => "Pinning profile of " + name,
    load: async (row) => ({ text: row.seapath_alloc || "" }),
    save: (name, profile, restart) => {
      const commit = lastView && lastView.inventory_commit;
      return API.put(
        "/vms/" + encodeURIComponent(name) + "/seapath-alloc",
        { profile, restart },
        commit ? { "If-Match": commit } : undefined
      );
    },
  });

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
    placementNodes = view.placement_nodes || [];
    // What one `VMs` group cannot say about a file that declares both a
    // cluster and a machine outside it. Said where the guests are listed,
    // because that is where the reading would otherwise be trusted.
    showBanner((view.warnings || []).join(" "));
    element("runtime-note").textContent = view.runtime_note;
    element("empty").textContent = view.note;
    element("empty").hidden = !view.note;

    renderFilter(view);
    const rows = element("guest-rows");
    rows.replaceChildren();
    const listed = (view.guests || []).filter((guest) => shown(guest.deployment));
    // The Creation column, only where it has something to say about the guests
    // on screen. Both readings belong to a window: the recipe until the run
    // that creates the guest takes it out of the entry, and `force` for as
    // long as the entry carries it. Outside those a converged inventory gives
    // the column nothing, and an empty column on every row is width this table
    // does not have on a laptop. It comes back on its own with the next guest
    // declared.
    //
    // Judged against the rows the filter is showing, because the width is what
    // is being spent and the rows are what spend it.
    const creation = listed.some(hasCreation);
    element("creation-head").hidden = !creation;
    listed.forEach((guest) => {
      row(rows, [
        nameCell(guest),
        deployedBy(guest),
        state(guest),
        nodeCell(guest),
        addressCell(guest),
        specsCell(guest.domain),
        ...(creation ? [creationCell(guest)] : []),
        acts(guest),
        placement(guest),
        metaButton(guest.name, guest.deployment, guest),
      ]);
    });
    element("guest-table").hidden = !(view.guests || []).length;
  }

  // What `creationCell` would draw for this guest, as a yes or no. The two
  // have to agree: a column shown with nothing in it is the state this hides,
  // and a column hidden over a row that had something takes a reading away.
  function hasCreation(guest) {
    return Boolean((guest.creation || []).length || guest.force);
  }

  // The filter, with how many rows of the table each choice lists. The count
  // sits on the guest table and is read against it, so it counts the guests
  // the inventory declares and nothing else. The filter still narrows the
  // undeclared panel beside it, whose own rows say how many there are.
  function renderFilter(view) {
    const counts = { cluster: 0, standalone: 0 };
    (view.guests || []).forEach((guest) => {
      counts[guest.deployment] = (counts[guest.deployment] || 0) + 1;
    });
    counts.all = (view.guests || []).length;
    const box = element("filter");
    box.hidden = !counts.all || !split(view);
    box.querySelectorAll("button").forEach((button) => {
      const name = button.dataset.filter;
      const label = name.charAt(0).toUpperCase() + name.slice(1);
      button.textContent = label + " (" + (counts[name] || 0) + ")";
      button.setAttribute("aria-pressed", String(name === filter));
    });
  }

  element("filter").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) {
      return;
    }
    filter = button.dataset.filter;
    try {
      localStorage.setItem(FILTER_KEY, filter);
    } catch (ignored) {
      // A browser that keeps nothing still filters, for this visit.
    }
    if (lastView) {
      renderGuests(lastView);
      renderUndeclared(lastView);
    }
  });

  function renderUndeclared(view) {
    const resources = shown("cluster") ? view.undeclared || [] : [];
    const domains = shown("standalone") ? view.undeclared_domains || [] : [];
    element("undeclared-card").hidden = !(resources.length || domains.length);
    const rows = element("undeclared-rows");
    rows.replaceChildren();
    // A domain a machine runs and no inventory declares, on a machine
    // Pacemaker does not answer for. Same finding, other reading.
    domains.forEach((domain) => {
      // Its size on hover rather than in a column: this table sits in the
      // narrow column, where one more cell pushes the buttons out of view.
      const name = cell(domain.name);
      const specs = specsCell(domain);
      if (specs.textContent) {
        name.title = specs.textContent + "\n" + specs.title;
      }
      row(rows, [
        name,
        cell(domain.host),
        // No role: a role is what Pacemaker gives a resource, and nothing
        // holds one for this domain. Its state is libvirt's, in the same
        // words the table above uses.
        cell(""),
        cell(domain.state),
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
      element("add-replace").hidden = true;
      element("add-steps").hidden = true;
      element("add-name").focus();
      loadHeldFiles();
    }
  }

  // The images and XML files this node already holds, offered beside the
  // upload. Read each time the form opens rather than kept, because the files
  // a previous guest uploaded belong in the next guest's lists.
  const IMAGE_SUFFIXES = [".qcow2", ".img", ".gz"];
  const XML_SUFFIXES = [".xml", ".j2"];

  async function loadHeldFiles() {
    let folder = null;
    try {
      folder = await API.get("/inventory/folder");
    } catch (ignored) {
      // The lists stay at the upload alone, which still declares a guest. A
      // node that cannot read its own folder says so on the Inventory page,
      // and the declaration that follows fails with the reason if it matters.
      return;
    }
    fillHeld(
      "add-disk-source",
      "Upload an image",
      (folder.artefacts || []).map((item) => item.path),
      IMAGE_SUFFIXES
    );
    const xmlFiles = (folder.files || [])
      .map((item) => item.path)
      .filter((path) => XML_SUFFIXES.some((suffix) => path.endsWith(suffix)));
    // SEAPATH's own template, where the collection ships it. Offered first and
    // chosen when the folder holds no XML, because it is the file that serves
    // every guest: it takes the name, the disk and the MAC from each entry.
    // Stored without the leading `../`, which the declaration adds to every
    // held path alike.
    const shipped = lastView && lastView.collection_template;
    const collection = shipped
      ? [{ value: shipped.replace(/^\.\.\//, ""), label: "SEAPATH guest.xml.j2 (collection)" }]
      : [];
    fillHeld("add-xml-source", "Upload an XML", xmlFiles, XML_SUFFIXES, collection);
    if (!element("add-xml-source").value && collection.length && !xmlFiles.length) {
      element("add-xml-source").value = collection[0].value;
      showUploadFor("add-xml-source");
    }
    noteSingleUse();
  }

  function fillHeld(id, uploadLabel, paths, suffixes, first = []) {
    const select = element(id);
    const chosen = select.value;
    select.replaceChildren(new Option(uploadLabel, ""));
    first.forEach((item) => select.append(new Option(item.label, item.value)));
    paths
      .filter((path) => suffixes.some((suffix) => path.endsWith(suffix)))
      .sort()
      .forEach((path) => select.append(new Option(path, path)));
    // The choice survives a reopening as long as the file is still there:
    // the next guest of a site is usually made from the same two files.
    select.value = [...select.options].some((option) => option.value === chosen)
      ? chosen
      : "";
    showUploadFor(id);
  }

  // The file input is for an upload, so it goes away while a held file is
  // chosen rather than sitting there being ignored.
  function showUploadFor(id) {
    const input = id === "add-disk-source" ? "add-disk" : "add-xml";
    element(input).hidden = Boolean(element(id).value);
  }

  // A plain XML names one domain, and on a standalone machine libvirt takes
  // that name from the file, so the XML serves one guest. Said beside the
  // choice rather than only when the declaration refuses a second guest.
  function noteSingleUse() {
    const held = element("add-xml-source").value;
    const uploaded = element("add-xml").files[0];
    const name = held || (uploaded ? uploaded.name : "");
    element("add-xml-single").hidden =
      !name || name.endsWith(".j2") || chosenDeployment() === "cluster";
  }

  ["add-disk-source", "add-xml-source"].forEach((id) => {
    element(id).addEventListener("change", () => {
      showUploadFor(id);
      noteSingleUse();
    });
  });
  element("add-xml").addEventListener("change", noteSingleUse);
  element("add-deployment").addEventListener("change", noteSingleUse);

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

  // The profile most guests want: every vCPU on a logical CPU of its own, in
  // FIFO at the lowest real time priority. A starting point typed for the
  // operator, who edits it in the field like any profile of their own.
  const BASIC_PROFILE = [
    "version: 1",
    "vcpus:",
    "  isolation: exclusive_logical",
    "  scheduler: FIFO",
    "  priority: 1",
    "",
  ].join("\n");

  element("add-profile-basic").addEventListener("click", () => {
    const field = element("add-profile");
    const current = field.value.trim();
    if (
      current &&
      current !== BASIC_PROFILE.trim() &&
      !window.confirm("Replace the profile in the field with the basic one?")
    ) {
      return;
    }
    field.value = BASIC_PROFILE;
    field.focus();
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
      seapath_alloc: element("add-profile").value.trim() || null,
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

  // The guest's network, as the one section of this form that writes three
  // variables at once. Null where the section was left empty, which is the
  // guest whose image carries its own configuration: the API then writes no
  // interface, no seed and no address, rather than writing empty ones.
  function network() {
    const text = (id) => element(id).value.trim();
    const asked = {
      bridge: text("add-bridge"),
      mac_address: text("add-mac"),
      address: text("add-address"),
      gateway: text("add-gateway"),
      hostname: text("add-hostname"),
      dns: text("add-dns")
        .split(",")
        .map((server) => server.trim())
        .filter((server) => server !== ""),
      dhcp: element("add-dhcp").checked,
      packages: text("add-packages")
        .split(/[\s,]+/)
        .filter((name) => name !== ""),
    };
    // The box rather than the field it reveals, so that a box checked over an
    // empty field reaches the refusal the API has for it. Read without
    // trimming: a space at either end of a password is part of it.
    const root = element("add-root-password").checked;
    const empty =
      !asked.bridge &&
      !asked.mac_address &&
      !asked.address &&
      !asked.gateway &&
      !asked.hostname &&
      !asked.dns.length &&
      !asked.dhcp &&
      !root &&
      !asked.packages.length;
    if (empty) {
      return null;
    }
    // Judged after the emptiness above rather than as part of it: the box is
    // checked by default, and a guest whose section was left blank is one
    // whose image carries its own configuration, which a seed would only get
    // in the way of.
    asked.trust_this_node = element("add-trust").checked;
    asked.grant_sudo = asked.trust_this_node && element("add-grant-sudo").checked;
    asked.accept_host_key = element("add-accept-host-key").checked;
    asked.root_password = root ? element("add-root-secret").value : null;
    return asked;
  }

  // A lease carries the address, the route and the resolvers, so the fields
  // for those three go away rather than sitting there being ignored. The API
  // refuses the pair as well, because a page is not where a rule lives.
  element("add-dhcp").addEventListener("change", () => {
    element("add-static").hidden = element("add-dhcp").checked;
  });

  element("add-root-password").addEventListener("change", (event) => {
    element("add-root").hidden = !event.target.checked;
    if (event.target.checked) {
      element("add-root-secret").focus();
    } else {
      element("add-root-secret").value = "";
    }
  });

  // Whether something already answers at the address typed. Asked on the
  // button rather than as the field changes, because each ask sends echo
  // requests onto a substation network and waits up to three seconds. The
  // answer belongs to the value it was asked about, so editing the field
  // takes it away.
  const pingAnswer = element("add-address-answer");
  element("add-address").addEventListener("input", () => {
    pingAnswer.hidden = true;
  });
  element("add-address-ping").addEventListener("click", async () => {
    const button = element("add-address-ping");
    const address = element("add-address").value.trim();
    if (!address) {
      element("add-address").focus();
      return;
    }
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    pingAnswer.className = "help";
    pingAnswer.textContent = "Pinging " + address + "...";
    pingAnswer.hidden = false;
    try {
      const answer = await API.get(
        "/vms/ping?address=" + encodeURIComponent(address)
      );
      // Answered is the warning, since it is the one conclusive result and
      // the one that should stop the form. Silence is reassuring, not proof.
      pingAnswer.className =
        answer.state === "answered"
          ? "warning"
          : answer.state === "silent"
          ? "clear"
          : "help";
      pingAnswer.textContent =
        answer.state === "answered" && answer.round_trip_ms !== null
          ? answer.detail + " (" + answer.round_trip_ms + " ms)"
          : answer.detail;
    } catch (failure) {
      pingAnswer.className = "error";
      pingAnswer.textContent = failure.message;
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });

  // Which variable names the XML, which the two roles answer differently. The
  // cluster role takes a `.j2` as a template and anything else as the XML
  // itself, through `xml_path`. The standalone role reads `vm_template` and
  // nothing else, and a plain XML is a template with no `{{ }}` in it, so there
  // every file is written as `vm_template`: `xml_path` on a standalone guest
  // fails at the first task that looks the template up.
  function xmlVariable(filename) {
    if (filename.endsWith(".j2") || chosenDeployment() !== "cluster") {
      return "vm_template";
    }
    return "xml_path";
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

  // The files the last attempt stored, so that a retry after a refusal sends
  // none of them again. The disk image can be twenty gigabytes.
  let sent = null;

  function declaredAlready(name) {
    return ((lastView && lastView.guests) || []).some(
      (guest) => guest.name === name
    );
  }

  async function addGuest(replace) {
    const name = element("add-name").value.trim();
    // A path this node already holds, or empty where the file is uploaded for
    // this guest. The two are exclusive, which the form shows by hiding the
    // file input while a held file is chosen.
    const diskReused = element("add-disk-source").value;
    const xmlReused = element("add-xml-source").value;
    const disk = diskReused ? null : element("add-disk").files[0];
    const xml = xmlReused ? null : element("add-xml").files[0];
    const error = element("add-error");
    const replaceButton = element("add-replace");
    error.hidden = true;
    replaceButton.hidden = true;

    if (!name || !(diskReused || disk) || !(xmlReused || xml)) {
      error.textContent =
        "A VM needs a name, a disk image and a libvirt XML. All three are " +
        "what the deployment run is given.";
      error.hidden = false;
      return;
    }

    // Asked before the upload rather than after it: the declaration is the
    // third step, and learning there that the name is taken costs the whole
    // disk image in transfer.
    if (!replace && declaredAlready(name)) {
      error.textContent =
        name +
        " is already declared in this inventory, typically by an earlier " +
        "attempt. Replacing its declaration writes this form over it and " +
        "deploys it. A guest the hypervisor already runs under that name is " +
        "left as it is.";
      error.hidden = false;
      replaceButton.hidden = false;
      return;
    }

    gzipped = (diskReused || disk.name).endsWith(".gz");
    const diskPath = diskReused || stored(name, disk.name);
    const xmlPath = xmlReused || stored(name, xml.name);
    // Judged file by file, so a retry after a refusal sends neither a file it
    // already stored nor one this node held from the start.
    const diskThere =
      Boolean(diskReused) ||
      (sent !== null && sent.disk === disk && sent.diskPath === diskPath);
    const xmlThere =
      Boolean(xmlReused) ||
      (sent !== null && sent.xml === xml && sent.xmlPath === xmlPath);
    const said = (reused, there, verb, path) =>
      (reused ? "Reusing " : there ? "Already " + verb.done + ": " : verb.doing + " ") +
      path;
    const progress = steps([
      said(diskReused, diskThere, { doing: "Uploading", done: "uploaded" }, diskPath),
      said(xmlReused, xmlThere, { doing: "Committing", done: "committed" }, xmlPath),
      (replace ? "Replacing the declaration of " : "Declaring ") +
        name +
        " in the inventory",
      "Launching the deployment",
    ]);

    const go = element("add-go");
    go.disabled = true;
    go.setAttribute("aria-busy", "true");
    try {
      if (!diskThere) {
        progress.at(0, "doing");
        await API.upload("/inventory/artefacts/" + diskPath, disk);
        sent = Object.assign({}, sent, { disk, diskPath });
      }
      progress.at(0, "done");

      if (!xmlThere) {
        progress.at(1, "doing");
        await API.upload("/inventory/files/" + xmlPath, xml);
        sent = Object.assign({}, sent, { xml, xmlPath });
      }
      progress.at(1, "done");

      progress.at(2, "doing");
      const entry = Object.assign(
        {
          name,
          vm_disk: "../" + diskPath,
          deployment: chosenDeployment(),
          network: network(),
        },
        declaration()
      );
      entry[xmlVariable(xmlPath)] = "../" + xmlPath;
      if (replace) {
        entry.replace = true;
      }
      const declared = await API.post("/vms", entry);
      // The step says the MAC the entry ended up with, which this service
      // generates when a bridge was named and the field was left empty. It is
      // now the guest's identity on that bridge, and a DHCP reservation or a
      // switch's port security is written against it.
      progress.at(
        2,
        "done",
        declared.mac_address
          ? "Declared " + name + ", on " + declared.mac_address
          : ""
      );

      progress.at(3, "doing");
      // Where the root password is actually read. The declaration above sent
      // it too, and the entry it committed names no password at all: that call
      // checks the value and writes nothing, because a hash written into the
      // inventory is a hash git keeps, replicated to every machine the file
      // declares. This run splices it into the copy of the inventory it reads,
      // and wipes that copy when it ends.
      //
      // Read off the form once more rather than held across the calls above:
      // the field is still filled in until this returns, and it is emptied
      // below.
      const rootPassword = element("add-root-password").checked
        ? element("add-root-secret").value
        : "";
      const started = await API.post("/runs", {
        playbook: declared.playbook,
        root_passwords: rootPassword ? { [name]: rootPassword } : {},
      });
      progress.at(3, "done");
      // The form is finished with, and what follows it is the run. It used to
      // be closed by the navigation to the Runs page; now the window over this
      // page is what the operator watches, and the form behind it would be a
      // filled in copy of the guest that was just declared.
      //
      // What identifies that guest is emptied with it: a name and two files
      // reopened as they were are what declares the same image twice. The
      // shape below them is left alone, because the next guest of a site is
      // usually the same shape.
      //
      // The address, the MAC and the hostname go with the name, and for a
      // harder reason than convenience: they are the three values no two
      // guests may share, and a form that kept them would offer the next
      // guest a collision the API then refuses.
      //
      // A held image or template chosen in the two lists stays chosen, since
      // declaring the next guest from the same files is what they are for.
      // Files uploaded for this guest are held now too, and the lists read
      // the folder again the next time the form opens.
      element("add-name").value = "";
      element("add-disk").value = "";
      element("add-xml").value = "";
      element("add-address").value = "";
      element("add-address-answer").hidden = true;
      element("add-mac").value = "";
      element("add-hostname").value = "";
      // The password goes with them, and the box with it. The rest of the
      // shape is kept because the next guest of a site is usually the same
      // shape; a root password is one guest's, and a form holding it after
      // the guest was declared is a password on a screen nobody is watching.
      element("add-root-password").checked = false;
      element("add-root").hidden = true;
      element("add-root-secret").value = "";
      sent = null;
      showAdd(false);
      RunWatch.open(started.run_id);
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
      // The page's reading of the inventory can be older than the file,
      // when another administrator declared the name in between.
      if (failure.code === "guest_exists") {
        replaceButton.hidden = false;
      }
    } finally {
      go.disabled = false;
      go.removeAttribute("aria-busy");
    }
  }

  element("add").addEventListener("click", () => showAdd(true));
  element("add-cancel").addEventListener("click", () => showAdd(false));
  element("add-go").addEventListener("click", () => addGuest(false));
  element("add-replace").addEventListener("click", () => addGuest(true));
  // The offer is about the name that was refused, and a different name is a
  // new question.
  element("add-name").addEventListener("input", () => {
    element("add-replace").hidden = true;
  });

  // What this page is made of: the declaration of every guest joined to what
  // the cluster is doing with it. One request, and the three renders that
  // divide it up. Called again by the control in the heading, which is why it
  // stands on its own: a guest started from here, or by somebody else on
  // another node, shows up without the page being loaded again.
  const KEPT = "vms";

  function draw(view) {
    lastView = view;
    renderGuests(view);
    renderUndeclared(view);
    fillChoices(view);
  }

  // What the console offers is read beside the guests, so a row draws its
  // button in the same pass. A description that fails costs the buttons and
  // nothing else on the page.
  async function describeConsole() {
    try {
      await Console.describe(Chrome.current());
    } catch (ignored) {
      // The rows are drawn without a console button.
    }
  }

  async function refresh(fresh, pending) {
    const reading = readDisplays();
    const [view] = await Promise.all([
      pending || API.get(API.reading("/vms", fresh)),
      describeConsole(),
    ]);
    draw(view);
    Kept.keep(KEPT, view);
    Kept.release();
    // Drawn again once the definitions said which guests have a screen: one
    // `rbd` per cluster guest and one `ssh` per standalone machine, which the
    // table does not wait for.
    if (await reading) {
      renderGuests(lastView);
    }
  }

  // Whether the reading changed anything worth drawing again. One that fails
  // keeps what the last one said, and costs the new buttons alone.
  async function readDisplays() {
    try {
      const answer = await API.get("/vms/displays");
      displays = answer.guests;
      return true;
    } catch (ignored) {
      return false;
    }
  }

  async function start() {
    const me = Chrome.current();
    // Starting a guest changes no desired state, so it is the operator's act
    // rather than the administrator's, the way cancelling a run is.
    canAct = me.role === "operator" || Chrome.isAdmin(me);
    // Changing what a guest is configured with is an administrator's act, the
    // way every other write in this service is.
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
    // paints the table it last drew. Every control in that table is held until
    // the answer lands: a guest may have moved to another node since, and
    // starting or migrating one from a row that old is an act on a live
    // substation hypervisor aimed at the wrong machine. See D46.
    const pending = API.started("/vms");
    const age = Kept.paint(KEPT, draw);
    if (age !== null) {
      Kept.rereading(["loading"], age);
      Kept.hold(["card-guests", "undeclared-card"]);
    }
    await refresh(false, pending);
    // Adding a VM commits the inventory and launches a run, which is an
    // administrator's act like every other write in this service.
    element("add").hidden = !Chrome.isAdmin(me);
  }

  start().catch((failure) => {
    showBanner(failure.message);
    element("loading").hidden = true;
    Kept.release();
  });
})();
