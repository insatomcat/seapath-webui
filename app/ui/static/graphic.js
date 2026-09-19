// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The graphic console panel: noVNC on one side, a websocket to this node on
// the other, carrying the VNC protocol between noVNC and the guest's display.
// The page names the guest and the node picks the machine that runs it. See
// D62.
//
// noVNC is loaded the first time a panel opens rather than with the page: it
// is some sixty modules, and most visits to the VMs page open no screen.
//
// The socket is opened here and handed to noVNC, so that its close event, and
// the reason the node gives in it, reaches the panel. It is the only place a
// refusal is said: the wire carries the VNC protocol alone.

const Graphic = (function () {
  let RFB = null;
  let rfb = null;
  let guest = null;
  // Whether the screen is scaled to the panel, or shown at its own size in a
  // panel that scrolls.
  let fit = true;

  function element(id) {
    return document.getElementById(id);
  }

  function state(text, kind) {
    const node = element("graphic-state");
    node.textContent = text;
    node.className = "console-state" + (kind ? " " + kind : "");
  }

  function said(text) {
    const node = element("graphic-said");
    node.textContent = text || "";
    node.hidden = !text;
  }

  async function library() {
    if (RFB === null) {
      const url = new URL(element("graphic-modal").dataset.rfb, document.baseURI);
      RFB = (await import(url.href)).default;
    }
    return RFB;
  }

  // Offered on a row only when the session may open a console at all. Where
  // it opens is the node's decision, made when it is asked.
  function permitted() {
    return typeof Console !== "undefined" && Console.permitted();
  }

  function open(name) {
    if (!permitted()) {
      return;
    }
    guest = name;
    element("graphic-target").textContent = name;
    element("graphic-modal").hidden = false;
    connect();
  }

  function disconnect() {
    if (rfb !== null) {
      // Ending the socket is what ends the session: the node terminates the
      // ssh, and the relay on the hypervisor with it.
      rfb.disconnect();
      rfb = null;
    }
  }

  async function connect() {
    disconnect();
    said("");
    state("connecting", "");
    element("graphic-reconnect").hidden = true;

    let Library;
    try {
      Library = await library();
    } catch (error) {
      state("failed", "bad");
      said("The VNC client could not be loaded: " + error.message);
      element("graphic-reconnect").hidden = false;
      return;
    }

    const url = new URL("api/v1/node/console/graphic", window.location.href);
    url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("guest", guest);
    const socket = new WebSocket(url);
    const session = new Library(element("graphic-screen"), socket, {
      shared: true,
    });
    rfb = session;
    session.scaleViewport = fit;
    session.clipViewport = false;
    session.resizeSession = false;
    session.focusOnClick = true;
    session.background = getComputedStyle(document.documentElement)
      .getPropertyValue("--console-bg")
      .trim();

    session.addEventListener("connect", () => {
      state("connected", "ok");
      session.focus();
    });
    session.addEventListener("desktopname", (event) => {
      element("graphic-target").textContent =
        guest + " (" + event.detail.name + ")";
    });
    // The reason is the node's, and it is what tells a guest with no display
    // from an idle timeout or a machine that could not be reached.
    socket.addEventListener("close", (event) => {
      if (rfb !== session) {
        return;
      }
      rfb = null;
      state("closed", event.code === 1000 ? "muted" : "bad");
      said(event.reason || "The graphic console was closed.");
      element("graphic-reconnect").hidden = false;
    });
  }

  function close() {
    disconnect();
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    }
    element("graphic-modal").hidden = true;
  }

  function toggleFit() {
    fit = !fit;
    element("graphic-fit").textContent = fit ? "Actual size" : "Fit to panel";
    element("graphic-fit").title = fit
      ? "Show the screen at its own size, and scroll"
      : "Scale the screen to the panel";
    if (rfb !== null) {
      rfb.scaleViewport = fit;
      rfb.focus();
    }
  }

  element("graphic-cad").addEventListener("click", () => {
    if (rfb !== null) {
      rfb.sendCtrlAltDel();
      rfb.focus();
    }
  });
  element("graphic-fit").addEventListener("click", toggleFit);
  element("graphic-full").addEventListener("click", () => {
    const screen = element("graphic-screen");
    if (screen.requestFullscreen) {
      screen.requestFullscreen().then(
        () => rfb !== null && rfb.focus(),
        () => {}
      );
    }
  });
  element("graphic-reconnect").addEventListener("click", connect);
  element("graphic-close").addEventListener("click", close);

  return { open, close, permitted };
})();
