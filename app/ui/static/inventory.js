// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The inventory page: the desired state, and the two ways to change it.
//
// The guided form knows what a text area cannot: which NICs this machine
// actually has, which disks by their stable path, how many CPUs there are to
// isolate, and how to turn a typed password into the PBKDF2 hash GRUB wants.
// The file below it is the escape hatch, for the fifty variables no form
// models. Both write to the same git repository and leave the same audit
// trail.

(function () {
  const state = {
    inventory: null,
    commit: null,
    host: null,
    me: null,
    me_host: null,
    hostKeys: [],
  };

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

  // The form edits one machine at a time and any machine in the inventory. A
  // three node cluster is configured from one browser, and defaulting to this
  // node is a convenience rather than a limit.
  function fillHosts(names) {
    const select = element("host-select");
    select.replaceChildren();
    names.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name === state.me_host ? name + " (this machine)" : name;
      option.selected = name === state.host;
      select.append(option);
    });
    select.disabled = names.length < 2;
    element("host-select-help").textContent =
      state.host === state.me_host
        ? "The entry that describes the machine serving this page."
        : "Editing another machine's entry. Applying still runs from here.";
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
    // Which entry describes this machine. The host key is frequently not the
    // machine's name: a site can key an inventory node1, node2, node3 and
    // carry the real names in `hostname`. The server works it out.
    state.me_host = payload.this_host;
    const names = Object.keys(payload.inventory.hosts);
    if (!state.host || !names.includes(state.host)) {
      state.host = payload.this_host || names[0];
    }
    fillHosts(names);

    const pairs = [
      ["Machine", state.host],
      ["Inventory commit", (state.commit || "none").slice(0, 12)],
      ["Mode", payload.inventory.mode],
      ["Machines", Object.keys(payload.inventory.hosts).join(", ")],
      ["Written", payload.adopted ? "elsewhere, edited in place here" : "here"],
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
    // Warnings are advice, not refusals. They are the difference between "this
    // is wrong" and "this is unusual, and you may have meant it".
    showBanner(
      (payload.validation.findings || [])
        .filter((finding) => finding.level === "warning")
        .map((finding) => finding.message)
    );
    return true;
  }

  // Bringing an inventory in. The file is read in the browser and posted as
  // text: what a site owns is a file, and anything this service cannot express
  // in its model has to survive the trip.
  const importFile = element("import-file");

  const importGo = element("import-go");

  importFile.addEventListener("change", () => {
    importGo.disabled = !importFile.files.length;
    element("import-error").hidden = true;
  });

  importGo.addEventListener("click", async () => {
    const error = element("import-error");
    error.hidden = true;
    const file = importFile.files[0];
    if (!file) {
      return;
    }
    importGo.disabled = true;
    try {
      const imported = await API.post("/inventory/import", {
        document: await file.text(),
      });
      element("import-details").open = false;
      importFile.value = "";
      await refresh();
      showBanner([
        "Imported " + imported.hosts.join(", ") + ". Nothing has been applied " +
          "to any machine: that is the Apply section below.",
      ]);
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      importGo.disabled = false;
    }
  });

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

  // The folder around the inventory. An inventory is rarely alone: a dozen
  // roles take a path to a file this machine has to hold, and a run mounts
  // this folder where a checkout of seapath-ansible would be.

  function humanSize(bytes) {
    const units = ["B", "kB", "MB", "GB", "TB"];
    let size = bytes;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return (unit === 0 ? size : size.toFixed(1)) + " " + units[unit];
  }

  function fileRow(entry, store) {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    const link = document.createElement("a");
    link.href = "/api/v1/inventory/" + store + "/" + entry.path;
    link.textContent = entry.path;
    name.append(link);
    row.append(name);

    const size = document.createElement("td");
    size.textContent = humanSize(entry.size);
    row.append(size);

    const actions = document.createElement("td");
    if (Chrome.isAdmin(state.me)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = "remove";
      button.addEventListener("click", async () => {
        try {
          await API.del("/inventory/" + store + "/" + entry.path);
        } catch (failure) {
          showBanner([failure.message]);
        }
        await loadFolder();
        await loadInventory();
      });
      actions.append(button);
    }
    row.append(actions);
    return row;
  }

  function fillTable(id, entries, store) {
    const body = document.querySelector("#" + id + " tbody");
    body.replaceChildren();
    entries.forEach((entry) => body.append(fileRow(entry, store)));
    if (!entries.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 3;
      cell.className = "help";
      cell.textContent = "Nothing here yet.";
      row.append(cell);
      body.append(row);
    }
  }

  function referenceRow(reference) {
    const row = document.createElement("tr");
    [reference.host, reference.variable, reference.value].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    });

    const status = document.createElement("td");
    if (reference.found) {
      status.textContent = reference.where || "resolved at run time";
    } else if (reference.expected) {
      // The answer an operator can act on, rather than the fact that something
      // is wrong: the name to upload the file under, ready to use.
      status.className = "missing";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = "missing, upload as " + reference.expected;
      button.addEventListener("click", () => {
        element("file-name").value = reference.expected;
        element("file-input").focus();
      });
      status.append(button);
    } else {
      status.className = "missing";
      status.textContent = "points above the folder, no run can reach it";
    }
    row.append(status);
    return row;
  }

  async function loadFolder() {
    const [folder, refs] = await Promise.all([
      API.get("/inventory/folder"),
      API.get("/inventory/references"),
    ]);

    fillTable("files-table", folder.files, "files");
    fillTable("artefacts-table", folder.artefacts, "artefacts");

    const body = document.querySelector("#references-table tbody");
    body.replaceChildren();
    refs.forEach((reference) => body.append(referenceRow(reference)));
    element("references-empty").hidden = refs.length > 0;
    document.querySelector("#references-table").hidden = refs.length === 0;

    element("artefacts-help").dataset.free = folder.free_bytes || "";
    state.maxFileBytes = folder.max_file_bytes;
  }

  function uploadHandler(store, nameId, inputId, errorId, buttonId) {
    const name = element(nameId);
    const input = element(inputId);
    const error = element(errorId);
    const button = element(buttonId);

    function ready() {
      // One file is stored under the name typed; several are stored under
      // their own names, in the directory typed, which may be the root. A
      // folder arrives in one act rather than in five.
      button.disabled = !(input.files.length && (name.value.trim() || multiple()));
    }

    function multiple() {
      return input.files.length > 1;
    }

    function pathOf(file) {
      const prefix = name.value.trim().replace(/\/+$/, "");
      if (!multiple()) {
        return prefix || file.name;
      }
      // `webkitRelativePath` is set when a directory was picked, and keeping
      // it means the folder arrives with the shape the inventory references.
      const own = file.webkitRelativePath || file.name;
      return prefix ? prefix + "/" + own : own;
    }

    input.addEventListener("change", () => {
      if (!name.value.trim() && input.files.length === 1) {
        name.value = input.files[0].name;
      }
      ready();
    });
    name.addEventListener("input", ready);

    button.addEventListener("click", async () => {
      error.hidden = true;
      const files = Array.from(input.files);
      const label = button.textContent;
      button.disabled = true;
      const stored = [];
      try {
        for (const file of files) {
          const path = pathOf(file);
          button.textContent =
            "Uploading " + path + " (" + humanSize(file.size) + ")...";
          await API.upload("/inventory/" + store + "/" + path, file);
          stored.push(path);
        }
        name.value = "";
        input.value = "";
        showBanner([
          stored.join(", ") +
            (stored.length > 1 ? " are stored. " : " is stored. ") +
            "Nothing has been pushed to any machine: that is the Apply section.",
        ]);
      } catch (failure) {
        // Each file is its own commit, so the ones already stored stay. The
        // message names the one that stopped it.
        error.textContent = failure.message;
        error.hidden = false;
      } finally {
        button.textContent = label;
        await loadFolder();
        await loadInventory();
        ready();
      }
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

  element("host-select").addEventListener("change", async (event) => {
    state.host = event.target.value;
    element("diff-card").hidden = true;
    await loadInventory();
  });

  // The file itself. Everything the form does not model lives here, and an
  // operator who has to leave the browser to change one line has been given a
  // form rather than an editor.
  async function loadRaw() {
    const response = await fetch("/api/v1/inventory/raw", {
      credentials: "same-origin",
    });
    element("raw-editor").value = await response.text();
    element("raw-error").hidden = true;
    element("raw-findings").hidden = true;
  }

  function showFindings(findings) {
    const list = element("raw-findings");
    list.replaceChildren();
    if (!findings || !findings.length) {
      list.hidden = true;
      return;
    }
    findings.forEach((finding) => {
      const item = document.createElement("li");
      item.textContent =
        finding.level.toUpperCase() +
        " " +
        finding.rule +
        (finding.host ? " on " + finding.host : "") +
        ": " +
        finding.message;
      list.append(item);
    });
    list.hidden = false;
  }

  element("raw-details").addEventListener("toggle", (event) => {
    if (event.target.open && !element("raw-editor").value) {
      loadRaw();
    }
  });

  element("raw-reload").addEventListener("click", loadRaw);

  element("raw-check").addEventListener("click", async () => {
    const error = element("raw-error");
    error.hidden = true;
    try {
      const result = await API.post("/inventory/raw/check", {
        document: element("raw-editor").value,
      });
      showFindings(result.findings);
      if (!result.findings.length) {
        showBanner(["The file is valid, and Ansible parses it. Nothing saved."]);
      }
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    }
  });

  element("raw-save").addEventListener("click", async () => {
    const error = element("raw-error");
    error.hidden = true;
    try {
      await API.put("/inventory/raw", { document: element("raw-editor").value });
      showFindings([]);
      await refresh();
      await loadRaw();
      showBanner([
        "The file is saved and committed. Nothing has been applied to any " +
          "machine: that is the Apply section below.",
      ]);
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      showFindings(
        (failure.detail && failure.detail.findings) || []
      );
    }
  });

  element("preview").addEventListener("click", preview);

  element("node-form").addEventListener("submit", save);

  async function refresh() {
    const loaded = await loadInventory();
    await loadHistory();
    await loadFolder();
    return loaded;
  }

  uploadHandler("files", "file-name", "file-input", "file-error", "file-upload");
  uploadHandler(
    "artefacts",
    "artefact-name",
    "artefact-input",
    "artefact-error",
    "artefact-upload"
  );

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
      // The picker stays live: reading another machine's entry is a viewer's
      // business too.
      element("host-select").disabled = false;
      // The folder is read by a viewer and written by an administrator, like
      // everything else on this page. The tables and their download links stay.
      element("folder-card")
        .querySelectorAll("input, button")
        .forEach((control) => {
          control.disabled = true;
        });
      showBanner(["Changing the inventory requires the admin role."]);
    }
  }

  start();
})();
