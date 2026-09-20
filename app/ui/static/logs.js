// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The Logs page: every machine's journal over one window, merged.
//
// The controls are the matches first and the pattern last, which is the order
// D63 settled and the order the API enforces. A scope is a set of units or a
// priority, served from the journal's index; a pattern alone would scan every
// entry of a 517 MB journal on a machine whose CPUs belong to its guests. The
// page offers no way to send one.
//
// The units an operator names are a match of the same kind, and they replace
// the scope rather than narrowing it: `journalctl` matches the values of one
// field as OR and two fields against each other as AND, so a scope's units
// beside theirs would ask for the lines that are both.
//
// The whole state of the page is in its query string, so a reading is a link.
// That is what lets the VMs, Cluster and Runs pages send an operator here with
// the question already asked, and what lets one operator paste what they are
// looking at to another.
//
// Nothing reads on a timer. Each reading opens an SSH connection to every
// machine of the inventory, which is a load on the cluster rather than a
// number that moves, so the control is attached with `timer: false` and an
// operator asks when they want to know.

(function () {
  let sources = null;
  // The window a link named outright, rather than one of the relative ones in
  // the control. A run happened between two moments, and a link that sent an
  // operator to "the last fifteen minutes" would answer about now instead.
  let fixed = null;

  function element(id) {
    return document.getElementById(id);
  }

  function showBanner(message) {
    const banner = element("banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  function show(id, on) {
    element(id).hidden = !on;
  }

  // The page's state, read from the URL and written back to it. `history
  // .replaceState` rather than a navigation: the back button belongs to the
  // pages an operator visited, and every reading of this one is the same page.
  function params() {
    return new URLSearchParams(window.location.search);
  }

  function remember(query) {
    const url = window.location.pathname + "?" + query.toString();
    window.history.replaceState(null, "", url);
  }

  // The units the field names, separated by a comma or by spaces. They reach
  // the API as they were typed and are checked there, so a glob like `ceph*`
  // is refused or accepted in one place rather than in two.
  function namedUnits() {
    return element("units")
      .value.split(/[\s,]+/)
      .filter((item) => item.length > 0);
  }

  // The select goes grey while the field holds anything, because what it says
  // is then not what is being asked.
  function scopeFollowsUnits() {
    element("scope").disabled = namedUnits().length > 0;
  }

  function selectedMachines() {
    return Array.from(
      document.querySelectorAll("#machines input[type=checkbox]:checked")
    ).map((box) => box.value);
  }

  // What the controls currently say, as the query the API takes. The machines
  // are sent only when some of them are left out: every machine is the default
  // and saying so in the URL would pin a link to the cluster it was made on.
  function query() {
    const built = new URLSearchParams();
    const units = namedUnits();
    if (units.length) {
      units.forEach((unit) => built.append("unit", unit));
    } else {
      built.set("scope", element("scope").value);
    }
    if (element("window").value === "fixed" && fixed) {
      built.set("since", fixed.since);
      if (fixed.until) {
        built.set("until", fixed.until);
      }
    } else {
      built.set("minutes", element("window").value);
    }
    built.set("lines", element("lines").value);
    const grep = element("grep").value.trim();
    if (grep) {
      built.set("grep", grep);
    }
    const machines = selectedMachines();
    if (machines.length && machines.length < sources.machines.length) {
      machines.forEach((host) => built.append("host", host));
    }
    return built;
  }

  // The same thing as the API's own parameters. `minutes` is this page's, and
  // becomes the `since` the endpoint takes: a window an operator picks from a
  // list is relative to the moment they asked, so the page computes it at the
  // moment it asks rather than pinning it when the page loaded.
  function apiPath(state) {
    const asked = new URLSearchParams();
    const units = state.getAll("unit");
    if (units.length) {
      units.forEach((unit) => asked.append("unit", unit));
    } else {
      asked.set("scope", state.get("scope"));
    }
    asked.set("lines", state.get("lines"));
    if (state.get("since")) {
      asked.set("since", state.get("since"));
      if (state.get("until")) {
        asked.set("until", state.get("until"));
      }
    } else {
      const minutes = Number(state.get("minutes"));
      asked.set("since", new Date(Date.now() - minutes * 60000).toISOString());
    }
    const grep = state.get("grep");
    if (grep) {
      asked.set("grep", grep);
    }
    state.getAll("host").forEach((host) => asked.append("host", host));
    return "/logs?" + asked.toString();
  }

  function option(value, label, selected) {
    const node = document.createElement("option");
    node.value = value;
    node.textContent = label;
    node.selected = selected;
    return node;
  }

  // The controls are built from `/logs/sources` rather than written into the
  // template, so the scopes the service offers and the machines the inventory
  // declares are the ones on screen, with no second list to keep in step.
  function renderControls(state) {
    const scope = element("scope");
    scope.replaceChildren(
      ...sources.scopes.map((item) =>
        option(item.name, item.label, item.name === state.get("scope"))
      )
    );

    const lines = element("lines");
    const wanted = state.get("lines");
    lines.replaceChildren(
      ...[100, 500, 1000, sources.max_lines]
        .filter((value, index, all) => value <= sources.max_lines && all.indexOf(value) === index)
        .map((value) => option(String(value), String(value), String(value) === wanted))
    );

    const window_ = element("window");
    if (fixed) {
      // First in the list and selected, so the window the link named is what
      // the control says. Picking a relative one below leaves it, and it is
      // then gone: there is no way back to a moment that has passed.
      window_.prepend(option("fixed", windowLabel(fixed), true));
    }
    const minutes = state.get("minutes");
    Array.from(window_.options).forEach((item) => {
      item.selected = fixed ? item.value === "fixed" : item.value === minutes;
    });

    element("grep").value = state.get("grep") || "";
    element("units").value = state.getAll("unit").join(", ");
    scopeFollowsUnits();

    // Every machine is ticked when the link named none, which is what "the
    // cluster" means on this page.
    const named = state.getAll("host");
    const machines = element("machines");
    machines.replaceChildren(
      ...sources.machines.map((machine) => {
        const label = document.createElement("label");
        label.className = "checkbox";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = machine.host;
        box.checked = named.length === 0 || named.includes(machine.host);
        label.append(box, document.createTextNode(machine.host));
        return label;
      })
    );
  }

  function windowLabel(window_) {
    const from = new Date(window_.since).toLocaleString();
    const to = window_.until ? new Date(window_.until).toLocaleTimeString() : "now";
    return from + " to " + to;
  }

  // What a link left out. A link from another page names the question and
  // nothing else, so everything else here comes from the service's own
  // defaults rather than from whichever option a select happens to list first.
  function defaults(state) {
    if (!state.get("scope") && state.getAll("unit").length === 0) {
      state.set("scope", sources.scopes[0].name);
    }
    if (!state.get("lines")) {
      state.set("lines", String(sources.default_lines));
    }
    if (state.get("since")) {
      fixed = { since: state.get("since"), until: state.get("until") };
    } else if (!state.get("minutes")) {
      state.set("minutes", String(sources.default_window_minutes));
    }
    return state;
  }

  function stamp(iso) {
    const when = new Date(iso);
    // The operator's own timezone, since they are reading it on their screen,
    // with the milliseconds kept: two lines of one second on two machines are
    // most of what a failover looks like.
    return (
      when.toLocaleTimeString([], { hour12: false }) +
      "." +
      String(when.getMilliseconds()).padStart(3, "0")
    );
  }

  // The unit, or the syslog identifier of a program that runs without one.
  function source(entry) {
    if (entry.unit) {
      return entry.unit.replace(/\.service$/, "");
    }
    return entry.identifier || "";
  }

  // Priorities up to `warning` are drawn as themselves, and the urgent ones
  // are marked. A journal is read for the lines that stand out, and the
  // number is meaningless to anyone who does not carry the syslog table.
  const SEVERITY = { 0: "emerg", 1: "alert", 2: "crit", 3: "err", 4: "warning" };

  function cell(content, className) {
    const node = document.createElement("td");
    if (className) {
      node.className = className;
    }
    if (content instanceof Node) {
      node.append(content);
    } else {
      node.textContent = content;
    }
    return node;
  }

  function row(entry) {
    const line = document.createElement("tr");
    const severity = SEVERITY[entry.priority];
    if (severity) {
      line.className = "severity-" + (entry.priority <= 3 ? "error" : "warning");
    }
    const message = document.createElement("span");
    message.className = "log-message";
    message.textContent = entry.message;
    const last = cell(message, "log-line");
    if (severity) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = severity;
      last.prepend(tag, " ");
    }
    line.append(
      cell(stamp(entry.timestamp)),
      cell(entry.host),
      cell(source(entry)),
      last
    );
    return line;
  }

  function renderEntries(reading) {
    element("rows").replaceChildren(...reading.entries.map(row));
    show("table", reading.entries.length > 0);
    const empty = element("empty");
    empty.textContent = reading.entries.length
      ? ""
      : "No entry matched on the machines that answered.";
    empty.hidden = reading.entries.length > 0;
  }

  // Which machines answered, and what could not be read. A machine that is
  // silent is a finding rather than an error, so it is said here and the lines
  // of the others are drawn all the same.
  function renderNote(reading) {
    const answered = reading.machines.filter((machine) => machine.answered);
    const lead = element("lead");
    lead.textContent =
      answered.length === 0
        ? ""
        : answered.length +
          " of " +
          reading.machines.length +
          " machine(s) answered, " +
          reading.entries.length +
          " entries.";
    const note = element("note");
    note.textContent = reading.warnings.join(" ");
    note.hidden = reading.warnings.length === 0;
  }

  async function read() {
    const state = query();
    remember(state);
    show("loading", true);
    try {
      const reading = await API.get(apiPath(state));
      renderNote(reading);
      renderEntries(reading);
      showBanner("");
    } finally {
      show("loading", false);
    }
  }

  async function start() {
    sources = await API.get("/logs/sources");
    const state = defaults(params());
    renderControls(state);
    if (sources.machines.length === 0) {
      showBanner(
        "This node declares no machine yet. Write an inventory on the " +
          "Inventory page, and its machines will be asked here."
      );
      return;
    }

    element("units").addEventListener("input", scopeFollowsUnits);

    element("window").addEventListener("change", (event) => {
      if (event.target.value !== "fixed") {
        fixed = null;
        const stale = event.target.querySelector('option[value="fixed"]');
        if (stale) {
          stale.remove();
        }
      }
    });

    element("filters").addEventListener("submit", (event) => {
      event.preventDefault();
      read().catch((failure) => showBanner(failure.message));
    });

    // Not on the timer: every reading opens an SSH connection to every machine
    // of the inventory. See `reread.js`.
    Reread.attach(
      element("reread"),
      async () => {
        showBanner("");
        await read();
      },
      (failure) => showBanner(failure.message),
      { timer: false }
    );

    await read();
  }

  start().catch((failure) => showBanner(failure.message));
})();
