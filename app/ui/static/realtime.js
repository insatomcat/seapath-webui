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
      lead: "latency-lead",
      button: "measure-go",
      results: (item) => item.latency,
      absent:
        "The collection installed on this node has no test_run_cyclictest " +
        "playbook, so the latency cannot be measured from here. Past " +
        "measurements are still listed below.",
      done:
        "cyclictest, run through Ansible on the machines the inventory " +
        "declares. Every measurement carries the inventory commit it was " +
        "taken under, because a latency figure without the isolation behind " +
        "it is an anecdote.",
      empty:
        "No latency has been measured from this node yet. cyclictest runs on " +
        "the machines over the same SSH path a convergence uses, so nothing " +
        "measures inside this container and nothing here runs at real time " +
        "priority.",
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
      lead: "hwlat-lead",
      button: "hwlat-go",
      results: (item) => item.interruptions,
      absent:
        "The collection installed on this node has no test_run_hwlatdetect " +
        "playbook, so the hardware cannot be measured from here. Past " +
        "measurements are still listed below.",
      done:
        "hwlatdetect, run through Ansible on the machines the inventory " +
        "declares.",
      empty:
        "The firmware has not been measured from this node yet. A machine " +
        "that passes every check above and still misses its deadline is the " +
        "case this answers.",
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

  function showBanner(messages) {
    const banner = element("banner");
    if (!messages.length) {
      banner.hidden = true;
      return;
    }
    banner.textContent = messages.join(" ");
    banner.hidden = false;
  }

  // Conformance

  async function loadChecks() {
    const report = await API.get("/realtime");
    element("checks-loading").hidden = true;
    const container = element("checks");
    container.hidden = false;
    container.replaceChildren();

    report.checks.forEach((check) => {
      container.append(renderCheck(check));
    });

    const warnings = report.checks.filter(
      (check) => check.status === "warning"
    ).length;
    element("checks-lead").textContent = report.this_host
      ? "Checked against " +
        report.this_host +
        " in the inventory" +
        (report.inventory_commit
          ? " at " + report.inventory_commit.slice(0, 8)
          : "") +
        ". " +
        (warnings
          ? warnings + " of " + report.checks.length + " want attention."
          : "Nothing wants attention.")
      : "No inventory entry describes this machine yet, so every check below " +
        "is advice. Write the inventory first: the comparison is what makes " +
        "this page worth reading.";

    showBanner(report.warnings || []);
    return report;
  }

  function renderCheck(check) {
    const row = document.createElement("div");
    row.className = "check " + check.status;

    const title = document.createElement("span");
    title.className = "check-title";
    title.textContent = check.title;

    const kind = document.createElement("span");
    kind.className = "check-kind";
    kind.textContent = check.kind;

    const values = document.createElement("div");
    values.className = "check-values";
    values.textContent = check.observed;
    if (check.declared !== null && check.declared !== undefined) {
      const declared = document.createElement("span");
      declared.className = "check-declared";
      declared.textContent = "  declared: " + check.declared;
      values.append(declared);
    }

    row.append(title, kind, values);
    if (check.detail) {
      const detail = document.createElement("p");
      detail.className = "check-detail";
      detail.textContent = check.detail;
      row.append(detail);
    }
    return row;
  }

  // The CPU map, grouped by physical core so hyperthread siblings sit
  // together. The node view already draws a flat grid of the same CPUs; this
  // one exists to answer a different question, which is whether an isolated
  // CPU is sharing a core with one that is not.
  async function loadMap() {
    const cpu = await API.get("/node/cpu");
    const cores = new Map();
    cpu.topology.forEach((entry) => {
      const key = entry.socket + ":" + entry.core;
      if (!cores.has(key)) {
        cores.set(key, []);
      }
      cores.get(key).push(entry);
    });

    const map = element("core-map");
    map.replaceChildren();
    let split = 0;
    cores.forEach((threads) => {
      const isolated = threads.filter((entry) => entry.isolated).length;
      const box = document.createElement("div");
      box.className = "core";
      if (isolated > 0 && isolated < threads.length) {
        box.classList.add("split");
        split += 1;
      }
      threads.forEach((entry) => {
        const cell = document.createElement("div");
        cell.className = "cpu " + (entry.isolated ? "isolated" : "housekeeping");
        cell.textContent = entry.cpu;
        cell.title =
          "CPU " +
          entry.cpu +
          " - socket " +
          entry.socket +
          ", core " +
          entry.core;
        map.append();
        box.append(cell);
      });
      map.append(box);
    });

    fillList(element("map-summary"), [
      ["Isolated", cpu.isolated.length ? cpu.isolated.join(",") : "none"],
      [
        "Housekeeping",
        cpu.housekeeping.length ? cpu.housekeeping.join(",") : "unknown",
      ],
      ["nohz_full", cpu.nohz_full.length ? cpu.nohz_full.join(",") : "none"],
      ["Cores", String(cores.size)],
      [
        "Cores split across the isolation",
        split ? String(split) + " - see the outlined ones" : "none",
      ],
    ]);
  }

  function fillList(list, pairs) {
    list.replaceChildren();
    pairs.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = value;
      list.append(term, definition);
    });
  }

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
    element(spec.lead).textContent = done.length ? spec.done : spec.empty;
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

    const meta = document.createElement("dl");
    fillList(meta, [
      ["Run", measurement.run_id],
      ["Launched by", measurement.launched_by],
      ["Inventory", measurement.inventory_commit || "unknown"],
      [
        "Parameters",
        Object.entries(measurement.variables)
          .map(([name, value]) => name + "=" + value)
          .join(", ") || "the upstream defaults",
      ],
    ]);
    body.append(meta);

    const render =
      kind === "cyclictest" ? renderLatency : renderInterruptions;
    spec.results(measurement).forEach((result) => {
      body.append(render(result));
    });

    const link = document.createElement("p");
    link.className = "legend";
    const anchor = document.createElement("a");
    anchor.href = "/runs?run=" + encodeURIComponent(measurement.run_id);
    anchor.textContent = "Open the run";
    link.append(anchor, document.createTextNode(" to read its event stream."));
    body.append(link);
  }

  // hwlatdetect returns a list of gaps rather than a distribution, and usually
  // an empty one. So this card is a verdict and a table: on a clean machine the
  // whole result is "nothing above the threshold", and on a dirty one what
  // matters is when each interruption happened and how long it lasted.
  function renderInterruptions(result) {
    const section = document.createElement("section");
    const heading = document.createElement("h3");
    heading.textContent = result.host;
    section.append(heading);

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

  function renderLatency(result) {
    const section = document.createElement("section");
    const heading = document.createElement("h3");
    heading.textContent = result.host;
    section.append(heading);

    if (result.parse_error) {
      const error = document.createElement("p");
      error.className = "warning";
      error.textContent = result.parse_error;
      section.append(error);
      return section;
    }

    section.append(renderSummary(result), chart(result), legend(result));
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

  // One line per thread on a logarithmic count axis, drawn as inline SVG. A
  // chart library is 200kB of vendored JavaScript for one chart, and the whole
  // point of a latency histogram is the four decades between the bulk of the
  // samples and the one that matters: a linear axis hides exactly the tail an
  // operator is looking for.
  //
  // A bucket nobody landed in is a gap in the line rather than a point on the
  // baseline. On a log axis zero has no place, and drawing it at the bottom
  // produced a continuous line along the axis that claimed one sample in every
  // bucket out to the worst case, in the region an operator reads most
  // carefully. So the polyline is broken wherever the count is zero, and a
  // bucket standing alone between two empty ones is marked with a dot: a
  // single sample at 341us is the whole reason for looking at this chart, and
  // a zero length line segment draws nothing.
  function chart(result) {
    const width = 720;
    const height = 240;
    const left = 44;
    const bottom = 26;
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
    counts.forEach((series) => {
      series.forEach((value, index) => {
        if (value > 0 && index > lastUsed) {
          lastUsed = index;
        }
      });
    });
    const span = Math.max(buckets[lastUsed], 1);
    let peak = 1;
    counts.forEach((series) => {
      series.forEach((value) => {
        if (value > peak) {
          peak = value;
        }
      });
    });
    const decades = Math.ceil(Math.log10(peak)) || 1;

    const x = (us) => left + (us / span) * (width - left - right);
    const y = (count) =>
      count <= 0
        ? height - bottom
        : height -
          bottom -
          (Math.log10(count) / decades) * (height - bottom - 10);

    svg.append(
      line(left, height - bottom, width - right, height - bottom),
      line(left, 10, left, height - bottom)
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
      svg.append(text(x(us), height - bottom + 14, us + "us", anchor));
    }

    counts.forEach((series, index) => {
      const colour = SERIES[index % SERIES.length];
      let run = [];
      const flush = () => {
        if (run.length > 1) {
          svg.append(polyline(run, colour));
        } else if (run.length === 1) {
          svg.append(dot(run[0][0], run[0][1], colour));
        }
        run = [];
      };
      for (let bucket = 0; bucket <= lastUsed; bucket += 1) {
        if (series[bucket] > 0) {
          run.push([x(buckets[bucket]), y(series[bucket])]);
        } else {
          flush();
        }
      }
      flush();
    });

    return svg;
  }

  function polyline(points, colour) {
    const node = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "polyline"
    );
    node.setAttribute("class", "series");
    node.setAttribute("points", points.map((p) => p[0] + "," + p[1]).join(" "));
    node.setAttribute("stroke", colour);
    return node;
  }

  function dot(cx, cy, colour) {
    const node = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "circle"
    );
    node.setAttribute("cx", cx);
    node.setAttribute("cy", cy);
    node.setAttribute("r", 1.6);
    node.setAttribute("fill", colour);
    return node;
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

  element("measure-cancel").addEventListener("click", () => {
    element("measure-confirm").hidden = true;
  });
  Object.entries(MEASUREMENTS).forEach(([kind, spec]) => {
    element(spec.button).addEventListener("click", () => confirm(kind));
  });

  async function loadMachines() {
    const payload = await API.get("/inventory");
    state.machines = payload.inventory ? Object.keys(payload.inventory.hosts) : [];
  }

  async function start() {
    const { me } = await Chrome.load();
    // A measurement loads every machine of the inventory at real time
    // priority for as long as the operator asked, on a live substation. That
    // is a run like any other, and POST /runs asks for the admin role.
    state.canLaunch = Chrome.isAdmin(me);
    await Promise.all([
      loadChecks(),
      loadMap(),
      loadMachines().then(loadCatalogue),
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
