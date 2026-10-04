// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The Bookmarks page: a list of named links held in this browser's local
// storage, added, edited and removed here, and opened in a new browser tab.

(function () {
  const KEY = "seapath-bookmarks";

  const banner = document.getElementById("banner");
  const empty = document.getElementById("bookmark-empty");
  const table = document.getElementById("bookmark-table");
  const rows = document.getElementById("bookmark-rows");
  const form = document.getElementById("bookmark-form");
  const formTitle = document.getElementById("bookmark-form-title");
  const nameInput = document.getElementById("bookmark-name");
  const urlInput = document.getElementById("bookmark-url");
  const save = document.getElementById("bookmark-save");
  const cancel = document.getElementById("bookmark-cancel");
  const hide = document.getElementById("bookmark-hide");

  // The index of the bookmark the form is editing, or null when it adds one.
  let editing = null;

  function say(message) {
    banner.textContent = message;
    banner.hidden = message === "";
  }

  function load() {
    try {
      const value = JSON.parse(localStorage.getItem(KEY) || "[]");
      return Array.isArray(value)
        ? value.filter(
            (item) =>
              item && typeof item.name === "string" && typeof item.url === "string"
          )
        : [];
    } catch (error) {
      return [];
    }
  }

  function store(bookmarks) {
    try {
      localStorage.setItem(KEY, JSON.stringify(bookmarks));
      return true;
    } catch (error) {
      say("This browser refused to store the bookmarks: " + error.message);
      return false;
    }
  }

  // Only a web link is opened. A `javascript:` URL in an anchor would run in
  // this page, with the operator's session, when clicked.
  function safe(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "http:" || parsed.protocol === "https:";
    } catch (error) {
      return false;
    }
  }

  function reset() {
    editing = null;
    form.reset();
    formTitle.textContent = "Add a bookmark";
    save.textContent = "Add";
    cancel.hidden = true;
  }

  function edit(index) {
    const bookmark = load()[index];
    if (!bookmark) {
      return;
    }
    editing = index;
    nameInput.value = bookmark.name;
    urlInput.value = bookmark.url;
    formTitle.textContent = "Edit " + bookmark.name;
    save.textContent = "Save";
    cancel.hidden = false;
    nameInput.focus();
  }

  function remove(index) {
    const bookmarks = load();
    const bookmark = bookmarks[index];
    if (!bookmark || !window.confirm("Delete the bookmark " + bookmark.name + "?")) {
      return;
    }
    bookmarks.splice(index, 1);
    if (store(bookmarks)) {
      if (editing === index) {
        reset();
      } else if (editing !== null && editing > index) {
        editing -= 1;
      }
      render();
    }
  }

  function button(label, action, secondary) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    if (secondary) {
      element.className = "secondary";
    }
    element.addEventListener("click", action);
    return element;
  }

  function render() {
    const bookmarks = load();
    rows.replaceChildren();
    bookmarks.forEach((bookmark, index) => {
      const row = document.createElement("tr");

      const name = document.createElement("td");
      const link = document.createElement("td");
      if (safe(bookmark.url)) {
        const anchor = document.createElement("a");
        anchor.href = bookmark.url;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        anchor.textContent = bookmark.name;
        name.append(anchor);
      } else {
        name.textContent = bookmark.name;
      }
      link.textContent = bookmark.url;
      link.className = "bookmark-url";

      const acts = document.createElement("td");
      acts.className = "bookmark-acts";
      acts.append(
        button("Edit", () => edit(index), true),
        button("Delete", () => remove(index), true)
      );

      row.append(name, link, acts);
      rows.append(row);
    });
    empty.hidden = bookmarks.length > 0;
    table.hidden = bookmarks.length === 0;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = nameInput.value.trim();
    const url = urlInput.value.trim();
    if (name === "" || !safe(url)) {
      say("A bookmark needs a name and an http or https link.");
      return;
    }
    say("");
    const bookmarks = load();
    if (editing !== null && bookmarks[editing]) {
      bookmarks[editing] = { name, url };
    } else {
      bookmarks.push({ name, url });
    }
    if (store(bookmarks)) {
      reset();
      render();
    }
  });

  cancel.addEventListener("click", reset);

  hide.addEventListener("click", () => {
    Konami.show(false);
    window.location.href = "./";
  });

  // Another tab of this browser editing the same list.
  window.addEventListener("storage", (event) => {
    if (event.key === KEY) {
      render();
    }
  });

  render();
})();
