// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The cluster page: what Pacemaker, Corosync and Ceph are doing right now.
//
// Three views, one on screen at a time, and a bar that says what the other two
// found. The layout is the Real time page's, for the same reason (D28): each
// of these readings is a table an operator scans, and three of them sharing a
// screen means three truncated tables and a scroll.
//
// Nothing here writes. There is no button on this page that reaches a machine,
// and that is a design decision rather than an unfinished one: standby, cleanup
// and migrate are `crm` commands, and this service does not run those. What a
// machine should be is the inventory and a run.

(function () {
  const VIEWS = {
    members: { card: "card-members" },
    resources: { card: "card-resources" },
    storage: { card: "card-storage" },
  };

  function element(id) {
    return document.getElementById(id);
  }

  // What a panel has to say, said on its tab. Two of the three are off screen
  // at any moment, and an operator has to know which one is worth opening
  // without opening it.
  function summarise(view, status, answer) {
    const tab = document.querySelector(`.view[data-view="${view}"]`);
    tab.querySelector(".dot").className = "dot status-" + status;
    tab.querySelector(".view-answer").textContent = answer;
  }

  function showBanner(message) {
    const banner = element("banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

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

  function clear(node) {
    node.replaceChildren();
    return node;
  }

  // A tile: one number and what it counts. The panels lead with three or four
  // of them, because the tables below answer "which one" and these answer
  // "how many", which is the question asked first.
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

  // Binary units, because that is what Ceph counts in and what the `ceph`
  // CLI prints: a page reporting 3.3 TB where `ceph -s` says 3 TiB sends an
  // operator looking for the missing capacity.
  function bytes(value) {
    if (!value) {
      return "0 B";
    }
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let index = 0;
    let scaled = value;
    while (scaled >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    return `${scaled.toFixed(scaled >= 100 || index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function percent(ratio) {
    return ratio === null || ratio === undefined
      ? "–"
      : `${(ratio * 100).toFixed(1)}%`;
  }

  function localTime(iso) {
    // The browser's zone, because an operator reads this against the clock on
    // the wall in front of them and not against UTC.
    return iso ? new Date(iso).toLocaleString() : "–";
  }

  function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
  }

  // Membership

  function renderMembers(cluster) {
    element("members-loading").hidden = true;
    if (!cluster.available) {
      element("members-blocked").textContent = cluster.error;
      element("members-blocked").hidden = false;
      summarise("members", "absent", "No cluster to read");
      // The reach table is still worth drawing: on a cluster that should
      // exist, which machine failed to answer is the whole finding. The member
      // table goes with the cluster, so an empty header row is not left
      // standing above it.
      renderReach(cluster.reach);
      element("members-body").hidden = cluster.reach.length === 0;
      element("member-table").hidden = true;
      renderSbd([]);
      clear(element("quorum"));
      return;
    }

    element("members-body").hidden = false;
    element("member-table").hidden = false;
    const corosync = cluster.corosync;
    const online = cluster.nodes.filter((node) => node.online).length;
    const tiles = clear(element("quorum"));
    tiles.append(
      stat(
        "Quorum",
        corosync.quorate === null ? "unknown" : corosync.quorate ? "yes" : "no",
        corosync.quorate === false ? "bad" : corosync.quorate ? "ok" : ""
      ),
      stat("Nodes online", `${online} / ${cluster.nodes.length}`,
        online === cluster.nodes.length ? "ok" : "warn"),
      stat(
        "Votes",
        corosync.total_votes === null || corosync.total_votes === undefined
          ? "–"
          : `${corosync.total_votes} / ${corosync.expected_votes ?? "?"}`,
        ""
      ),
      stat("Quorum takes", corosync.quorum ?? "–", ""),
      stat("Ring errors", corosync.ring_errors, corosync.ring_errors ? "bad" : "ok"),
      stat(
        "Fencing",
        cluster.stonith_enabled === null
          ? "unknown"
          : cluster.stonith_enabled
            ? "enabled"
            : "disabled",
        cluster.stonith_enabled === false ? "warn" : ""
      )
    );
    if (cluster.sbd_devices.length) {
      const healthy = cluster.sbd_devices.filter((item) => item.healthy).length;
      tiles.append(
        stat(
          "SBD devices",
          `${healthy} / ${cluster.sbd_devices.length}`,
          healthy === cluster.sbd_devices.length ? "ok" : "bad"
        )
      );
    }

    const body = clear(element("member-rows"));
    cluster.nodes.forEach((node) => {
      const name = cell(node.name);
      if (node.name === cluster.dc) {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = "coordinator";
        name.append(" ", tag);
      }
      row(body, [
        name,
        cell(node.state, "state-" + stateClass(node.state)),
        cell(node.votes === null || node.votes === undefined ? "–" : node.votes),
        cell(node.flags.join(", ")),
        cell(
          Object.entries(node.attributes)
            .map(([key, value]) => `${key}=${value}`)
            .join(", ") || "–"
        ),
      ]);
    });
    renderSbd(cluster.sbd_devices);
    renderReach(cluster.reach);

    element("members-lead").textContent = lead(cluster);
    summarise("members", membersStatus(cluster), membersAnswer(cluster, online));
  }

  function stateClass(word) {
    if (word === "online") {
      return "free";
    }
    return word === "unclean" || word === "offline" ? "failed" : "claimed";
  }

  function lead(cluster) {
    const from = cluster.from_dc
      ? `Read from ${cluster.source}, the coordinator.`
      : `Read from ${cluster.source}, which is not the coordinator` +
        `${cluster.dc ? " (" + cluster.dc + " is)" : ""}: the members either ` +
        "disagree or the coordinator could not be reached.";
    const changed = cluster.config_last_change
      ? ` Configuration last changed ${localTime(cluster.config_last_change)}.`
      : "";
    return from + changed;
  }

  function membersStatus(cluster) {
    if (cluster.corosync.quorate === false) {
      return "error";
    }
    if (
      cluster.nodes.some((node) => node.state === "unclean") ||
      cluster.corosync.ring_errors
    ) {
      return "error";
    }
    if (
      cluster.nodes.some((node) => !node.online || node.state !== "online") ||
      !cluster.from_dc ||
      cluster.reach.some((item) => !item.reachable)
    ) {
      return "warning";
    }
    return "ok";
  }

  function membersAnswer(cluster, online) {
    const parts = [`${online} of ${cluster.nodes.length} online`];
    const off = cluster.nodes.filter((node) => node.state !== "online");
    if (off.length) {
      parts.push(off.map((node) => `${node.name} ${node.state}`).join(", "));
    }
    parts.push(
      cluster.corosync.quorate === false
        ? "no quorum"
        : cluster.corosync.quorate
          ? "quorate"
          : "quorum unknown"
    );
    return parts.join(", ");
  }

  function renderSbd(devices) {
    const table = element("sbd");
    table.hidden = devices.length === 0;
    const body = clear(element("sbd-rows"));
    devices.forEach((device) => {
      const line = row(body, [
        cell(device.device),
        cell(device.status, device.healthy ? "state-free" : "state-failed"),
      ]);
      line.classList.toggle("row-bad", !device.healthy);
    });
  }

  function renderReach(reach) {
    const body = clear(element("reach-rows"));
    reach.forEach((item) => {
      row(body, [
        cell(item.host),
        cell(item.address),
        cell(
          item.reachable
            ? item.reporting
              ? "answering"
              : "up, publishes no cluster"
            : item.error || "unreachable",
          item.reachable && item.reporting ? "state-free" : "state-failed"
        ),
      ]);
    });
  }

  // Resources

  function renderResources(cluster) {
    element("resources-loading").hidden = true;
    if (!cluster.available) {
      element("resources-blocked").textContent = cluster.error;
      element("resources-blocked").hidden = false;
      summarise("resources", "absent", "No cluster to read");
      return;
    }

    element("resources-body").hidden = false;
    const body = clear(element("resource-rows"));
    cluster.resources.forEach((resource) => {
      const failures = resource.fail_count_infinite
        ? "INFINITY"
        : resource.fail_count
          ? `${resource.fail_count}${
              resource.migration_threshold
                ? " of " + resource.migration_threshold
                : ""
            }`
          : "–";
      const name = cell(resource.id);
      if (!resource.managed) {
        const tag = document.createElement("span");
        tag.className = "tag warn";
        tag.textContent = "unmanaged";
        name.append(" ", tag);
      }
      const line = row(body, [
        name,
        cell(resource.agent || "–"),
        cell(resource.node || "–"),
        cell(resource.role || "–"),
        cell(
          resource.state,
          resource.failed ? "state-failed" : "state-free"
        ),
        cell(failures, resource.fail_count_infinite ? "state-failed" : ""),
      ]);
      // The row an operator opened the panel for, washed rather than only
      // coloured in one cell: on a table of thirty resources the eye has to
      // land on it without reading the State column.
      line.classList.toggle("row-bad", resource.failed);
    });

    const constraints = cluster.constraints;
    element("constraints").hidden = constraints.length === 0;
    const rules = clear(element("constraint-rows"));
    constraints.forEach((item) =>
      row(rules, [
        cell(item.id),
        cell(item.resource),
        cell(item.node),
        cell(item.role || "–"),
        cell(item.score),
      ])
    );

    element("resources-lead").textContent =
      `${plural(cluster.resources.length, "resource instance")} across ` +
      `${plural(cluster.nodes.length, "node")}. In SEAPATH most of them are ` +
      "VMs: vm_manager creates one Pacemaker resource per guest.";
    summarise("resources", resourcesStatus(cluster), resourcesAnswer(cluster));
  }

  function resourcesStatus(cluster) {
    if (cluster.resources.some((resource) => resource.failed)) {
      return "error";
    }
    if (
      cluster.resources.some(
        (resource) => !resource.managed || resource.fail_count
      )
    ) {
      return "warning";
    }
    return cluster.resources.length ? "ok" : "info";
  }

  function resourcesAnswer(cluster) {
    const broken = cluster.resources.filter((resource) => resource.failed);
    if (broken.length) {
      return (
        `${plural(broken.length, "resource")} failed: ` +
        broken
          .slice(0, 3)
          .map((item) => item.id + (item.node ? " on " + item.node : ""))
          .join(", ")
      );
    }
    const counted = cluster.resources.filter((item) => item.fail_count).length;
    return counted
      ? `${plural(cluster.resources.length, "resource")}, all active, ` +
        `${counted} with a failure count`
      : `${plural(cluster.resources.length, "resource")}, all active`;
  }

  // Storage

  function renderStorage(ceph) {
    element("storage-loading").hidden = true;
    if (!ceph.available) {
      element("storage-blocked").textContent = ceph.error;
      element("storage-blocked").hidden = false;
      // Not a warning dot: a cluster with local storage is a supported SEAPATH
      // configuration, and an amber tab would report it as a fault.
      summarise("storage", "absent", "No Ceph on this cluster");
      return;
    }

    element("storage-body").hidden = false;
    const tiles = clear(element("ceph-stats"));
    tiles.append(
      stat("Health", ceph.health.replace("HEALTH_", ""), healthClass(ceph.health)),
      stat(
        "Monitors",
        `${ceph.monitors_in_quorum} / ${ceph.monitors.length}`,
        ceph.monitors.length === ceph.monitors_in_quorum ? "ok" : "bad"
      ),
      stat(
        "OSDs up",
        `${ceph.osds_up} / ${ceph.osds.length}`,
        ceph.osds_up === ceph.osds.length ? "ok" : "bad"
      ),
      stat(
        "OSDs in",
        `${ceph.osds_in} / ${ceph.osds.length}`,
        ceph.osds_in === ceph.osds.length ? "ok" : "warn"
      ),
      stat("Objects", ceph.objects.toLocaleString(), ""),
      stat(
        "Placement groups",
        `${ceph.placement_groups}`,
        ceph.pgs_not_clean ? "warn" : "ok"
      )
    );

    const messages = clear(element("ceph-messages"));
    element("ceph-messages").hidden = ceph.messages.length === 0;
    ceph.messages.forEach((message) => {
      const item = document.createElement("li");
      item.textContent = message.severity
        ? `${message.name} (${message.severity.replace("HEALTH_", "")})`
        : message.name;
      messages.append(item);
    });

    renderCapacity(ceph);
    renderDaemons(ceph);
    renderOsds(ceph);
    renderPools(ceph);
    renderPgs(ceph);

    element("storage-lead").textContent =
      `Read from ${ceph.source}, which holds the active manager. ` +
      (ceph.versions && Object.keys(ceph.versions).length > 1
        ? "The daemons are not all on the same Ceph version: " +
          Object.entries(ceph.versions)
            .map(([version, count]) => `${count} on ${version}`)
            .join(", ") +
          "."
        : "All daemons on " + (Object.keys(ceph.versions)[0] || "an unknown version") + ".");
    summarise("storage", healthStatus(ceph), storageAnswer(ceph));
  }

  function healthClass(health) {
    if (health === "HEALTH_OK") {
      return "ok";
    }
    return health === "HEALTH_ERR" ? "bad" : health === "HEALTH_WARN" ? "warn" : "";
  }

  function healthStatus(ceph) {
    if (ceph.health === "HEALTH_ERR") {
      return "error";
    }
    if (ceph.health === "HEALTH_WARN") {
      return "warning";
    }
    return ceph.health === "HEALTH_OK" ? "ok" : "unknown";
  }

  function storageAnswer(ceph) {
    const parts = [ceph.health.replace("HEALTH_", "")];
    const down = ceph.osds.length - ceph.osds_up;
    if (down) {
      parts.push(`${plural(down, "OSD")} down`);
    }
    if (ceph.pgs_not_clean) {
      parts.push(`${ceph.pgs_not_clean} PGs not clean`);
    }
    parts.push(`${percent(ceph.used_ratio)} of ${bytes(ceph.total_bytes)} used`);
    return parts.join(", ");
  }

  // Capacity as a bar rather than as two numbers. What an operator reads here
  // is how close the cluster is to full, and a ratio is read faster from a
  // length than from a division.
  function renderCapacity(ceph) {
    const box = clear(element("ceph-capacity"));
    const ratio = ceph.used_ratio || 0;
    const label = document.createElement("p");
    label.className = "capacity-label";
    label.textContent =
      `${bytes(ceph.used_bytes)} used of ${bytes(ceph.total_bytes)} raw ` +
      `(${percent(ceph.used_ratio)}), ` +
      `${bytes(ceph.total_bytes - ceph.used_bytes)} free`;
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("span");
    // Ceph's own thresholds: it warns at 85% and stops accepting writes at 95%.
    fill.className =
      ratio >= 0.95 ? "fill bad" : ratio >= 0.85 ? "fill warn" : "fill";
    fill.style.width = `${Math.min(100, ratio * 100).toFixed(1)}%`;
    bar.append(fill);
    box.append(label, bar);
  }

  function renderDaemons(ceph) {
    const body = clear(element("daemon-rows"));
    [...ceph.monitors, ...ceph.managers, ...ceph.metadata_servers].forEach(
      (daemon) =>
        row(body, [
          cell(daemon.name),
          cell(daemon.host || "–"),
          cell(daemon.state || "–", daemon.ok ? "state-free" : "state-failed"),
          cell(daemon.version || "–"),
        ])
    );
  }

  function renderOsds(ceph) {
    const body = clear(element("osd-rows"));
    ceph.osds.forEach((osd) => {
      const state = osd.up
        ? osd.in_cluster
          ? "up, in"
          : "up, out"
        : osd.in_cluster
          ? "down, in"
          : "down, out";
      const latency =
        osd.apply_latency_ms === null || osd.apply_latency_ms === undefined
          ? "–"
          : `${osd.apply_latency_ms} / ${osd.commit_latency_ms ?? "?"} ms`;
      const line = row(body, [
        cell(osd.name),
        cell(osd.host || "–"),
        cell(osd.device_class || "–"),
        cell(state, osd.up && osd.in_cluster ? "state-free" : "state-failed"),
        cell(
          osd.total_bytes
            ? `${percent(osd.used_ratio)} of ${bytes(osd.total_bytes)}`
            : "–"
        ),
        cell(osd.pgs),
        cell(latency),
      ]);
      line.classList.toggle("row-bad", !(osd.up && osd.in_cluster));
    });
  }

  function renderPools(ceph) {
    const body = clear(element("pool-rows"));
    ceph.pools.forEach((pool) =>
      row(body, [
        cell(pool.name),
        cell(pool.type || "–"),
        cell(bytes(pool.stored_bytes)),
        cell(bytes(pool.used_bytes)),
        cell(bytes(pool.available_bytes)),
        cell(pool.objects.toLocaleString()),
      ])
    );
  }

  function renderPgs(ceph) {
    const box = clear(element("pg-states"));
    const states = Object.entries(ceph.pg_states);
    if (!states.length) {
      box.textContent = "The manager published no placement group states.";
      return;
    }
    states.forEach(([name, count]) => {
      const tag = document.createElement("span");
      // Clean and active are the two that mean nothing is wrong. Everything
      // else is either work in progress or a finding, and both are worth the
      // eye: the count beside the word says which.
      tag.className =
        name === "clean" || name === "active" ? "tag" : "tag warn";
      tag.textContent = `${count} ${name}`;
      box.append(tag, " ");
    });
  }

  // Views

  function showView(name) {
    document.querySelectorAll("#views .view").forEach((tab) => {
      const current = tab.dataset.view === name;
      tab.classList.toggle("current", current);
      if (current) {
        tab.setAttribute("aria-current", "true");
      } else {
        tab.removeAttribute("aria-current");
      }
    });
    Object.entries(VIEWS).forEach(([view, entry]) => {
      element(entry.card).hidden = view !== name;
    });
  }

  document.querySelectorAll("#views .view").forEach((tab) => {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  });

  async function loadCluster() {
    const cluster = await API.get("/cluster");
    renderMembers(cluster);
    renderResources(cluster);
  }

  async function loadStorage() {
    renderStorage(await API.get("/storage"));
  }

  async function start() {
    await Chrome.load();
    showView("members");
    // Both readings are fetched before either panel is looked at, so switching
    // views is a show and a hide. They are independent requests because they
    // fail independently: a cluster with no Ceph must not cost the membership
    // panel its answer, and a manager that is slow must not hold it up.
    const results = await Promise.allSettled([loadCluster(), loadStorage()]);
    const failures = results
      .filter((item) => item.status === "rejected")
      .map((item) => item.reason.message);
    if (failures.length) {
      showBanner(failures.join(" "));
      ["members-loading", "resources-loading", "storage-loading"].forEach(
        (id) => {
          element(id).hidden = true;
        }
      );
    }
  }

  start().catch((failure) => {
    showBanner(failure.message);
    ["members-loading", "resources-loading", "storage-loading"].forEach((id) => {
      element(id).hidden = true;
    });
  });
})();
