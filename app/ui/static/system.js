// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The system page: what turns a desired state into a configured machine.
//
// One playbook is the page: `seapath_setup_main`, the commissioning path and
// the one the CI runs. The rest of the catalogue is a picker, because thirteen
// stacked rows put the entry an operator came for below the fold, and the
// entry they came for is never the first one. The two credentials that let
// this node reach the others sit at the bottom, shut, once they hold.
//
// Nothing here edits the desired state, and nothing here changes a machine
// except through Ansible.

(function () {
  const state = {
    me: null,
    node: null,
    inventory: null,
    thisHost: null,
    siteKey: null,
    hostKeys: [],
    catalogue: [],
    // Whether the panel at the bottom has already been opened or left shut for
    // this page load. Decided once: re-rendering it after every accepted host
    // key would fold it away under the cursor of the operator accepting them.
    reachDecided: false,
  };

  const MAIN = "seapath_setup_main";

  function element(id) {
    return document.getElementById(id);
  }

  function showBanner(messages) {
    const banner = element("banner");
    if (!messages.length) {
      banner.hidden = true;
      return;
    }
    banner.replaceChildren();
    messages.forEach((message) => {
      const line = document.createElement("div");
      line.textContent = message;
      banner.append(line);
    });
    banner.hidden = false;
  }

  // Reaching the other machines. Two acts, both explicit, both reversible:
  // holding the site key, and accepting the host keys of the machines this
  // node is about to drive.
  async function loadSiteKey() {
    const key = await API.get("/trust/site-key");
    state.siteKey = key;
    const summary = element("site-key-summary");
    summary.replaceChildren();
    const pairs = key.installed
      ? [["Type", key.key_type], ["Fingerprint", key.fingerprint]]
      : [["Status", "No site key. This node reaches only itself."]];
    pairs.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = value;
      summary.append(term, definition);
    });
    element("site-key-remove").hidden = !key.installed;
    renderReach();
    return key;
  }

  // One list, whose rows change state. Scanning merges what it found into
  // what is already accepted, so an operator checking three fingerprints in
  // one sitting never sees the list replaced under the cursor.
  async function loadHostKeys() {
    state.hostKeys = await API.get("/trust/host-keys");
    renderHostKeys();
  }

  function keyOf(row) {
    return row.address + " " + row.key;
  }

  function mergeHostKeys(found) {
    const byKey = new Map(state.hostKeys.map((row) => [keyOf(row), row]));
    found.forEach((row) => {
      const existing = byKey.get(keyOf(row));
      if (existing) {
        existing.accepted = row.accepted;
      } else {
        byKey.set(keyOf(row), row);
      }
    });
    state.hostKeys = [...byKey.values()].sort((a, b) =>
      a.address.localeCompare(b.address)
    );
  }

  function renderHostKeys() {
    const body = document.querySelector("#host-keys-table tbody");
    body.replaceChildren();
    state.hostKeys.forEach((row) => {
      const line = document.createElement("tr");
      [row.address, row.key_type, row.fingerprint].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        line.append(cell);
      });

      const status = document.createElement("td");
      status.textContent = row.accepted ? "accepted" : "seen, not accepted";
      line.append(status);

      const actions = document.createElement("td");
      if (Chrome.isAdmin(state.me)) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = row.accepted ? "forget" : "accept";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await (row.accepted ? forgetHostKey(row) : acceptHostKeys([row]));
          } finally {
            button.disabled = false;
          }
        });
        actions.append(button);
      }
      line.append(actions);
      body.append(line);
    });

    const pending = state.hostKeys.filter((row) => !row.accepted);
    const all = element("host-keys-accept-all");
    all.hidden = pending.length < 2 || !Chrome.isAdmin(state.me);
    all.textContent = "Accept all " + pending.length + " remaining";
    renderReach();
  }

  // The machines a run has to reach, which is every machine of the inventory
  // but this one: the node drives the run and needs no host key of its own.
  function peers() {
    const hosts = state.inventory ? state.inventory.hosts : {};
    return Object.entries(hosts)
      .filter(([name]) => name !== state.thisHost)
      .map(([, node]) => node.ansible_host)
      .filter(Boolean);
  }

  // The one line the panel is worth once it holds. Same two facts the
  // `peer_reachable` precondition is computed from, said as a state rather
  // than as a form: a key would be offered, and the host keys are known.
  function reachState() {
    const accepted = new Set(
      state.hostKeys.filter((row) => row.accepted).map((row) => row.address)
    );
    const seen = state.hostKeys.filter((row) => !row.accepted).length;
    const unknown = peers().filter((address) => !accepted.has(address));

    if (state.siteKey && !state.siteKey.installed && peers().length) {
      return { ok: false, text: "no site key, this node reaches only itself" };
    }
    if (unknown.length) {
      return {
        ok: false,
        text:
          unknown.length +
          (unknown.length === 1 ? " machine has" : " machines have") +
          " no accepted host key",
      };
    }
    const held = peers().length
      ? "site key held, " + accepted.size + " accepted"
      : "this node only, nothing else to reach";
    return {
      ok: true,
      text: seen ? held + ", " + seen + " seen and not accepted" : held,
    };
  }

  function renderReach() {
    const reach = reachState();
    const line = element("reach-state");
    line.textContent = reach.text;
    line.className = "reach-state " + (reach.ok ? "ok" : "warn");
    if (!state.reachDecided) {
      element("reach-details").open = !reach.ok;
    }
  }

  async function acceptHostKeys(rows) {
    const error = element("host-keys-error");
    error.hidden = true;
    try {
      const accepted = await API.post("/trust/host-keys", { keys: rows });
      const acceptedKeys = new Set(accepted.map(keyOf));
      // The rows change state in place. Replacing the list here would drop the
      // fingerprints of everything scanned but not yet accepted.
      state.hostKeys.forEach((row) => {
        if (acceptedKeys.has(keyOf(row))) {
          row.accepted = true;
        }
      });
      renderHostKeys();
      await loadPlaybooks();
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    }
  }

  async function forgetHostKey(row) {
    await API.del("/trust/host-keys/" + encodeURIComponent(row.address));
    state.hostKeys.forEach((other) => {
      if (other.address === row.address) {
        other.accepted = false;
      }
    });
    renderHostKeys();
    await loadPlaybooks();
  }

  // Opening the panel is the operator saying they are working in it, so it
  // stays as they left it for the rest of the visit.
  element("reach-details").addEventListener("toggle", () => {
    state.reachDecided = true;
  });

  element("site-key-file").addEventListener("change", (event) => {
    element("site-key-go").disabled = !event.target.files.length;
    element("site-key-error").hidden = true;
  });

  element("site-key-go").addEventListener("click", async () => {
    const error = element("site-key-error");
    error.hidden = true;
    const file = element("site-key-file").files[0];
    if (!file) {
      return;
    }
    try {
      await API.put("/trust/site-key", { material: await file.text() });
      element("site-key-file").value = "";
      element("site-key-go").disabled = true;
      await loadSiteKey();
      await loadPlaybooks();
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    }
  });

  element("site-key-remove").addEventListener("click", async () => {
    await API.del("/trust/site-key");
    await loadSiteKey();
    await loadPlaybooks();
  });

  element("host-keys-scan").addEventListener("click", async () => {
    const error = element("host-keys-error");
    error.hidden = true;
    try {
      mergeHostKeys(await API.post("/trust/host-keys/scan", { addresses: peers() }));
      renderHostKeys();
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    }
  });

  element("host-keys-accept-all").addEventListener("click", () =>
    acceptHostKeys(state.hostKeys.filter((row) => !row.accepted))
  );

  // Applying
  //
  // An entry renders the same whether it is the commissioning path at the top
  // of the page or the one picked from the list: same title, same playbook
  // name, same scope, same sentence about what it disturbs. A run launched
  // from the second place is not a smaller act than one launched from the
  // first.
  function renderEntry(item, container) {
    const entry = item.entry;
    container.replaceChildren();
    container.className = "playbook" + (item.available ? "" : " unavailable");

    const title = document.createElement("div");
    title.className = "playbook-title";
    title.textContent = entry.title;
    if (entry.reboots !== "no") {
      const tag = document.createElement("span");
      tag.className = "tag warn";
      tag.textContent = entry.reboots === "gated" ? "reboots (optional)" : "reboots";
      title.append(" ", tag);
    }
    // An entry read off the collection rather than written by a human. The
    // sentence below it says what was counted; this says who counted it.
    if (!entry.reviewed) {
      const tag = document.createElement("span");
      tag.className = "tag warn";
      tag.textContent = "not reviewed";
      title.append(" ", tag);
    }

    // The name everything outside this page uses: docs/playbooks.md, the
    // upstream repository, the run list and the artefacts of a run all say
    // `seapath_setup_deploy_seapath_alloc`, and the row above says "Apply the
    // dynamic CPU pinning". An operator looking for the playbook they were
    // told to run has to be able to find it here.
    const name = document.createElement("div");
    name.className = "playbook-id";
    name.textContent = entry.id;

    // Which machines the run reaches. `targets` is copied from the playbook's
    // own `hosts:` lines, so the groups are named here exactly as
    // docs/playbooks.md and the upstream playbook name them, intersections
    // included. Without this line the only statement of scope on the page is
    // the title, and a title has room for "every machine" but not for "the
    // hypervisors that are also cluster members".
    const scope = document.createElement("div");
    scope.className = "playbook-scope";
    const groups = document.createElement("span");
    groups.className = "playbook-groups";
    groups.textContent = entry.targets.join(", ");
    scope.append("Plays ", groups);

    const detail = document.createElement("p");
    detail.className = item.available ? "help" : "warning";
    withCode(detail, item.available ? entry.disruption : item.unmet.join(" "));

    container.append(title, name, scope, detail);

    // What the reader counted in the playbook, for every entry. It is the
    // substance of an unreviewed description, and on a reviewed one it is the
    // size of the act: eleven roles and four hundred tasks is not one template
    // and a restart.
    if (entry.derivation && entry.derivation.tasks) {
      const counts = document.createElement("div");
      counts.className = "playbook-counts";
      const facts = entry.derivation;
      const parts = [
        facts.plays + (facts.plays === 1 ? " play" : " plays"),
        facts.tasks + (facts.tasks === 1 ? " task" : " tasks"),
      ];
      if (facts.command_tasks) {
        parts.push(facts.command_tasks + " command driven");
      }
      if (facts.roles.length) {
        parts.push(
          facts.roles.length + (facts.roles.length === 1 ? " role" : " roles")
        );
      }
      counts.textContent = parts.join(", ");
      container.append(counts);
    }

    // What the catalogue knows and the old list had no room to say: run the
    // network playbook from another node, a removal names a machine that has
    // died. Operational, and worth the two lines now that one entry is on
    // screen at a time.
    if (entry.notes) {
      const notes = document.createElement("p");
      notes.className = "help";
      withCode(notes, entry.notes);
      container.append(notes);
    }

    const actions = document.createElement("div");
    actions.className = "actions";
    if (item.available && Chrome.isAdmin(state.me)) {
      if (entry.preview !== "none") {
        const check = document.createElement("button");
        check.type = "button";
        check.className = "secondary";
        check.textContent =
          entry.preview === "full" ? "Preview" : "Preview (partial)";
        check.addEventListener("click", () => confirmRun(entry, true));
        actions.append(check);
      }
      const apply = document.createElement("button");
      apply.type = "button";
      apply.textContent = "Apply";
      apply.addEventListener("click", () => confirmRun(entry, false));
      actions.append(apply);
    }
    container.append(actions);
  }

  // Catalogue prose names commands and variables in backticks, the way the
  // documents it was written alongside do. Rendered as text they show as
  // literal backticks, and these sentences are read right before an apply.
  // Split rather than parsed: this is the only markup the strings carry, and
  // every piece still goes in as text, so nothing here can inject markup.
  function withCode(target, text) {
    text.split("`").forEach((piece, index) => {
      if (index % 2) {
        const code = document.createElement("code");
        code.textContent = piece;
        target.append(code);
      } else if (piece) {
        target.append(document.createTextNode(piece));
      }
    });
    return target;
  }

  function isCluster(item) {
    return item.entry.requires.includes("cluster");
  }

  // Every playbook of the collection is in the list. The ones nobody has
  // written a sentence for are last, under a heading that says so, because an
  // operator reaching for the network playbook must not have to tell the
  // reviewed entry apart from a description this service counted itself.
  function groups(rest) {
    const reviewed = rest.filter((item) => item.entry.reviewed);
    return [
      ["Machine configuration", reviewed.filter((item) => !isCluster(item))],
      ["Cluster", reviewed.filter(isCluster)],
      ["Read from the collection, not reviewed",
        rest.filter((item) => !item.entry.reviewed)],
    ];
  }

  // The catalogue as a list, grouped the way docs/playbooks.md groups it. The
  // unavailable entries stay in the list rather than disappearing from it: an
  // operator told to run `cluster_setup_ha` has to find it and read why it is
  // not offered, and a list that hides what it cannot do is a list that cannot
  // answer that question.
  function renderChoices() {
    const select = element("playbook-choice");
    const chosen = select.value;
    select.replaceChildren();

    const rest = state.catalogue.filter((item) => item.entry.id !== MAIN);
    groups(rest).forEach(([label, items]) => {
      if (!items.length) {
        return;
      }
      const group = document.createElement("optgroup");
      group.label = label;
      items.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.entry.id;
        option.textContent =
          item.entry.title + (item.available ? "" : " (unavailable)");
        group.append(option);
      });
      select.append(group);
    });

    const available = rest.find((item) => item.available && item.entry.reviewed);
    const keep = rest.some((item) => item.entry.id === chosen);
    select.value = keep
      ? chosen
      : (available || rest[0] || { entry: { id: "" } }).entry.id;
    renderChoice();
  }

  function renderChoice() {
    const id = element("playbook-choice").value;
    const item = state.catalogue.find((row) => row.entry.id === id);
    const detail = element("playbook-detail");
    if (!item) {
      detail.replaceChildren();
      return;
    }
    renderEntry(item, detail);
  }

  element("playbook-choice").addEventListener("change", renderChoice);

  // The two cards this page is, while the catalogue is being read. Reading it
  // walks every playbook of the installed collection, which is a few hundred
  // YAML files on a first load, and an empty card is what a node with no
  // collection at all looks like.
  function showPlaybooksLoading(loading) {
    element("main-loading").hidden = !loading;
    element("other-loading").hidden = !loading;
    element("main-playbook").hidden = loading;
    element("playbook-field").hidden = loading;
    if (loading) {
      element("playbook-detail").replaceChildren();
    }
  }

  async function loadPlaybooks() {
    showPlaybooksLoading(true);
    try {
      state.catalogue = await API.get("/playbooks");
    } catch (failure) {
      // The banner carries the message. What matters here is that the cards
      // do not fall back to looking empty, which is what a node with no
      // collection looks like, so the line says which of the two happened.
      element("main-loading").textContent = "The catalogue could not be read.";
      element("other-loading").textContent = "The catalogue could not be read.";
      throw failure;
    }
    showPlaybooksLoading(false);

    // When nothing can run, the reason is almost always one reason, and
    // repeating it in small print under every dimmed entry is how an operator
    // ends up asking why the buttons are greyed out. Say it once, at the top.
    const blocked = element("apply-blocked");
    blocked.textContent = blockingEverything(state.catalogue).join("  ");
    blocked.hidden = !blocked.textContent;

    const main = state.catalogue.find((item) => item.entry.id === MAIN);
    const hero = element("main-playbook");
    if (main) {
      renderEntry(main, hero);
      hero.classList.add("hero");
    } else {
      // The catalogue and the collection are released separately, so the
      // commissioning entry can be absent from what this image ships.
      hero.replaceChildren();
      hero.className = "playbook unavailable";
      const line = document.createElement("p");
      line.className = "warning";
      line.textContent =
        "This service knows no " + MAIN + " entry, which is the commissioning " +
        "path. Every playbook below is a part of it.";
      hero.append(line);
    }

    renderChoices();
  }

  // What blocks every entry, said once. Grouped by the code behind the
  // sentence rather than by the sentence: thirteen playbooks missing from the
  // collection produce thirteen different sentences and one problem.
  function blockingEverything(catalogue) {
    if (!catalogue.length || catalogue.some((item) => item.available)) {
      return [];
    }
    const shared = catalogue
      .map((item) => new Set(item.unmet_codes))
      .reduce((left, right) => new Set([...left].filter((c) => right.has(c))));

    return [...shared].map((code) => {
      if (code === "playbook_present") {
        return (
          "None of these playbooks is in the SEAPATH collection this service " +
          "is running with, so nothing can be launched from here. That is what " +
          "the image ships and a run needs; a service started from a source " +
          "checkout has to be pointed at a collection with " +
          "SEAPATH_WEBUI_COLLECTIONS_PATH."
        );
      }
      const first = catalogue.find((item) => item.unmet_codes.includes(code));
      return first.unmet[first.unmet_codes.indexOf(code)];
    });
  }

  function machineNames() {
    return state.inventory ? Object.keys(state.inventory.hosts) : [];
  }

  // A playbook that needs a machine named, `cluster_remove_machine` being the
  // one, gets a list of the machines the inventory declares rather than a text
  // field. The name has to match an inventory entry exactly, since the
  // playbook reads `hostvars[machine_to_remove]`, and typing it is the one way
  // to get that wrong. This node is not in the list: it drives the run, and it
  // cannot evict itself from the cluster it is driving.
  function machineField(spec, values, onChange) {
    const wrapper = document.createElement("label");
    wrapper.className = "field";
    const caption = document.createElement("span");
    caption.textContent = spec.description;
    const select = document.createElement("select");
    const candidates = machineNames().filter((name) => name !== state.thisHost);

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = candidates.length
      ? "Choose a machine"
      : "No other machine in this inventory";
    select.append(placeholder);
    candidates.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.append(option);
    });
    select.disabled = !candidates.length;
    select.addEventListener("change", () => {
      if (select.value) {
        values[spec.name] = select.value;
      } else {
        delete values[spec.name];
      }
      onChange();
    });

    wrapper.append(caption, select);
    return wrapper;
  }

  // What the modal can ask for, and what it cannot. The reboot question has
  // its own checkbox below, so it is not asked twice here.
  function variableFields(entry, values, onChange) {
    const container = element("confirm-variables");
    container.replaceChildren();
    const asked = entry.variables.filter(
      (spec) => spec.name !== entry.reboot_variable
    );
    asked.forEach((spec) => {
      if (spec.type === "machine") {
        container.append(machineField(spec, values, onChange));
      } else {
        // An entry declaring a kind of variable this page has no field for.
        // Said out loud, because the alternative is an Apply that comes back
        // with a 400 nobody can act on.
        const line = document.createElement("p");
        line.className = "warning";
        line.textContent =
          spec.name + " cannot be filled in from this page yet.";
        container.append(line);
      }
    });
    container.hidden = !asked.length;
    return asked;
  }

  // The single most dangerous button in the product. It asks once, in a modal
  // that says what the run will disturb and which machines it will play, and
  // that is where the friction stops. Typing the machine's name back as a
  // confirmation was also asked for here at first, and an operator who applies
  // twenty times a day types it twenty times without reading the sentence
  // above it, which buys nothing. The picker below stays because the playbook
  // has no other way of knowing which machine is leaving.
  function confirmRun(entry, check) {
    const modal = element("confirm");
    const go = element("confirm-go");
    const reboot = element("confirm-reboot");

    element("confirm-title").textContent =
      (check ? "Preview " : "Apply ") + entry.title.toLowerCase();
    element("confirm-disruption").textContent = check
      ? "Check mode changes nothing. " +
        (entry.preview === "full"
          ? "This playbook writes through modules check mode understands, so " +
            "what it reports is what an apply would change."
          : "Part of this playbook is command driven, and check mode skips " +
            "those tasks. Read the result as an indication, not as a " +
            "guarantee.")
      : entry.disruption +
        " Machines played: " +
        machineNames().join(", ") +
        ".";
    element("confirm-error").hidden = true;
    go.textContent = check ? "Preview" : "Apply";

    // A required variable with nothing chosen keeps the button down. The
    // alternative is an Apply that travels to the API to be told what the
    // page already knew.
    const values = {};
    const asked = variableFields(entry, values, () => {
      go.disabled = !satisfied(asked, values);
    });
    go.disabled = !satisfied(asked, values);

    let skipReboot = false;
    reboot.hidden = check || entry.reboots === "no";
    if (!reboot.hidden) {
      reboot.replaceChildren();
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.addEventListener("change", () => {
        skipReboot = box.checked;
      });
      label.append(
        box,
        document.createTextNode(
          entry.reboots === "gated"
            ? " Converge without rebooting. The configuration is not fully " +
              "applied until a reboot happens."
            : " This playbook reboots every machine it plays, and cannot " +
              "be told not to."
        )
      );
      if (entry.reboots !== "gated") {
        box.disabled = true;
      }
      reboot.append(label);
    }

    go.onclick = async () => {
      go.disabled = true;
      try {
        const variables = { ...values };
        if (skipReboot && entry.reboot_variable) {
          variables[entry.reboot_variable] = true;
        }
        const started = await API.post("/runs", {
          playbook: entry.id,
          check,
          variables,
        });
        window.location.assign("/runs?run=" + encodeURIComponent(started.run_id));
      } catch (failure) {
        const error = element("confirm-error");
        error.textContent = failure.message;
        error.hidden = false;
        go.disabled = false;
      }
    };
    modal.hidden = false;
  }

  function satisfied(asked, values) {
    return asked.every((spec) => !spec.required || values[spec.name]);
  }

  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  async function loadInventoryHosts() {
    const payload = await API.get("/inventory");
    state.inventory = payload.inventory;
    state.thisHost = payload.this_host;
    const lead = element("apply-lead");
    if (!payload.inventory) {
      lead.textContent =
        "There is no inventory to apply yet. Fill it in on the Inventory page.";
      return;
    }
    // Said plainly, because it is what surprises people: there is no --limit,
    // so a run plays every machine the file declares.
    lead.textContent =
      "A run plays every machine the inventory declares, which is " +
      Object.keys(payload.inventory.hosts).join(", ") +
      ". It runs from this node, so this node needs a way to reach the others." +
      (Chrome.isAdmin(state.me) ? "" : " Applying requires the admin role.");
  }

  async function refresh() {
    // The inventory first: the machines it declares are what the panel at the
    // bottom is measured against, and what the confirmation names.
    await loadInventoryHosts();
    await Promise.all([loadSiteKey(), loadHostKeys()]);
    await loadPlaybooks();
  }

  async function start() {
    try {
      const chrome = await Chrome.load();
      state.me = chrome.me;
      state.node = chrome.node;
      await refresh();
    } catch (failure) {
      // Every card on this page is built from an API answer, so one call that
      // fails leaves empty boxes and no explanation, which reads as a service
      // with nothing to run. Say what failed, above them.
      showBanner([failure.message]);
    }
  }

  start();
})();
