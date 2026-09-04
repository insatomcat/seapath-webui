// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The real time page: what the tuning came out as, and what the latency
// measured. Two halves of one question, and the page keeps them apart because
// the answers are of different kinds. The conformance half is a reading of
// this machine and costs nothing. The measurement half is an Ansible run
// against every machine of the inventory, and it is confirmed like one.
//
// Nothing here writes to a host. The launch button posts to /runs, which is
// the same path the System page uses, so there is one lock, one history and
// one confirmation across everything that touches a machine.

(function () {
  // Two measurements, kept apart because they answer different questions:
  // cyclictest reports what the scheduler delivered, which the tuning can
  // change, and hwlatdetect what the firmware took without telling the kernel,
  // which no inventory variable reaches.
  const MEASUREMENTS = {
    cyclictest: {
      playbook: "test_run_cyclictest",
      form: "measure-form",
      blocked: "measure-blocked",
      loading: "latency-loading",
      panel: "latency",
      picker: "measure-picker",
      body: "latency-body",
      button: "measure-go",
      results: (item) => item.latency,
      absent:
        "The collection installed on this node has no test_run_cyclictest " +
        "playbook, so the latency cannot be measured from here. Past " +
        "measurements are still listed below.",
      empty:
        "No latency has been measured from this node yet. cyclictest runs on " +
        "the machines over the same SSH path a convergence uses, so nothing " +
        "measures inside this container and nothing here runs at real time " +
        "priority.",
      note: "every machine the run measured, this one first",
      fields: {
        cyclictest_duration: "measure-duration",
        cyclictest_priority: "measure-priority",
        cyclictest_affinity: "measure-affinity",
      },
    },
    hwlatdetect: {
      playbook: "test_run_hwlatdetect",
      form: "hwlat-form",
      blocked: "hwlat-blocked",
      loading: "hwlat-loading",
      panel: "hwlat",
      picker: "hwlat-picker",
      body: "hwlat-body",
      button: "hwlat-go",
      results: (item) => item.interruptions,
      absent:
        "The collection installed on this node has no test_run_hwlatdetect " +
        "playbook, so the hardware cannot be measured from here. Past " +
        "measurements are still listed below.",
      empty:
        "The firmware has not been measured from this node yet. A machine " +
        "that passes every check above and still misses its deadline is the " +
        "case this answers.",
      note: "what the firmware took without telling the kernel",
      fields: {
        hwlatdetect_duration: "hwlat-duration",
        hwlatdetect_threshold: "hwlat-threshold",
        hwlatdetect_width: "hwlat-width",
        hwlatdetect_window: "hwlat-window",
      },
    },
  };

  const state = {
    catalogue: {},
    measurements: { cyclictest: [], hwlatdetect: [] },
    selected: { cyclictest: null, hwlatdetect: null },
    canLaunch: false,
    machines: [],
    isolated: [],
    thisHost: null,
  };

  // Enough for a machine with more threads than anyone measures at once, and
  // distinguishable at a 1.5px stroke on the dark background.
  const SERIES = [
    "#4a9eff",
    "#46b16b",
    "#d9a441",
    "#d9534f",
    "#7a5cd6",
    "#3fbfb0",
    "#c86bd0",
    "#8d9bad",
  ];

  function element(id) {
    return document.getElementById(id);
  }

  // Warnings accumulate for the load, and a later success never clears them.
  // The panels load in parallel, so a card that failed had its message wiped
  // by the next card that succeeded, and the page looked merely empty.
  let banners = [];

  function showBanner(messages) {
    banners = banners.concat(messages.filter((m) => !banners.includes(m)));
    const banner = element("banner");
    banner.textContent = banners.join(" ");
    banner.hidden = !banners.length;
  }

  // Conformance

  async function loadChecks() {
    const report = await API.get("/realtime");
    element("checks-loading").hidden = true;
    element("checks").hidden = false;
    const rows = element("check-rows");
    rows.replaceChildren();
    report.checks.forEach((check) => rows.append(renderCheck(check)));

    const wanting = report.checks.filter(
      (check) => check.status === "warning"
    ).length;
    element("checks-lead").textContent = report.this_host
      ? (wanting
          ? wanting + " of " + report.checks.length + " worth a look"
          : "nothing worth a look") +
        ", against " +
        report.this_host +
        (report.inventory_commit
          ? " at " + report.inventory_commit.slice(0, 8)
          : "")
      : "no inventory entry describes this machine yet";

    // The report carries the CPU reading the checks were formed from, so the
    // affinity picker gets the isolated set without a second request.
    state.isolated = (report.cpu && report.cpu.isolated) || [];
    renderAffinity();

    showBanner(report.warnings || []);
    return report;
  }

  // One row per check, and the detail only when asked for. Ten checks with
  // their reasoning always on screen is the page an operator has to scroll,
  // and scrolling is what this layout exists to avoid.
  function renderCheck(check) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "check-row";
    if (check.declared && check.declared !== check.observed) {
      row.classList.add("differs");
    }

    const dot = document.createElement("span");
    dot.className = "dot status-" + check.status;

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = check.title;

    const observed = document.createElement("span");
    observed.className = "observed";
    observed.textContent = check.observed;

    const declared = document.createElement("span");
    declared.className = "declared";
    if (check.declared === null || check.declared === undefined) {
      // A dash rather than an empty cell: the column reads as answered, and
      // the answer is that nothing in the inventory has an opinion here.
      declared.classList.add("none");
      declared.textContent = "\u2013";
    } else {
      declared.textContent = check.declared;
    }

    row.append(dot, name, observed, declared);

    const detail = check.detail || defaultDetail(check);
    if (detail) {
      const note = document.createElement("p");
      note.className = "check-detail";
      note.textContent = detail;
      note.hidden = true;
      row.append(note);
      row.addEventListener("click", () => {
        note.hidden = !note.hidden;
      });
    }
    return row;
  }

  // Something to say on a row that passed. A check with no detail would open
  // to nothing, which reads as a page that failed rather than as a machine
  // that is fine.
  function defaultDetail(check) {
    if (check.declared !== null && check.declared !== undefined) {
      return (
        "This machine matches what the inventory declares for it. A " +
        "difference here would be fixed by editing the inventory and " +
        "converging, never from this page."
      );
    }
    return (
      "Reported for information. Nothing in the inventory declares this, so " +
      "there is nothing to compare it against and no run that would change it."
    );
  }

  // What each state means, and what an operator does about it. Straight from
  // the exporter's own vocabulary, so a colour here and a colour on the
  // Grafana dashboard this repository ships mean the same thing.
  const STATES = {
    free: ["An isolated core with nothing on it", "#46b16b"],
    vm: ["A guest thread", "#4a9eff"],
    irq: ["A NIC interrupt", "#d98a2b"],
    irq_slot: ["A slot holding NIC interrupts", "#d98a2b"],
    slot: ["A named shared core slot", "#d9a441"],
    quadlet: ["A pinned container", "#3fbfb0"],
    run: ["A seapath-run process", "#c86bd0"],
    claim: ["A claim held by an operator tool", "#c86bd0"],
    reserved: ["The idle sibling of an exclusive core", "#7a2f2f"],
    housekeeping: ["Not isolated", "#39424f"],
    unknown: ["The exporter did not say", "#8d9bad"],
  };

  // The pool of every node, read from each one's exporter. This is the only
  // panel that leaves the local machine, and it is the only way the question
  // can be answered: occupancy is the affinity of every QEMU thread in /proc,
  // which this container's PID namespace hides.
  async function loadPool() {
    const pool = await API.get("/realtime/pool");
    element("map-loading").hidden = true;

    const reachable = pool.nodes.filter((node) => node.cpus.length);
    const blocked = element("pool-blocked");
    if (!reachable.length) {
      // Said as what is missing rather than drawn as an empty grid. On a
      // machine whose exporters were never deployed this is the ordinary
      // state, and the sentence names the role that fixes it.
      blocked.textContent = pool.nodes.length
        ? "No node published a seapath-alloc pool. " +
          pool.nodes
            .map((node) => node.host + ": " + (node.error || "no metrics"))
            .join("; ") +
          ". deploy_prometheus_exporters and deploy_seapath_alloc install what " +
          "publishes it."
        : "There is no inventory yet, so there is no node to ask.";
      blocked.hidden = false;
      return;
    }
    blocked.hidden = true;
    element("pool").hidden = false;
    element("pool-legend").hidden = false;

    const grid = element("pool");
    grid.replaceChildren();
    pool.nodes.forEach((node) => grid.append(renderNode(node, pool.this_host)));

    const stale = reachable
      .map((node) => node.scrape_age_seconds)
      .filter((age) => age !== null && age !== undefined);
    element("map-note").textContent =
      reachable.length +
      " of " +
      pool.nodes.length +
      " nodes answered" +
      (stale.length
        ? ", " + Math.round(Math.max.apply(null, stale)) + "s ago"
        : "");
  }

  function renderNode(node, thisHost) {
    const box = document.createElement("div");
    box.className = "pool-node";

    const head = document.createElement("h3");
    head.textContent = node.host;
    if (node.host === thisHost) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "this node";
      head.append(" ", tag);
    }
    box.append(head);

    if (!node.cpus.length) {
      const why = document.createElement("p");
      why.className = "legend";
      why.textContent = node.error || "No metrics.";
      box.append(why);
      return box;
    }

    // One column per physical core, threads stacked inside it, which is the
    // shape the Grafana dashboard uses and the shape that makes a hyperthread
    // pair readable at a glance.
    const cores = new Map();
    node.cpus.forEach((slot) => {
      const key = slot.core === null ? slot.cpu : slot.core;
      if (!cores.has(key)) {
        cores.set(key, []);
      }
      cores.get(key).push(slot);
    });

    const map = document.createElement("div");
    map.className = "pool-cores";
    [...cores.keys()].sort((a, b) => a - b).forEach((key) => {
      const column = document.createElement("div");
      column.className = "pool-core";
      cores.get(key).forEach((slot) => column.append(renderSlot(slot)));
      map.append(column);
    });
    box.append(map);

    const notes = [];
    if (node.hard_fallbacks) {
      notes.push(
        node.hard_fallbacks +
          " actor" +
          (node.hard_fallbacks > 1 ? "s" : "") +
          " asked for isolation and is running on housekeeping cores"
      );
    }
    if (node.soft_fallbacks) {
      notes.push(node.soft_fallbacks + " degraded to a shared core");
    }
    node.slot_warnings.forEach((warning) => notes.push(warning));
    if (notes.length) {
      const line = document.createElement("p");
      line.className = "pool-warning";
      line.textContent = notes.join(". ") + ".";
      box.append(line);
    }
    return box;
  }

  function renderSlot(slot) {
    const cell = document.createElement("div");
    cell.className = "pool-cpu";
    const [meaning, colour] = STATES[slot.state] || STATES.unknown;
    cell.style.background = colour;
    // The label where it fits, the number otherwise. A core carrying a guest
    // is read by the guest's name; a free one is read by its number, which is
    // what an operator types into isolcpus.
    cell.textContent = slot.label || String(slot.cpu);
    cell.title =
      "CPU " +
      slot.cpu +
      ": " +
      meaning +
      (slot.label ? " (" + slot.label + ")" : "") +
      (slot.group ? ", " + slot.group : "") +
      (slot.scheduler ? ", " + slot.scheduler + "/" + slot.priority : "") +
      (slot.members ? ", sharing with " + slot.members : "");
    return cell;
  }

  // The kernel's own range notation, `4-7` rather than `4,5,6,7`. The same
  // shape the inventory writes, so what the operator picks here reads like
  // what they declared there.
  function ranges(cpus) {
    if (!cpus.length) {
      return "";
    }
    const ordered = cpus.slice().sort((a, b) => a - b);
    const parts = [];
    let start = ordered[0];
    let previous = start;
    ordered.slice(1).forEach((cpu) => {
      if (cpu === previous + 1) {
        previous = cpu;
        return;
      }
      parts.push(start === previous ? String(start) : start + "-" + previous);
      start = cpu;
      previous = cpu;
    });
    parts.push(start === previous ? String(start) : start + "-" + previous);
    return parts.join(",");
  }

  // What "CPUs to measure" offers. `smp` is the upstream role's word for every
  // online CPU and says nothing to a reader, so it is spelled out and kept as
  // the value behind the label. The isolated set is offered by name and comes
  // first, because it is the set a real time guest runs on and the answer this
  // page is usually being asked for.
  function renderAffinity() {
    const choice = element("measure-affinity-choice");
    const value = element("measure-affinity");
    const help = element("measure-affinity-help");
    const isolated = ranges(state.isolated);

    const options = [];
    if (isolated) {
      options.push({ value: isolated, label: "The isolated set, " + isolated });
    }
    options.push({ value: "smp", label: "Every online CPU" });
    options.push({ value: "", label: "A list I type" });

    choice.replaceChildren();
    options.forEach((option) => {
      const node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.label;
      choice.append(node);
    });

    const apply = () => {
      const custom = choice.value === "";
      value.hidden = !custom;
      if (custom) {
        value.focus();
      } else {
        value.value = choice.value;
      }
      help.textContent = custom
        ? "A CPU list in the kernel notation, such as 4-7 or 2,4-6."
        : choice.value === "smp"
          ? "One thread per online CPU, the housekeeping ones included, which " +
            "is where this service itself runs."
          : "One thread per isolated CPU, which is what a real time guest " +
            "runs on.";
    };
    choice.onchange = apply;
    apply();
  }

  // Width over window is the fraction of wall clock time the hardware is
  // actually watched, and it is what an operator is really choosing with those
  // two numbers. Shown as they type, because nobody divides 500000 by 1000000
  // in their head while deciding how hard to load a substation.
  function renderShare() {
    const width = Number(element("hwlat-width").value);
    const window_ = Number(element("hwlat-window").value);
    const help = element("hwlat-share");
    if (!width || !window_ || width > window_) {
      help.textContent =
        "The width has to fit inside the window: it is the part of each " +
        "window spent watching.";
      return;
    }
    help.textContent =
      "Watching " +
      Math.round((width / window_) * 100) +
      "% of the time. Raising it finds rarer events by taking more of the CPU " +
      "it is watching.";
  }

  ["hwlat-width", "hwlat-window"].forEach((id) => {
    element(id).addEventListener("input", renderShare);
  });
  renderShare();

  // Measurement

  async function loadCatalogue() {
    let playbooks = [];
    try {
      playbooks = await API.get("/playbooks");
    } finally {
      // Even when the catalogue could not be read. A launch panel that stays
      // blank says nothing, and the operator is left looking for a button that
      // was never rendered rather than reading why.
      Object.entries(MEASUREMENTS).forEach(([kind, spec]) => {
        state.catalogue[kind] = playbooks.find(
          (item) => item.entry.id === spec.playbook
        );
        renderLaunch(kind);
      });
    }
  }

  function renderLaunch(kind) {
    const spec = MEASUREMENTS[kind];
    const entry = state.catalogue[kind];
    const form = element(spec.form);
    const blocked = element(spec.blocked);
    if (!entry) {
      // Either the collection has no such playbook, or the catalogue could not
      // be read at all. The sentence covers both, since neither leaves a
      // button that would work.
      blocked.textContent = spec.absent;
      blocked.hidden = false;
      form.hidden = true;
      return;
    }
    if (!entry.available) {
      blocked.textContent = entry.unmet.join(" ");
      blocked.hidden = false;
      form.hidden = true;
      return;
    }
    blocked.hidden = true;
    form.hidden = !state.canLaunch;
  }

  async function loadMeasurements(kind) {
    const spec = MEASUREMENTS[kind];
    const items = await API.get("/realtime/measurements?limit=10&kind=" + kind);
    state.measurements[kind] = items;
    element(spec.loading).hidden = true;
    element(spec.panel).hidden = false;
    const done = items.filter((item) => spec.results(item).length);
    // The empty sentence goes where the results would be, so a panel with no
    // history says why rather than showing a blank rectangle.
    element(spec.body).textContent = done.length ? "" : spec.empty;
    state.selected[kind] = done.length ? done[0].run_id : null;
    renderPicker(kind);
    renderMeasurement(kind);
  }

  function renderPicker(kind) {
    const spec = MEASUREMENTS[kind];
    const picker = element(spec.picker);
    picker.replaceChildren();
    state.measurements[kind].forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      if (item.run_id === state.selected[kind]) {
        button.classList.add("current");
      }
      button.textContent =
        (item.started_at ? item.started_at.slice(0, 16).replace("T", " ") : "?") +
        (spec.results(item).length ? "" : " - " + item.state);
      button.addEventListener("click", () => {
        state.selected[kind] = item.run_id;
        renderPicker(kind);
        renderMeasurement(kind);
      });
      picker.append(button);
    });
  }

  function renderMeasurement(kind) {
    const spec = MEASUREMENTS[kind];
    const body = element(spec.body);
    body.replaceChildren();
    const measurement = state.measurements[kind].find(
      (item) => item.run_id === state.selected[kind]
    );
    if (!measurement) {
      return;
    }

    // One wrapped line rather than a four row list. Every value here is
    // provenance, read once when an operator wonders where a number came
    // from, and it was costing four lines of a pane that has to fit.
    const meta = document.createElement("p");
    meta.className = "measure-meta";
    const parameters =
      Object.entries(measurement.variables)
        .map(([name, value]) => name.split("_").slice(1).join(" ") + " " + value)
        .join(", ") || "upstream defaults";
    [
      "by " + measurement.launched_by,
      "inventory " + (measurement.inventory_commit || "unknown").slice(0, 8),
      parameters,
    ].forEach((text) => {
      const item = document.createElement("span");
      item.textContent = text;
      meta.append(item);
    });
    const anchor = document.createElement("a");
    anchor.href = "/runs?run=" + encodeURIComponent(measurement.run_id);
    anchor.textContent = "open the run";
    meta.append(anchor);
    body.append(meta);

    // Every machine the run measured, this one first. A run has no --limit, so
    // a measurement plays the whole inventory and brings back one file each,
    // and the pool above reads every node too: the page is the cluster's, and
    // showing one machine of a measurement that took three would be the odd
    // panel out. Local first, because it is the one the operator is standing
    // on and the one every other reading here is about.
    const results = spec.results(measurement).slice().sort((a, b) => {
      if (a.host === state.thisHost) return -1;
      if (b.host === state.thisHost) return 1;
      return a.host.localeCompare(b.host);
    });

    const render =
      kind === "cyclictest" ? renderLatency : renderInterruptions;
    results.forEach((result) => {
      body.append(render(result));
    });

  }

  // hwlatdetect returns a list of gaps rather than a distribution, and usually
  // an empty one. So this card is a verdict and a table: on a clean machine the
  // whole result is "nothing above the threshold", and on a dirty one what
  // matters is when each interruption happened and how long it lasted.
  function renderInterruptions(result) {
    const section = document.createElement("section");
    section.append(hostHeading(result.host));

    if (!result.supported || result.message) {
      const note = document.createElement("p");
      // A kernel that cannot be asked is reported apart from a kernel that was
      // asked and found nothing. Reading the first as a clean machine is the
      // one mistake this card must never invite.
      note.className = result.supported ? "warning" : "legend";
      note.textContent = result.message;
      section.append(note);
      return section;
    }

    const verdict = document.createElement("p");
    const worst = result.interruptions.length
      ? Math.max.apply(
          null,
          result.interruptions.map((item) =>
            Math.max(item.inner_us, item.outer_us)
          )
        )
      : null;
    if (worst === null) {
      verdict.className = "legend";
      // The sample count is deliberately absent. hwlatdetect's "Samples
      // recorded" counts the gaps it saw, so on a clean machine it is zero,
      // and quoting it here read as "nothing was measured" on exactly the
      // machine that was measured and came back clean.
      verdict.textContent =
        "No interruption above the threshold. The firmware took nothing this " +
        "run could see.";
      section.append(verdict);
      return section;
    }

    verdict.className = "warning";
    verdict.textContent =
      result.interruptions.length +
      " interruption" +
      (result.interruptions.length > 1 ? "s" : "") +
      " above the threshold, the worst " +
      worst +
      "us. That is time the kernel never saw, so no isolation and no priority " +
      "protects a guest from it. The fix is in the firmware settings.";
    section.append(verdict, interruptionTable(result));

    if (result.command) {
      const command = document.createElement("p");
      command.className = "legend";
      const code = document.createElement("code");
      code.textContent = result.command;
      command.append(code);
      section.append(command);
    }
    return section;
  }

  function interruptionTable(result) {
    const wrapper = document.createElement("div");
    wrapper.className = "table-scroll";
    const table = document.createElement("table");
    const head = document.createElement("thead");
    head.innerHTML =
      "<tr><th>When</th><th>CPU</th><th>Inner</th><th>Outer</th>" +
      "<th>Worst</th></tr>";
    const body = document.createElement("tbody");
    // The longest first: on a machine with hundreds of small gaps and one long
    // one, the long one is the whole finding.
    const ordered = result.interruptions.slice().sort(
      (a, b) =>
        Math.max(b.inner_us, b.outer_us) - Math.max(a.inner_us, a.outer_us)
    );
    ordered.slice(0, 50).forEach((item) => {
      const row = document.createElement("tr");
      [
        item.timestamp === null
          ? "unknown"
          : new Date(item.timestamp * 1000).toISOString().slice(11, 19),
        item.cpu === null ? "unknown" : String(item.cpu),
        item.inner_us + "us",
        item.outer_us + "us",
        Math.max(item.inner_us, item.outer_us) + "us",
      ].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      body.append(row);
    });
    table.append(head, body);
    wrapper.append(table);
    if (ordered.length > 50) {
      const more = document.createElement("p");
      more.className = "legend";
      more.textContent =
        "The 50 longest of " + ordered.length + ". The run holds them all.";
      wrapper.append(more);
    }
    return wrapper;
  }

  // Which machine a result belongs to, marked when it is this one. Three
  // results under one heading otherwise read as one machine measured three
  // times.
  function hostHeading(host) {
    const heading = document.createElement("h3");
    heading.textContent = host;
    if (host === state.thisHost) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "this node";
      heading.append(" ", tag);
    }
    return heading;
  }

  function renderLatency(result) {
    const section = document.createElement("section");
    section.append(hostHeading(result.host));

    if (result.parse_error) {
      const error = document.createElement("p");
      error.className = "warning";
      error.textContent = result.parse_error;
      section.append(error);
      return section;
    }

    // The answer first, in one line. A latency measurement is read for its
    // worst case, and a seven column table of every thread was burying it
    // under numbers that agree with each other.
    const worst = result.threads.reduce(
      (found, thread) =>
        thread.max_us !== null && (found === null || thread.max_us > found.max_us)
          ? thread
          : found,
      null
    );
    const overflows = result.threads.reduce(
      (total, thread) => total + thread.overflows,
      0
    );
    const verdict = document.createElement("p");
    verdict.className = "verdict";
    verdict.textContent = worst
      ? "Worst " +
        worst.max_us +
        "us, on " +
        (worst.cpu === null ? "thread " + worst.thread : "CPU " + worst.cpu) +
        " of " +
        result.threads.length +
        " measured" +
        (overflows
          ? ". " + overflows + " samples ran off the end of the histogram."
          : ".")
      : "No thread reported a latency.";
    section.append(verdict, chart(result), legend(result));

    // Every thread, for the operator who wants to see them agree. Folded away,
    // because on a 32 CPU machine it is 32 rows of a pane that has to fit.
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Per thread, and the command";
    details.append(summary, renderSummary(result));
    if (result.command) {
      const command = document.createElement("p");
      command.className = "legend";
      const code = document.createElement("code");
      code.textContent = result.command;
      command.append(code);
      details.append(command);
    }
    section.append(details);
    return section;
  }

  // Every measuring thread, folded behind a disclosure on the panel. It is the
  // evidence behind the one line verdict rather than the answer itself, and on
  // a machine with many isolated CPUs it is one row each.
  function renderSummary(result) {
    const wrapper = document.createElement("div");
    wrapper.className = "table-scroll";
    const table = document.createElement("table");
    const head = document.createElement("thead");
    // The overflow threshold is the histogram's own size, which the role sets
    // with `-h`. Naming a fixed 400us here would go quietly wrong the day that
    // changes, in the column that says how much of the tail is missing.
    const ceiling = result.histogram.buckets.length;
    head.innerHTML =
      "<tr><th>Thread</th><th>CPU</th><th>Min</th><th>Avg</th>" +
      "<th>Max</th><th>Samples</th><th>Over " +
      ceiling +
      "us</th></tr>";
    const body = document.createElement("tbody");
    result.threads.forEach((thread, index) => {
      const row = document.createElement("tr");
      [
        String(thread.thread),
        thread.cpu === null ? "unknown" : String(thread.cpu),
        micro(thread.min_us),
        micro(thread.avg_us),
        micro(thread.max_us),
        String(thread.samples),
        String(thread.overflows),
      ].forEach((value, column) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (column === 0) {
          cell.style.color = SERIES[index % SERIES.length];
        }
        row.append(cell);
      });
      body.append(row);
    });
    table.append(head, body);
    wrapper.append(table);
    return wrapper;
  }

  function micro(value) {
    return value === null || value === undefined ? "unknown" : value + "us";
  }

  // A histogram, which is what cyclictest produces and how it is read
  // everywhere else: one bar per microsecond bucket, on a logarithmic count
  // axis. rtperfui drew it this way and was right to.
  //
  // This was a line at first, which forced a workaround the bars make
  // unnecessary: a bucket nobody landed in has no bar, while a line had to be
  // broken at every zero or it drew a continuous run along the axis claiming
  // one sample in every bucket out to the worst case.
  //
  // The four decades between the bulk and the tail are the whole point, and a
  // linear axis hides exactly the part an operator is looking for. A chart
  // library is 200kB of vendored JavaScript for one chart.
  function chart(result) {
    const width = 900;
    const height = 170;
    const left = 40;
    const bottom = 22;
    // The plot stops short of the right edge so the last x label, which is
    // anchored to its end, has somewhere to sit.
    const right = 14;
    const buckets = result.histogram.buckets;
    const counts = result.histogram.counts;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "chart");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("role", "img");

    if (!buckets.length || !counts.length) {
      return svg;
    }

    // The last bucket any thread reached, so a 400 bucket histogram whose
    // samples all sit under 30us is drawn over the range that has data.
    let lastUsed = 0;
    let peak = 1;
    counts.forEach((series) => {
      series.forEach((value, index) => {
        if (value > 0 && index > lastUsed) {
          lastUsed = index;
        }
        if (value > peak) {
          peak = value;
        }
      });
    });
    const span = Math.max(buckets[lastUsed], 1);
    const decades = Math.ceil(Math.log10(peak)) || 1;
    const base = height - bottom;

    const x = (us) => left + (us / span) * (width - left - right);
    const y = (count) =>
      count <= 0 ? base : base - (Math.log10(count) / decades) * (base - 8);

    svg.append(
      line(left, base, width - right, base),
      line(left, 8, left, base)
    );

    for (let decade = 0; decade <= decades; decade += 1) {
      const value = Math.pow(10, decade);
      svg.append(text(left - 6, y(value) + 3, format(value), "end"));
    }
    for (let step = 0; step <= 4; step += 1) {
      const us = Math.round((span / 4) * step);
      // The last label is anchored to its end and the first to its start, so
      // neither runs off the side of the viewBox.
      const anchor = step === 4 ? "end" : step === 0 ? "start" : "middle";
      svg.append(text(x(us), base + 15, us + "us", anchor));
    }

    // One bucket is this many units wide. Never below a hairline: with 400
    // buckets over 850 units a bar is about two units, and a bucket holding a
    // single sample in the tail is the one an operator came to see.
    const bar = Math.max((width - left - right) / (lastUsed + 1), 1);

    counts.forEach((series, index) => {
      const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
      group.setAttribute("fill", SERIES[index % SERIES.length]);
      // Threads overlap on the same buckets, so they are drawn translucent:
      // where they agree the colour deepens, and a thread that is alone out in
      // the tail still shows its own.
      group.setAttribute("opacity", "0.72");
      for (let bucket = 0; bucket <= lastUsed; bucket += 1) {
        const count = series[bucket];
        if (count <= 0) {
          continue;
        }
        const top = y(count);
        const rect = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "rect"
        );
        rect.setAttribute("x", x(buckets[bucket]));
        rect.setAttribute("y", top);
        rect.setAttribute("width", bar);
        // A bucket with one sample sits on the axis and would be zero high.
        rect.setAttribute("height", Math.max(base - top, 1));
        group.append(rect);
      }
      svg.append(group);
    });

    return svg;
  }



  function line(x1, y1, x2, y2) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "line");
    node.setAttribute("class", "axis");
    node.setAttribute("x1", x1);
    node.setAttribute("y1", y1);
    node.setAttribute("x2", x2);
    node.setAttribute("y2", y2);
    return node;
  }

  function text(x, y, value, anchor) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "text");
    node.setAttribute("class", "tick");
    node.setAttribute("x", x);
    node.setAttribute("y", y);
    node.setAttribute("text-anchor", anchor);
    node.textContent = value;
    return node;
  }

  function format(value) {
    if (value >= 1000000) {
      return value / 1000000 + "M";
    }
    if (value >= 1000) {
      return value / 1000 + "k";
    }
    return String(value);
  }

  function legend(result) {
    const box = document.createElement("div");
    box.className = "chart-legend";
    // What the two axes are, said once. A logarithmic count axis is worth
    // naming: the reader has to know that the dots along the bottom are single
    // samples rather than noise in the drawing.
    const axes = document.createElement("span");
    axes.className = "chart-axes";
    axes.textContent = "latency across, samples up, logarithmic";
    box.append(axes);
    result.threads.forEach((thread, index) => {
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.style.background = SERIES[index % SERIES.length];
      item.append(
        swatch,
        document.createTextNode(
          thread.cpu === null ? "thread " + thread.thread : "CPU " + thread.cpu
        )
      );
      box.append(item);
    });
    return box;
  }

  // Launching. The same confirmation shape as an apply, with the sentence the
  // catalogue entry wrote: a measurement disturbs a live substation through
  // what it runs rather than through what it writes, and the operator needs
  // that sentence rather than the convergence one.
  function confirm(kind) {
    const spec = MEASUREMENTS[kind];
    const modal = element("measure-confirm");
    const go = element("measure-confirm-go");
    const error = element("measure-confirm-error");
    error.hidden = true;

    const variables = {};
    Object.entries(spec.fields).forEach(([name, id]) => {
      const field = element(id);
      variables[name] =
        field.type === "number" ? Number(field.value) : field.value.trim();
    });

    element("measure-confirm-title").textContent =
      state.catalogue[kind].entry.title;
    // The values the operator actually chose, rather than the catalogue's
    // defaults. A confirmation that named a priority the form no longer holds
    // is a sentence an operator learns to stop reading, and this is the page
    // where that costs the most.
    element("measure-disruption").textContent =
      state.catalogue[kind].entry.disruption +
      " " +
      settings(kind, variables) +
      " on " +
      (state.machines.join(", ") || "no machine, the inventory is empty") +
      ". It changes nothing on them.";

    go.disabled = false;
    go.onclick = async () => {
      go.disabled = true;
      try {
        const started = await API.post("/runs", {
          playbook: spec.playbook,
          check: false,
          variables,
        });
        window.location.assign(
          "/runs?run=" + encodeURIComponent(started.run_id)
        );
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
        go.disabled = false;
      }
    };
    modal.hidden = false;
  }

  // The chosen settings in the sentence's own words. Written per measurement
  // because the numbers mean different things: a duration and a priority for
  // one, a duration and a sampled fraction for the other, and the fraction is
  // what an operator is really choosing when they set width and window.
  function settings(kind, variables) {
    if (kind === "cyclictest") {
      return (
        "This one runs for " +
        variables.cyclictest_duration +
        "s at priority " +
        variables.cyclictest_priority +
        ", on " +
        (variables.cyclictest_affinity === "smp"
          ? "every online CPU"
          : "CPUs " + variables.cyclictest_affinity) +
        " of"
      );
    }
    const share = Math.round(
      (variables.hwlatdetect_width / variables.hwlatdetect_window) * 100
    );
    return (
      "This one runs for " +
      variables.hwlatdetect_duration +
      "s, holding interrupts off " +
      variables.hwlatdetect_width +
      "us out of every " +
      variables.hwlatdetect_window +
      "us, so about " +
      share +
      "% of that time, on"
    );
  }

  // The two measurements share the pane. Switching is local: both histories
  // are already loaded, so a tab is a show and a hide rather than a fetch.
  function showTab(kind) {
    document.querySelectorAll("#measure-tabs .tab").forEach((tab) => {
      tab.classList.toggle("current", tab.dataset.kind === kind);
    });
    Object.keys(MEASUREMENTS).forEach((name) => {
      element("panel-" + name).hidden = name !== kind;
    });
    element("measure-note").textContent = MEASUREMENTS[kind].note;
  }

  document.querySelectorAll("#measure-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => showTab(tab.dataset.kind));
  });

  element("measure-cancel").addEventListener("click", () => {
    element("measure-confirm").hidden = true;
  });
  Object.entries(MEASUREMENTS).forEach(([kind, spec]) => {
    element(spec.button).addEventListener("click", () => confirm(kind));
  });

  // Which machines a run plays, and which of them is this one. Fetched before
  // anything else rather than beside it: the measurement panels filter their
  // results to the local machine, and doing that in parallel with the fetch
  // that names it is a race the panels lost, so every machine of the run was
  // rendered on a page about one.
  async function loadMachines() {
    const payload = await API.get("/inventory");
    state.machines = payload.inventory ? Object.keys(payload.inventory.hosts) : [];
    state.thisHost = payload.this_host;
  }

  async function start() {
    const { me } = await Chrome.load();
    // A measurement loads every machine of the inventory at real time
    // priority for as long as the operator asked, on a live substation. That
    // is a run like any other, and POST /runs asks for the admin role.
    state.canLaunch = Chrome.isAdmin(me);
    showTab("cyclictest");
    // The inventory first, on its own. It names the machines a run plays and
    // which of them is this one, and the measurement panels filter to that
    // name: fetching it beside them is a race the panels lost, so every
    // machine of the run was rendered on a page about one.
    await loadMachines();
    await Promise.all([
      loadChecks(),
      loadPool(),
      loadCatalogue(),
      loadMeasurements("cyclictest"),
      loadMeasurements("hwlatdetect"),
    ]);
  }

  start().catch((failure) => {
    showBanner([failure.message]);
    // Every spinner, so a page that failed halfway does not sit there looking
    // like a page still loading.
    ["checks-loading", "latency-loading", "hwlat-loading"].forEach((id) => {
      element(id).hidden = true;
    });
  });
})();
