// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The system page: what turns a desired state into a configured machine.
//
// Two credentials and a button. The credentials are what lets this node reach
// the others at all, and the button runs an upstream playbook against the
// inventory. Nothing here edits the desired state, and nothing here changes a
// machine except through Ansible.

(function () {
  const state = {
    me: null,
    node: null,
    inventory: null,
    thisHost: null,
    hostKeys: [],
  };

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
    const state = await API.get("/trust/site-key");
    const summary = element("site-key-summary");
    summary.replaceChildren();
    const pairs = state.installed
      ? [["Type", state.key_type], ["Fingerprint", state.fingerprint]]
      : [["Status", "No site key. This node reaches only itself."]];
    pairs.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = value;
      summary.append(term, definition);
    });
    element("site-key-remove").hidden = !state.installed;
    return state;
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
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    }
  });

  element("site-key-remove").addEventListener("click", async () => {
    await API.del("/trust/site-key");
    await loadSiteKey();
  });

  element("host-keys-scan").addEventListener("click", async () => {
    const error = element("host-keys-error");
    error.hidden = true;
    const addresses = Object.values(state.inventory ? state.inventory.hosts : {})
      .map((node) => node.ansible_host)
      .filter(Boolean);
    try {
      mergeHostKeys(await API.post("/trust/host-keys/scan", { addresses }));
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
  async function loadPlaybooks() {
    const catalogue = await API.get("/playbooks");
    const container = element("playbooks");
    container.replaceChildren();

    // When nothing can run, the reason is almost always one reason, and
    // repeating it in small print under nine dimmed rows is how an operator
    // ends up asking why the buttons are greyed out. Say it once, at the top.
    const blocked = element("apply-blocked");
    blocked.textContent = blockingEverything(catalogue).join("  ");
    blocked.hidden = !blocked.textContent;

    catalogue.forEach((item) => {
      const entry = item.entry;
      const row = document.createElement("div");
      row.className = "playbook" + (item.available ? "" : " unavailable");

      const title = document.createElement("div");
      title.className = "playbook-title";
      title.textContent = entry.title;
      if (entry.reboots !== "no") {
        const tag = document.createElement("span");
        tag.className = "tag warn";
        tag.textContent = entry.reboots === "gated" ? "reboots (optional)" : "reboots";
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

      // Which machines the run reaches. `targets` is copied from the
      // playbook's own `hosts:` lines, so the groups are named here exactly as
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
      detail.textContent = item.available
        ? entry.disruption
        : item.unmet.join(" ");

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

      row.append(title, name, scope, detail, actions);
      container.append(row);
    });
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
    await Promise.all([loadSiteKey(), loadHostKeys()]);
    await loadPlaybooks();
    await loadInventoryHosts();
  }

  async function start() {
    const chrome = await Chrome.load();
    state.me = chrome.me;
    state.node = chrome.node;
    await refresh();
  }

  start();
})();
