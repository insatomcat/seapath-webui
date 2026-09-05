// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The real time page: what the tuning came out as, and what the latency
// measured. Two halves of one question, and the page keeps them apart because
// the answers are of different kinds. The conformance half is a reading of
// this machine and costs nothing. The measurement half is an Ansible run
// against every machine of the inventory, and it is confirmed like one.
//
// Four views, one on screen at a time, and a bar that says what the other
// three found. The bar is the page's summary: a tab holds the worst status of
// its panel and the one number an operator reads from it, so the glance the
// old three panel layout paid for by truncating everything is kept, and the
// panel behind the tab gets the whole screen.
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
      note:
        "What the scheduler delivered, on every machine the run measured, " +
        "this one first.",
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
        "that passes every conformance check and still misses its deadline " +
        "is the case this answers.",
      note:
        "What the firmware took without telling the kernel, on every machine " +
        "the run measured.",
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
    matrix: null,
    machines: [],
    isolated: [],
    thisHost: null,
  };

  // Enough for a machine with more threads than anyone measures at once, and
  // distinguishable at a 1.5px stroke. One ramp per palette rather than one
  // ramp over two grounds: a 1.5px stroke is the thinnest thing on the page,
  // and the amber and the grey that carry it on #10141a are gone on white.
  //
  // These are drawn into an SVG and set as inline styles, so they cannot come
  // from a rule the way the rest of the page's colours do. The panel redraws
  // itself when the palette changes, at the bottom of this file.
  const RAMPS = {
    dark: [
      "#4a9eff",
      "#46b16b",
      "#d9a441",
      "#d9534f",
      "#7a5cd6",
      "#3fbfb0",
      "#c86bd0",
      "#8d9bad",
    ],
    light: [
      "#1668c9",
      "#157f43",
      "#8a5d00",
      "#c0322d",
      "#6b3fc4",
      "#10736a",
      "#a4359f",
      "#55637a",
    ],
  };

  // The colour of one thread, which is its position in the ramp in force.
  // Not `series`: in the histogram that word already means one thread's
  // buckets, and the two met inside the loop that draws them.
  function rampColour(index) {
    const ramp = RAMPS[Theme.current()];
    return ramp[index % ramp.length];
  }

  function element(id) {
    return document.getElementById(id);
  }

  // What a panel has to say, said on its tab. Three of the four panels are off
  // screen at any moment, and an operator has to know which one is worth
  // opening without opening it: the status is the worst thing the panel found
  // and the answer is the one line it would lead with.
  function summarise(view, status, answer) {
    const tab = document.querySelector(
      '#views .view[data-view="' + view + '"]'
    );
    tab.querySelector(".dot").className = "dot status-" + status;
    tab.querySelector(".view-answer").textContent = answer;
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

    // The local machine, drawn before any exporter has answered. It is read
    // from files this container already sees, so it costs no network and is
    // never stale, and it is the column that still works on a node where
    // nothing has been deployed yet. loadPool() widens the matrix to the
    // cluster when the other nodes answer.
    renderMatrix(
      [
        {
          host: report.this_host || report.hostname,
          reachable: true,
          checks: report.checks,
        },
      ],
      report.this_host,
      report.inventory_commit,
      "local"
    );

    // The report carries the CPU reading the checks were formed from, so the
    // affinity picker gets the isolated set without a second request.
    state.isolated = (report.cpu && report.cpu.isolated) || [];
    renderAffinity();

    showBanner(report.warnings || []);
    return report;
  }

  // One row per check, one column per machine. The comparison is the point:
  // ten checks on the node the browser happens to be pointed at said nothing
  // about the other two hypervisors of the cluster, and the commonest findings
  // in a substation, a machine converged and never rebooted or one left with
  // transparent hugepages on, are exactly the ones that hide on the machine
  // nobody is looking at.
  //
  // The two loads race, and the placeholder is the one that must lose. Both
  // panels are fetched in parallel and both draw here, so a local reading that
  // came back after the exporters did used to narrow the cluster back to one
  // column, which is what an operator saw flicker. A draw is refused when the
  // matrix already shows more than the one it carries.
  function renderMatrix(nodes, thisHost, commit, scope) {
    if (scope === "local" && state.matrix && state.matrix.scope === "cluster") {
      return;
    }
    state.matrix = {
      nodes: nodes,
      thisHost: thisHost,
      commit: commit,
      scope: scope,
    };
    const columns =
      "minmax(7rem, 1.3fr) repeat(" + nodes.length + ", minmax(5rem, 1fr))";

    const head = element("check-head");
    head.replaceChildren();
    head.style.gridTemplateColumns = columns;
    head.append(cell("span", "Check"));
    nodes.forEach((node) => head.append(nodeHeading(node, thisHost)));

    // Every check the nodes answered, in the order they were run. A node that
    // published nothing contributes no row of its own, so the list is what the
    // machines that did answer have to say.
    const ids = [];
    nodes.forEach((node) =>
      (node.checks || []).forEach((check) => {
        if (!ids.some((known) => known.id === check.id)) {
          ids.push({ id: check.id, title: check.title });
        }
      })
    );

    const rows = element("check-rows");
    rows.replaceChildren();
    ids.forEach((check) =>
      rows.append(renderRow(check, nodes, columns))
    );

    const wanting = nodes.reduce(
      (total, node) =>
        total +
        (node.checks || []).filter((check) => check.status === "warning")
          .length,
      0
    );
    const answering = nodes.filter((node) => (node.checks || []).length).length;
    const statuses = nodes.reduce(
      (all, node) => all.concat((node.checks || []).map((check) => check.status)),
      []
    );
    summarise(
      "checks",
      !statuses.length
        ? "absent"
        : wanting
          ? "warning"
          : statuses.includes("ok")
            ? "ok"
            : "unknown",
      wanting
        ? wanting + " worth a look on " + machines(answering)
        : "nothing worth a look on " + machines(answering)
    );
    // Which inventory the comparison was made against belongs to the panel
    // rather than to the tab: it is read once, when an operator wonders why a
    // machine is said to differ from something.
    element("checks-lead").textContent = commit
      ? "Compared with the inventory at " + commit.slice(0, 8) + "."
      : "";
  }

  function machines(count) {
    return count + (count > 1 ? " machines" : " machine");
  }

  function nodeHeading(node, thisHost) {
    const box = document.createElement("span");
    box.className = "check-node";
    // The name in its own element rather than as a bare text node, so the
    // heading can stack it above the tag instead of running the two together
    // into one wrapping line.
    const name = document.createElement("span");
    name.className = "check-host";
    name.textContent = node.host;
    box.append(name);
    if (node.host === thisHost) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "this node";
      box.append(tag);
    }
    return box;
  }

  function cell(tag, text) {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  }

  // A row is one check across the cluster, and it opens to what each machine
  // answered. The detail stays behind a click: ten checks times three nodes
  // with their reasoning on screen is a page an operator scrolls, and scrolling
  // is what this layout exists to avoid.
  function renderRow(check, nodes, columns) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "check-row";
    row.style.gridTemplateColumns = columns;

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = check.title;
    row.append(name);

    const answers = nodes.map((node) => answerOf(node, check.id));
    answers.forEach((answer) => row.append(renderAnswer(answer)));

    // The one thing a matrix says that a list could not: the machines
    // disagree. Two nodes converged from the same inventory answering
    // differently is the finding, whatever the answers are.
    if (
      answers.filter((answer) => answer.check).length > 1 &&
      new Set(
        answers
          .filter((answer) => answer.check)
          .map((answer) => answer.check.observed)
      ).size > 1
    ) {
      row.classList.add("uneven");
    }

    const detail = document.createElement("div");
    detail.className = "check-detail";
    detail.hidden = true;
    nodes.forEach((node, index) =>
      detail.append(detailLine(node, answers[index]))
    );
    row.append(detail);
    row.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
    });
    return row;
  }

  function answerOf(node, id) {
    const check = (node.checks || []).find((entry) => entry.id === id);
    if (check) {
      return { node: node, check: check };
    }
    // Why this machine has nothing to say, which is never a failed check. An
    // unreachable node, and a node whose collector predates the tuning block,
    // are two different faults fixed by two different acts.
    return {
      node: node,
      check: null,
      absence:
        node.error ||
        node.tuning_error ||
        "This machine published nothing for this check.",
    };
  }

  function renderAnswer(answer) {
    const box = document.createElement("span");
    box.className = "check-cell";

    const dot = document.createElement("span");
    dot.className = "dot status-" + (answer.check ? answer.check.status : "absent");
    box.append(dot);

    const value = document.createElement("span");
    value.className = "value";
    if (answer.check) {
      value.textContent = answer.check.observed;
      if (
        answer.check.declared &&
        answer.check.declared !== answer.check.observed
      ) {
        value.classList.add("differs");
      }
    } else {
      value.classList.add("none");
      value.textContent = "\u2013";
    }
    box.append(value);
    return box;
  }

  function detailLine(node, answer) {
    const line = document.createElement("p");
    line.className = "detail-line";

    const who = document.createElement("span");
    who.className = "detail-host";
    who.textContent = node.host;
    line.append(who, " ");

    if (!answer.check) {
      line.append(answer.absence);
      return line;
    }
    const check = answer.check;
    const declared =
      check.declared !== null && check.declared !== undefined
        ? " The inventory asks for " + check.declared + "."
        : "";
    line.append(
      check.observed +
        "." +
        declared +
        " " +
        (check.detail || defaultDetail(check))
    );
    return line;
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

    // The same reading answers both panels, so the cluster is asked once. Each
    // node's exporter carries its pool and its tuning in one exposition, and
    // fetching it twice would double what a page refresh costs a substation
    // hypervisor for nothing.
    element("checks-loading").hidden = true;
    element("checks").hidden = false;
    renderMatrix(pool.nodes, pool.this_host, pool.inventory_commit, "cluster");

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
      summarise(
        "pool",
        "absent",
        pool.nodes.length
          ? "no node published a pool"
          : "no inventory yet, so no node to ask"
      );
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
    // A machine worth opening the panel for: one isolating something other
    // than what the inventory declares, one running an actor on a housekeeping
    // core, or one whose kernel is not the real time one.
    const troubled = reachable.filter(
      (node) =>
        node.isolation_matches === false ||
        node.hard_fallbacks ||
        node.soft_fallbacks ||
        (node.slot_warnings || []).length ||
        (node.preemption && node.preemption !== "PREEMPT_RT")
    ).length;
    summarise(
      "pool",
      troubled
        ? "warning"
        : reachable.length < pool.nodes.length
          ? "unknown"
          : "ok",
      (troubled ? machines(troubled) + " worth a look, " : "") +
        reachable.length +
        " of " +
        pool.nodes.length +
        " nodes answered" +
        (stale.length
          ? ", " + Math.round(Math.max.apply(null, stale)) + "s ago"
          : "")
    );
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

    // The conformance this page can ask of a machine it cannot read. The
    // isolated set comes from the node's own exporter and the inventory holds
    // what it was told, so the commonest finding in a cluster, one machine
    // converged and never rebooted, is caught on every node rather than only
    // on the one the browser is pointed at.
    const status = document.createElement("p");
    status.className = "pool-status";
    if (node.isolation_matches === true) {
      status.textContent = "Isolating " + node.observed_isolcpus + ", as declared";
    } else if (node.isolation_matches === false) {
      status.classList.add("differs");
      status.textContent =
        "Isolating " +
        (node.observed_isolcpus || "nothing") +
        ", the inventory declares " +
        node.declared_isolcpus +
        ". isolcpus is read at boot, so a convergence not followed by a reboot " +
        "looks exactly like this.";
    } else if (node.observed_isolcpus) {
      status.textContent =
        "Isolating " + node.observed_isolcpus + ", nothing declared";
    }
    if (status.textContent) {
      if (node.preemption && node.preemption !== "PREEMPT_RT") {
        status.classList.add("differs");
        status.textContent += " Kernel is " + node.preemption + ".";
      }
      box.append(status);
    }

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
    // The label where it fits, the number otherwise. A core carrying a guest is
    // read by the guest's name; a free one is read by its number, which is what
    // an operator types into isolcpus.
    cell.textContent = slot.label || String(slot.cpu);
    // A `title` gave one run-on line, and the interesting cores are exactly the
    // ones with the most to say: a slot holds several actors, each with its own
    // scheduler and priority, and that is a table rather than a sentence.
    cell.addEventListener("mouseenter", () => showTip(cell, slot, meaning));
    cell.addEventListener("mouseleave", hideTip);
    cell.addEventListener("focus", () => showTip(cell, slot, meaning));
    cell.addEventListener("blur", hideTip);
    cell.tabIndex = 0;
    return cell;
  }

  function tipRow(label, value) {
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = value;
    return [term, definition];
  }

  // What seapath-alloc knows about one core, laid out. The members of a slot
  // are the point: a core shared between a vCPU at FIFO/90 and an IRQ thread at
  // FIFO/50 is a colocation an operator has to be able to read, and the flat
  // string the exporter joins them into is not readable.
  function showTip(cell, slot, meaning) {
    const tip = element("pool-tip");
    const list = document.createElement("dl");
    list.append(...tipRow("CPU", String(slot.cpu)));
    list.append(...tipRow("State", meaning));
    if (slot.core !== null && slot.core !== undefined) {
      list.append(
        ...tipRow(
          "Core",
          slot.core +
            (slot.sibling === null || slot.sibling === undefined
              ? ""
              : ", sibling CPU " + slot.sibling)
        )
      );
    }
    if (slot.label && slot.state !== "reserved") {
      list.append(...tipRow(slot.slot ? "Slot" : "Actor", slot.label));
    }
    if (slot.state === "reserved" && slot.label) {
      // The exporter puts the active sibling's CPU number in `label` here,
      // which reads as a name unless it is spelled out.
      list.append(...tipRow("Idle for", "the allocation on CPU " + slot.label));
    }
    // `group` repeats the state on a slot core, where the exporter sets both
    // to "slot" and the members below carry the real answer.
    if (slot.group && slot.group !== slot.state && slot.group !== "slot") {
      list.append(...tipRow("Thread", slot.group));
    }
    if (slot.scheduler) {
      list.append(...tipRow("Scheduling", slot.scheduler + "/" + slot.priority));
    }
    // One line per member, numbered, which is how the colocation is read.
    splitMembers(slot.members).forEach((member, index) => {
      list.append(...tipRow("Member " + (index + 1), member));
    });

    tip.replaceChildren(list);
    tip.hidden = false;

    // Placed against the cell, and pulled back inside the pane when it would
    // run off the right edge.
    const box = cell.getBoundingClientRect();
    const width = tip.getBoundingClientRect().width;
    tip.style.top = box.bottom + 6 + "px";
    tip.style.left =
      Math.max(8, Math.min(box.left, window.innerWidth - width - 8)) + "px";
  }

  function hideTip() {
    element("pool-tip").hidden = true;
  }

  // The exporter joins members with ", " and each is "label/group SCHED/prio".
  // Splitting on the comma is safe because neither half may contain one.
  function splitMembers(members) {
    return (members || "")
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
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
      // The sentence that used to sit under this select is gone: the options
      // say what they do, which is why it became a select, and the line was
      // costing the chart below its own axis.
      choice.title = custom
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
    state.selected[kind] = done.length ? done[0].run_id : null;
    renderPicker(kind);
    renderMeasurement(kind);
  }

  // Every instant on this page is the browser's, because the operator reading
  // it is comparing a measurement with what they saw on a screen beside it.
  // The service stamps runs in UTC and the tracer stamps its samples in epoch
  // seconds, so both are converted here rather than shown as the machine
  // wrote them.
  function localTime(iso) {
    return new Date(iso).toLocaleString([], {
      dateStyle: "short",
      timeStyle: "short",
    });
  }

  function localClock(milliseconds) {
    return new Date(milliseconds).toLocaleTimeString([], { hour12: false });
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
        (item.started_at ? localTime(item.started_at) : "?") +
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
      // The sentence goes where the results would be, so a panel with no
      // history says why rather than showing a blank rectangle. Here rather
      // than in the loader: this is the function that empties the body, and it
      // runs right after it.
      body.textContent = spec.empty;
      summarise(kind, "absent", "never measured from this node");
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
    anchor.href = "runs?run=" + encodeURIComponent(measurement.run_id);
    anchor.textContent = "open the run";
    meta.append(anchor);
    body.append(meta);

    // Every machine the run measured, this one first. A run has no --limit, so
    // a measurement plays the whole inventory and brings back one file each,
    // and the pool view reads every node too: the page is the cluster's, and
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

    // The tab says what the measurement on screen came out as, over every
    // machine it played rather than over the local one: a run measures the
    // cluster, and the worst machine is the one that decides whether the
    // cluster meets its deadline.
    const [status, answer] =
      kind === "cyclictest"
        ? latencySummary(results)
        : interruptionSummary(results);
    summarise(kind, status, answer);
  }

  // The worst thread of the worst machine, and where it ran. No colour claims
  // a verdict: nothing in the inventory declares a latency budget, so this is
  // a number an operator holds against the deadline their application has,
  // and a green dot here would be this page inventing a threshold.
  function latencySummary(results) {
    let worst = null;
    results.forEach((result) =>
      (result.threads || []).forEach((thread) => {
        if (
          thread.max_us !== null &&
          (worst === null || thread.max_us > worst.max)
        ) {
          worst = { max: thread.max_us, host: result.host };
        }
      })
    );
    return worst
      ? ["info", "worst " + worst.max + "us on " + worst.host]
      : ["unknown", "no thread reported a latency"];
  }

  // hwlatdetect does have a verdict, because the threshold is the operator's
  // own: anything above it is time the kernel never saw.
  function interruptionSummary(results) {
    const measured = results.filter(
      (result) => result.supported && !result.message
    );
    if (!measured.length) {
      return ["unknown", "the kernel could not be asked"];
    }
    let worst = 0;
    let gaps = 0;
    measured.forEach((result) =>
      result.interruptions.forEach((item) => {
        gaps += 1;
        worst = Math.max(worst, item.inner_us, item.outer_us);
      })
    );
    return gaps
      ? [
          "warning",
          gaps +
            " interruption" +
            (gaps > 1 ? "s" : "") +
            " above the threshold, the worst " +
            worst +
            "us",
        ]
      : ["ok", "nothing above the threshold on " + machines(measured.length)];
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
          : localClock(item.timestamp * 1000),
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
          cell.style.color = rampColour(index);
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

    // Grouped, never stacked and never overlaid. Each bucket is divided
    // between the threads and they stand side by side, which is what rtperfui
    // did and what makes the chart answer the question it is drawn for: not
    // "what is the distribution" but "is one CPU worse than the others".
    // Overlaying them hid exactly that, since threads usually agree and the
    // colours piled into one mass.
    const slotWidth = (width - left - right) / (lastUsed + 1);
    const perThread = slotWidth / counts.length;
    // Never below a hairline. On a machine whose worst case is far out, a
    // bucket is a fraction of a unit wide, and the single sample in the tail is
    // the one an operator came to see.
    const barWidth = Math.max(perThread, 0.6);

    counts.forEach((series, index) => {
      const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
      group.setAttribute("fill", rampColour(index));
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
        rect.setAttribute("x", x(buckets[bucket]) + index * perThread);
        rect.setAttribute("y", top);
        rect.setAttribute("width", barWidth);
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
      swatch.style.background = rampColour(index);
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
          "runs?run=" + encodeURIComponent(started.run_id)
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

  // The four views and the panel each one shows. Both measurements live in the
  // same card, which is why two views land on it: they share a form area and a
  // history picker, and an operator switching between them is comparing two
  // readings of the same machines rather than opening a different page.
  const VIEWS = {
    checks: { card: "card-checks" },
    pool: { card: "card-map" },
    cyclictest: { card: "card-measure", panel: "panel-cyclictest" },
    hwlatdetect: { card: "card-measure", panel: "panel-hwlatdetect" },
  };

  // Switching is local. Everything on this page is loaded before the first tab
  // is drawn, so a view is a show and a hide rather than a fetch: no reading
  // is asked of a substation hypervisor twice because an operator looked at
  // the pool and came back.
  function showView(name) {
    const view = VIEWS[name];
    document.querySelectorAll("#views .view").forEach((tab) => {
      const current = tab.dataset.view === name;
      tab.classList.toggle("current", current);
      // Said rather than only drawn: the bar is this page's navigation, and a
      // border is not something a screen reader announces.
      if (current) {
        tab.setAttribute("aria-current", "true");
      } else {
        tab.removeAttribute("aria-current");
      }
    });
    Object.values(VIEWS).forEach((entry) => {
      element(entry.card).hidden = entry.card !== view.card;
    });
    if (view.panel) {
      Object.keys(MEASUREMENTS).forEach((kind) => {
        element("panel-" + kind).hidden = kind !== name;
      });
      element("measure-note").textContent = MEASUREMENTS[name].note;
    }
  }

  document.querySelectorAll("#views .view").forEach((tab) => {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  });

  document.addEventListener("keydown", (event) => {
    // A confirmation names the machines a run is about to load at real time
    // priority. Escape is the way out of it that does not involve aiming at a
    // button.
    if (event.key === "Escape" && !element("measure-confirm").hidden) {
      element("measure-confirm").hidden = true;
    }
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
    // Conformance first. It is the question the page exists to answer, and the
    // only panel that says something on a machine where nothing has been
    // deployed yet.
    showView("checks");
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

  // The palette changed under the page. Everything else on it is a rule away
  // from the right colour already; the histogram, its legend and the per
  // thread table are painted with the ramp, so they are redrawn. Both
  // measurements are already in hand, so this is local and there is no fetch.
  window.addEventListener(Theme.EVENT, () => {
    Object.keys(MEASUREMENTS).forEach((kind) => {
      if (state.selected[kind]) {
        renderMeasurement(kind);
      }
    });
  });

  start().catch((failure) => {
    showBanner([failure.message]);
    // Every spinner, so a page that failed halfway does not sit there looking
    // like a page still loading.
    ["checks-loading", "latency-loading", "hwlat-loading"].forEach((id) => {
      element(id).hidden = true;
    });
  });
})();
