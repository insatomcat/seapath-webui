// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The system page: what turns a desired state into a configured machine.
//
// Two credentials and a button. The credentials are what lets this node reach
// the others at all, and the button runs an upstream playbook against the
// inventory. Nothing here edits the desired state, and nothing here changes a
// machine except through Ansible.

(function () {
  const state = { me: null, node: null, inventory: null, hostKeys: [] };

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
    element("apply-lead").textContent = Chrome.isAdmin(state.me)
      ? "Applying runs an upstream SEAPATH playbook against this machine. " +
        "This is the part that changes it."
      : "Applying requires the admin role.";

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

      const detail = document.createElement("p");
      detail.className = "help";
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

      row.append(title, detail, actions);
      container.append(row);
    });
  }

  function machineNames() {
    return state.inventory ? Object.keys(state.inventory.hosts) : [];
  }

  // The single most dangerous button in the product, and it has to look like
  // it: the operator types the machine's name before anything happens.
  function confirmRun(entry, check) {
    const modal = element("confirm");
    const input = element("confirm-input");
    const go = element("confirm-go");
    const reboot = element("confirm-reboot");

    element("confirm-title").textContent =
      (check ? "Preview " : "Apply ") + entry.title.toLowerCase();
    element("confirm-disruption").textContent = check
      ? "Check mode changes nothing. This playbook is " +
        entry.preview +
        "ly previewable, so treat the result as an indication and not as a " +
        "guarantee."
      : entry.disruption +
        " Machines played: " +
        machineNames().join(", ") +
        ".";
    // Typed to confirm: this machine's name, because this is the one the
    // operator is sitting in front of and the one a mistake reboots first.
    element("confirm-name").textContent = state.node.hostname;
    element("confirm-error").hidden = true;
    input.value = "";
    go.disabled = true;
    go.textContent = check ? "Preview" : "Apply";

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
            : " This playbook reboots the machine and cannot be told not to."
        )
      );
      if (entry.reboots !== "gated") {
        box.disabled = true;
      }
      reboot.append(label);
    }

    input.oninput = () => {
      go.disabled = input.value !== state.node.hostname;
    };
    go.onclick = async () => {
      go.disabled = true;
      try {
        const variables = {};
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

  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  async function loadInventoryHosts() {
    const payload = await API.get("/inventory");
    state.inventory = payload.inventory;
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
      ". It runs from this node, so this node needs a way to reach the others.";
  }

  async function refresh() {
    await Promise.all([loadSiteKey(), loadHostKeys()]);
    await loadPlaybooks();
  }

  async function start() {
    const chrome = await Chrome.load();
    state.me = chrome.me;
    state.node = chrome.node;
    await loadInventoryHosts();
    await refresh();
  }

  start();
})();
