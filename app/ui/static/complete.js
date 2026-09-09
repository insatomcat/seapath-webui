// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// Completing a variable name, where one is being typed.
//
// The vocabulary knows 81 variables, what each one does, which role reads it
// and what goes wrong when it is absent. Until now an operator could read all
// of that through the API and none of it while typing the file, which is the
// one moment it is worth anything.
//
// Three things decide whether this helps or annoys.
//
// It offers only where a variable name goes. A key sits at the start of a
// line, after the indentation and after an optional `- `, and anything past
// the colon is a value: offering `ceph_osd_disks` inside an IP address is an
// autocomplete that has to be dismissed rather than one that has to be read.
//
// It offers what may be written *there*. The scope of the caret is worked out
// from the shape of the file above it, so a guest entry is offered `vm_disk`
// and a hypervisor is not. That is the same boundary the assistant reports
// after the fact, applied before the mistake.
//
// And it says what the variable is. A list of names an operator could have
// guessed is a list they will stop opening; the role that reads it, the
// summary and the caution are the reason the vocabulary was written by hand.
//
// The caret's position on screen comes from a mirror: a div carrying the
// textarea's own metrics, filled with the text up to the caret, whose last
// span is where the caret is. A textarea offers no other way to ask.

const Complete = (function () {
  // How the groups of an inventory decide what a host key under them is. The
  // three the model knows by name, which is how a file says which playbook
  // creates a guest.
  const GUEST_GROUPS = ["VMs", "cluster_VMs", "standalone_VMs"];
  // Enough of a prefix to be an intention rather than a stray keystroke.
  const MINIMUM = 1;
  const MOST = 8;
  // What the list keeps between itself and the edge of the window before
  // it decides there is no room under the caret.
  const MARGIN = 8;

  // The styles the mirror has to carry for a character to land in the same
  // place in it as in the textarea.
  const MIRRORED = [
    "boxSizing",
    "fontFamily",
    "fontSize",
    "fontStyle",
    "fontWeight",
    "letterSpacing",
    "lineHeight",
    "paddingTop",
    "paddingRight",
    "paddingBottom",
    "paddingLeft",
    "borderTopWidth",
    "borderRightWidth",
    "borderBottomWidth",
    "borderLeftWidth",
    "tabSize",
    "textIndent",
    "whiteSpace",
    "wordSpacing",
  ];

  function lineStart(text, index) {
    return text.lastIndexOf("\n", index - 1) + 1;
  }

  // What is being typed, when a variable name is what is being typed.
  //
  // `null` for everywhere else: inside a value, in a comment, on a line whose
  // key is already closed by a colon. The test is the text between the start
  // of the line and the caret, since that is all a key position is.
  function context(area) {
    if (area.selectionStart !== area.selectionEnd) {
      return null;
    }
    const text = area.value;
    const from = lineStart(text, area.selectionStart);
    const before = text.slice(from, area.selectionStart);
    const parsed = /^([ \t]*)(-[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)?$/.exec(before);
    if (parsed === null) {
      return null;
    }
    const prefix = parsed[3] || "";
    if (prefix.length < MINIMUM) {
      return null;
    }
    return { from: area.selectionStart - prefix.length, prefix };
  }

  // Where in the file the caret is, in the terms the vocabulary uses.
  //
  // Walked backwards from the caret, taking each line that is less indented
  // than the last one taken, which is the chain of keys the caret sits under.
  // A YAML parser would be exact and would also have to parse a file that is
  // half typed; this reads the shape, and a shape is what a half typed file
  // still has.
  function scopeAt(area) {
    const lines = area.value.slice(0, area.selectionStart).split("\n");
    const caretLine = lines.pop();
    const chain = [];
    let indent = width(caretLine);

    for (let index = lines.length - 1; index >= 0; index -= 1) {
      const line = lines[index];
      if (line.trim() === "" || /^[ \t]*#/.test(line)) {
        continue;
      }
      const here = width(line);
      if (here >= indent) {
        continue;
      }
      indent = here;
      const key = /^[ \t]*(-[ \t]+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*:/.exec(line);
      chain.unshift(key ? key[2] : null);
      if (here === 0) {
        break;
      }
    }

    // `<group>: hosts: <host>:` is a machine, or a guest when the group is one
    // of the three that hold guests. `<group>: vars:` is the group itself.
    const group = chain[0];
    const guests = GUEST_GROUPS.indexOf(group) !== -1;
    if (chain.indexOf("hosts") === 1) {
      return guests ? "guest" : "host";
    }
    if (chain.indexOf("vars") === 1) {
      return guests ? "guest" : "group";
    }
    // Anywhere the shape does not say, including the top of a file nobody has
    // structured yet. Everything is offered rather than nothing.
    return null;
  }

  function width(line) {
    return /^[ \t]*/.exec(line)[0].length;
  }

  // A term belongs at the caret when its own scope allows it there. `any` is
  // written on a group or on a machine, `connection` anywhere a host is, and
  // an unknown position offers the lot.
  function fits(term, scope) {
    if (scope === null || term.scope === "connection") {
      return true;
    }
    if (scope === "guest") {
      return term.scope === "guest";
    }
    if (term.scope === "guest") {
      return false;
    }
    return term.scope === scope || term.scope === "any";
  }

  // Ranked so that what was typed at the front of a name comes first: an
  // operator typing `ceph` means `ceph_osd_disks` before `deploy_cephfs`.
  function candidates(terms, scope, prefix) {
    const wanted = prefix.toLowerCase();
    return terms
      .filter(
        (term) =>
          fits(term, scope) && term.name.toLowerCase().indexOf(wanted) !== -1
      )
      .sort((left, right) => {
        const here = left.name.toLowerCase().indexOf(wanted);
        const there = right.name.toLowerCase().indexOf(wanted);
        if (here !== there) {
          return here - there;
        }
        return left.name.localeCompare(right.name);
      })
      .slice(0, MOST);
  }

  function attach(area, terms, enabled) {
    const list = document.createElement("ul");
    list.className = "completion";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    area.parentNode.insertBefore(list, area.nextSibling);

    const mirror = document.createElement("div");
    mirror.className = "completion-mirror";
    mirror.setAttribute("aria-hidden", "true");
    area.parentNode.insertBefore(mirror, area.nextSibling);

    let open = [];
    let chosen = 0;
    let at = 0;

    function close() {
      if (list.hidden) {
        return;
      }
      list.hidden = true;
      list.replaceChildren();
      open = [];
      area.removeAttribute("aria-activedescendant");
    }

    // Both the mirror and the list are placed against the card, and the text
    // starts a header, a note and a margin below the top of it. So the mirror
    // is laid over the textarea rather than over the card: anchored on the
    // card the list came out one header too high, which is over the line being
    // typed.
    function place() {
      const style = window.getComputedStyle(area);
      MIRRORED.forEach((name) => {
        mirror.style[name] = style[name];
      });
      mirror.style.top = area.offsetTop + "px";
      mirror.style.left = area.offsetLeft + "px";
      mirror.style.width = area.clientWidth + "px";
      mirror.textContent = area.value.slice(0, at);
      const marker = document.createElement("span");
      // A zero width space rather than nothing: an empty span has no box, and
      // a box is the whole point of the marker.
      marker.textContent = "​";
      mirror.append(marker);

      const line = parseFloat(style.lineHeight || "16") || 16;
      const x = marker.offsetLeft - area.scrollLeft;
      const y = marker.offsetTop - area.scrollTop;
      const box = area.getBoundingClientRect();

      // Under the line, and over it only when the window leaves no room under
      // it. The list is read while the name is still being typed, so the one
      // place it may not sit is on the line it completes.
      const height = list.offsetHeight;
      const under = window.innerHeight - (box.top + y + line) - MARGIN;
      const over = under < height && box.top + y - height > MARGIN;
      list.style.top = area.offsetTop + (over ? y - height : y + line) + "px";

      // The left edge on the caret, kept inside the text: a name typed at the
      // far right of a long line would open a list running off the card.
      const room = area.offsetLeft + area.clientWidth - list.offsetWidth;
      list.style.left =
        Math.round(
          Math.max(area.offsetLeft, Math.min(area.offsetLeft + x, room))
        ) + "px";
    }

    function draw() {
      list.replaceChildren();
      open.forEach((term, index) => {
        const item = document.createElement("li");
        item.id = "completion-" + index;
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", String(index === chosen));
        item.className = index === chosen ? "chosen" : "";

        const name = document.createElement("span");
        name.className = "completion-name";
        name.textContent = term.name;
        item.append(name);

        if (term.role) {
          const role = document.createElement("span");
          role.className = "completion-role";
          role.textContent = term.role;
          item.append(role);
        }

        const summary = document.createElement("span");
        summary.className = "completion-summary";
        // The caution is what an operator cannot work out from the name, so it
        // wins the line when there is one.
        summary.textContent = term.caution || term.summary;
        item.append(summary);

        // `mousedown` rather than `click`: the textarea must not lose the
        // caret before the name is inserted into it.
        item.addEventListener("mousedown", (event) => {
          event.preventDefault();
          accept(index);
        });
        list.append(item);
      });
      list.hidden = false;
      area.setAttribute("aria-activedescendant", "completion-" + chosen);
    }

    function accept(index) {
      const term = open[index];
      if (!term) {
        return;
      }
      const caret = area.selectionStart;
      area.setSelectionRange(at, caret);
      // The colon and the space come with it: a key is never the whole of what
      // is about to be typed, and `execCommand` is what keeps the browser's
      // undo stack whole, as in `yamledit.js`.
      const written = term.name + ": ";
      if (!document.execCommand("insertText", false, written)) {
        area.setRangeText(written, at, caret, "end");
        area.dispatchEvent(new Event("input", { bubbles: true }));
      }
      close();
    }

    function refresh() {
      if (!enabled()) {
        close();
        return;
      }
      const found = context(area);
      if (found === null) {
        close();
        return;
      }
      const offered = candidates(terms(), scopeAt(area), found.prefix);
      if (offered.length === 0) {
        close();
        return;
      }
      // A single candidate already spelled out in full is a list saying what
      // the operator has just finished typing.
      if (offered.length === 1 && offered[0].name === found.prefix) {
        close();
        return;
      }
      open = offered;
      chosen = 0;
      at = found.from;
      // Drawn before it is placed: the list is placed from its own height, and
      // a list that is still hidden has none.
      draw();
      place();
    }

    area.addEventListener("input", refresh);
    area.addEventListener("blur", close);
    area.addEventListener("scroll", () => {
      if (!list.hidden) {
        place();
      }
    });

    // Registered before `yamledit.js` attaches its own, so the keys the list
    // owns while it is open never reach the ones that indent a block. An open
    // list is a mode, and Tab in that mode takes the name rather than two
    // spaces.
    area.addEventListener("keydown", (event) => {
      if (list.hidden) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        close();
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        event.stopImmediatePropagation();
        const step = event.key === "ArrowDown" ? 1 : open.length - 1;
        chosen = (chosen + step) % open.length;
        draw();
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        event.stopImmediatePropagation();
        accept(chosen);
      }
    });

    return { close, refresh };
  }

  return { attach, context, scopeAt, candidates };
})();
