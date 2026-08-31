// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The configuration page. It edits the inventory and launches playbooks, and
// it never pretends to do anything else: "save" makes a commit, "apply" starts
// a run. The two are separate because deciding what a machine should be and
// making it so are separate decisions.

(function () {
  const state = { inventory: null, commit: null, host: null, me: null };

  const fields = [
    "ansible_host",
    "network_interface",
    "subnet",
    "gateway_addr",
    "dns_servers",
    "ptp_interface",
    "ptp_domain_number",
    "ntp_servers",
    "admin_user",
    "isolcpus",
  ];
  const listFields = new Set(["dns_servers", "ntp_servers"]);
  const numberFields = new Set(["subnet", "ptp_domain_number"]);

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

  function fillInterfaces(select, interfaces, allowEmpty) {
    select.replaceChildren();
    if (allowEmpty) {
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "none";
      select.append(none);
    }
    interfaces.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.name;
      const notes = [];
      if (item.carries_default_route) notes.push("default route");
      if (item.operstate) notes.push(item.operstate);
      if (item.speed_mbps) notes.push(item.speed_mbps + " Mb/s");
      option.textContent =
        item.name + (notes.length ? "  (" + notes.join(", ") + ")" : "");
      select.append(option);
    });
  }

  function readForm() {
    const node = Object.assign({}, state.inventory.hosts[state.host]);
    fields.forEach((name) => {
      const raw = element(name).value.trim();
      if (listFields.has(name)) {
        node[name] = raw ? raw.split(",").map((item) => item.trim()) : [];
      } else if (numberFields.has(name)) {
        node[name] = raw === "" ? null : Number(raw);
      } else {
        node[name] = raw === "" ? null : raw;
      }
    });
    // subnet is not optional: the renderer needs a prefix length.
    if (node.subnet === null) {
      node.subnet = 24;
    }
    const candidate = JSON.parse(JSON.stringify(state.inventory));
    candidate.hosts[state.host] = node;
    return candidate;
  }

  function writeForm(node) {
    fields.forEach((name) => {
      const value = node[name];
      element(name).value =
        value === null || value === undefined
          ? ""
          : Array.isArray(value)
            ? value.join(", ")
            : value;
    });
  }

  async function loadInventory() {
    const payload = await API.get("/inventory");
    if (payload.parse_error) {
      showBanner([
        "The inventory file could not be read: " + payload.parse_error,
      ]);
      return false;
    }
    if (!payload.seeded || !payload.inventory) {
      showBanner([
        "This machine has no inventory yet, and could not describe itself. " +
          "Fill in the form from scratch.",
      ]);
      return false;
    }
    state.inventory = payload.inventory;
    state.commit = payload.commit;
    state.host = Object.keys(payload.inventory.hosts)[0];

    const pairs = [
      ["Machine", state.host],
      ["Inventory commit", (state.commit || "none").slice(0, 12)],
      ["Mode", payload.inventory.mode],
    ];
    const summary = element("repo-summary");
    summary.replaceChildren();
    pairs.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = value;
      summary.append(term, definition);
    });

    writeForm(payload.inventory.hosts[state.host]);
    if (showReadOnly(payload)) {
      // The validation warnings describe a desired state this page cannot
      // change. Showing them next to a locked form would read as a list of
      // repairs to make here, and there are none to make here.
      showBanner([]);
      return true;
    }
    // Warnings are advice, not refusals. They are the difference between "this
    // is wrong" and "this is unusual, and you may have meant it".
    showBanner(
      (payload.validation.findings || [])
        .filter((finding) => finding.level === "warning")
        .map((finding) => finding.message)
    );
    return true;
  }

  // The service refuses to rewrite a file it cannot reproduce, and the page
  // says so where the operator is about to type: the list of what a save would
  // have changed, and a form that cannot be typed into. Controls are only ever
  // disabled here, never re-enabled, so this cannot undo the lock a viewer
  // account is already under.
  function showReadOnly(payload) {
    const card = element("readonly-card");
    if (payload.writable !== false) {
      card.hidden = true;
      return false;
    }

    element("readonly-reason").textContent = payload.read_only_reason || "";
    const list = element("readonly-list");
    list.replaceChildren();
    (payload.divergences || []).forEach((divergence) => {
      const item = document.createElement("li");
      item.textContent = divergence.message;
      list.append(item);
    });
    card.hidden = false;

    element("node-form")
      .querySelectorAll("input, select, button")
      .forEach((control) => {
        control.disabled = true;
      });
    return true;
  }

  async function loadDiscovery() {
    const discovery = await API.get("/inventory/discovery");
    fillInterfaces(element("network_interface"), discovery.interfaces, false);
    fillInterfaces(element("ptp_interface"), discovery.interfaces, true);
    element("isolcpus-help").textContent =
      "This machine reports " +
      (discovery.cpu_count || "an unknown number of") +
      " CPUs" +
      (discovery.isolated_now.length
        ? ", currently isolating " + discovery.isolated_now.join(",")
        : ", none of them isolated") +
      ". CPU 0 can never be isolated.";
    return discovery;
  }

  async function loadHistory() {
    const history = await API.get("/inventory/history?limit=20");
    const body = document.querySelector("#history-table tbody");
    body.replaceChildren();
    history.forEach((commit) => {
      const row = document.createElement("tr");
      [
        new Date(commit.timestamp).toLocaleString(),
        commit.author,
        commit.message,
      ].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      const actions = document.createElement("td");
      if (Chrome.isAdmin(state.me)) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "revert";
        button.addEventListener("click", () => revert(commit));
        actions.append(button);
      }
      row.append(actions);
      body.append(row);
    });
  }

  async function revert(commit) {
    element("form-error").hidden = true;
    try {
      await API.post("/inventory/revert/" + commit.hash);
      // A revert is a commit, not an apply. The machine is unchanged until a
      // run happens, and the page says so by refreshing the pending diff.
      await refresh();
      showBanner([
        "Reverted " +
          commit.message +
          ". The machine is unchanged until you apply.",
      ]);
    } catch (failure) {
      showError(failure.message);
    }
  }

  function showError(message) {
    const error = element("form-error");
    error.textContent = message;
    error.hidden = false;
  }

  async function preview() {
    element("form-error").hidden = true;
    try {
      const diff = await fetch("/api/v1/inventory/preview", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": API.csrfToken(),
        },
        credentials: "same-origin",
        body: JSON.stringify({ inventory: readForm() }),
      }).then((response) => response.text());
      element("diff").textContent = diff.trim() || "Nothing would change.";
      element("diff-card").hidden = false;
    } catch (failure) {
      showError(failure.message);
    }
  }

  async function save(event) {
    event.preventDefault();
    element("form-error").hidden = true;
    const changes = {};
    const candidate = readForm().hosts[state.host];
    const current = state.inventory.hosts[state.host];
    fields.forEach((name) => {
      if (JSON.stringify(candidate[name]) !== JSON.stringify(current[name])) {
        changes[name] = candidate[name];
      }
    });
    const password = element("grub_password_plain").value;

    if (!Object.keys(changes).length && !password) {
      showError("Nothing changed.");
      return;
    }

    try {
      const body = { changes };
      if (password) {
        body.grub_password_plain = password;
      }
      const response = await fetch(
        "/api/v1/inventory/hosts/" + encodeURIComponent(state.host),
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": API.csrfToken(),
            "If-Match": state.commit || "",
          },
          credentials: "same-origin",
          body: JSON.stringify(body),
        }
      );
      const payload = await response.json();
      if (!response.ok) {
        const detail = payload.error || {};
        showError(detail.message || "The change was refused.");
        return;
      }
      element("grub_password_plain").value = "";
      element("diff-card").hidden = true;
      await refresh();
      showBanner([
        "Committed as " +
          (payload.commit || "").slice(0, 12) +
          ". The machine is unchanged until you apply.",
      ]);
    } catch (failure) {
      showError(failure.message);
    }
  }

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
      : entry.disruption + " Impacted machine: " + state.host + ".";
    element("confirm-name").textContent = state.host;
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
      go.disabled = input.value !== state.host;
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

  async function refresh() {
    const loaded = await loadInventory();
    await Promise.all([loadHistory(), loadPlaybooks()]);
    return loaded;
  }

  async function start() {
    const chrome = await Chrome.load();
    state.me = chrome.me;
    await loadDiscovery();
    await refresh();

    if (!Chrome.isAdmin(state.me)) {
      element("node-form")
        .querySelectorAll("input, select, button")
        .forEach((control) => {
          control.disabled = true;
        });
      showBanner(["Changing the inventory requires the admin role."]);
    }
  }

  element("preview").addEventListener("click", preview);
  element("node-form").addEventListener("submit", save);
  start();
})();
