// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The inventory page: a folder of files, and an editor for them.
//
// The desired state of these machines is a folder: `inventory.yaml` and the
// files a dozen roles name from it. So the page is the folder on the left and
// the open file on the right, and the acts are the ones a folder has: open,
// edit, save, add, delete. Every one of them is a commit, and the history at
// the bottom is the audit trail.
//
// The files the inventory names and this folder does not hold are listed in
// the tree as well, greyed. A run would stop at the task that copies one, on
// every host at once, and finding that out here costs nothing.

(function () {
  const INVENTORY = "inventory.yaml";

  const state = {
    me: null,
    // The commit the open inventory was read at, echoed as `If-Match` so two
    // browsers editing the same file cannot silently overwrite each other.
    commit: null,
    maxFileBytes: 4 * 1024 * 1024,
    entries: [],
    buffers: new Map(),
    current: null,
    pending: [],
    confirm: null,
  };

  function element(id) {
    return document.getElementById(id);
  }

  function admin() {
    return state.me !== null && Chrome.isAdmin(state.me);
  }

  function humanSize(bytes) {
    const units = ["B", "kB", "MB", "GB", "TB"];
    let size = bytes;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return (unit === 0 ? size : size.toFixed(1)) + " " + units[unit];
  }

  function normalise(path) {
    return (path || "").trim().replace(/^\/+/, "").replace(/\/+$/, "");
  }

  function keyOf(store, path) {
    return store + ":" + path;
  }

  // The path is a URL segment by segment: a file called `snmpd.conf copy` is a
  // legitimate name and a broken URL.
  function route(store, path) {
    return (
      "/inventory/" +
      store +
      "/" +
      path.split("/").map(encodeURIComponent).join("/")
    );
  }

  function showBanner(messages) {
    const banner = element("banner");
    banner.replaceChildren();
    messages.forEach((message) => {
      const line = document.createElement("div");
      line.textContent = message;
      banner.append(line);
    });
    banner.hidden = messages.length === 0;
  }

  function showError(id, message) {
    const target = element(id);
    target.textContent = message;
    target.hidden = !message;
  }

  // One line for every act that has to ask once before it happens. A modal
  // belongs to the one act that disturbs a running machine, which is the apply
  // on the System page; nothing here leaves this browser until it is saved.
  function ask(question, label, action) {
    state.confirm = action;
    element("confirm-question").textContent = question;
    element("confirm-go").textContent = label;
    element("confirm-line").hidden = false;
  }

  function stopAsking() {
    state.confirm = null;
    element("confirm-line").hidden = true;
  }

  element("confirm-go").addEventListener("click", async () => {
    const action = state.confirm;
    stopAsking();
    if (action) {
      await action();
    }
  });

  element("confirm-cancel").addEventListener("click", stopAsking);

  // The folder, as one list

  async function loadFolder() {
    const [inventory, folder, references] = await Promise.all([
      API.get("/inventory"),
      API.get("/inventory/folder"),
      API.get("/inventory/references"),
    ]);

    state.commit = inventory.commit;
    state.maxFileBytes = folder.max_file_bytes || state.maxFileBytes;

    const entries = [
      {
        store: "inventory",
        path: INVENTORY,
        editable: true,
        seeded: inventory.seeded,
      },
    ];
    folder.files.forEach((file) =>
      entries.push({
        store: "files",
        path: file.path,
        size: file.size,
        modified: file.modified,
        editable: file.text,
      })
    );
    folder.artefacts.forEach((file) =>
      entries.push({
        store: "artefacts",
        path: file.path,
        size: file.size,
        modified: file.modified,
        editable: false,
      })
    );
    entries.push(...missingEntries(references, entries));
    state.entries = entries;

    fillReferences(references);
    element("folder-note").textContent =
      "A file over " +
      humanSize(state.maxFileBytes) +
      " is stored as an artefact: a run mounts it in the same place, and git " +
      "does not carry it." +
      (folder.free_bytes
        ? " There is " + humanSize(folder.free_bytes) + " left for them."
        : "");

    // A banner for what the tree cannot say. The missing files are in the
    // tree, where the answer is one click away, so repeating them here would
    // be noise over the thing that fixes them.
    const messages = (inventory.validation.findings || [])
      .filter(
        (finding) =>
          finding.level === "warning" &&
          finding.rule !== "referenced_file_present"
      )
      .map((finding) => finding.message);
    if (inventory.parse_error) {
      messages.unshift(
        "The inventory does not parse: " +
          inventory.parse_error +
          " Everything else on this page still works, and the file is open " +
          "on the right."
      );
    }
    if (!admin()) {
      messages.push("Changing the inventory requires the admin role.");
    }
    showBanner(messages);
    return inventory;
  }

  // A file the inventory names that nothing here holds. Listed beside the
  // files that do exist, because that is where an operator is looking when
  // they find out, and clicking one opens it as a file to write.
  function missingEntries(references, existing) {
    const held = new Set(existing.map((entry) => entry.path));
    const seen = new Set();
    const missing = [];
    references.forEach((reference) => {
      if (reference.found) {
        return;
      }
      const path = reference.expected || reference.value;
      if (seen.has(path) || held.has(path)) {
        return;
      }
      seen.add(path);
      missing.push({
        store: "missing",
        path,
        editable: Boolean(reference.expected),
        reference,
      });
    });
    return missing;
  }

  function fillReferences(references) {
    const body = document.querySelector("#references-table tbody");
    body.replaceChildren();
    references.forEach((reference) => {
      const row = document.createElement("tr");
      [reference.host, reference.variable, reference.value].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      const where = document.createElement("td");
      if (reference.found) {
        where.textContent = reference.where || "resolved at run time";
      } else {
        where.className = "missing";
        where.textContent = reference.expected
          ? "missing, expected at " + reference.expected
          : "points above the folder, no run can reach it";
      }
      row.append(where);
      body.append(row);
    });
    element("references-empty").hidden = references.length > 0;
    element("references-table").hidden = references.length === 0;
  }

  // The tree

  const GROUPS = [
    ["inventory", ""],
    ["files", "Files, versioned with it"],
    ["artefacts", "Artefacts, kept out of git"],
    ["missing", "Named by the inventory, not here"],
  ];

  function render() {
    const tree = element("tree");
    tree.replaceChildren();

    const shown = state.entries.slice();
    // A file created here and not saved yet has no entry on disk, so it is
    // added to the list: an editor that hides the file you are typing into is
    // an editor that loses it.
    state.buffers.forEach((buffer) => {
      if (buffer.isNew && !shown.some((entry) => entry.path === buffer.path)) {
        shown.push({
          store: buffer.store,
          path: buffer.path,
          editable: true,
          isNew: true,
        });
      }
    });

    GROUPS.forEach(([store, label]) => {
      const group = shown
        .filter((entry) => entry.store === store)
        .sort((left, right) => left.path.localeCompare(right.path));
      if (!group.length) {
        return;
      }
      if (label) {
        const heading = document.createElement("li");
        heading.className = "tree-group";
        heading.textContent = label;
        tree.append(heading);
      }
      group.forEach((entry) => tree.append(treeItem(entry)));
    });
  }

  function treeItem(entry) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tree-item";
    if (entry.store === "missing") {
      button.classList.add("missing");
    }
    const key = keyOf(
      entry.store === "missing" ? "files" : entry.store,
      entry.path
    );
    if (key === state.current) {
      button.classList.add("current");
    }

    const buffer = state.buffers.get(key);
    const dirty = buffer !== undefined && buffer.text !== buffer.saved;

    const name = document.createElement("span");
    name.className = "tree-name";
    name.textContent = (dirty ? "● " : "") + entry.path;
    button.append(name);

    const note = document.createElement("span");
    note.className = "tree-note";
    if (entry.store === "inventory") {
      note.textContent = entry.seeded ? "desired state" : "empty";
    } else if (entry.isNew) {
      note.textContent = "new";
    } else if (entry.store === "missing") {
      note.textContent = entry.editable ? "missing" : "unreachable";
      button.title =
        entry.reference.host +
        ": " +
        entry.reference.variable +
        " names " +
        entry.reference.value;
    } else {
      note.textContent = humanSize(entry.size);
    }
    button.append(note);

    button.addEventListener("click", () => open(entry));
    item.append(button);
    return item;
  }

  // The editor

  async function open(entry) {
    const store = entry.store === "missing" ? "files" : entry.store;
    const key = keyOf(store, entry.path);
    showError("editor-error", "");
    showFindings([]);
    stopAsking();

    if (!state.buffers.has(key)) {
      let text = "";
      if (entry.store === "inventory") {
        text = await fetch("/api/v1/inventory/raw", {
          credentials: "same-origin",
        }).then((response) => response.text());
      } else if (entry.store !== "missing" && entry.editable) {
        text = await fetch("/api/v1" + route(store, entry.path), {
          credentials: "same-origin",
        }).then((response) => response.text());
      }
      state.buffers.set(key, {
        store,
        path: entry.path,
        text,
        saved: text,
        // The version this buffer was read at, which is what a save has to
        // name. The folder's head moves under an editor open on it, as soon as
        // any other file here is written, and echoing that would turn the
        // check into a formality.
        commit: state.commit,
        // A missing file is a file to write: the buffer is new, and saving it
        // creates it where the inventory already says it is.
        isNew: entry.store === "missing" || Boolean(entry.isNew),
        editable: entry.editable,
        entry,
      });
    }
    state.current = key;
    renderEditor();
    render();
  }

  function currentBuffer() {
    return state.current === null ? null : state.buffers.get(state.current);
  }

  function renderEditor() {
    const buffer = currentBuffer();
    const editor = element("editor");
    const download = element("editor-download");
    const tag = element("editor-store");

    if (!buffer) {
      element("editor-path").textContent = "Nothing open";
      element("editor-note").textContent =
        "Pick a file on the left, add one, or create one.";
      editor.hidden = true;
      tag.hidden = true;
      download.hidden = true;
      element("editor-actions").hidden = true;
      element("editor-meta").textContent = "";
      return;
    }

    element("editor-path").textContent = buffer.path;
    tag.hidden = false;
    tag.textContent =
      buffer.store === "inventory"
        ? "the inventory"
        : buffer.store === "artefacts"
          ? "artefact"
          : "versioned";
    tag.className = buffer.store === "artefacts" ? "tag warn" : "tag";
    element("editor-note").textContent = noteFor(buffer);

    const entry = buffer.entry;
    download.hidden = buffer.isNew || buffer.store === "inventory";
    if (!download.hidden) {
      download.href = "/api/v1" + route(buffer.store, buffer.path);
    }

    editor.hidden = !buffer.editable;
    if (buffer.editable && editor.value !== buffer.text) {
      editor.value = buffer.text;
    }
    editor.readOnly = !admin();

    const dirty = buffer.text !== buffer.saved;
    element("editor-actions").hidden = !admin();
    element("save").disabled = !dirty && !buffer.isNew;
    element("save").hidden = !buffer.editable;
    element("discard").hidden = !buffer.editable || buffer.isNew;
    element("check").hidden = buffer.store !== "inventory";
    element("propose").hidden = buffer.store !== "inventory";
    element("delete").hidden = buffer.store === "inventory";
    element("delete").textContent = buffer.isNew ? "Close it" : "Delete";

    element("editor-meta").textContent = dirty
      ? "unsaved changes"
      : buffer.isNew
        ? "not stored yet"
        : buffer.store === "inventory"
          ? state.commit
            ? "at " + state.commit.slice(0, 12)
            : "never written"
          : entry && entry.size !== undefined
            ? humanSize(entry.size) +
              ", changed " +
              new Date(entry.modified).toLocaleString()
            : "";
  }

  function noteFor(buffer) {
    if (buffer.store === "inventory") {
      return (
        "The desired state. Saving parses it, checks the rules and asks " +
        "ansible-inventory about it before committing anything."
      );
    }
    if (buffer.store === "artefacts") {
      return (
        "Kept out of git, because a VM image in a repository stays in its " +
        "history forever. A run mounts it under the same root, so the path " +
        "the inventory writes resolves the same way. Changing it leaves no " +
        "trace in the history below."
      );
    }
    if (buffer.isNew && buffer.entry && buffer.entry.reference) {
      const reference = buffer.entry.reference;
      if (!buffer.editable) {
        return (
          reference.host +
          " names " +
          reference.value +
          ", which points above this folder. No run from here can reach it, " +
          "so the path has to change rather than the folder."
        );
      }
      return (
        reference.host +
        ": " +
        reference.variable +
        " names " +
        reference.value +
        ", and nothing here holds it. Write it now, or add it with Add files."
      );
    }
    if (!buffer.editable) {
      return (
        "Not text, so there is nothing to edit here. Download it or delete it."
      );
    }
    return (
      "Committed with the inventory, so git log says what changed in it and " +
      "a revert undoes it."
    );
  }

  function showFindings(findings) {
    const list = element("editor-findings");
    list.replaceChildren();
    (findings || []).forEach((finding) => {
      const item = document.createElement("li");
      item.textContent =
        finding.level.toUpperCase() +
        " " +
        finding.rule +
        (finding.host ? " on " + finding.host : "") +
        ": " +
        finding.message;
      list.append(item);
    });
    list.hidden = !findings || !findings.length;
  }

  element("editor").addEventListener("input", (event) => {
    const buffer = currentBuffer();
    if (!buffer) {
      return;
    }
    const wasDirty = buffer.text !== buffer.saved;
    buffer.text = event.target.value;
    element("save").disabled = false;
    element("editor-meta").textContent = "unsaved changes";
    // The list only changes when the file becomes dirty or stops being it, and
    // rebuilding it on every keystroke would be a redraw per character typed.
    if (wasDirty !== (buffer.text !== buffer.saved)) {
      render();
    }
  });

  // A file whose indentation carries meaning, edited in a text area: the tab
  // key has to type something rather than leave the field.
  element("editor").addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || event.ctrlKey || event.altKey || event.metaKey) {
      return;
    }
    event.preventDefault();
    const editor = event.target;
    editor.setRangeText("  ", editor.selectionStart, editor.selectionEnd, "end");
    editor.dispatchEvent(new Event("input"));
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "s") {
      event.preventDefault();
      save();
    }
  });

  window.addEventListener("beforeunload", (event) => {
    const dirty = Array.from(state.buffers.values()).some(
      (buffer) => buffer.text !== buffer.saved
    );
    if (dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // Saving, which is a commit

  async function save() {
    const buffer = currentBuffer();
    if (!buffer || !buffer.editable || !admin()) {
      return;
    }
    showError("editor-error", "");
    showFindings([]);
    const button = element("save");
    button.disabled = true;
    try {
      const committed =
        buffer.store === "inventory"
          ? await saveInventory(buffer)
          : await API.upload(route("files", buffer.path), buffer.text);
      buffer.saved = buffer.text;
      buffer.isNew = false;
      await refresh();
      buffer.commit = state.commit;
      // The buffer's entry is stale once the file is written: its size and its
      // date are the ones from before the save.
      const entry = state.entries.find(
        (candidate) =>
          candidate.store === buffer.store && candidate.path === buffer.path
      );
      if (entry) {
        buffer.entry = entry;
      }
      renderEditor();
      showBannerCommit(committed, buffer.path);
    } catch (failure) {
      showError("editor-error", failure.message);
      showFindings((failure.detail && failure.detail.findings) || []);
      button.disabled = false;
    }
  }

  function saveInventory(buffer) {
    // `If-Match` only once there is a commit to match: on a node whose
    // inventory has never been written there is no head, and an empty header
    // would read as one.
    return API.put(
      "/inventory/raw",
      { document: buffer.text },
      buffer.commit ? { "If-Match": buffer.commit } : undefined
    );
  }

  function showBannerCommit(committed, path) {
    const hash = committed && committed.commit ? committed.commit : null;
    showBanner([
      path +
        " is saved" +
        (hash ? " as " + hash.slice(0, 12) : "") +
        ". Nothing has been pushed to any machine: that is the System page.",
    ]);
  }

  element("save").addEventListener("click", save);

  element("discard").addEventListener("click", async () => {
    const buffer = currentBuffer();
    if (!buffer) {
      return;
    }
    state.buffers.delete(state.current);
    state.current = null;
    await open(buffer.entry);
  });

  element("check").addEventListener("click", async () => {
    const buffer = currentBuffer();
    if (!buffer) {
      return;
    }
    showError("editor-error", "");
    try {
      const result = await API.post("/inventory/raw/check", {
        document: buffer.text,
      });
      showFindings(result.findings);
      if (!result.findings.length) {
        showBanner([
          "The file is valid, and Ansible parses it. Nothing was saved.",
        ]);
      }
    } catch (failure) {
      showError("editor-error", failure.message);
    }
  });

  // What this machine says it is, as a file to start from
  //
  // The seed of first boot, on demand. A machine whose discovery failed then,
  // one re-cabled since, and one whose file somebody emptied all want the same
  // starting point, and none of them wants a service that writes it for them:
  // the proposal lands in the editor, and saving it is the operator's act.

  async function propose() {
    const buffer = currentBuffer();
    if (!buffer || buffer.store !== "inventory") {
      return;
    }
    showError("editor-error", "");
    showFindings([]);
    try {
      const document = await fetch("/api/v1/inventory/proposed", {
        credentials: "same-origin",
      }).then(async (response) => {
        if (!response.ok) {
          const payload = await response.json().catch(() => null);
          throw new Error(
            (payload && payload.error && payload.error.message) ||
              "This machine could not describe itself."
          );
        }
        return response.text();
      });
      buffer.text = document;
      element("editor").value = document;
      renderEditor();
      render();
      showBanner([
        "This is the standalone inventory this machine describes for itself, " +
          "from what it can observe. Read it, change what it could not know, " +
          "and save it: nothing is committed until you do.",
      ]);
    } catch (failure) {
      showError("editor-error", failure.message);
    }
  }

  element("propose").addEventListener("click", () => {
    const buffer = currentBuffer();
    if (!buffer) {
      return;
    }
    if (!buffer.text.trim()) {
      propose();
      return;
    }
    ask(
      "Replace what is in the editor with the inventory this machine " +
        "describes for itself? What is committed stays committed, and " +
        "Discard my changes brings it back.",
      "Replace it",
      propose
    );
  });

  // Deleting, which is a commit too, and revertible from the history

  element("delete").addEventListener("click", () => {
    const buffer = currentBuffer();
    if (!buffer) {
      return;
    }
    if (buffer.isNew) {
      state.buffers.delete(state.current);
      state.current = null;
      renderEditor();
      render();
      return;
    }
    ask(
      buffer.store === "artefacts"
        ? "Delete " +
          buffer.path +
          "? Artefacts are outside the history, so this one does not come back."
        : "Delete " +
          buffer.path +
          "? It stays in the history, and a revert brings it back.",
      "Delete it",
      () => remove(buffer)
    );
  });

  async function remove(buffer) {
    try {
      await API.del(route(buffer.store, buffer.path));
      state.buffers.delete(keyOf(buffer.store, buffer.path));
      state.current = null;
      await refresh();
      renderEditor();
      showBanner([buffer.path + " is removed from the folder."]);
    } catch (failure) {
      showError("editor-error", failure.message);
    }
  }

  // Adding files
  //
  // One control for every file, including the inventory: a file named
  // `inventory.yaml` is the desired state and goes through the validated
  // path, a file over the size limit is an artefact, everything else is
  // committed beside the inventory.

  element("new-file").addEventListener("click", () => {
    element("new-file-row").hidden = false;
    element("new-file-path").focus();
  });

  element("new-file-cancel").addEventListener("click", () => {
    element("new-file-row").hidden = true;
    element("new-file-path").value = "";
  });

  element("new-file-path").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      element("new-file-go").click();
    }
  });

  element("new-file-go").addEventListener("click", async () => {
    const path = normalise(element("new-file-path").value);
    showError("folder-error", "");
    if (!path) {
      return;
    }
    element("new-file-row").hidden = true;
    element("new-file-path").value = "";
    const existing = state.entries.find((entry) => entry.path === path);
    if (existing) {
      await open(existing);
      return;
    }
    await open({ store: "files", path, editable: true, isNew: true });
  });

  element("add-file").addEventListener("click", () =>
    element("file-input").click()
  );

  element("file-input").addEventListener("change", (event) => {
    const missing = state.entries.filter((entry) => entry.store === "missing");
    state.pending = Array.from(event.target.files).map((file) => {
      const own = file.webkitRelativePath || file.name;
      // A file whose name is the one the inventory is waiting for lands where
      // the inventory says it is, rather than at the root to be moved after.
      const wanted = missing.find(
        (entry) => entry.path.split("/").pop() === file.name
      );
      return {
        file,
        path: wanted ? wanted.path : own,
        store: file.size > state.maxFileBytes ? "artefacts" : "files",
      };
    });
    event.target.value = "";
    renderPending();
  });

  function renderPending() {
    const list = element("pending");
    list.replaceChildren();
    state.pending.forEach((item, index) => {
      const row = document.createElement("li");

      const input = document.createElement("input");
      input.value = item.path;
      input.spellcheck = false;
      input.addEventListener("input", (event) => {
        state.pending[index].path = event.target.value;
        renderPendingTag(tag, state.pending[index]);
      });
      row.append(input);

      const tag = document.createElement("span");
      renderPendingTag(tag, item);
      row.append(tag);

      const size = document.createElement("span");
      size.className = "tree-note";
      size.textContent = humanSize(item.file.size);
      row.append(size);

      list.append(row);
    });
    list.hidden = !state.pending.length;
    element("pending-actions").hidden = !state.pending.length;
  }

  function renderPendingTag(tag, item) {
    const inventory = normalise(item.path) === INVENTORY;
    tag.className = item.store === "artefacts" && !inventory ? "tag warn" : "tag";
    tag.textContent = inventory
      ? "the inventory"
      : item.store === "artefacts"
        ? "artefact"
        : "versioned";
  }

  element("upload-cancel").addEventListener("click", () => {
    state.pending = [];
    renderPending();
    showError("folder-error", "");
  });

  element("upload").addEventListener("click", async () => {
    const button = element("upload");
    const label = button.textContent;
    button.disabled = true;
    showError("folder-error", "");
    const stored = [];
    try {
      for (const item of state.pending) {
        const path = normalise(item.path);
        if (!path) {
          throw new Error(item.file.name + " needs a name in the folder.");
        }
        button.textContent =
          "Storing " + path + " (" + humanSize(item.file.size) + ")...";
        if (path === INVENTORY) {
          // The desired state arriving as a file: parsed, checked against the
          // rules and against Ansible, committed whole, and one revert away
          // from the version it replaces.
          await API.post("/inventory/import", {
            document: await item.file.text(),
          });
        } else {
          await API.upload(route(item.store, path), item.file);
        }
        stored.push(path);
        // A buffer of a file that just changed underneath is a stale buffer.
        state.buffers.delete(keyOf(item.store, path));
        state.buffers.delete(keyOf("inventory", INVENTORY));
      }
      state.pending = [];
    } catch (failure) {
      // Each file is its own commit, so the ones already stored stay. The
      // message names the one that stopped it.
      showError("folder-error", failure.message);
      state.pending = state.pending.filter(
        (item) => !stored.includes(normalise(item.path))
      );
    } finally {
      button.textContent = label;
      button.disabled = false;
      renderPending();
      await refresh();
      if (state.current !== null && !state.buffers.has(state.current)) {
        state.current = null;
      }
      renderEditor();
      // After the refresh, which has a banner of its own: what just happened
      // is what the operator is waiting to read, and the files that landed are
      // worth naming even when one of them stopped the rest.
      if (stored.length) {
        showBanner([
          stored.join(", ") +
            (stored.length > 1 ? " are stored. " : " is stored. ") +
            "Nothing has been pushed to any machine: that is the System page.",
        ]);
      }
    }
  });

  // The history

  async function loadHistory() {
    const history = await API.get("/inventory/history?limit=20");
    const body = document.querySelector("#history-table tbody");
    body.replaceChildren();
    history.forEach((commit) => {
      const row = document.createElement("tr");
      [
        new Date(commit.timestamp).toLocaleString(),
        commit.author,
        commit.message,
      ].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      const actions = document.createElement("td");
      if (admin()) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "revert";
        button.addEventListener("click", () => revert(commit));
        actions.append(button);
      }
      row.append(actions);
      body.append(row);
    });
  }

  async function revert(commit) {
    try {
      await API.post("/inventory/revert/" + commit.hash);
      // A revert is a commit. The machines stay as they are until a run
      // happens, and every open buffer is now behind the folder.
      state.buffers.clear();
      const current = state.current;
      state.current = null;
      await refresh();
      if (current) {
        const entry = state.entries.find(
          (candidate) => keyOf(candidate.store, candidate.path) === current
        );
        if (entry) {
          await open(entry);
        }
      }
      renderEditor();
      showBanner([
        "Reverted " +
          commit.message +
          ". The machines are unchanged until you apply.",
      ]);
    } catch (failure) {
      showError("editor-error", failure.message);
    }
  }

  async function refresh() {
    await loadFolder();
    await loadHistory();
    render();
  }

  async function start() {
    const chrome = await Chrome.load();
    state.me = chrome.me;
    if (!admin()) {
      ["add-file", "new-file"].forEach((id) => {
        element(id).disabled = true;
      });
    }
    await refresh();
    // The file the operator came here for. Opening it saves a click on every
    // visit, and it is the one file on this page that is always there.
    const inventory = state.entries.find((entry) => entry.store === "inventory");
    if (inventory) {
      await open(inventory);
    }
  }

  start();
})();
