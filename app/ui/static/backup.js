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

  function renderEstimate(estimate) {
    const guests = estimate.guests || [];
    element("estimate-error").textContent = estimate.error || "";
    element("estimate-error").hidden = !estimate.error;
    element("estimate-table").hidden = guests.length === 0;
    element("estimate-total").textContent = guests.length
      ? size(estimate.used_bytes) + " over " + guests.length + " guests"
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

  // What the server holds

  function renderCatalogue(payload) {
    const catalogue = payload.catalogue || {};
    const backups = catalogue.backups || [];
    element("catalogue-note").textContent = catalogue.note || "";
    element("catalogue-note").hidden = !catalogue.note;
    element("catalogue-table").hidden = backups.length === 0;
    element("catalogue-when").textContent = catalogue.read_at
      ? "Read " + whenRead(catalogue.read_at)
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

  async function readServer() {
    const button = element("act-list");
    button.disabled = true;
    try {
      const started = await API.post("/backup/listing");
      RunWatch.open(started.run_id);
    } catch (failure) {
      showBanner(failure.message);
    } finally {
      button.disabled = false;
    }
  }

  // Where they go

  function showSettings(open) {
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
        ? "Committed as " + answer.commit.slice(0, 8) + ": " + answer.message
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
    renderCatalogue(answer);
    element("settings-open").hidden = !canWrite;
    // Both panels are about a backup that has somewhere to go. Until the
    // inventory says where, the page is the form and the sentence that sends
    // an operator to it.
    element("estimate-card").hidden = !answer.configured;
    element("catalogue-card").hidden = !answer.configured;
  }

  async function refresh(fresh, pending) {
    draw(await (pending || API.get(API.reading("/backup", fresh))));
    Kept.keep(KEPT, view);
    Kept.release();
  }

  element("settings-open").addEventListener("click", () => showSettings(true));
  element("settings-cancel").addEventListener("click", () => showSettings(false));
  element("settings-take").addEventListener("click", takeFromMachine);
  element("settings-save").addEventListener("click", saveSettings);
  element("act-full").addEventListener("click", confirmFull);
  element("act-inc").addEventListener("click", confirmIncremental);
  element("act-list").addEventListener("click", readServer);
  element("estimate-go").addEventListener("click", measure);
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
    Reread.attach(
      element("reread"),
      async (fresh) => {
        showBanner("");
        await refresh(fresh);
      },
      (failure) => showBanner(failure.message)
    );
    const pending = API.started("/backup");
    const age = Kept.paint(KEPT, draw);
    if (age !== null) {
      Kept.rereading(["loading"], age);
      Kept.hold(["card-backup", "estimate-card", "catalogue-card"]);
    }
    await refresh(false, pending);
  }

  start().catch((failure) => {
    Kept.release();
    showBanner(failure.message);
    element("loading").hidden = true;
  });
})();
