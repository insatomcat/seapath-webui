// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The Usage page: what each machine consumes, and which workload consumes it.
//
// The service answers counters, in seconds and bytes since some start, and the
// moment each was read. Everything per second on this page is the difference
// between two of its answers over the time between them, so this script is
// where the first answer of each pair is remembered. It keeps five minutes of
// them, for the charts, and forgets them when the page is left. See D67.
//
// One reading asks every machine at once, since the tab of each machine says
// what it is doing, and draws the machine the bar points at.

(function () {
  // Five seconds: a figure per second over a longer interval hides the burst
  // an operator opened the page to see, and a shorter one asks node_exporter
  // to walk /proc and /sys on every machine more often than Prometheus does.
  const PERIOD_MS = 5000;
  // What the charts show, and what this browser keeps.
  const WINDOW_MS = 5 * 60 * 1000;
  // A pair of readings further apart than this spans a pause or a hidden tab,
  // and the chart leaves a gap rather than drawing one flat average across it.
  const GAP_MS = PERIOD_MS * 3;
  // How many workloads get a colour of their own on a machine. The rest are
  // one band, "Other workloads", on the charts and a grey swatch in the table.
  const SLOTS = 6;
  const SVG = "http://www.w3.org/2000/svg";

  // Per machine: the answers read, oldest first, each with the time this
  // browser received it, which is what the charts are laid out against.
  const readings = new Map();
  // Per machine: which workload holds which colour. Assigned once and kept,
  // so a workload keeps its colour when others come and go.
  const slots = new Map();

  let selected = null;
  let latest = null;
  let timer = null;
  let inFlight = false;
  let paused = false;

  function element(id) {
    return document.getElementById(id);
  }

  function showBanner(message) {
    const banner = element("banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  // Formatting

  // Binary units, the ones `free` and `df -h` print, so a figure here can be
  // held against the one an operator reads in a shell.
  function bytes(value) {
    if (value === null || value === undefined) {
      return "–";
    }
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let index = 0;
    let scaled = Math.abs(value);
    while (scaled >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    const digits = scaled >= 100 || index === 0 ? 0 : 1;
    return `${scaled.toFixed(digits)} ${units[index]}`;
  }

  function perSecond(value) {
    return value === null || value === undefined ? "–" : `${bytes(value)}/s`;
  }

  function cores(value) {
    if (value === null || value === undefined) {
      return "–";
    }
    const digits = value >= 10 ? 1 : 2;
    return `${value.toFixed(digits)} CPU${value === 1 ? "" : "s"}`;
  }

  function percent(ratio) {
    if (ratio === null || ratio === undefined || !isFinite(ratio)) {
      return "–";
    }
    return `${(ratio * 100).toFixed(ratio < 0.1 ? 1 : 0)}%`;
  }

  function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
  }

  // Rates

  // Counts per second between two readings of one counter. None when either
  // reading lacks it, or when it went down: a counter that goes down was
  // reset, by a reboot or a guest started again, and the interval holds
  // nothing that can be divided.
  function rate(before, after, seconds) {
    if (
      before === null ||
      before === undefined ||
      after === null ||
      after === undefined ||
      !(seconds > 0) ||
      after < before
    ) {
      return null;
    }
    return (after - before) / seconds;
  }

  function sum(values) {
    let total = null;
    values.forEach((value) => {
      if (value !== null && value !== undefined) {
        total = (total || 0) + value;
      }
    });
    return total;
  }

  function workloadKey(kind, name) {
    return `${kind}:${name}`;
  }

  // Every running workload of one answer, by key, with its counters.
  function workloadsOf(machine) {
    const found = new Map();
    machine.guests.forEach((guest) => {
      if (guest.running) {
        found.set(workloadKey("vm", guest.name), {
          kind: "vm",
          name: guest.name,
          item: guest,
          at: machine.guests_read_at,
        });
      }
    });
    machine.containers.forEach((container) => {
      if (container.running) {
        found.set(workloadKey("ct", container.name), {
          kind: "ct",
          name: container.name,
          item: container,
          at: machine.containers_read_at,
        });
      }
    });
    return found;
  }

  // What one answer says on its own, and what it says against the one before
  // it. The memory is a gauge and needs no pair; everything per second does.
  function point(previous, current) {
    const node = current.machine.node;
    const result = {
      x: current.received,
      memory: null,
      cpu: null,
      disk: null,
      network: null,
      workloads: new Map(),
    };
    const now = workloadsOf(current.machine);
    now.forEach((entry, key) => {
      result.workloads.set(key, {
        kind: entry.kind,
        name: entry.name,
        memory: entry.item.memory_bytes,
        vcpus: entry.item.vcpus === undefined ? null : entry.item.vcpus,
        cpu: null,
        diskRead: null,
        diskWrite: null,
        netIn: null,
        netOut: null,
      });
    });
    if (node) {
      const memory = node.memory;
      if (memory.total_bytes && memory.available_bytes !== null) {
        result.memory = {
          total: memory.total_bytes,
          used: memory.total_bytes - memory.available_bytes,
          reserved: memory.hugepages_total_bytes || 0,
        };
      }
    }

    const before = previous && previous.machine;
    if (!before || current.received - previous.received > GAP_MS) {
      return result;
    }
    const was = workloadsOf(before);
    now.forEach((entry, key) => {
      const old = was.get(key);
      if (!old) {
        return;
      }
      const seconds = entry.at - old.at;
      const into = result.workloads.get(key);
      into.cpu = rate(old.item.cpu_seconds, entry.item.cpu_seconds, seconds);
      into.diskRead = rate(
        old.item.disk_read_bytes,
        entry.item.disk_read_bytes,
        seconds
      );
      into.diskWrite = rate(
        old.item.disk_written_bytes,
        entry.item.disk_written_bytes,
        seconds
      );
      into.netIn = rate(
        old.item.network_receive_bytes,
        entry.item.network_receive_bytes,
        seconds
      );
      into.netOut = rate(
        old.item.network_transmit_bytes,
        entry.item.network_transmit_bytes,
        seconds
      );
    });

    const oldNode = before.node;
    if (!node || !oldNode) {
      return result;
    }
    const seconds = node.read_at - oldNode.read_at;
    const busy = (side) => {
      const total = rate(oldNode[side].total_seconds, node[side].total_seconds, seconds);
      const idle = rate(oldNode[side].idle_seconds, node[side].idle_seconds, seconds);
      if (total === null || idle === null || node[side].cpus === 0) {
        return null;
      }
      return Math.max(total - idle, 0);
    };
    const housekeeping = busy("housekeeping");
    const isolated = busy("isolated");
    if (housekeeping !== null) {
      result.cpu = {
        cpus: node.housekeeping.cpus + node.isolated.cpus,
        housekeepingCpus: node.housekeeping.cpus,
        isolatedCpus: node.isolated.cpus,
        housekeeping,
        isolated: isolated || 0,
        busy: housekeeping + (isolated || 0),
      };
    }

    const disks = new Map(oldNode.disks.map((disk) => [disk.device, disk]));
    const perDisk = node.disks.map((disk) => {
      const old = disks.get(disk.device);
      return {
        device: disk.device,
        read: old ? rate(old.read_bytes, disk.read_bytes, seconds) : null,
        write: old ? rate(old.written_bytes, disk.written_bytes, seconds) : null,
        busy: old ? rate(old.io_time_seconds, disk.io_time_seconds, seconds) : null,
      };
    });
    result.disk = {
      devices: perDisk,
      read: sum(perDisk.map((disk) => disk.read)),
      write: sum(perDisk.map((disk) => disk.write)),
    };

    const links = new Map(
      oldNode.interfaces.map((link) => [link.device, link])
    );
    const perLink = node.interfaces.map((link) => {
      const old = links.get(link.device);
      return {
        device: link.device,
        rx: old ? rate(old.receive_bytes, link.receive_bytes, seconds) : null,
        tx: old ? rate(old.transmit_bytes, link.transmit_bytes, seconds) : null,
      };
    });
    const physical = new Set(
      node.interfaces
        .filter((link) => link.kind === "physical")
        .map((link) => link.device)
    );
    result.network = {
      links: perLink,
      rx: sum(perLink.filter((l) => physical.has(l.device)).map((l) => l.rx)),
      tx: sum(perLink.filter((l) => physical.has(l.device)).map((l) => l.tx)),
    };
    return result;
  }

  function pointsOf(host) {
    const list = readings.get(host) || [];
    return list.map((current, index) => point(list[index - 1], current));
  }

  // Colours

  // A workload gets a colour when it is among the heaviest on its machine by
  // CPU or by memory and a colour is free, and keeps it for as long as the
  // page is open. Handing colours out by rank at every reading would repaint
  // the whole chart each time two guests swapped places.
  function assignSlots(host, latestPoint) {
    if (!slots.has(host)) {
      slots.set(host, new Map());
    }
    const held = slots.get(host);
    if (held.size >= SLOTS || !latestPoint) {
      return held;
    }
    const cpus = latestPoint.cpu ? latestPoint.cpu.cpus : 0;
    const memory = latestPoint.memory ? latestPoint.memory.total : 0;
    const ranked = Array.from(latestPoint.workloads.entries())
      .map(([key, load]) => ({
        key,
        score: Math.max(
          cpus && load.cpu ? load.cpu / cpus : 0,
          memory && load.memory ? load.memory / memory : 0
        ),
      }))
      .filter((entry) => entry.score > 0 && !held.has(entry.key))
      .sort((a, b) => b.score - a.score);
    const used = new Set(held.values());
    ranked.forEach((entry) => {
      if (held.size >= SLOTS) {
        return;
      }
      let slot = 1;
      while (used.has(slot)) {
        slot += 1;
      }
      held.set(entry.key, slot);
      used.add(slot);
    });
    return held;
  }

  // Charts

  function svgNode(name, attributes) {
    const node = document.createElementNS(SVG, name);
    Object.entries(attributes || {}).forEach(([key, value]) => {
      node.setAttribute(key, value);
    });
    return node;
  }

  // The top of an axis: 1, 2 or 5 times a power of ten, the first at or over
  // the largest value, so the ticks fall on numbers a reader can add up.
  function ceiling(value, step) {
    if (!(value > 0)) {
      return step || 1;
    }
    if (step) {
      // Bytes: powers of 1024 times 1, 2, 5, so the ticks read 256 MiB/s
      // rather than 268.4 MB/s.
      let unit = 1;
      while (value / unit >= 1024) {
        unit *= 1024;
      }
      const scaled = value / unit;
      const nice = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1024].find(
        (candidate) => candidate >= scaled
      );
      return nice * unit;
    }
    const power = Math.pow(10, Math.floor(Math.log10(value)));
    const nice = [1, 2, 5, 10].find((candidate) => candidate * power >= value);
    return nice * power;
  }

  const WIDTH = 600;
  const HEIGHT = 170;
  const LEFT = 58;
  const RIGHT = 8;
  const TOP = 8;
  const BOTTOM = 20;

  // One chart's frame: the axis at 0, a hairline at the top of the scale and
  // one halfway, their values, and the time along the bottom.
  function frame(svg, top, format, now) {
    const base = HEIGHT - BOTTOM;
    [0, 0.5, 1].forEach((share) => {
      const y = base - share * (base - TOP);
      svg.append(
        svgNode("line", {
          class: share === 0 ? "axis" : "grid",
          x1: LEFT,
          x2: WIDTH - RIGHT,
          y1: y,
          y2: y,
        })
      );
      const label = svgNode("text", {
        class: "tick",
        x: LEFT - 6,
        y: y + 3,
        "text-anchor": "end",
      });
      label.textContent = format(top * share);
      svg.append(label);
    });
    [
      [0, "5 min ago", "start"],
      [0.5, "2.5 min", "middle"],
      [1, "now", "end"],
    ].forEach(([share, words, anchor]) => {
      const label = svgNode("text", {
        class: "tick",
        x: LEFT + share * (WIDTH - LEFT - RIGHT),
        y: HEIGHT - 5,
        "text-anchor": anchor,
      });
      label.textContent = words;
      svg.append(label);
    });
    const x = (time) =>
      LEFT + ((time - (now - WINDOW_MS)) / WINDOW_MS) * (WIDTH - LEFT - RIGHT);
    const y = (value) => base - (Math.min(value, top) / top) * (base - TOP);
    return { x, y, base };
  }

  // Runs of consecutive points that have a value, so a gap in the readings
  // is a gap on the chart.
  function runs(points, has) {
    const found = [];
    let current = [];
    points.forEach((item, index) => {
      const previous = points[index - 1];
      const broken = previous && item.x - previous.x > GAP_MS;
      if (!has(item) || broken) {
        if (current.length) {
          found.push(current);
        }
        current = [];
      }
      if (has(item)) {
        current.push(item);
      }
    });
    if (current.length) {
      found.push(current);
    }
    return found;
  }

  // Areas stacked from the axis up, one per band, in the order given. Each
  // band is its own path, so a band is the hover target and the gap between
  // two bands is the surface showing through.
  function stacked(plot, options) {
    const { points, bands, top, format, now, has } = options;
    const svg = svgNode("svg", {
      class: "chart usage-svg",
      viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
      role: "img",
      "aria-label": options.label,
    });
    const scale = frame(svg, top, format, now);
    runs(points, has).forEach((run) => {
      const floor = run.map(() => 0);
      bands.forEach((band) => {
        const values = run.map((item) => Math.max(band.value(item) || 0, 0));
        if (!values.some((value) => value > 0)) {
          values.forEach((value, index) => {
            floor[index] += value;
          });
          return;
        }
        const upper = run.map(
          (item, index) =>
            `${scale.x(item.x).toFixed(1)},${scale.y(floor[index] + values[index]).toFixed(1)}`
        );
        const lower = run
          .map(
            (item, index) =>
              `${scale.x(item.x).toFixed(1)},${scale.y(floor[index]).toFixed(1)}`
          )
          .reverse();
        values.forEach((value, index) => {
          floor[index] += value;
        });
        if (run.length === 1) {
          // One reading is a moment rather than a span: a thin bar where it
          // fell, so a page that has just opened shows something.
          const x = scale.x(run[0].x);
          upper.push(`${(x + 2).toFixed(1)},${upper[0].split(",")[1]}`);
          lower.unshift(`${(x + 2).toFixed(1)},${lower[0].split(",")[1]}`);
        }
        svg.append(
          svgNode("polygon", {
            class: `usage-band ${band.className}`,
            points: upper.concat(lower).join(" "),
          })
        );
      });
    });
    plot.replaceChildren(svg);
    hover(plot, svg, scale, points, (item) =>
      bands
        .slice()
        .reverse()
        .filter((band) => (band.value(item) || 0) > 0)
        .map((band) => [band.className, band.label, format(band.value(item))])
    );
  }

  // Lines for two series in one unit, the second one dashed, so the pair is
  // told apart without its colour. Each is named at its end.
  function lines(plot, options) {
    const { points, series, format, now } = options;
    let peak = 0;
    points.forEach((item) => {
      series.forEach((line) => {
        peak = Math.max(peak, line.value(item) || 0);
      });
    });
    const top = ceiling(peak, 1024);
    const svg = svgNode("svg", {
      class: "chart usage-svg",
      viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
      role: "img",
      "aria-label": options.label,
    });
    const scale = frame(svg, top, format, now);
    series.forEach((line) => {
      runs(points, (item) => line.value(item) !== null).forEach((run) => {
        const path = run
          .map(
            (item) =>
              `${scale.x(item.x).toFixed(1)},${scale.y(line.value(item)).toFixed(1)}`
          )
          .join(" ");
        svg.append(
          svgNode("polyline", {
            class: `usage-line ${line.className}`,
            points: path,
          })
        );
      });
    });
    plot.replaceChildren(svg);
    hover(plot, svg, scale, points, (item) =>
      series.map((line) => [line.className, line.label, format(line.value(item))])
    );
    return top;
  }

  // The crosshair: the reading nearest the pointer, and every band or line at
  // that moment in one box, so no value needs aiming at.
  function hover(plot, svg, scale, points, describe) {
    const cross = svgNode("line", {
      class: "usage-cross",
      y1: TOP,
      y2: scale.base,
      x1: 0,
      x2: 0,
    });
    cross.style.visibility = "hidden";
    svg.append(cross);
    const tip = document.createElement("div");
    tip.className = "usage-tip";
    tip.hidden = true;
    plot.append(tip);

    function hide() {
      cross.style.visibility = "hidden";
      tip.hidden = true;
    }

    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("pointermove", (event) => {
      const box = svg.getBoundingClientRect();
      const at = ((event.clientX - box.left) / box.width) * WIDTH;
      let nearest = null;
      points.forEach((item) => {
        const distance = Math.abs(scale.x(item.x) - at);
        if (nearest === null || distance < nearest.distance) {
          nearest = { item, distance };
        }
      });
      if (!nearest || nearest.distance > 30) {
        hide();
        return;
      }
      const rows = describe(nearest.item);
      if (!rows.length) {
        hide();
        return;
      }
      const x = scale.x(nearest.item.x);
      cross.setAttribute("x1", x);
      cross.setAttribute("x2", x);
      cross.style.visibility = "visible";

      const when = document.createElement("div");
      when.className = "usage-tip-time";
      when.textContent = new Date(nearest.item.x).toLocaleTimeString();
      const list = rows.map(([className, label, value]) => {
        const line = document.createElement("div");
        line.className = "usage-tip-row";
        const swatch = document.createElement("i");
        swatch.className = `usage-swatch ${className}`;
        const name = document.createElement("span");
        name.textContent = label;
        const figure = document.createElement("strong");
        figure.textContent = value;
        line.append(swatch, name, figure);
        return line;
      });
      tip.replaceChildren(when, ...list);
      tip.hidden = false;
      const left = (x / WIDTH) * box.width;
      tip.style.left = `${left}px`;
      tip.classList.toggle("flip", left > box.width / 2);
    });
  }

  // The bands for the two charts broken down by workload, in the order they
  // are stacked: the coloured workloads by colour, then the rest of them,
  // then what the machine uses itself.
  function workloadBands(host, measure, machineShare) {
    const held = slots.get(host) || new Map();
    const bands = Array.from(held.entries())
      .sort((a, b) => a[1] - b[1])
      .map(([key, slot]) => ({
        key,
        className: `usage-s${slot}`,
        label: labelOf(key),
        value: (item) => {
          const load = item.workloads.get(key);
          return load ? load[measure] : null;
        },
      }));
    bands.push({
      key: "other",
      className: "usage-other",
      label: "Other workloads",
      value: (item) => {
        let total = null;
        item.workloads.forEach((load, key) => {
          if (!held.has(key) && load[measure] !== null) {
            total = (total || 0) + load[measure];
          }
        });
        return total;
      },
    });
    bands.push(...machineShare);
    return bands;
  }

  function labelOf(key) {
    const [kind, ...rest] = key.split(":");
    return `${rest.join(":")}${kind === "vm" ? " (VM)" : ""}`;
  }

  function attributed(item, measure) {
    let total = 0;
    item.workloads.forEach((load) => {
      total += load[measure] || 0;
    });
    return total;
  }

  function drawCharts(host, points, now) {
    const last = points[points.length - 1];

    // CPU, in CPUs busy. The scale is as high as the busiest moment needs
    // rather than every CPU of the machine: on a hypervisor of forty eight
    // CPUs, three busy ones are a line along the axis at full scale.
    let peak = 0;
    points.forEach((item) => {
      if (item.cpu) {
        peak = Math.max(peak, item.cpu.busy, attributed(item, "cpu"));
      }
    });
    const cpuTop = Math.min(
      ceiling(Math.max(peak, 1)),
      last && last.cpu ? last.cpu.cpus : Infinity
    );
    const cpuBands = workloadBands(host, "cpu", [
      {
        key: "machine",
        className: "usage-machine",
        label: "The machine itself",
        value: (item) =>
          item.cpu ? Math.max(item.cpu.busy - attributed(item, "cpu"), 0) : null,
      },
    ]);
    const cpu = element("chart-cpu");
    stacked(cpu.querySelector("[data-plot]"), {
      points,
      bands: cpuBands,
      top: cpuTop,
      format: cores,
      now,
      has: (item) => item.cpu !== null,
      label: "CPUs busy, by workload, over the last five minutes",
    });
    cpu.querySelector("[data-scale]").textContent =
      last && last.cpu ? `of ${plural(last.cpu.cpus, "CPU")}` : "";

    // Memory, against the whole of it: it is a gauge that is mostly full on
    // any machine running guests, and its share of the total is the question.
    const total = last && last.memory ? last.memory.total : 0;
    const memoryBands = workloadBands(host, "memory", [
      {
        key: "reserved",
        className: "usage-reserved",
        label: "Hugepage pool",
        value: (item) => (item.memory ? item.memory.reserved : null),
      },
      {
        key: "machine",
        className: "usage-machine",
        label: "The machine itself",
        value: (item) =>
          item.memory
            ? Math.max(
                item.memory.used -
                  item.memory.reserved -
                  attributed(item, "memory"),
                0
              )
            : null,
      },
    ]);
    const memory = element("chart-memory");
    stacked(memory.querySelector("[data-plot]"), {
      points,
      bands: memoryBands,
      top: total || 1,
      format: bytes,
      now,
      has: (item) => item.memory !== null,
      label: "Memory used, by workload, over the last five minutes",
    });
    memory.querySelector("[data-scale]").textContent = total
      ? `of ${bytes(total)}`
      : "";

    lines(element("chart-disk").querySelector("[data-plot]"), {
      points,
      series: [
        {
          className: "usage-s1",
          label: "Read",
          value: (item) => (item.disk ? item.disk.read : null),
        },
        {
          className: "usage-s2 dashed",
          label: "Write",
          value: (item) => (item.disk ? item.disk.write : null),
        },
      ],
      format: perSecond,
      now,
      label: "Disk reads and writes per second over the last five minutes",
    });
    element("chart-disk").querySelector("[data-scale]").textContent =
      "read, and write dashed";

    lines(element("chart-network").querySelector("[data-plot]"), {
      points,
      series: [
        {
          className: "usage-s1",
          label: "In",
          value: (item) => (item.network ? item.network.rx : null),
        },
        {
          className: "usage-s2 dashed",
          label: "Out",
          value: (item) => (item.network ? item.network.tx : null),
        },
      ],
      format: perSecond,
      now,
      label: "Network traffic per second over the last five minutes",
    });
    element("chart-network").querySelector("[data-scale]").textContent =
      "in, and out dashed";

    drawLegend(cpuBands, memoryBands, last);
  }

  // One legend for the two charts that share their colours, so a workload is
  // named once and found in both.
  function drawLegend(cpuBands, memoryBands, last) {
    const legend = element("legend");
    const seen = new Set();
    const entries = [];
    cpuBands.concat(memoryBands).forEach((band) => {
      if (seen.has(band.key)) {
        return;
      }
      seen.add(band.key);
      if (band.key === "reserved" && !(last && last.memory && last.memory.reserved)) {
        return;
      }
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = `usage-swatch ${band.className}`;
      item.append(swatch, band.label);
      entries.push(item);
    });
    legend.replaceChildren(...entries);
  }

  // Tables

  function cell(text, className) {
    const node = document.createElement("td");
    node.textContent = text;
    if (className) {
      node.className = className;
    }
    return node;
  }

  function row(parent, cells) {
    const line = document.createElement("tr");
    cells.forEach((item) => line.append(item));
    parent.append(line);
    return line;
  }

  // A length for a share of something, with the figure beside it.
  function fill(ratio, words) {
    const node = document.createElement("td");
    node.className = "usage-fill-cell";
    const bar = document.createElement("span");
    bar.className = "usage-fill";
    const inner = document.createElement("span");
    inner.style.width = `${Math.min(Math.max(ratio, 0), 1) * 100}%`;
    if (ratio >= 0.9) {
      inner.className = "full";
    }
    bar.append(inner);
    const figure = document.createElement("span");
    figure.textContent = words;
    node.append(bar, figure);
    return node;
  }

  function drawWorkloads(host, machine, last) {
    const body = element("workload-rows");
    body.replaceChildren();
    const held = slots.get(host) || new Map();
    const loads = last ? Array.from(last.workloads.entries()) : [];
    loads.sort((a, b) => {
      const byCpu = (b[1].cpu || 0) - (a[1].cpu || 0);
      return byCpu !== 0 ? byCpu : (b[1].memory || 0) - (a[1].memory || 0);
    });
    loads.forEach(([key, load]) => {
      const name = document.createElement("td");
      const swatch = document.createElement("i");
      swatch.className = `usage-swatch ${
        held.has(key) ? "usage-s" + held.get(key) : "usage-other"
      }`;
      name.append(swatch, " ", load.name);
      const kind = load.kind === "vm" ? "VM" : "Container";
      const cpu = cell(cores(load.cpu), "num");
      if (load.vcpus) {
        cpu.title = `${plural(load.vcpus, "vCPU")}`;
      }
      row(body, [
        name,
        cell(kind),
        cpu,
        cell(bytes(load.memory), "num"),
        cell(perSecond(load.diskRead), "num"),
        cell(perSecond(load.diskWrite), "num"),
        cell(perSecond(load.netIn), "num"),
        cell(perSecond(load.netOut), "num"),
      ]);
    });
    if (!loads.length) {
      const empty = cell("No guest or container is running on this machine.");
      empty.colSpan = 8;
      row(body, [empty]);
    }

    const stopped = machine.guests
      .filter((guest) => !guest.running)
      .map((guest) => `${guest.name} (VM)`)
      .concat(
        machine.containers
          .filter((container) => !container.running)
          .map((container) => `${container.name} (${container.state})`)
      );
    const note = element("workloads-note");
    const parts = [];
    if (!machine.guests_reach.reachable) {
      parts.push(
        `libvirt-exporter did not answer (${machine.guests_reach.error}), so no guest is listed.`
      );
    }
    if (!machine.containers_reach.reachable) {
      parts.push(
        `prometheus-podman-exporter did not answer (${machine.containers_reach.error}), so no container is listed.`
      );
    }
    if (stopped.length) {
      parts.push(`Not running, so using nothing: ${stopped.join(", ")}.`);
    }
    parts.push(
      "A container on the host's network shows no traffic of its own: it is " +
        "the machine's, on its ports."
    );
    note.textContent = parts.join(" ");
  }

  function drawFilesystems(node) {
    const body = element("filesystem-rows");
    body.replaceChildren();
    node.filesystems.forEach((filesystem) => {
      const used = filesystem.size_bytes - filesystem.available_bytes;
      const ratio = filesystem.size_bytes ? used / filesystem.size_bytes : 0;
      row(body, [
        cell(filesystem.mountpoint + (filesystem.readonly ? " (read only)" : "")),
        cell(filesystem.device),
        cell(filesystem.fstype),
        fill(ratio, `${bytes(used)} (${percent(ratio)})`),
        cell(bytes(filesystem.available_bytes), "num"),
        cell(bytes(filesystem.size_bytes), "num"),
      ]);
    });
  }

  function drawDisks(node, last) {
    const body = element("disk-rows");
    body.replaceChildren();
    const rates = new Map(
      last && last.disk ? last.disk.devices.map((disk) => [disk.device, disk]) : []
    );
    node.disks.forEach((disk) => {
      const now = rates.get(disk.device) || {};
      row(body, [
        cell(disk.device),
        cell(perSecond(now.read), "num"),
        cell(perSecond(now.write), "num"),
        cell(percent(now.busy === undefined ? null : Math.min(now.busy, 1)), "num"),
      ]);
    });
  }

  const KINDS = {
    physical: "Port",
    logical: "Team, bridge or VLAN",
    workload: "A workload's end",
    loopback: "Loopback",
  };

  function drawInterfaces(node, last) {
    const body = element("interface-rows");
    body.replaceChildren();
    const all = element("show-all-interfaces").checked;
    const rates = new Map(
      last && last.network
        ? last.network.links.map((link) => [link.device, link])
        : []
    );
    node.interfaces
      .filter((link) => all || link.kind === "physical")
      .forEach((link) => {
        const now = rates.get(link.device) || {};
        const busiest = Math.max(now.rx || 0, now.tx || 0);
        row(body, [
          cell(link.device),
          cell(KINDS[link.kind] || link.kind),
          cell(link.operstate || "–"),
          cell(link.speed_bytes ? `${formatBits(link.speed_bytes)}` : "–", "num"),
          cell(perSecond(now.rx), "num"),
          cell(perSecond(now.tx), "num"),
          cell(
            link.speed_bytes && now.rx !== undefined
              ? percent(busiest / link.speed_bytes)
              : "–",
            "num"
          ),
        ]);
      });
  }

  // A link's speed the way it is sold and negotiated: in bits.
  function formatBits(bytesPerSecond) {
    const bits = bytesPerSecond * 8;
    if (bits >= 1e9) {
      return `${(bits / 1e9).toFixed(bits % 1e9 ? 1 : 0)} Gb/s`;
    }
    return `${Math.round(bits / 1e6)} Mb/s`;
  }

  function drawReach(machine) {
    const body = element("reach-rows");
    body.replaceChildren();
    [
      ["node_exporter", machine.node_reach],
      ["libvirt-exporter", machine.guests_reach],
      ["prometheus-podman-exporter", machine.containers_reach],
    ].forEach(([name, reach]) => {
      row(body, [
        cell(name),
        cell(reach.reachable ? "Answered" : reach.error || "No answer"),
      ]);
    });
  }

  // The machine the bar points at

  function stat(label, value, status) {
    const box = document.createElement("div");
    box.className = "stat" + (status ? " stat-" + status : "");
    const number = document.createElement("strong");
    number.textContent = value;
    const name = document.createElement("span");
    name.textContent = label;
    box.append(number, name);
    return box;
  }

  function drawMachine() {
    if (!latest) {
      return;
    }
    const machine = latest.machines.find((item) => item.host === selected);
    element("machine-loading").hidden = true;
    const blocked = element("machine-blocked");
    const body = element("machine-body");
    if (!machine) {
      blocked.textContent = latest.note || "This machine is no longer in the inventory.";
      blocked.hidden = false;
      body.hidden = true;
      return;
    }
    const node = machine.node;
    if (!node) {
      blocked.textContent =
        `${machine.host} (${machine.address}): node_exporter did not answer` +
        (machine.node_reach.error ? `: ${machine.node_reach.error}.` : ".") +
        " The page reads it again every five seconds.";
      blocked.hidden = false;
      body.hidden = true;
      element("machine-lead").textContent = "";
      return;
    }
    blocked.hidden = true;
    body.hidden = false;

    const points = pointsOf(machine.host);
    const last = points[points.length - 1];
    assignSlots(machine.host, last);
    const now = Date.now();

    const cpus = node.housekeeping.cpus + node.isolated.cpus;
    const lead = [
      `${machine.host} (${machine.address})`,
      plural(cpus, "CPU") +
        (node.isolated.cpus ? `, ${node.isolated.cpus} isolated` : ""),
      node.memory.total_bytes ? `${bytes(node.memory.total_bytes)} of memory` : null,
      node.load1 !== null
        ? `load ${node.load1.toFixed(2)} ${node.load5.toFixed(2)} ${node.load15.toFixed(2)}`
        : null,
      node.boot_time ? `up ${uptime(node.read_at - node.boot_time)}` : null,
    ];
    element("machine-lead").textContent = lead.filter(Boolean).join(" · ");

    const stats = element("machine-stats");
    stats.replaceChildren();
    const cpu = last && last.cpu;
    if (cpu) {
      const share = cpu.housekeepingCpus ? cpu.housekeeping / cpu.housekeepingCpus : null;
      stats.append(
        stat(
          node.isolated.cpus ? "Housekeeping CPUs" : "CPU",
          percent(share),
          share >= 0.9 ? "warn" : null
        )
      );
      if (node.isolated.cpus) {
        stats.append(
          stat("Isolated CPUs", percent(cpu.isolated / cpu.isolatedCpus))
        );
      }
    }
    const memory = last && last.memory;
    if (memory) {
      const share = memory.used / memory.total;
      stats.append(
        stat("Memory used", percent(share), share >= 0.9 ? "warn" : null)
      );
    }
    if (last && last.disk) {
      stats.append(
        stat("Disk read / write", `${perSecond(last.disk.read)} / ${perSecond(last.disk.write)}`)
      );
    }
    if (last && last.network) {
      stats.append(
        stat("Network in / out", `${perSecond(last.network.rx)} / ${perSecond(last.network.tx)}`)
      );
    }
    element("machine-waiting").hidden = Boolean(cpu);

    drawCharts(machine.host, points, now);
    drawWorkloads(machine.host, machine, last);
    drawFilesystems(node);
    drawDisks(node, last);
    drawInterfaces(node, last);
    drawReach(machine);
  }

  function uptime(seconds) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    if (days) {
      return `${days} d ${hours} h`;
    }
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${hours} h ${minutes} min`;
  }

  // The bar

  function summary(machine) {
    if (!machine.node) {
      return ["error", `No answer: ${machine.node_reach.error || "node_exporter is down"}`];
    }
    const points = pointsOf(machine.host);
    const last = points[points.length - 1];
    const parts = [];
    if (last && last.cpu) {
      parts.push(`CPU ${percent(last.cpu.busy / last.cpu.cpus)}`);
    }
    if (last && last.memory) {
      parts.push(`memory ${percent(last.memory.used / last.memory.total)}`);
    }
    const guests = machine.guests.filter((guest) => guest.running).length;
    const containers = machine.containers.filter((item) => item.running).length;
    parts.push(`${plural(guests, "VM")}, ${plural(containers, "container")}`);
    return ["ok", parts.join(" · ")];
  }

  function drawViews() {
    const views = element("views");
    const existing = new Map(
      Array.from(views.querySelectorAll(".view[data-host]")).map((tab) => [
        tab.dataset.host,
        tab,
      ])
    );
    const wanted = latest.machines.map((machine) => machine.host);
    const same =
      existing.size === wanted.length && wanted.every((host) => existing.has(host));
    if (!same) {
      views.replaceChildren(
        ...latest.machines.map((machine) => {
          const tab = document.createElement("button");
          tab.type = "button";
          tab.className = "view";
          tab.dataset.host = machine.host;
          const dot = document.createElement("span");
          dot.className = "dot status-unknown";
          const name = document.createElement("span");
          name.className = "view-name";
          name.textContent = machine.host;
          const answer = document.createElement("span");
          answer.className = "view-answer";
          tab.append(dot, name, answer);
          tab.addEventListener("click", () => select(machine.host));
          return tab;
        })
      );
    }
    latest.machines.forEach((machine) => {
      const tab = views.querySelector(`.view[data-host="${CSS.escape(machine.host)}"]`);
      const [status, answer] = summary(machine);
      tab.querySelector(".dot").className = "dot status-" + status;
      tab.querySelector(".view-answer").textContent = answer;
      const current = machine.host === selected;
      tab.classList.toggle("current", current);
      if (current) {
        tab.setAttribute("aria-current", "true");
      } else {
        tab.removeAttribute("aria-current");
      }
    });
  }

  function select(host) {
    selected = host;
    drawViews();
    drawMachine();
  }

  // Reading

  function remember(view) {
    const received = Date.now();
    view.machines.forEach((machine) => {
      const list = readings.get(machine.host) || [];
      list.push({ received, machine });
      while (list.length && received - list[0].received > WINDOW_MS + PERIOD_MS) {
        list.shift();
      }
      readings.set(machine.host, list);
    });
    // A machine taken out of the inventory takes its history with it.
    const present = new Set(view.machines.map((machine) => machine.host));
    Array.from(readings.keys()).forEach((host) => {
      if (!present.has(host)) {
        readings.delete(host);
        slots.delete(host);
      }
    });
  }

  async function read() {
    if (inFlight) {
      return;
    }
    inFlight = true;
    try {
      const view = await API.get("/usage");
      showBanner("");
      latest = view;
      if (!view.machines.length) {
        element("machine-loading").hidden = true;
        const blocked = element("machine-blocked");
        blocked.textContent = view.note || "There is no machine to ask.";
        blocked.hidden = false;
        return;
      }
      remember(view);
      if (!view.machines.some((machine) => machine.host === selected)) {
        const here = view.machines.find((machine) => machine.host === view.this_host);
        selected = (here || view.machines[0]).host;
      }
      drawViews();
      drawMachine();
      element("clock").textContent = `Read at ${new Date().toLocaleTimeString()}`;
    } catch (error) {
      // The charts keep what was read before, which is the honest thing to
      // show, and say here that the reading stopped.
      showBanner(`The last reading failed: ${error.message}`);
      element("machine-loading").hidden = true;
    } finally {
      inFlight = false;
    }
  }

  function start() {
    if (timer === null && !paused && !document.hidden) {
      read();
      timer = window.setInterval(read, PERIOD_MS);
    }
  }

  function stop() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  element("pause").addEventListener("click", () => {
    paused = !paused;
    const button = element("pause");
    button.textContent = paused ? "Resume" : "Pause";
    button.setAttribute("aria-pressed", String(paused));
    if (paused) {
      stop();
    } else {
      start();
    }
  });

  // A tab nobody is looking at asks nothing of the machines.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stop();
    } else {
      start();
    }
  });

  element("show-all-interfaces").addEventListener("change", drawMachine);

  start();
})();
