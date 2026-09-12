// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The node view. It reads, it never writes: the only request that is not a GET
// is signing out. Configuration arrives at M1, as inventory edits and runs.
//
// What the machine *is*, which is what the inventory form is filled from. What
// it is *doing* is in Grafana, off the node exporter every node runs.

(function () {
  const REFRESH_MS = 5000;
  // The warnings of the poll in progress, and only of that one. This used to
  // be a set that lived as long as the page, so a mount that was repaired an
  // hour and three service restarts ago went on being reported until somebody
  // reloaded the page by hand. A banner that cannot go back down is a banner
  // an operator learns to stop reading.
  let warnings = new Set();

  function text(value, fallback) {
    if (value === null || value === undefined || value === "") {
      return fallback || "unknown";
    }
    return String(value);
  }

  function duration(seconds) {
    if (seconds === null || seconds === undefined) {
      return "unknown";
    }
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0) {
      return days + "d " + hours + "h " + minutes + "m";
    }
    return hours + "h " + minutes + "m";
  }

  function size(bytes) {
    if (!bytes) {
      return "unknown";
    }
    const units = ["B", "kB", "MB", "GB", "TB"];
    let value = bytes;
    let index = 0;
    while (value >= 1000 && index < units.length - 1) {
      value /= 1000;
      index += 1;
    }
    return value.toFixed(index === 0 ? 0 : 1) + " " + units[index];
  }

  function fillList(element, pairs) {
    element.replaceChildren();
    pairs.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = value;
      element.append(term, definition);
    });
  }

  function row(cells) {
    const line = document.createElement("tr");
    cells.forEach((cell) => {
      const element = document.createElement("td");
      if (cell instanceof Node) {
        element.append(cell);
      } else {
        element.textContent = cell;
      }
      line.append(element);
    });
    return line;
  }

  // Readings say what they could not read. Hiding that would let "unknown"
  // pass for "nothing wrong", which on a substation hypervisor is the wrong
  // failure mode.
  function collectWarnings(reading) {
    (reading.warnings || []).forEach((warning) => warnings.add(warning));
    const banner = document.getElementById("banner");
    if (warnings.size === 0) {
      banner.hidden = true;
      return;
    }
    banner.replaceChildren();
    warnings.forEach((warning) => {
      const line = document.createElement("div");
      line.textContent = warning;
      banner.append(line);
    });
    banner.hidden = false;
  }

  async function loadSummary(pending) {
    const node = await (pending || API.get("/node"));
    collectWarnings(node);
    // The bar arrived with the document, and this page is where a rename or a
    // cluster join is watched landing: the reading it takes on its own timer is
    // what keeps the two strings up there true, at no cost.
    Chrome.saw(node);

    fillList(document.getElementById("summary"), [
      ["Hostname", text(node.hostname)],
      ["Mode", text(node.mode)],
      ["Distribution", text(node.distribution)],
      ["Kernel", text(node.kernel_release)],
      ["Uptime", duration(node.uptime_seconds)],
      ["SEAPATH collection", text(node.collection_version)],
      ["Applied inventory commit", text(node.inventory_commit, "none yet")],
    ]);
  }

  async function loadCpu(pending) {
    const cpu = await (pending || API.get("/node/cpu"));
    collectWarnings(cpu);
    fillList(document.getElementById("cpu-summary"), [
      ["Model", text(cpu.model)],
      ["CPUs", text(cpu.online)],
      [
        "Isolated",
        cpu.isolated.length
          ? cpu.isolated.join(",") + " (" + text(cpu.isolated_source) + ")"
          : "none",
      ],
      ["Housekeeping", cpu.housekeeping.join(",") || "unknown"],
      [
        "Load average",
        cpu.load_average ? cpu.load_average.join("  ") : "unknown",
      ],
    ]);

    const grid = document.getElementById("cpu-grid");
    grid.replaceChildren();
    cpu.topology.forEach((entry) => {
      const cell = document.createElement("div");
      cell.className = "cpu " + (entry.isolated ? "isolated" : "housekeeping");
      const busy = entry.busy_percent;
      cell.title =
        "CPU " +
        entry.cpu +
        (busy === null || busy === undefined ? "" : " - " + busy + "% busy");
      cell.textContent = entry.cpu;
      if (busy !== null && busy !== undefined) {
        cell.style.setProperty("--busy", busy + "%");
      }
      grid.append(cell);
    });
  }

  async function loadNetwork(pending) {
    const network = await (pending || API.get("/node/network"));
    collectWarnings(network);
    const body = document.querySelector("#network-table tbody");
    body.replaceChildren();
    network.interfaces.forEach((item) => {
      const name = document.createElement("span");
      name.textContent = item.name;
      if (item.name === network.default_route_interface) {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = "default route";
        name.append(" ", tag);
      }
      body.append(
        row([
          name,
          text(item.operstate),
          item.addresses
            .map((address) => address.address + "/" + address.prefix_length)
            .join(" ") || "-",
          item.speed_mbps ? item.speed_mbps + " Mb/s" : "-",
          text(item.driver, "-"),
          text(item.mac, "-"),
        ])
      );
    });
  }

  async function loadDisks(pending) {
    const disks = await (pending || API.get("/node/disks"));
    collectWarnings(disks);
    const body = document.querySelector("#disks-table tbody");
    body.replaceChildren();
    disks.devices.forEach((device) => {
      const state = document.createElement("span");
      state.className = device.claimed ? "state state-claimed" : "state state-free";
      state.textContent = device.claimed ? "in use" : "available";
      state.title = device.claim_reason || "";
      body.append(
        row([
          device.path,
          size(device.size_bytes),
          text(device.model, "-"),
          text(device.by_path, "unknown"),
          state,
        ])
      );
    });
  }

  const KEPT = "node";

  // The four readings this page is made of. `answers` is a promise per card,
  // which is how the same code draws a reading in flight and one this browser
  // kept: a kept answer is handed over already resolved.
  async function draw(answers) {
    await Promise.all([
      loadSummary(answers.node),
      loadCpu(answers.cpu),
      loadNetwork(answers.network),
      loadDisks(answers.disks),
    ]);
  }

  async function refresh() {
    // Before the requests, not after: the readings come back one by one and
    // each renders the banner as it lands, so the cycle they belong to has to
    // be open when the first one arrives.
    warnings = new Set();
    const pending = {
      node: API.started("/node"),
      cpu: API.started("/node/cpu"),
      network: API.started("/node/network"),
      disks: API.started("/node/disks"),
    };
    try {
      const answers = {};
      await draw(
        Object.fromEntries(
          Object.entries(pending).map(([name, request]) => [
            name,
            request.then((payload) => {
              answers[name] = payload;
              return payload;
            }),
          ])
        )
      );
      document.getElementById("reading").hidden = true;
      Kept.keep(KEPT, answers);
      // After the readings, with the role the document arrived carrying: what
      // the console button offers depends on who is looking at it.
      await Console.describe(Chrome.current());
    } catch (failure) {
      if (failure.status === 401) {
        window.location.assign("login");
        return;
      }
      document.getElementById("reading").hidden = true;
      warnings.add(failure.message);
      collectWarnings({});
    }
  }

  // This machine as this browser last read it, drawn before anything is asked.
  // Nothing is held: the one control on this page opens a shell on this node,
  // which is not an act aimed at any row of a reading. What is on screen says
  // its age until the reading lands, and the reading is five seconds away at
  // most, because this page reads itself on a timer.
  const kept = Kept.held(KEPT);
  if (kept) {
    try {
      draw(
        Object.fromEntries(
          Object.entries(kept.payload).map(([name, payload]) => [
            name,
            Promise.resolve(payload),
          ])
        )
      );
      Kept.rereading(["reading"], Date.now() - kept.at);
    } catch (failure) {
      Kept.forget(KEPT);
    }
  }

  refresh();
  window.setInterval(refresh, REFRESH_MS);
})();
