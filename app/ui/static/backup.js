// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The Backup page: the upstream `backup_restore` role, without its menu.
//
// Three readings and four acts. Where the backups go comes out of the
// inventory, what a full backup would weigh comes out of `rbd du`, and what
// the backup server holds comes out of the last listing run, because nothing
// in this container can reach that server: the SSH trust to it belongs to the
// cluster members and is the one the backups are pushed with.
//
// The one decision worth stating is what the confirmations say. Each script
// pauses on a `read -r` before it deletes something, and a run answers that
// prompt on purpose so the question is asked here instead. A full backup
// purges the snapshots of every image it exports, which retires the increments
// of the previous backup; a restore replaces a running guest with what a
// directory on another machine holds. Both windows say so in those words.

(function () {
  let canAct = false;
  let canWrite = false;
  let view = null;
  // The last two readings the staging card compares: the room on the machine
  // and what a full backup would write into it.
  let staged = null;
  let measured = null;
  // The mounted volume the staging directories could move to.
  let target = null;
  // The local volume window: the machine it reads, and what it read there.
  let disks = null;
  let nameEdited = false;
  // The last reading of the connection to the backup server.
  let connection = null;

  function element(id) {
    return document.getElementById(id);
  }

  function showBanner(message) {
    const banner = element("banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  function clear(node) {
    node.replaceChildren();
    return node;
  }

  function cell(text) {
    const node = document.createElement("td");
    node.textContent = text;
    return node;
  }

  function row(parent, cells) {
    const line = document.createElement("tr");
    cells.forEach((item) => line.append(item));
    parent.append(line);
    return line;
  }

  // Binary multiples, which is what `rbd du` counts in and what a storage
  // figure on the Cluster page is already shown in.
  function size(bytes) {
    if (!bytes) {
      return "0 B";
    }
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return (value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)) +
      " " + units[unit];
  }

  // `202603110836` as `2026-03-11 08:36`. The scripts name every file after
  // the minute the backup started, and twelve digits is a string an operator
  // has to decode before deciding whether it is the right one.
  function readable(date) {
    if (!/^[0-9]{12}$/.test(date)) {
      return date;
    }
    return (
      date.slice(0, 4) + "-" + date.slice(4, 6) + "-" + date.slice(6, 8) +
      " " + date.slice(8, 10) + ":" + date.slice(10, 12)
    );
  }

  // Where the backups go

  function renderTarget(payload) {
    const configured = payload.configured;
    element("loading").hidden = true;
    element("target").hidden = !configured;
    element("acts").hidden = !configured || !canAct;
    element("acts-help").hidden = !configured || !canAct;
    element("empty").textContent = payload.note || "";
    element("empty").hidden = !payload.note;

    const lead = element("lead");
    const warnings = payload.warnings || [];
    lead.textContent = warnings.join(" ");
    lead.hidden = warnings.length === 0;

    if (!configured) {
      return;
    }
    const held = settingsByKey(payload);
    element("target-server").textContent = payload.target;
    element("target-filter").textContent =
      (held.include_vm || ".*") +
      (held.exclude_vm ? ", except " + held.exclude_vm : "");
    element("target-staging").textContent =
      held.local_dir + " for a backup, " + held.local_tmp_dir + " for a restore";
    // One member runs every backup and answers every reading on this page,
    // so the staging room and the key that matter are that machine's.
    element("target-runner").textContent = payload.runs_on
      ? payload.runs_on + ", the first hypervisor of the cluster by name"
      : "no cluster member";
  }

  function settingsByKey(payload) {
    const held = {};
    (payload.settings || []).forEach((setting) => {
      held[setting.key] = setting.value;
    });
    return held;
  }

  // What a full backup would weigh
  //
  // Asked for rather than read with the page. `rbd du` adds up the objects of
  // every image in the pool, which is minutes on a real cluster, so a panel
  // that fetched it on every visit held the page up and then showed a timeout.
  //
  // A ceiling, and the page says so in those words. The rows `rbd du` answers
  // are deltas between snapshots, so a guest is worth its image and its
  // snapshots added up, and a block rewritten since a snapshot is counted in
  // both rows. The service bounds the sum by what the disk provisions.

  function renderEstimate(estimate) {
    measured = estimate.error ? null : estimate;
    renderRoom();
    const guests = estimate.guests || [];
    element("estimate-error").textContent = estimate.error || "";
    element("estimate-error").hidden = !estimate.error;
    element("estimate-table").hidden = guests.length === 0;
    element("estimate-total").textContent = guests.length
      ? "at most " + size(estimate.used_bytes) + " over " + guests.length +
        " guests"
      : "";

    const body = clear(element("estimate-rows"));
    guests.forEach((guest) => {
      row(body, [
        cell(guest.guest),
        cell(String(guest.images.length)),
        cell(size(guest.used_bytes)),
        cell(size(guest.provisioned_bytes)),
      ]);
    });

    const excluded = estimate.excluded || [];
    element("estimate-excluded").textContent = excluded.length
      ? "Left out by the filters: " + excluded.join(", ")
      : "";
  }

  async function measure() {
    const button = element("estimate-go");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    element("estimate-loading").hidden = false;
    element("estimate-error").hidden = true;
    try {
      renderEstimate(await API.get("/backup/estimate"));
    } catch (failure) {
      const error = element("estimate-error");
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      element("estimate-loading").hidden = true;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  // The staging directories
  //
  // Read on the member the backups run on, since that is where a full backup
  // writes a qcow2 of every guest before sending anything. The root file
  // system of a machine installed from the ISO is a few tens of gigabytes, so
  // the room is the finding, and the estimate, once measured, is what it is
  // held against.

  const PURPOSES = { backup: "a full backup", restore: "a restore" };

  function renderStaging(reading) {
    staged = reading;
    element("staging-host").textContent = reading.host || "the cluster";
    element("staging-when").textContent = reading.read_at
      ? "read " + whenRead(reading.read_at)
      : "";
    element("staging-note").textContent = reading.note || "";
    element("staging-note").hidden = !reading.note;

    const directories = reading.directories || [];
    element("staging-table").hidden = directories.length === 0;
    const body = clear(element("staging-rows"));
    directories.forEach((item) => {
      const state = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = RunStream.stateClass(item.exists ? "success" : "failed");
      badge.textContent = item.exists ? "present" : "missing";
      state.append(badge);
      row(body, [
        cell(item.path),
        cell(PURPOSES[item.purpose] || item.purpose),
        state,
        cell(
          item.mountpoint
            ? item.mountpoint +
                (item.exists ? "" : " (where it would be created)")
            : "unknown"
        ),
        cell(
          item.free_bytes === null || item.free_bytes === undefined
            ? "unknown"
            : size(item.free_bytes) + " of " + size(item.size_bytes)
        ),
      ]);
    });

    const missing = directories.some((item) => !item.exists);
    element("staging-create").hidden = !missing;
    element("staging-help").hidden = !canWrite || !missing;

    // A volume the inventory declares on that machine, mounted, and not yet
    // holding the staging directories, is where they were meant to go. One
    // that is not mounted is not offered: the directories would be created on
    // the file system below it, and the role would then refuse to mount over
    // a directory that is not empty.
    const backup = directories.find((item) => item.purpose === "backup");
    target = (reading.volumes || []).find(
      (volume) =>
        volume.mounted &&
        backup &&
        !backup.path.startsWith(volume.mountpoint.replace(/\/$/, "") + "/")
    );
    const move = element("staging-move");
    move.hidden = !target;
    move.textContent = target ? "Stage them on " + target.mountpoint : "";
    element("staging-acts").hidden = !canWrite || !reading.host;
    renderRoom();
  }

  // The settings form, opened with the two staging directories under the
  // volume. Saving it is the ordinary commit, and the card then offers to
  // create them there.
  function stageOn(volume) {
    const root = volume.mountpoint.replace(/\/$/, "");
    showSettings(true, {
      local_dir: root + "/seapath-backup/",
      local_tmp_dir: root + "/seapath-restore/",
    });
  }

  function renderRoom() {
    const note = element("staging-room");
    const backup = ((staged && staged.directories) || []).find(
      (item) => item.purpose === "backup"
    );
    let text = "";
    if (backup && typeof backup.free_bytes === "number") {
      const where =
        backup.mountpoint === "/"
          ? "the root file system"
          : "the file system mounted on " + backup.mountpoint;
      if (measured && measured.used_bytes > backup.free_bytes) {
        text =
          "A full backup of the selected guests writes up to " +
          size(measured.used_bytes) + " into " + backup.path + ", and " +
          where + " has " + size(backup.free_bytes) + " free. It would " +
          "fail before anything reaches the backup server.";
      } else if (!measured && backup.mountpoint === "/") {
        text =
          backup.path + " is on the root file system, with " +
          size(backup.free_bytes) + " free. A full backup writes a qcow2 " +
          "of every selected guest there first: measure the estimated " +
          "volume below to compare.";
      }
    }
    note.textContent = text;
    note.hidden = !text;
  }

  async function readStaging() {
    const button = element("staging-read");
    button.disabled = true;
    element("staging-loading").hidden = false;
    try {
      renderStaging(await API.get("/backup/staging"));
    } catch (failure) {
      renderStaging({ note: failure.message, directories: [] });
    } finally {
      element("staging-loading").hidden = true;
      button.disabled = false;
    }
  }

  function confirmCreateStaging() {
    const missing = ((staged && staged.directories) || [])
      .filter((item) => !item.exists)
      .map((item) => item.path);
    confirm({
      title: "Create the staging directories",
      body:
        "Runs seapath_setup_backup_restore on every cluster member. It creates " +
        missing.join(" and ") + ", readable by root only, installs the " +
        "backup scripts and renders /etc/backup-restore.conf from the " +
        "inventory. No service restarts and no guest is touched.",
      note:
        "The directories are created on the file system that holds them " +
        "today. If that is the root file system and a full backup needs " +
        "more room than it has, give them a volume of their own first.",
      label: "Create them",
      act: async () => {
        const started = await API.post("/runs", {
          playbook: "seapath_setup_backup_restore",
        });
        RunWatch.open(started.run_id, readStaging);
      },
    });
  }

  // A local volume
  //
  // One entry in the machine's `configure_local_storage_volumes`, then a run of
  // `seapath_setup_local_storage` narrowed to that machine. The window reads
  // the disks first, offers only those the role would accept, and says in its
  // last sentence which disk is about to be partitioned.

  async function openVolume() {
    element("volume").hidden = false;
    element("volume-error").hidden = true;
    await readVolume(staged && staged.host);
  }

  async function readVolume(host) {
    element("volume-loading").hidden = false;
    element("volume-form").hidden = true;
    element("volume-go").disabled = true;
    try {
      const path = "/storage/local" + (host ? "?host=" + encodeURIComponent(host) : "");
      renderVolume(await API.get(path));
    } catch (failure) {
      renderVolume({ note: failure.message, disks: [], hosts: [] });
    } finally {
      element("volume-loading").hidden = true;
    }
  }

  function renderVolume(reading) {
    disks = reading;
    const hosts = clear(element("volume-host"));
    (reading.hosts || []).forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent =
        name + (view && name === view.runs_on ? ", where the backups run" : "");
      hosts.append(option);
    });
    hosts.value = reading.host || "";

    element("volume-note").textContent = reading.note || "";
    element("volume-note").hidden = !reading.note;

    const declared = reading.declared || [];
    element("volume-declared-block").hidden = declared.length === 0;
    const list = clear(element("volume-declared"));
    declared.forEach((volume) => {
      const item = document.createElement("li");
      item.textContent =
        volume.mountpoint + " (" + volume.name + ", " +
        (volume.lvm_vg ? "LVM " + volume.lvm_vg + "/" + volume.lvm_lv : "direct") +
        "), " +
        (volume.mounted ? "mounted" : "not mounted yet");
      list.append(item);
    });

    const body = clear(element("volume-disks"));
    const usable = (reading.disks || []).filter((disk) => disk.usable);
    // The system disk first: on a machine from the ISO it is where the room
    // is, and it is the case this window was written for.
    const preferred =
      usable.find((disk) => disk.system) || usable[0] || null;
    (reading.disks || []).forEach((disk) => {
      const pick = document.createElement("td");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "volume-disk";
      radio.value = disk.path;
      radio.disabled = !disk.usable;
      radio.checked = disk === preferred;
      radio.setAttribute("aria-label", disk.path);
      radio.addEventListener("change", summarise);
      pick.append(radio);
      const notes = [];
      if (disk.system) {
        notes.push("holds the running system");
      }
      if (disk.reason) {
        notes.push(disk.reason);
      }
      row(body, [
        pick,
        cell(disk.path + (disk.model ? " (" + disk.model + ")" : "")),
        cell(size(disk.size_bytes)),
        cell(disk.usable ? size(disk.free_bytes) : "none usable"),
        cell(notes.join(". ")),
      ]);
    });

    const groups = clear(element("volume-vg"));
    const fresh = document.createElement("option");
    fresh.value = "";
    fresh.textContent = "A new group";
    groups.append(fresh);
    (reading.volume_groups || []).forEach((group) => {
      const option = document.createElement("option");
      option.value = group.name;
      option.textContent =
        group.name + ", " + size(group.free_bytes) + " free of " +
        size(group.size_bytes);
      groups.append(option);
    });

    // A mount point nothing on this machine declares yet: /data, then /data2.
    const taken = new Set(declared.map((volume) => volume.mountpoint));
    const mountpoint = element("volume-mountpoint");
    let count = 1;
    while (taken.has(mountpoint.value.trim())) {
      count += 1;
      mountpoint.value = "/data" + count;
    }

    element("volume-form").hidden = !preferred;
    summarise();
  }

  function choice(name) {
    const checked = document.querySelector('input[name="' + name + '"]:checked');
    return checked ? checked.value : "";
  }

  // What the window would write, or null when it cannot write anything yet.
  function volumeEntry() {
    const disk = choice("volume-disk");
    if (!disk) {
      return null;
    }
    const lvm = choice("volume-layout") === "lvm";
    const group = element("volume-vg").value;
    const gib = parseInt(element("volume-gib").value, 10);
    return {
      name: element("volume-name").value.trim(),
      disk,
      mountpoint: element("volume-mountpoint").value.trim(),
      size: choice("volume-size") === "some" && gib > 0 ? gib + "G" : "100%",
      fstype: element("volume-fstype").value,
      lvm_vg: lvm ? group || element("volume-vg-name").value.trim() : "",
      lvm_lv: lvm ? element("volume-lv").value.trim() : "",
    };
  }

  function summarise() {
    const lvm = choice("volume-layout") === "lvm";
    element("volume-lvm").hidden = !lvm;
    const group = element("volume-vg").value;
    element("volume-vg-name").hidden = !lvm || Boolean(group);
    element("volume-vg-name-label").hidden = !lvm || Boolean(group);
    element("volume-vg-help").textContent = group
      ? "The new partition joins " + group + " as another PV, and its other " +
        "PVs stay where they are. A logical volume of all the free space " +
        "then takes what the group already had free as well."
      : "A group of its own, holding the new partition alone.";
    if (!nameEdited) {
      const last = element("volume-mountpoint").value.trim().split("/").pop();
      element("volume-name").value = last || "data";
    }

    const entry = volumeEntry();
    const summary = element("volume-summary");
    element("volume-go").disabled = !entry;
    if (!entry) {
      summary.hidden = true;
      return;
    }
    const disk = (disks.disks || []).find((item) => item.path === entry.disk);
    const free = size(disk ? disk.free_bytes : 0) + " free after its last partition";
    summary.textContent =
      "Commits this volume on " + disks.host + ", then partitions " +
      entry.disk +
      (disk && disk.system ? ", the disk the system runs from," : "") +
      " with " +
      (entry.size === "100%" ? "all of the " + free : entry.size + "iB of the " + free) +
      ", " +
      (entry.lvm_vg
        ? "makes it a PV of " + entry.lvm_vg + " with a logical volume " + entry.lvm_lv + ", "
        : "") +
      "formats it " + entry.fstype + " and mounts it on " + entry.mountpoint +
      " by UUID. The partitions already there are neither moved nor resized.";
    summary.hidden = false;
  }

  async function createVolume() {
    const go = element("volume-go");
    const error = element("volume-error");
    const entry = volumeEntry();
    error.hidden = true;
    go.disabled = true;
    go.setAttribute("aria-busy", "true");
    try {
      await API.post(
        "/storage/local/" + encodeURIComponent(disks.host) + "/volumes",
        entry
      );
      const started = await API.post("/runs", {
        playbook: "seapath_setup_local_storage",
        scope: { hosts: [disks.host] },
      });
      element("volume").hidden = true;
      RunWatch.open(started.run_id, () => refresh(true));
      await refresh(true);
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      go.disabled = false;
    } finally {
      go.removeAttribute("aria-busy");
    }
  }

  // The connection to the backup server
  //
  // Three steps, and the card offers whichever comes next. A dedicated key is
  // a commit of the server's confirmed host key, the key path and a shell
  // using it; the role then generates the key on every member; and the keys
  // reach the server with a password typed once. Every member is then asked
  // to connect the way a backup does.

  function renderConnection(reading) {
    connection = reading;
    element("connection-when").textContent = reading.read_at
      ? "checked " + whenRead(reading.read_at)
      : "";
    element("connection-note").textContent = reading.note || "";
    element("connection-note").hidden = !reading.note;

    const members = reading.members || [];
    element("connection-facts").hidden = !reading.server;
    element("connection-hostkey").textContent = (reading.host_keys || []).length
      ? reading.host_keys
          .map((key) => key.key_type + " " + key.fingerprint)
          .join(", ") + ", confirmed in the inventory"
      : "not confirmed: the members accept whatever answers only if their " +
        "known_hosts already holds it";
    element("connection-key").textContent = reading.key_path
      ? reading.key_path +
        (reading.uses_key
          ? ", which the remote shell uses"
          : ", which the remote shell does not name yet")
      : "root's default key on each member";

    element("connection-table").hidden = members.length === 0;
    const body = clear(element("connection-rows"));
    members.forEach((member) => {
      const reach = document.createElement("td");
      const badge = document.createElement("span");
      const state =
        member.reaches === true
          ? "success"
          : member.reaches === false
            ? "failed"
            : "interrupted";
      badge.className = RunStream.stateClass(state);
      badge.textContent =
        member.reaches === true ? "yes" : member.reaches === false ? "no" : "unknown";
      reach.append(badge);
      if (member.message) {
        const why = document.createElement("span");
        why.className = "pane-foot-note";
        why.textContent = " " + member.message;
        reach.append(why);
      }
      row(body, [
        cell(member.host),
        cell(
          member.key
            ? member.key.split(" ")[0] + " " + member.key.split(" ")[2]
            : reading.key_path
              ? "not generated yet"
              : "root's default key"
        ),
        reach,
      ]);
    });

    const dedicated = Boolean(reading.key_path && (reading.host_keys || []).length);
    const keyless = members.some((member) => member.reaches !== null && !member.key);
    const refused = members.some((member) => member.reaches === false);
    const prepare = !dedicated || !reading.uses_key;
    const generate = dedicated && keyless;
    const install =
      dedicated && refused && members.some((member) => member.key);
    element("connection-prepare").hidden = !prepare;
    element("connection-generate").hidden = !generate;
    element("connection-install").hidden = !install;
    element("connection-acts").hidden =
      !canWrite || !reading.server || !(prepare || generate || install);

    const help = element("connection-help");
    help.textContent = prepare
      ? "The backups are pushed by root on the member that runs them, with no " +
        "password. A key of their own, generated by the backup_restore role, " +
        "can be revoked on the server without touching anything else root " +
        "trusts."
      : generate
        ? "The inventory names the key, and some members do not have it yet. " +
          "Generating it is a run of seapath_setup_backup_restore, which " +
          "creates it once on every member and never replaces it."
        : install
          ? "Installing the keys is what ssh-copy-id does, for every member at " +
            "once: one connection to " + reading.server + " with its password, " +
            "and each member's public key appended to authorized_keys there."
          : "";
    help.hidden = !canWrite || !help.textContent;
  }

  async function readConnection() {
    const button = element("connection-read");
    button.disabled = true;
    element("connection-loading").hidden = false;
    try {
      renderConnection(await API.get("/backup/connection"));
    } catch (failure) {
      renderConnection({ note: failure.message, members: [] });
    } finally {
      element("connection-loading").hidden = true;
      button.disabled = false;
    }
  }

  async function openTrust() {
    element("trust").hidden = false;
    element("trust-error").hidden = true;
    element("trust-compare").hidden = true;
    element("trust-go").disabled = true;
    const list = clear(element("trust-keys"));
    element("trust-loading").hidden = false;
    try {
      const keys = await API.post("/backup/connection/scan");
      const host = connection.server.split("@").pop();
      element("trust-compare").textContent =
        host + " answered with the keys below. Compare each fingerprint with " +
        "what the server itself prints for `ssh-keygen -lf " +
        "/etc/ssh/ssh_host_ed25519_key.pub` before trusting it: a key read " +
        "over the network comes from whoever answered.";
      element("trust-compare").hidden = false;
      keys.forEach((key) => {
        const label = document.createElement("label");
        label.className = "switch";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = true;
        box.value = key.line;
        box.addEventListener("change", () => {
          element("trust-go").disabled = !list.querySelector("input:checked");
        });
        const text = document.createElement("code");
        text.textContent = key.key_type + " " + key.fingerprint;
        label.append(box, text);
        list.append(label);
      });
      element("trust-go").disabled = keys.length === 0;
    } catch (failure) {
      element("trust-error").textContent = failure.message;
      element("trust-error").hidden = false;
    } finally {
      element("trust-loading").hidden = true;
    }
  }

  async function trustServer() {
    const go = element("trust-go");
    const error = element("trust-error");
    const lines = Array.from(
      element("trust-keys").querySelectorAll("input:checked")
    ).map((box) => box.value);
    go.disabled = true;
    go.setAttribute("aria-busy", "true");
    error.hidden = true;
    try {
      await API.put(
        "/backup/connection/key",
        { host_keys: lines },
        view.commit ? { "If-Match": view.commit } : undefined
      );
      const started = await API.post("/runs", {
        playbook: "seapath_setup_backup_restore",
      });
      element("trust").hidden = true;
      RunWatch.open(started.run_id, () => refresh(true));
      await refresh(true);
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      go.disabled = false;
    } finally {
      go.removeAttribute("aria-busy");
    }
  }

  function confirmGenerate() {
    confirm({
      title: "Generate the backup keys",
      body:
        "Runs seapath_setup_backup_restore on every cluster member. Where a " +
        "member has no key at " + connection.key_path + " yet, the role " +
        "generates one; an existing key is never replaced, since the server " +
        "trusts it. It also renders /etc/backup-restore.conf and trusts the " +
        "server's host key. No service restarts and no guest is touched.",
      label: "Generate them",
      act: async () => {
        const started = await API.post("/runs", {
          playbook: "seapath_setup_backup_restore",
        });
        RunWatch.open(started.run_id, readConnection);
      },
    });
  }

  function openInstall() {
    const server = connection.server;
    const user = server.includes("@") ? server.split("@")[0] : "the account";
    const keys = (connection.members || []).filter((member) => member.key);
    element("install-what").textContent =
      "Appends the backup key of " +
      keys.map((member) => member.host).join(", ") +
      " to ~/.ssh/authorized_keys of " + server + ". A key already there is " +
      "left alone and nothing is removed.";
    element("install-label").textContent =
      "Password of " + user + " on " + server.split("@").pop();
    element("install-password").value = "";
    element("install-error").hidden = true;
    element("install").hidden = false;
    element("install-password").focus();
  }

  async function installKeys() {
    const go = element("install-go");
    const error = element("install-error");
    const password = element("install-password");
    go.disabled = true;
    go.setAttribute("aria-busy", "true");
    error.hidden = true;
    try {
      const answer = await API.post("/backup/connection/install", {
        password: password.value,
      });
      password.value = "";
      element("install").hidden = true;
      renderConnection(answer);
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      go.disabled = false;
      go.removeAttribute("aria-busy");
    }
  }

  // What the server holds

  function renderCatalogue(catalogue) {
    const backups = catalogue.backups || [];
    element("catalogue-note").textContent = catalogue.note || "";
    element("catalogue-note").hidden = !catalogue.note;
    element("catalogue-table").hidden = backups.length === 0;
    element("catalogue-when").textContent = catalogue.read_at
      ? "Read from " + catalogue.read_from + ", " + whenRead(catalogue.read_at)
      : "";

    const body = clear(element("catalogue-rows"));
    backups.forEach((backup) => {
      (backup.guests || []).forEach((guest) => {
        const dates = guest.dates || [];
        row(body, [
          cell(readable(backup.date)),
          cell(guest.guest),
          cell(String(guest.disks)),
          cell(
            dates.length
              ? readable(dates[0]) +
                  (dates.length > 1
                    ? " to " + readable(dates[dates.length - 1])
                    : "")
              : "no libvirt XML"
          ),
          restoreCell(backup, guest),
        ]);
      });
    });
  }

  // An ISO timestamp the run record carries, shown to the minute like every
  // other date on this page.
  function whenRead(iso) {
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) {
      return iso;
    }
    return at.toLocaleString();
  }

  function restoreCell(backup, guest) {
    const node = document.createElement("td");
    node.className = "acts";
    if (!canWrite || !(guest.dates || []).length) {
      return node;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = "Restore";
    button.addEventListener("click", () => confirmRestore(backup, guest));
    node.append(button);
    return node;
  }

  // The confirmations

  function confirm({ title, body, note, label, choose, act }) {
    element("confirm-title").textContent = title;
    element("confirm-disruption").textContent = body;
    element("confirm-note").textContent = note || "";
    element("confirm-note").hidden = !note;
    element("confirm-error").hidden = true;

    const picker = element("confirm-date");
    element("confirm-choice").hidden = !choose;
    if (choose) {
      element("confirm-choice-label").textContent = choose.label;
      clear(picker);
      choose.options.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = readable(value);
        picker.append(option);
      });
      // The newest is what an operator almost always wants, and the list is
      // oldest first because that is the order the diffs are replayed in.
      picker.value = choose.options[choose.options.length - 1];
    }

    const go = element("confirm-go");
    go.textContent = label;
    go.disabled = false;
    go.onclick = async () => {
      go.disabled = true;
      go.setAttribute("aria-busy", "true");
      try {
        await act(choose ? picker.value : undefined);
        element("confirm").hidden = true;
      } catch (failure) {
        const error = element("confirm-error");
        error.textContent = failure.message;
        error.hidden = false;
        go.disabled = false;
      } finally {
        go.removeAttribute("aria-busy");
      }
    };
    element("confirm").hidden = false;
  }

  function confirmFull() {
    confirm({
      title: "Back up every guest, in full",
      body:
        "Exports every selected guest's disks from Ceph as qcow2 and sends " +
        "them to " + view.target + ". The guests keep running throughout. " +
        "Expect an hour or more on a cluster holding a dozen guests, and the " +
        "cluster takes no other run until it ends.",
      note:
        "Before it exports an image it removes every snapshot that image " +
        "carries, with `rbd snap purge`, and takes the base snapshot this " +
        "backup and the increments after it are made against. The increments " +
        "of the previous full backup can no longer be applied afterwards. It " +
        "also empties the staging directory on the machine, which is where " +
        "the previous full backup was kept.",
      label: "Back it all up",
      act: async () => {
        RunWatch.open((await API.post("/backup/full")).run_id);
      },
    });
  }

  function confirmIncremental() {
    confirm({
      title: "Back up what changed",
      body:
        "Exports what each image changed since its latest snapshot, as an RBD " +
        "diff beside the full backup it belongs to, and sends the directory " +
        "to " + view.target + " again. The guests keep running.",
      note:
        "A disk added since the last full backup has no snapshot to diff " +
        "against. It is skipped with a warning in the log and stays out of " +
        "every incremental backup until a new full one is taken.",
      label: "Back up the changes",
      act: async () => {
        RunWatch.open((await API.post("/backup/incremental")).run_id);
      },
    });
  }

  function confirmRestore(backup, guest) {
    confirm({
      title: "Restore " + guest.guest + " from " + readable(backup.date),
      body:
        "Recreates " + guest.guest + " from the backup and starts it. " +
        "`vm-mgr create --force` replaces whatever is there under that name: " +
        "the disks it has now, its Pacemaker resource and the metadata on its " +
        "image are all overwritten by what the backup carries.",
      note:
        "Everything written to " + guest.guest + " since the date chosen here " +
        "is gone, and nothing on this page brings it back. If it is running " +
        "now, the running guest is destroyed. The restore staging directory " +
        "on the machine is emptied first.",
      choose: { label: "Replay the changes up to", options: guest.dates },
      label: "Restore it",
      act: async (date) => {
        const started = await API.post("/backup/restore", {
          guest: guest.guest,
          full_date: backup.date,
          date,
        });
        RunWatch.open(started.run_id);
      },
    });
  }

  // A read, over one SSH connection, so it answers here rather than launching
  // a run and asking the operator to watch it. It used to be a run, which held
  // the cluster's lock while somebody browsed.
  async function readServer() {
    const button = element("act-list");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    element("catalogue-loading").hidden = false;
    try {
      renderCatalogue(await API.get("/backup/catalogue"));
    } catch (failure) {
      showBanner(failure.message);
    } finally {
      element("catalogue-loading").hidden = true;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  // Where they go

  function showSettings(open, overrides) {
    element("settings").hidden = !open;
    element("settings-error").hidden = true;
    element("settings-done").hidden = true;
    if (!open) {
      return;
    }
    const settings = view.settings || [];
    // A field the inventory is silent about starts from what this machine's
    // own /etc/backup-restore.conf says, because a site that has been driving
    // the whiptail menu decided that value years ago and retyping it is how it
    // gets typed wrong. Where the inventory holds a value it wins: it is the
    // one a run passes, and the one the role renders the file from.
    const fields = clear(element("settings-fields"));
    settings.forEach((setting) => {
      const label = document.createElement("label");
      label.setAttribute("for", "set-" + setting.key);
      label.textContent = setting.label + (setting.required ? "" : " (optional)");
      const input = document.createElement("input");
      input.type = "text";
      input.id = "set-" + setting.key;
      input.value = setting.value || setting.on_machine || "";
      input.placeholder = setting.placeholder || "";
      input.autocomplete = "off";
      input.spellcheck = false;
      const help = document.createElement("p");
      help.className = "help";
      help.textContent = setting.help + " Written as `" + setting.name + "`.";
      if (setting.on_machine && setting.on_machine !== setting.value) {
        const said = document.createElement("span");
        said.className = "assumed";
        said.textContent =
          " " + view.conf_path + " on this machine says " + setting.on_machine + ".";
        help.append(said);
      }
      if (overrides && overrides[setting.key]) {
        input.value = overrides[setting.key];
      }
      fields.append(label, input, help);
    });

    const fromFile = settings.filter((setting) => setting.on_machine);
    element("settings-from-file").textContent = view.conf_found
      ? view.conf_path +
        " on this machine holds " +
        fromFile.length +
        " of the seven, and the fields below start from them where the " +
        "inventory is silent. Committing puts them under the inventory, which " +
        "is what reaches the other machines."
      : "";
    element("settings-from-file").hidden = !view.conf_found || !fromFile.length;
    element("settings-take-row").hidden = !fromFile.some(
      (setting) => setting.on_machine !== setting.value
    );
  }

  // Every field back to what the machine's own file says. One button, because
  // the alternative is seven copies by hand out of a panel the operator cannot
  // see while the form is open.
  function takeFromMachine() {
    (view.settings || []).forEach((setting) => {
      if (setting.on_machine) {
        element("set-" + setting.key).value = setting.on_machine;
      }
    });
  }

  async function saveSettings() {
    const save = element("settings-save");
    const error = element("settings-error");
    const done = element("settings-done");
    error.hidden = true;
    done.hidden = true;
    save.disabled = true;
    save.setAttribute("aria-busy", "true");
    try {
      const payload = {};
      (view.settings || []).forEach((setting) => {
        payload[setting.key] = element("set-" + setting.key).value.trim();
      });
      const answer = await API.put(
        "/backup/settings",
        payload,
        view.commit ? { "If-Match": view.commit } : undefined
      );
      done.textContent = answer.commit
        ? "Saved to the inventory as commit " +
          answer.commit.slice(0, 8) +
          ": " +
          answer.message
        : answer.message;
      done.hidden = false;
      await refresh(true);
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      save.disabled = false;
      save.removeAttribute("aria-busy");
    }
  }

  const KEPT = "backup";

  function draw(answer) {
    view = answer;
    renderTarget(answer);
    element("settings-open").hidden = !canWrite;
    // Both panels are about a backup that has somewhere to go. Until the
    // inventory says where, the page is the form and the sentence that sends
    // an operator to it.
    element("estimate-card").hidden = !answer.configured;
    element("catalogue-card").hidden = !answer.configured;
    element("staging-card").hidden = !answer.configured;
    element("connection-card").hidden = !answer.configured;
  }

  async function refresh(fresh, pending) {
    draw(await (pending || API.get(API.reading("/backup", fresh))));
    Kept.keep(KEPT, view);
    Kept.release();
    // One SSH connection, a second on a machine that answers. Not awaited:
    // the rest of the page is drawn off the disk and should not wait for it.
    if (view.configured) {
      readStaging();
      readConnection();
    }
  }

  element("settings-open").addEventListener("click", () => showSettings(true));
  element("settings-cancel").addEventListener("click", () => showSettings(false));
  element("settings-take").addEventListener("click", takeFromMachine);
  element("settings-save").addEventListener("click", saveSettings);
  element("act-full").addEventListener("click", confirmFull);
  element("act-inc").addEventListener("click", confirmIncremental);
  element("act-list").addEventListener("click", readServer);
  element("estimate-go").addEventListener("click", measure);
  element("staging-read").addEventListener("click", readStaging);
  element("staging-create").addEventListener("click", confirmCreateStaging);
  element("staging-move").addEventListener("click", () => stageOn(target));
  element("staging-volume").addEventListener("click", openVolume);
  element("volume-cancel").addEventListener("click", () => {
    element("volume").hidden = true;
  });
  element("volume-host").addEventListener("change", (event) =>
    readVolume(event.target.value)
  );
  element("volume-go").addEventListener("click", createVolume);
  element("connection-read").addEventListener("click", readConnection);
  element("connection-prepare").addEventListener("click", openTrust);
  element("connection-generate").addEventListener("click", confirmGenerate);
  element("connection-install").addEventListener("click", openInstall);
  element("trust-go").addEventListener("click", trustServer);
  element("trust-cancel").addEventListener("click", () => {
    element("trust").hidden = true;
  });
  element("install-go").addEventListener("click", installKeys);
  element("install-cancel").addEventListener("click", () => {
    element("install-password").value = "";
    element("install").hidden = true;
  });
  element("volume-name").addEventListener("input", () => {
    nameEdited = true;
  });
  [
    "volume-vg",
    "volume-vg-name",
    "volume-lv",
    "volume-gib",
    "volume-fstype",
    "volume-mountpoint",
  ].forEach((id) => element(id).addEventListener("input", summarise));
  document
    .querySelectorAll('input[name="volume-layout"], input[name="volume-size"]')
    .forEach((input) => input.addEventListener("change", summarise));
  element("confirm-cancel").addEventListener("click", () => {
    element("confirm").hidden = true;
  });

  async function start() {
    const me = Chrome.current();
    // Taking a backup and asking the server what it holds change no desired
    // state, so they are the operator's, the way starting a guest is.
    canAct = me.role === "operator" || Chrome.isAdmin(me);
    // Where the backups go is a commit, and a restore destroys a running
    // guest. Both are the administrator's.
    canWrite = Chrome.isAdmin(me);
    // Never on the timer. Reading this page again asks every cluster member
    // over SSH, for its staging directories and its connection to the backup
    // server, which is not something to do every ten seconds.
    Reread.attach(
      element("reread"),
      async (fresh) => {
        showBanner("");
        await refresh(fresh);
      },
      (failure) => showBanner(failure.message),
      { timer: false }
    );
    const pending = API.started("/backup");
    const age = Kept.paint(KEPT, draw);
    if (age !== null) {
      Kept.rereading(["loading"], age);
      Kept.hold([
        "card-backup",
        "estimate-card",
        "catalogue-card",
        "staging-card",
        "connection-card",
      ]);
    }
    await refresh(false, pending);
  }

  start().catch((failure) => {
    Kept.release();
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
