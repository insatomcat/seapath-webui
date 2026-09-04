// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The console panel: xterm.js on one side, a websocket to this node on the
// other, and nothing in between. The bytes the terminal draws are the bytes
// that came off the pseudo terminal, which is why the socket is binary in that
// direction and JSON in the other.
//
// The panel says what the console is every time it is opened, because a shell
// is the one place in this UI where what an operator does is not recorded
// anywhere and is not part of the desired state.

const Console = (function () {
  const FIT_DEBOUNCE_MS = 120;

  // The page's own palette. xterm.js paints its own background, so leaving it
  // to the library means a black rectangle in the middle of the panel.
  const THEME = {
    background: "#0b0f15",
    foreground: "#dfe6ee",
    cursor: "#4a9eff",
    selectionBackground: "rgba(74, 158, 255, 0.3)",
  };

  const RANKS = { viewer: 0, operator: 1, admin: 2 };

  let terminal = null;
  let fitAddon = null;
  let socket = null;
  let fitTimer = null;
  let allowed = false;

  function element(id) {
    return document.getElementById(id);
  }

  function state(text, kind) {
    const node = element("console-state");
    node.textContent = text;
    node.className = "console-state" + (kind ? " " + kind : "");
  }

  function isOpen() {
    return socket !== null && socket.readyState === WebSocket.OPEN;
  }

  function send(message) {
    if (isOpen()) {
      socket.send(JSON.stringify(message));
    }
  }

  function ensureTerminal() {
    if (terminal !== null) {
      return terminal;
    }
    terminal = new Terminal({
      theme: THEME,
      fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace',
      fontSize: 13,
      cursorBlink: true,
      scrollback: 5000,
      // The far end is a real terminal on a real machine, so it is the one
      // that decides what a newline means.
      convertEol: false,
    });
    fitAddon = new FitAddon.FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(element("console-screen"));
    terminal.onData((data) => send({ type: "input", data: data }));
    terminal.onResize(({ cols, rows }) =>
      send({ type: "resize", columns: cols, lines: rows })
    );
    window.addEventListener("resize", scheduleFit);
    return terminal;
  }

  function scheduleFit() {
    if (element("console-modal").hidden) {
      return;
    }
    window.clearTimeout(fitTimer);
    fitTimer = window.setTimeout(() => fitAddon.fit(), FIT_DEBOUNCE_MS);
  }

  // Written in the terminal's own colours rather than in the panel, so that
  // what the node says about the session stays in the transcript the operator
  // can scroll back through.
  function say(line) {
    terminal.write("\r\n\u001b[33m" + line + "\u001b[0m\r\n");
  }

  function connect() {
    const url = new URL("api/v1/node/console/ws", window.location.href);
    url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("columns", terminal.cols);
    url.searchParams.set("lines", terminal.rows);

    state("connecting", "");
    element("console-reconnect").hidden = true;
    socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";

    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        control(JSON.parse(event.data));
        return;
      }
      terminal.write(new Uint8Array(event.data));
    };

    socket.onclose = (event) => {
      socket = null;
      state("closed", "muted");
      element("console-reconnect").hidden = false;
      // The close reason carries why, and it is the only place it is said: an
      // idle timeout and a shell that exited look identical on screen.
      say(event.reason || "The console was closed.");
      terminal.blur();
    };

    socket.onerror = () => {
      state("failed", "bad");
    };
  }

  function control(message) {
    if (message.type === "ready") {
      state("connected", "ok");
      element("console-target").textContent = message.target;
      terminal.focus();
    } else if (message.type === "error") {
      state(message.code, "bad");
      say(message.message);
    }
  }

  function open() {
    if (!allowed) {
      return;
    }
    element("console-modal").hidden = false;
    ensureTerminal();
    terminal.reset();
    fitAddon.fit();
    terminal.focus();
    connect();
  }

  function close() {
    if (socket !== null) {
      // Closing the socket is what ends the session: the node terminates the
      // ssh it started as soon as the stream goes away.
      socket.close();
      socket = null;
    }
    element("console-modal").hidden = true;
  }

  function rank(role) {
    return RANKS[role] === undefined ? -1 : RANKS[role];
  }

  // Called on every refresh of the node page, so that a console turned off, a
  // node whose trust is not provisioned, or a limit already reached shows on
  // the button rather than being discovered on click.
  async function describe(me) {
    const info = await API.get("/node/console");
    allowed = info.enabled && rank(me.role) >= rank(info.required_role);
    element("console-open").hidden = !allowed;

    const note = element("console-note");
    if (!info.enabled) {
      note.textContent = "The console is turned off on this node.";
      return;
    }
    if (!allowed) {
      note.textContent =
        "A console requires the " + info.required_role + " role.";
      return;
    }
    note.textContent =
      "A shell on this machine as " +
      info.user +
      "@" +
      info.target +
      ", " +
      info.active_sessions +
      " of " +
      info.max_sessions +
      " open" +
      (info.idle_timeout_seconds
        ? ", closed after " + info.idle_timeout_seconds + "s without a keystroke."
        : ".");
  }

  element("console-open").addEventListener("click", open);
  element("console-close").addEventListener("click", close);
  element("console-reconnect").addEventListener("click", () => {
    terminal.reset();
    fitAddon.fit();
    connect();
  });

  return { describe, close };
})();
