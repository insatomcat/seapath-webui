// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The site on one screen, from readings the other pages already make.
//
// `/usage` is the window the service keeps while somebody uses it, asked every
// five seconds for what it read since, exactly as the Usage page asks it: the
// two pages share the recorder, and opening both costs the machines nothing
// more. `/cluster`, `/storage` and `/realtime/pool` are fan outs to every
// machine, asked once at load and then by the control in the lead line or the
// switch in the bar, every ten seconds, like on their own pages. A reading
// that fails leaves what was drawn and says so in the banner.
//
// Nothing here acts. Every count is a link to the page that explains it, and
// every act stays on that page, with its confirmation.

(function () {
  const PERIOD_MS = 5000;
  const SVG = "http://www.w3.org/2000/svg";
  // How full a filesystem is before it is counted: amber past 85 %, red past
  // 95 %, which is where Ceph itself warns and refuses writes.
  const FULL = 0.85;
  const VERY_FULL = 0.95;
  // Where seapath-rbd-mount mounts a container workload's RBD image.
  const RBD_MOUNTS = "/mnt/rbd/";
  // The containers every SEAPATH machine runs for itself: Ceph's daemons, the
  // exporters, and this service with its proxy. Counted on one line under the
  // site's workloads, so a card shows first what the substation runs.
  const SYSTEM = /^(ceph-|seapath-webui$|seapath-metrics-proxy$)|exporter/;

  let usage = null;
  let lastAt = null;
  const points = new Map();
  let cluster = null;
  let storage = null;
  let pool = null;
  let timer = null;
  let inFlight = false;
  // The workload tiles an operator opened, by machine and name, so the
  // redraw every reading makes leaves them open.
  const opened = new Set();

  // The tuning findings an operator chose to live with on the Real time page,
  // kept in this browser under its key. A finding ignored there is not
  // counted here either, or the two pages would disagree on the same machine.
  const IGNORED_KEY = "seapath-webui.realtime.ignored";

  function ignoredChecks() {
    try {
      const stored = JSON.parse(localStorage.getItem(IGNORED_KEY) || "[]");
      return new Set(Array.isArray(stored) ? stored : []);
    } catch (unreadable) {
      return new Set();
    }
  }

  function warnings(node) {
    const ignored = ignoredChecks();
    return ((node && node.checks) || []).filter(
      (check) => check.status === "warning" && !ignored.has(node.host + ":" + check.id)
    );
  }

  function element(id) {
    return document.getElementById(id);
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function showBanner(message) {
    const banner = element("banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  // Binary units, which is what the kernel, df and Ceph count in.
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
    return scaled.toFixed(scaled >= 100 || index === 0 ? 0 : 1) + " " + units[index];
  }

  function rate(value) {
    return value === null || value === undefined ? "–" : bytes(value) + "/s";
  }

  function percent(share) {
    return share === null || share === undefined || Number.isNaN(share)
      ? "–"
      : Math.round(share * 100) + " %";
  }

  function since(seconds) {
    if (seconds < 3600) {
      return Math.max(0, Math.round(seconds / 60)) + " min";
    }
    if (seconds < 86400 * 2) {
      return Math.round(seconds / 3600) + " h";
    }
    return Math.round(seconds / 86400) + " d";
  }

  function fullness(share) {
    return share > VERY_FULL ? "bad" : share > FULL ? "warn" : "";
  }

  // Reading

  function merge(view) {
    const span = view.window_seconds || 300;
    view.machines.forEach((machine) => {
      const kept = points.get(machine.host) || [];
      const merged = kept.concat(machine.points || []);
      const oldest = view.now - span;
      points.set(
        machine.host,
        merged.filter((point) => point.at >= oldest)
      );
    });
    usage = view;
  }

  async function readUsage() {
    if (inFlight) {
      return;
    }
    inFlight = true;
    try {
      const view = await API.get(
        lastAt === null ? "/usage" : "/usage?since=" + encodeURIComponent(lastAt)
      );
      merge(view);
      view.machines.forEach((machine) => {
        const last = (machine.points || []).slice(-1)[0];
        if (last && (lastAt === null || last.at > lastAt)) {
          lastAt = last.at;
        }
      });
      showBanner("");
      draw();
    } catch (failure) {
      showBanner("The last reading failed: " + failure.message);
      element("loading").hidden = true;
    } finally {
      inFlight = false;
    }
  }

  // The three fan outs, asked together. One that fails keeps the last answer
  // it gave: a standalone machine has no cluster and no Ceph, which its answer
  // says with `available: false` rather than with an error.
  async function readCluster(fresh) {
    const asked = [
      API.started(API.reading("/cluster", fresh)),
      API.started(API.reading("/storage", fresh)),
      API.started(API.reading("/realtime/pool", fresh)),
    ];
    const [members, ceph, tuning] = await Promise.allSettled(asked);
    if (members.status === "fulfilled") {
      cluster = members.value;
    }
    if (ceph.status === "fulfilled") {
      storage = ceph.value;
    }
    if (tuning.status === "fulfilled") {
      pool = tuning.value;
    }
    draw();
    const failed = [members, ceph, tuning].find((item) => item.status === "rejected");
    if (failed) {
      throw failed.reason;
    }
  }

  // Joining

  function machines() {
    return (usage && usage.machines) || [];
  }

  function nodeOf(host) {
    return ((cluster && cluster.available && cluster.nodes) || []).find(
      (node) => node.name === host
    );
  }

  function tuningOf(host) {
    return ((pool && pool.nodes) || []).find((node) => node.host === host);
  }

  function resourceFor(name) {
    const resources = (cluster && cluster.available && cluster.resources) || [];
    return resources.find(
      (resource) =>
        resource.id === name || resource.agent === "systemd:" + name + ".service"
    );
  }

  function filesystems(machine) {
    const node = machine.latest && machine.latest.node;
    return ((node && node.filesystems) || []).map((filesystem) => ({
      ...filesystem,
      used: filesystem.size_bytes - filesystem.available_bytes,
      share:
        filesystem.size_bytes > 0
          ? (filesystem.size_bytes - filesystem.available_bytes) / filesystem.size_bytes
          : 0,
    }));
  }

  function ports(machine) {
    const node = machine.latest && machine.latest.node;
    const rates = new Map(
      (machine.interfaces || []).map((item) => [item.device, item])
    );
    return ((node && node.interfaces) || [])
      .filter((item) => item.kind === "physical")
      .map((item) => ({ ...item, rates: rates.get(item.device) || {} }));
  }

  function containerName(name) {
    return name.replace(/^systemd-/, "");
  }

  function volumeOf(machine, name) {
    return filesystems(machine).find(
      (filesystem) => filesystem.mountpoint === RBD_MOUNTS + name
    );
  }

  function sum(values) {
    const known = values.filter((value) => value !== null && value !== undefined);
    return known.length ? known.reduce((a, b) => a + b, 0) : null;
  }

  // What runs on a machine: its guests, and the site's containers gathered
  // by pod, since a pod is the workload an operator deploys and the
  // containers in it are its parts. Each with its share of the last
  // interval, and the system containers in one sum.
  function workloads(machine) {
    const latest = machine.latest || {};
    const rates = new Map((machine.workloads || []).map((item) => [item.key, item]));
    const site = [];
    const system = { count: 0, cpu: 0, memory: 0 };
    (latest.guests || [])
      .filter((guest) => guest.running !== false)
      .forEach((guest) => {
        const own = rates.get("vm:" + guest.name) || {};
        const resource = resourceFor(guest.name);
        site.push({
          kind: "vm",
          name: guest.name,
          cpu: own.cpu,
          memory: own.memory,
          vcpus: guest.vcpus,
          members: [],
          resources: resource ? [resource] : [],
        });
      });
    const pods = new Map();
    (latest.containers || []).forEach((container) => {
      const own = rates.get("container:" + container.name) || {};
      if (!container.pod && SYSTEM.test(container.name)) {
        if (container.running) {
          system.count += 1;
          system.cpu += own.cpu || 0;
          system.memory += own.memory || 0;
        }
        return;
      }
      const name = containerName(container.pod || container.name);
      const key = (container.pod ? "pod:" : "container:") + name;
      if (!pods.has(key)) {
        pods.set(key, {
          kind: container.pod ? "pod" : "container",
          name,
          parts: [],
        });
      }
      pods.get(key).parts.push({ container, own });
    });
    pods.forEach((item) => {
      const running = item.parts.filter((part) => part.container.running);
      if (!running.length) {
        return;
      }
      const names = item.parts.map((part) => containerName(part.container.name));
      const candidates = item.kind === "pod" ? names.concat(item.name, item.name + "-pod") : names;
      const resources = [];
      candidates.forEach((candidate) => {
        const resource = resourceFor(candidate);
        if (resource && !resources.includes(resource)) {
          resources.push(resource);
        }
      });
      site.push({
        kind: item.kind,
        name: item.name,
        cpu: sum(running.map((part) => part.own.cpu)),
        memory: sum(running.map((part) => part.own.memory)),
        members: item.parts
          .map((part) => ({
            name: containerName(part.container.name),
            state: part.container.state,
            running: part.container.running,
          }))
          .sort((a, b) => a.name.localeCompare(b.name)),
        resources,
        volume: volumeOf(machine, item.name),
      });
    });
    site.sort((a, b) => a.name.localeCompare(b.name));
    return { site, system };
  }

  // The tiles

  function tile(value, label, href, status, title) {
    const box = make("a", "overview-tile" + (status ? " tile-" + status : ""));
    box.href = href;
    if (title) {
      box.title = title;
    }
    box.append(make("strong", "", value), make("span", "", label));
    return box;
  }

  function drawTiles() {
    const box = element("tiles");
    const found = [];
    const all = machines();

    const answering = all.filter((machine) => machine.latest && machine.latest.node_reach.reachable);
    found.push(
      tile(
        answering.length + " / " + all.length,
        "machines answering",
        "usage",
        answering.length === all.length ? "ok" : answering.length ? "warn" : "bad",
        all
          .filter((machine) => !answering.includes(machine))
          .map((machine) => machine.host + ": " + ((machine.latest && machine.latest.node_reach.error) || "no answer"))
          .join("\n")
      )
    );

    if (cluster && cluster.available) {
      const members = cluster.nodes || [];
      const online = members.filter((node) => node.online);
      const quorate = cluster.corosync && cluster.corosync.quorate;
      found.push(
        tile(
          online.length + " / " + members.length,
          quorate ? "members online, quorate" : "members online, no quorum",
          "cluster",
          !quorate ? "bad" : online.length === members.length ? "ok" : "warn"
        )
      );
      const resources = cluster.resources || [];
      const failed = resources.filter((resource) => resource.failed);
      const started = resources.filter((resource) => resource.role === "started");
      found.push(
        tile(
          failed.length ? String(failed.length) : started.length + " / " + resources.length,
          failed.length ? "resources failed" : "resources started",
          "cluster",
          failed.length ? "bad" : started.length === resources.length ? "ok" : "warn",
          failed.map((resource) => resource.id).join(", ")
        )
      );
    }

    if (storage && storage.available) {
      const health = (storage.health || "").replace(/^HEALTH_/, "");
      found.push(
        tile(
          health || "–",
          "Ceph, " + percent(storage.used_ratio) + " used",
          "cluster",
          health === "OK" ? "ok" : health === "WARN" ? "warn" : "bad",
          (storage.messages || []).map((message) => message.name).join("\n")
        )
      );
    }

    const tuned = ((pool && pool.nodes) || []).filter((node) => node.clock);
    if (tuned.length) {
      const synced = tuned.filter((node) => node.clock.synchronised);
      found.push(
        tile(
          synced.length + " / " + tuned.length,
          "clocks synchronised",
          "realtime",
          synced.length === tuned.length ? "ok" : "bad",
          tuned
            .filter((node) => !node.clock.synchronised)
            .map((node) => node.host)
            .join(", ")
        )
      );
      const warned = tuned.filter((node) => warnings(node).length);
      found.push(
        tile(
          String(warned.length),
          "machines with a tuning warning",
          "realtime",
          warned.length ? "warn" : "ok",
          warned.map((node) => node.host).join(", ")
        )
      );
    }

    const full = [];
    all.forEach((machine) =>
      filesystems(machine).forEach((filesystem) => {
        if (filesystem.share > FULL) {
          full.push({ host: machine.host, ...filesystem });
        }
      })
    );
    found.push(
      tile(
        String(full.length),
        "volumes nearly full",
        "usage",
        full.some((item) => item.share > VERY_FULL) ? "bad" : full.length ? "warn" : "ok",
        full
          .map((item) => item.host + " " + item.mountpoint + " " + percent(item.share))
          .join("\n")
      )
    );

    let up = 0;
    let total = 0;
    all.forEach((machine) =>
      ports(machine).forEach((port) => {
        total += 1;
        up += port.operstate === "up" ? 1 : 0;
      })
    );
    if (total) {
      found.push(tile(up + " / " + total, "physical ports up", "usage"));
    }

    let guests = 0;
    let containers = 0;
    all.forEach((machine) =>
      workloads(machine).site.forEach((item) => {
        guests += item.kind === "vm" ? 1 : 0;
        containers += item.kind === "vm" ? 0 : 1;
      })
    );
    found.push(tile(String(guests), "guests running", "vms"));
    found.push(tile(String(containers), "pods and containers running", "containers"));

    box.replaceChildren(...found);
  }

  // A machine

  function track(share, status) {
    const track = make("span", "bar");
    const fill = make("span", "fill" + (status ? " " + status : ""));
    fill.style.width = Math.min(100, Math.max(0, Math.round((share || 0) * 100))) + "%";
    track.append(fill);
    return track;
  }

  // A row: its name, how full, the last five minutes when there are some,
  // and the figures.
  function bar(label, share, figures, status, history) {
    const row = make("div", "overview-bar");
    const name = make("span", "overview-bar-name", label);
    name.title = label;
    row.append(
      name,
      track(share, status),
      history ? line(history, 1) : make("span"),
      make("span", "overview-bar-figures", figures)
    );
    return row;
  }

  // A line over the window, from oldest to newest, against a fixed ceiling so
  // two machines' lines compare. Points with no value break the line.
  function spark(label, values, ceiling, figure) {
    const box = make("div", "overview-spark");
    const head = make("div", "overview-spark-head");
    head.append(make("span", "", label), make("strong", "", figure));
    box.append(head, line(values, ceiling));
    return box;
  }

  function line(values, ceiling) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 100 24");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("aria-hidden", "true");
    const top = Math.max(ceiling || 0, ...values.filter((v) => v !== null)) || 1;
    let path = "";
    let drawing = false;
    values.forEach((value, index) => {
      if (value === null || value === undefined) {
        drawing = false;
        return;
      }
      const x = values.length > 1 ? (index / (values.length - 1)) * 100 : 100;
      const y = 23 - (value / top) * 22;
      path += (drawing ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
      drawing = true;
    });
    const stroke = document.createElementNS(SVG, "path");
    stroke.setAttribute("d", path);
    stroke.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(stroke);
    return svg;
  }

  function chip(text, status, title) {
    const node = make("span", "overview-chip" + (status ? " chip-" + status : ""), text);
    if (title) {
      node.title = title;
    }
    return node;
  }

  function chips(machine) {
    const box = make("div", "overview-chips");
    const member = nodeOf(machine.host);
    if (member) {
      const standby = (member.flags || []).includes("standby");
      box.append(
        chip(
          !member.online ? member.state || "offline" : standby ? "standby" : "online",
          !member.online ? "bad" : standby ? "warn" : "ok",
          "Pacemaker member" + (member.dc ? ", the coordinator" : "")
        )
      );
    }
    const tuning = tuningOf(machine.host);
    const clock = tuning && tuning.clock;
    if (clock && clock.synchronised !== null && clock.synchronised !== undefined) {
      box.append(
        chip(
          clock.synchronised ? "clock synchronised" : "clock not synchronised",
          clock.synchronised ? "ok" : "bad"
        )
      );
    }
    if (clock && clock.ptp && clock.ptp.smpsynch !== null) {
      const offset = clock.ptp.offset_seconds;
      box.append(
        chip(
          "PTP SmpSynch " +
            clock.ptp.smpsynch +
            (offset !== null && offset !== undefined
              ? ", " + Math.round(offset * 1e9) + " ns"
              : ""),
          clock.ptp.smpsynch === 2 ? "ok" : clock.ptp.smpsynch === 1 ? "warn" : "bad",
          "Grandmaster " + (clock.ptp.gm_identity || "unknown") +
            ", clockClass " + clock.ptp.clock_class
        )
      );
    }
    if (tuning && tuning.preemption) {
      box.append(chip(tuning.preemption, tuning.preemption === "PREEMPT_RT" ? "" : "warn"));
    }
    const warned = tuning ? warnings(tuning) : [];
    if (warned.length) {
      box.append(
        chip(
          warned.length + (warned.length === 1 ? " tuning warning" : " tuning warnings"),
          "warn",
          warned.map((check) => check.title).join("\n")
        )
      );
    }
    return box;
  }

  function usageBars(machine, claimed) {
    const box = make("div", "overview-bars");
    const kept = points.get(machine.host) || [];
    const last = kept.filter((point) => point.cpu).slice(-1)[0];
    if (last) {
      box.append(
        bar(
          "CPU housekeeping",
          last.cpu.housekeeping / (last.cpu.housekeeping_cpus || 1),
          percent(last.cpu.housekeeping / (last.cpu.housekeeping_cpus || 1)) +
            " of " + last.cpu.housekeeping_cpus,
          "",
          kept.map((point) =>
            point.cpu ? point.cpu.housekeeping / (point.cpu.housekeeping_cpus || 1) : null
          )
        )
      );
      if (last.cpu.isolated_cpus) {
        box.append(
          bar(
            "CPU isolated",
            last.cpu.isolated / last.cpu.isolated_cpus,
            percent(last.cpu.isolated / last.cpu.isolated_cpus) +
              " of " + last.cpu.isolated_cpus,
            "",
            kept.map((point) =>
              point.cpu && point.cpu.isolated_cpus
                ? point.cpu.isolated / point.cpu.isolated_cpus
                : null
            )
          )
        );
      }
    }
    const memory = kept.filter((point) => point.memory).slice(-1)[0];
    if (memory) {
      box.append(
        bar(
          "Memory",
          memory.memory.used / memory.memory.total,
          bytes(memory.memory.used) + " of " + bytes(memory.memory.total),
          "",
          kept.map((point) => (point.memory ? point.memory.used / point.memory.total : null))
        )
      );
    }
    const all = filesystems(machine);
    all
      .filter((filesystem) => !filesystem.mountpoint.startsWith(RBD_MOUNTS))
      .forEach((filesystem) =>
        box.append(
          bar(
            filesystem.mountpoint,
            filesystem.share,
            bytes(filesystem.used) + " of " + bytes(filesystem.size_bytes),
            fullness(filesystem.share)
          )
        )
      );
    // A volume a workload tile carries is drawn there; one no running
    // workload claims stays here.
    const volumes = all.filter(
      (filesystem) =>
        filesystem.mountpoint.startsWith(RBD_MOUNTS) && !claimed.has(filesystem.mountpoint)
    );
    if (volumes.length) {
      const row = make("div", "overview-volumes");
      row.append(make("span", "overview-bar-name", "RBD volumes"));
      const list = make("span", "overview-volume-list");
      volumes.forEach((volume) => {
        const item = make("span", "overview-volume");
        item.title =
          volume.mountpoint + ": " + bytes(volume.used) + " of " +
          bytes(volume.size_bytes) + " (" + percent(volume.share) + ")";
        item.append(
          make("span", "", volume.mountpoint.slice(RBD_MOUNTS.length)),
          track(volume.share, fullness(volume.share))
        );
        list.append(item);
      });
      row.append(list);
      box.append(row);
    }
    return box;
  }

  // The CPU and the memory carry their line on their bar; the traffic has
  // no ceiling to fill and is drawn on its own.
  function sparks(machine) {
    const kept = points.get(machine.host) || [];
    const box = make("div", "overview-sparks");
    const inbound = kept.map((point) => point.network_in);
    const outbound = kept.map((point) => point.network_out);
    const last = (values) => values.filter((value) => value !== null).slice(-1)[0];
    box.append(
      spark("network in", inbound, 0, rate(last(inbound))),
      spark("network out", outbound, 0, rate(last(outbound)))
    );
    return box;
  }

  function facts(machine) {
    const node = machine.latest && machine.latest.node;
    if (!node) {
      return make("div");
    }
    const parts = [];
    if (node.load1 !== null && node.load1 !== undefined) {
      const cpus =
        ((node.housekeeping && node.housekeeping.cpus) || 0) +
        ((node.isolated && node.isolated.cpus) || 0);
      parts.push("load " + node.load1.toFixed(2) + " on " + cpus + " CPUs");
    }
    if (node.boot_time && usage) {
      parts.push("up " + since(usage.now - node.boot_time));
    }
    const box = make("div", "overview-facts");
    parts.forEach((part) => box.append(make("span", "", part)));
    // The RBD devices are the workloads' volumes, whose fullness their tiles
    // carry; their traffic is in the workload's own.
    (machine.disks || [])
      .filter((disk) => !disk.device.startsWith("rbd"))
      .forEach((disk) => {
        box.append(
          make(
            "span",
            "",
            disk.device + " busy " + percent(disk.busy) + " · ↓" + rate(disk.read) +
              " ↑" + rate(disk.write)
          )
        );
      });
    return box;
  }

  const KIND = { vm: "VM", pod: "pod", container: "container" };

  function figuresOf(item) {
    return (
      (item.cpu === null || item.cpu === undefined ? "–" : item.cpu.toFixed(2)) +
      " CPU · " +
      bytes(item.memory)
    );
  }

  // What a tile shows once opened: the volume in words, where it runs, what
  // Pacemaker holds of it, and each container of a pod with its state.
  function workloadDetail(machine, item) {
    const box = make("div", "overview-workload-detail");
    if (item.volume) {
      box.append(
        make(
          "p",
          "",
          "volume " + percent(item.volume.share) + " full, " +
            bytes(item.volume.available_bytes) + " left of " +
            bytes(item.volume.size_bytes)
        )
      );
    }
    box.append(
      make(
        "p",
        "",
        "running on " + machine.host +
          (item.vcpus ? " with " + item.vcpus + " vCPUs" : "")
      )
    );
    item.resources.forEach((resource) =>
      box.append(
        make(
          "p",
          "",
          "Pacemaker resource " + resource.id + ", " + (resource.failed ? "failed" : resource.role) +
            (resource.fail_count ? ", " + resource.fail_count + " failures" : "")
        )
      )
    );
    if (item.kind === "pod") {
      const list = make("ul", "overview-members");
      item.members.forEach((member) => {
        const row = make("li");
        row.append(
          make("span", "", member.name),
          chip(member.state, member.running ? "ok" : "warn")
        );
        list.append(row);
      });
      box.append(list);
    }
    const open = make("a", "", item.kind === "vm" ? "Open the VMs page" : "Open the Containers page");
    open.href = item.kind === "vm" ? "vms" : "containers";
    box.append(open);
    return box;
  }

  function workloadTile(machine, item) {
    const key = machine.host + ":" + item.kind + ":" + item.name;
    const failed = item.resources.some((resource) => resource.failed);
    const isOpen = opened.has(key);
    const card = make(
      "div",
      "overview-workload" +
        (failed ? " workload-failed" : item.resources.length ? " workload-held" : "") +
        (isOpen ? " workload-open" : "")
    );
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.setAttribute("aria-expanded", isOpen ? "true" : "false");
    const head = make("div", "overview-workload-head");
    head.append(make("strong", "", item.name), make("span", "overview-workload-kind", KIND[item.kind]));
    card.append(head, make("span", "overview-workload-figures", figuresOf(item)));
    if (item.volume) {
      const gauge = track(item.volume.share, fullness(item.volume.share));
      gauge.title = "volume " + percent(item.volume.share) + " full";
      card.append(gauge);
    }
    if (isOpen) {
      card.append(workloadDetail(machine, item));
    }
    const toggle = (event) => {
      if (event.target.closest("a")) {
        return;
      }
      if (opened.has(key)) {
        opened.delete(key);
      } else {
        opened.add(key);
      }
      draw();
    };
    card.addEventListener("click", toggle);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle(event);
      }
    });
    return card;
  }

  function workloadTiles(machine, found) {
    const { site, system } = found;
    const box = make("div", "overview-workloads");
    site.forEach((item) => box.append(workloadTile(machine, item)));
    if (!site.length) {
      box.append(make("p", "overview-quiet", "No guest and no site container runs here."));
    }
    if (system.count) {
      box.append(
        make(
          "p",
          "overview-quiet",
          system.count + " system containers (Ceph, exporters, this service): " +
            system.cpu.toFixed(2) + " CPU · " + bytes(system.memory)
        )
      );
    }
    return box;
  }

  function portRows(machine) {
    const box = make("div", "overview-ports");
    ports(machine).forEach((port) => {
      const row = make("div", "overview-port");
      const up = port.operstate === "up";
      row.append(
        make("span", "dot " + (up ? "status-ok" : "status-unknown")),
        make("span", "overview-port-name", port.device),
        make(
          "span",
          "overview-port-rates",
          up ? "↓" + rate(port.rates.receive) + " ↑" + rate(port.rates.transmit) : port.operstate
        )
      );
      box.append(row);
    });
    return box;
  }

  function machineCard(machine) {
    const card = make("article", "card overview-machine");
    const head = make("div", "overview-machine-head");
    const name = make("a", "overview-machine-name", machine.host);
    name.href = "usage?host=" + encodeURIComponent(machine.host);
    head.append(name, make("span", "overview-address", machine.address));
    card.append(head, chips(machine));

    const reach = machine.latest && machine.latest.node_reach;
    if (!reach || !reach.reachable) {
      card.classList.add("overview-silent");
      card.append(
        make(
          "p",
          "warning",
          "Its node_exporter did not answer: " + ((reach && reach.error) || "no reading yet") + "."
        )
      );
      return card;
    }
    const found = workloads(machine);
    const claimed = new Set(
      found.site.filter((item) => item.volume).map((item) => item.volume.mountpoint)
    );
    card.append(usageBars(machine, claimed), sparks(machine), facts(machine));
    card.append(workloadTiles(machine, found), portRows(machine));
    const node = machine.latest.node;
    if (node && node.read_at && usage) {
      card.append(
        make(
          "p",
          "overview-observed",
          "read " + Math.max(0, Math.round(usage.now - node.read_at)) + " s ago"
        )
      );
    }
    return card;
  }

  function draw() {
    if (!usage) {
      return;
    }
    element("loading").hidden = true;
    const blocked = element("blocked");
    blocked.hidden = usage.machines.length > 0;
    blocked.textContent = usage.note || "There is no machine to ask.";
    const all = machines();
    element("lead").textContent =
      all.length +
      (all.length === 1 ? " machine" : " machines") +
      (cluster && cluster.available ? ", one cluster" : "") +
      ". What each consumes is the last five minutes this service keeps; the " +
      "cluster, Ceph and the tuning are read again with the control beside this line.";
    const taken = lastAt === null ? null : new Date(lastAt * 1000);
    element("clock").textContent = taken ? "Read at " + taken.toLocaleTimeString() : "";
    drawTiles();
    element("machines").replaceChildren(...all.map(machineCard));
  }

  function start() {
    if (timer === null && !document.hidden) {
      readUsage();
      timer = window.setInterval(readUsage, PERIOD_MS);
    }
  }

  function stop() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  // A tab nobody is looking at asks nothing; coming back fetches what the
  // service read meanwhile.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stop();
    } else {
      start();
    }
  });

  Reread.attach(
    element("reread"),
    async (fresh) => {
      showBanner("");
      await readCluster(fresh);
    },
    (failure) => showBanner(failure.message)
  );

  // Ignoring a finding on the Real time page in another tab is seen here at
  // once.
  window.addEventListener("storage", (event) => {
    if (event.key === IGNORED_KEY) {
      draw();
    }
  });

  start();
  readCluster(false).catch((failure) => showBanner(failure.message));
})();
