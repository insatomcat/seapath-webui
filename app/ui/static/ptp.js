// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The PTP wizard of the inventory page.
//
// Four questions and a diff: where the setup is written, the port on each
// machine, the VLAN, then what the commit changes. The service works out the
// variables and where they go (`POST /inventory/ptp/preview`), so the page
// only collects answers and shows what it was told.
//
// The page that holds it says two things through `attach`: whether the editor
// has unsaved changes to the inventory, which a commit would land under, and
// what to do once the commit is made.

const PtpWizard = (function () {
  const STEPS = 4;
  // Groups a PTP setup usually belongs on, offered first.
  const PREFERRED = ["hypervisors", "cluster_machines", "standalone_machine"];

  const state = {
    step: 0,
    survey: null,
    ports: {},
    interfaces: {},
    hooks: { dirty: () => false, committed: async () => {}, admin: () => false },
    commit: null,
  };

  function element(id) {
    return document.getElementById(id);
  }

  function showError(message, detail) {
    const target = element("ptp-error");
    target.textContent = message || "";
    target.hidden = !message;
    const list = element("ptp-findings");
    list.replaceChildren();
    const lines = [];
    ((detail && detail.divergences) || []).forEach((item) => lines.push(item.message));
    ((detail && detail.findings) || []).forEach((finding) =>
      lines.push(
        finding.level.toUpperCase() +
          (finding.host ? " on " + finding.host : "") +
          ": " +
          finding.message
      )
    );
    lines.forEach((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      list.append(item);
    });
    list.hidden = lines.length === 0;
  }

  function where() {
    return document.querySelector('input[name="ptp-where"]:checked').value;
  }

  function groupHosts() {
    const name = element("ptp-group").value;
    const group = state.survey.groups.find((candidate) => candidate.name === name);
    return group ? group.hosts : [];
  }

  function chosenHosts() {
    if (where() === "group") {
      return groupHosts();
    }
    return Array.from(
      element("ptp-hosts").querySelectorAll("input[type=checkbox]:checked")
    ).map((box) => box.value);
  }

  function machine(host) {
    return state.survey.machines.find((candidate) => candidate.host === host);
  }

  function describe(current) {
    if (!current || !current.interface) {
      return "no PTP";
    }
    let text = current.interface;
    if (current.vlan !== null && current.vlan !== undefined) {
      text += ", VLAN " + current.vlan;
      if (!current.vlan_entries) {
        text += " (no VLAN device declared)";
      }
    }
    return text + " (on " + current.interface_on + ")";
  }

  // The line on the page: what the machines receive now.
  function summary(survey) {
    const machines = survey.machines || [];
    if (!machines.length) {
      return "No machine in the inventory";
    }
    const set = machines.filter((current) => current.interface);
    if (!set.length) {
      return "Not set up";
    }
    const shapes = new Set(
      set.map((current) => current.interface + "/" + (current.vlan ?? ""))
    );
    const first = set[0];
    const text =
      shapes.size === 1
        ? first.interface + (first.vlan != null ? ", VLAN " + first.vlan : ", untagged")
        : "different ports";
    return text + " on " + set.length + " of " + machines.length + " machines";
  }

  // Step 1: where

  function renderWhere() {
    const select = element("ptp-group");
    select.replaceChildren();
    const groups = state.survey.groups.slice().sort((a, b) => {
      const rank = (name) => {
        const at = PREFERRED.indexOf(name);
        return at === -1 ? PREFERRED.length : at;
      };
      return rank(a.name) - rank(b.name) || a.name.localeCompare(b.name);
    });
    groups.forEach((group) => {
      const option = document.createElement("option");
      option.value = group.name;
      option.textContent = group.name + " (" + group.hosts.join(", ") + ")";
      select.append(option);
    });
    const noGroup = groups.length === 0;
    document.querySelector('input[name="ptp-where"][value="group"]').disabled = noGroup;
    if (noGroup) {
      document.querySelector('input[name="ptp-where"][value="hosts"]').checked = true;
    }

    const hosts = element("ptp-hosts");
    hosts.replaceChildren();
    state.survey.machines.forEach((current) => {
      const label = document.createElement("label");
      label.className = "switch";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = current.host;
      box.checked = true;
      label.append(box, " " + current.host + " ");
      const now = document.createElement("span");
      now.className = "help";
      now.textContent = describe(current);
      label.append(now);
      hosts.append(label);
    });
    toggleWhere();
  }

  function toggleWhere() {
    const onGroup = where() === "group";
    element("ptp-group-block").hidden = !onGroup;
    element("ptp-hosts").hidden = onGroup;
    element("ptp-group-help").textContent = onGroup
      ? "The VLAN and the device it needs are written on the group. The port " +
        "is too when every machine uses the same one, and on each machine " +
        "otherwise."
      : "";
  }

  // Step 2: ports

  function renderPorts() {
    const hosts = chosenHosts();
    const body = document.querySelector("#ptp-ports tbody");
    body.replaceChildren();
    const everywhere = new Set();
    hosts.forEach((host) => {
      const current = machine(host);
      const row = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = host;
      const cell = document.createElement("td");
      const input = document.createElement("input");
      input.type = "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.dataset.host = host;
      input.value =
        state.interfaces[host] || (current && current.interface) || "";
      input.addEventListener("input", () => {
        state.interfaces[host] = input.value.trim();
      });
      const ports = state.ports[host] || [];
      if (ports.length) {
        const list = document.createElement("datalist");
        list.id = "ptp-ports-" + host;
        ports.forEach((port) => {
          const option = document.createElement("option");
          option.value = port;
          list.append(option);
          everywhere.add(port);
        });
        input.setAttribute("list", list.id);
        cell.append(list);
      }
      cell.prepend(input);
      state.interfaces[host] = input.value.trim();
      const now = document.createElement("td");
      now.className = "help";
      now.textContent = describe(current);
      row.append(name, cell, now);
      body.append(row);
    });
    const shared = element("ptp-all-ports");
    shared.replaceChildren();
    everywhere.forEach((port) => {
      const option = document.createElement("option");
      option.value = port;
      shared.append(option);
    });
  }

  element("ptp-all-go").addEventListener("click", () => {
    const value = element("ptp-all").value.trim();
    if (!value) {
      return;
    }
    document.querySelectorAll("#ptp-ports input[data-host]").forEach((input) => {
      input.value = value;
      state.interfaces[input.dataset.host] = value;
    });
  });

  // The physical ports each machine's exporter reports, as suggestions. The
  // answer is only ever a help: a machine whose exporter is down gets a field
  // with nothing under it, and the operator types the name.
  async function loadPorts() {
    try {
      const usage = await API.get("/usage");
      (usage.machines || []).forEach((entry) => {
        const node = entry.latest && entry.latest.node;
        state.ports[entry.host] = ((node && node.interfaces) || [])
          .filter((item) => item.kind === "physical")
          .map((item) => item.device);
      });
    } catch (failure) {
      state.ports = {};
    }
  }

  // Step 3: VLAN

  function renderVlan() {
    const hosts = chosenHosts();
    const known = hosts
      .map((host) => machine(host))
      .find((current) => current && current.vlan != null);
    if (element("ptp-vlan").value === "" && known) {
      element("ptp-tagged").checked = true;
      element("ptp-vlan").value = known.vlan;
    }
    toggleVlan();
  }

  function toggleVlan() {
    const tagged = element("ptp-tagged").checked;
    element("ptp-vlan-block").hidden = !tagged;
    element("ptp-untagged-help").hidden = tagged;
  }

  element("ptp-tagged").addEventListener("change", toggleVlan);

  // Step 4: review

  function setup() {
    const hosts = chosenHosts();
    const interfaces = {};
    hosts.forEach((host) => {
      interfaces[host] = state.interfaces[host] || "";
    });
    const tagged = element("ptp-tagged").checked;
    return {
      group: where() === "group" ? element("ptp-group").value : null,
      interfaces,
      vlan: tagged ? Number(element("ptp-vlan").value) : null,
    };
  }

  async function preview() {
    const diff = element("ptp-diff");
    diff.hidden = true;
    element("ptp-diff-loading").hidden = false;
    element("ptp-commit").disabled = true;
    try {
      const response = await fetch("api/v1/inventory/ptp/preview", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "text/x-diff, application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": API.csrfToken(),
        },
        body: JSON.stringify(setup()),
      });
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch (error) {
          payload = null;
        }
        const detail = (payload && payload.error) || {};
        showError(detail.message || "The preview failed.", detail.detail);
        return;
      }
      const text = await response.text();
      diff.textContent = text.trim()
        ? text
        : "Nothing to change: the machines already receive this setup.";
      diff.hidden = false;
      element("ptp-commit").disabled =
        !text.trim() || !state.hooks.admin() || state.hooks.dirty();
    } finally {
      element("ptp-diff-loading").hidden = true;
    }
  }

  // Moving between the steps, each checked before it is left.

  function refuse(step) {
    if (step === 0 && chosenHosts().length === 0) {
      return "Choose at least one machine.";
    }
    if (step === 1) {
      const missing = chosenHosts().filter((host) => !state.interfaces[host]);
      if (missing.length) {
        return "No port for " + missing.join(", ") + ".";
      }
    }
    if (step === 2 && element("ptp-tagged").checked) {
      const vlan = Number(element("ptp-vlan").value);
      if (!Number.isInteger(vlan) || vlan < 1 || vlan > 4094) {
        return "A VLAN ID is a whole number from 1 to 4094.";
      }
    }
    return null;
  }

  function show(step) {
    state.step = step;
    document.querySelectorAll("#ptp-modal .wizard-page").forEach((page) => {
      page.hidden = Number(page.dataset.page) !== step;
    });
    document.querySelectorAll("#ptp-steps li").forEach((item) => {
      const at = Number(item.dataset.step);
      item.className = at === step ? "doing" : at < step ? "done" : "";
    });
    element("ptp-back").disabled = step === 0;
    element("ptp-next").hidden = step === STEPS - 1;
    element("ptp-commit").hidden = step !== STEPS - 1;
    showError("");
    if (step === 1) {
      renderPorts();
    } else if (step === 2) {
      renderVlan();
    } else if (step === 3) {
      preview();
    }
  }

  element("ptp-next").addEventListener("click", () => {
    const refusal = refuse(state.step);
    if (refusal) {
      showError(refusal);
      return;
    }
    show(state.step + 1);
  });

  element("ptp-back").addEventListener("click", () => {
    if (state.step > 0) {
      show(state.step - 1);
    }
  });

  document.querySelectorAll('input[name="ptp-where"]').forEach((radio) =>
    radio.addEventListener("change", toggleWhere)
  );

  element("ptp-commit").addEventListener("click", async () => {
    const button = element("ptp-commit");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    showError("");
    try {
      const committed = await API.post(
        "/inventory/ptp",
        setup(),
        state.commit ? { "If-Match": state.commit } : undefined
      );
      element("ptp-modal").hidden = true;
      await state.hooks.committed(committed);
    } catch (failure) {
      showError(failure.message, failure.detail);
      button.disabled = false;
    } finally {
      button.removeAttribute("aria-busy");
    }
  });

  element("ptp-cancel").addEventListener("click", () => {
    element("ptp-modal").hidden = true;
  });

  element("ptp-open").addEventListener("click", async () => {
    state.interfaces = {};
    element("ptp-vlan").value = "";
    element("ptp-tagged").checked = false;
    element("ptp-all").value = "";
    element("ptp-dirty").hidden = !state.hooks.dirty();
    element("ptp-modal").hidden = false;
    try {
      await refresh();
    } catch (failure) {
      showError(failure.message);
      return;
    }
    renderWhere();
    show(0);
    loadPorts();
  });

  async function refresh() {
    const [survey, inventory] = await Promise.all([
      API.get("/inventory/ptp"),
      API.get("/inventory"),
    ]);
    state.survey = survey;
    state.commit = inventory.commit;
    paint(survey);
    return survey;
  }

  function paint(survey) {
    element("ptp-state").textContent = summary(survey);
  }

  function attach(hooks) {
    Object.assign(state.hooks, hooks);
  }

  return { attach, refresh };
})();
