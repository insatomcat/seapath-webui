// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The keys a file whose indentation carries meaning has to answer to.
//
// The inventory is edited in a plain textarea, which is a box that inserts
// characters and moves the caret out of the field when Tab is pressed. That is
// a poor place to write YAML in, and the gap is not the syntax colouring: it is
// that shifting a block of six lines one level in means putting the caret on
// each of them by hand, and that a browser's Enter drops the caret in column
// zero under a key that opens a mapping.
//
// So this is the four bindings that close the gap, and nothing else. Tab and
// Shift+Tab shift the lines the selection touches, Enter carries the
// indentation of the line it leaves, and Ctrl+/ comments the block out. The
// rest of an editor, the vocabulary of the roles and the completion over it,
// is a separate matter and is not here.
//
// Every change goes through `document.execCommand("insertText")`. It is
// deprecated and it is still the only way to write into a textarea and leave
// the browser's own undo stack intact: `value` and `setRangeText` both empty
// it, so one Ctrl+Z after an automatic indent would throw away everything
// typed before it. That is not a trade to make in a box holding the desired
// state of a substation.

const YamlEdit = (function () {
  const INDENT = 2;
  const STEP = " ".repeat(INDENT);

  function lineStart(text, index) {
    const previous = text.lastIndexOf("\n", index - 1);
    return previous + 1;
  }

  function lineEnd(text, index) {
    const next = text.indexOf("\n", index);
    return next === -1 ? text.length : next;
  }

  // The whole lines a selection touches. A selection that stops exactly on a
  // line start took nothing from that line, so it does not get shifted: this
  // is what a triple click or a Shift+Down selects, and pulling the line after
  // it into the block surprises every time.
  function block(area) {
    const text = area.value;
    let end = area.selectionEnd;
    if (end > area.selectionStart && end === lineStart(text, end)) {
      end -= 1;
    }
    return { from: lineStart(text, area.selectionStart), to: lineEnd(text, end) };
  }

  function spansLines(area) {
    return area.value.slice(area.selectionStart, area.selectionEnd).includes("\n");
  }

  // The one write path. `execCommand` reports failure by returning false, and
  // an empty replacement is a no-op in some browsers rather than a deletion,
  // so both cases fall back to the assignment that loses the undo stack. A
  // lost undo stack beats an edit that does not happen.
  function replace(area, from, to, text) {
    area.setSelectionRange(from, to);
    const written =
      text === ""
        ? document.execCommand("delete")
        : document.execCommand("insertText", false, text);
    if (!written) {
      area.setRangeText(text, from, to, "end");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  // A block operation reselects what it produced, so Tab, Tab, Tab shifts the
  // same six lines three levels in rather than one level and then a caret.
  function rewrite(area, from, to, text) {
    replace(area, from, to, text);
    area.setSelectionRange(from, from + text.length);
  }

  function indent(text) {
    return text
      .split("\n")
      .map((line) => (line === "" ? line : STEP + line))
      .join("\n");
  }

  function dedent(text) {
    return text.split("\n").map(stripStep).join("\n");
  }

  // Up to one level, and less than that when the line has less to give. A
  // line indented by three spaces goes to one and then to zero, which is the
  // behaviour that lets Shift+Tab straighten a block someone pasted.
  function stripStep(line) {
    let removed = 0;
    while (removed < INDENT && line[removed] === " ") {
      removed += 1;
    }
    return line.slice(removed);
  }

  function leading(line) {
    return /^[ \t]*/.exec(line)[0];
  }

  // What Enter writes. The line being left decides it:
  //
  // - a key that opens a mapping or a list, so ending in a colon, indents one
  //   level in;
  // - a list item that is a mapping, `- src: '...'`, aligns the next line
  //   under its first key, since that is where the second key of the entry
  //   goes;
  // - any other list item repeats the dash, which is what a list of NTP
  //   servers or of OSD disks is being typed for;
  // - an empty list item ends the list, and is removed on the way out. That is
  //   the escape hatch, and the reason the repeated dash is not a trap.
  function afterEnter(prefix, suffix) {
    const parsed = /^([ \t]*)(-[ \t]+)?(.*)$/.exec(prefix);
    const margin = parsed[1];
    const dash = parsed[2] || "";
    const rest = parsed[3];

    if (rest.startsWith("#")) {
      return { insert: "\n" + margin + " ".repeat(dash.length) };
    }
    // Nothing typed after the dash and nothing waiting after the caret: the
    // list is over. The item goes with it, and the caret stays on the line it
    // was on, so ending a list leaves no blank line behind and no line holding
    // two characters of punctuation.
    if (dash && rest === "" && suffix.trim() === "") {
      return { insert: margin, drop: prefix.length };
    }
    // What the line will read once the caret has left it, which is what says
    // whether a key opens a block. Reading the caret's side of it alone makes
    // `a: 1` split in the middle look like a key opening one.
    const line = rest + suffix;
    const opens = /:$/.test(line.trimEnd()) ? STEP : "";
    if (dash && !/:([ \t]|$)/.test(line)) {
      return { insert: "\n" + margin + dash };
    }
    return { insert: "\n" + margin + " ".repeat(dash.length) + opens };
  }

  function onEnter(area) {
    const text = area.value;
    const from = lineStart(text, area.selectionStart);
    const next = afterEnter(
      text.slice(from, area.selectionStart),
      text.slice(area.selectionEnd, lineEnd(text, area.selectionEnd))
    );
    // Dropping the empty list item means the replacement starts before the
    // caret: `  - ` becomes `  `, and the line the caret lands on is the one
    // the item was going to be.
    const start = next.drop ? area.selectionStart - next.drop : area.selectionStart;
    replace(area, start, area.selectionEnd, next.insert);
  }

  function onTab(area, back) {
    const text = area.value;
    if (!back && !spansLines(area)) {
      const column = area.selectionStart - lineStart(text, area.selectionStart);
      replace(
        area,
        area.selectionStart,
        area.selectionEnd,
        " ".repeat(INDENT - (column % INDENT))
      );
      return;
    }
    if (back && !spansLines(area)) {
      // Inside one line: shift that line and carry the caret with it, rather
      // than reselecting a line the operator did not select.
      const from = lineStart(text, area.selectionStart);
      const to = lineEnd(text, area.selectionStart);
      const line = text.slice(from, to);
      const shifted = stripStep(line);
      const removed = line.length - shifted.length;
      if (removed === 0) {
        return;
      }
      const start = Math.max(from, area.selectionStart - removed);
      const end = Math.max(from, area.selectionEnd - removed);
      replace(area, from, to, shifted);
      area.setSelectionRange(start, end);
      return;
    }
    const lines = block(area);
    const body = text.slice(lines.from, lines.to);
    rewrite(area, lines.from, lines.to, back ? dedent(body) : indent(body));
  }

  // Commenting a block puts every `#` in the same column, the outermost one
  // the block uses, so uncommenting restores the shape it had. Blank lines are
  // left blank: a `#` alone on one is noise in a diff.
  function comment(text) {
    const lines = text.split("\n");
    const filled = lines.filter((line) => line.trim() !== "");
    if (filled.length === 0) {
      return text;
    }
    if (filled.every((line) => /^[ \t]*#/.test(line))) {
      return lines.map((line) => line.replace(/^([ \t]*)#[ ]?/, "$1")).join("\n");
    }
    const column = Math.min(...filled.map((line) => leading(line).length));
    return lines
      .map((line) =>
        line.trim() === ""
          ? line
          : line.slice(0, column) + "# " + line.slice(column)
      )
      .join("\n");
  }

  function onComment(area) {
    const lines = block(area);
    const body = area.value.slice(lines.from, lines.to);
    rewrite(area, lines.from, lines.to, comment(body));
  }

  function attach(area) {
    area.addEventListener("keydown", (event) => {
      if (area.readOnly) {
        return;
      }
      const chord = event.ctrlKey || event.metaKey;
      if (event.key === "Tab" && !chord && !event.altKey) {
        event.preventDefault();
        onTab(area, event.shiftKey);
        return;
      }
      if (event.key === "Enter" && !chord && !event.altKey && !event.shiftKey) {
        event.preventDefault();
        onEnter(area);
        return;
      }
      // Shift is not tested: on an AZERTY keyboard the slash is typed with it,
      // so requiring it absent would leave this page without the binding on
      // the keyboards it is operated from.
      if (event.key === "/" && chord && !event.altKey) {
        event.preventDefault();
        onComment(area);
      }
    });
  }

  return { attach };
})();
