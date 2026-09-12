// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The top bar, shared by every signed in page: who is here, which node this
// is, the way out, and the two gestures that shut a window.
//
// Nothing here is fetched. The three strings of the bar are rendered into the
// document by the service that served it, and this file reads them back for
// the pages that gate an action on the role.

const Chrome = (function () {
  // A window is dismissed by clicking the control it names in `data-dismiss`
  // and never by hiding the element, so the page's own teardown runs: the
  // console closes its socket, a confirmation clears the machine it was about
  // to name, a form empties the file it was holding, the run window stops
  // following its stream. Hiding the element would leave all of those behind.
  function dismiss(modal) {
    const control = modal
      ? document.getElementById(modal.dataset.dismiss)
      : null;
    if (control && !control.disabled) {
      control.click();
    }
  }

  // The window on top, which is the last one in the document that is open: a
  // window later in the document is the one drawn over the others, and it is
  // the one an operator means.
  function top() {
    const windows = Array.from(document.querySelectorAll(".modal[data-dismiss]"));
    return windows.reverse().find((modal) => !modal.hidden) || null;
  }

  // Escape dismisses it, on every page.
  //
  // One window keeps the key: the console, where Escape is a byte the shell is
  // waiting for and the terminal has already claimed it. That is what the
  // `defaultPrevented` guard leaves alone, and why the console has a Close
  // button of its own.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented) {
      return;
    }
    dismiss(top());
  });

  // So does clicking beside it. The scrim is the element the click lands on
  // when it lands outside the window, and nothing else in this UI uses it, so
  // a click whose target is the scrim itself is an operator reaching past the
  // window to the page.
  //
  // Both ends of that click, because a selection that starts on a value inside
  // the window and ends past its edge is released on the scrim. Losing a form
  // that way is the kind of thing an operator does not risk twice, and they
  // stop selecting text in these windows altogether.
  let pressed = null;
  document.addEventListener("mousedown", (event) => {
    pressed = event.target;
  });
  document.addEventListener("click", (event) => {
    const scrim = event.target;
    if (scrim !== pressed || !(scrim instanceof Element)) {
      return;
    }
    if (scrim.classList.contains("modal") && scrim.dataset.dismiss) {
      dismiss(scrim);
    }
  });

  // Who is signed in, said by the document that carries the bar. It is the
  // session's own user and role, which is exactly what `/auth/me` answers
  // with, and this page was rendered by the service that holds the session.
  //
  // Asked over the API until it was rendered here, which put two requests in
  // front of every screen for three strings that had not changed since the
  // last page, and sent the header through its placeholders on the way back to
  // them. The browser used to keep them to cover the gap. Now there is no gap.
  //
  // Read as this file loads rather than when a page asks, because the run
  // window is opened from a click rather than from a page's own start and it
  // has a control only an admin may press: it saw nothing at all until the
  // first reading of the page under it had resolved.
  //
  // `Chrome.current()` is what the pages read, and they read it without waiting:
  // every one of them used to open with `await Chrome.load()`, which was two
  // requests standing in front of the page's own.
  const signedIn = (function () {
    const bar = document.querySelector(".topbar");
    if (!bar) {
      return null;
    }
    return { username: bar.dataset.username, role: bar.dataset.role };
  })();

  // The node's name and its mode, from a page that has just read them. Both
  // change when a machine is renamed or joins a cluster, which is a run, and
  // the Node page is where an operator watches one land: it reads `/node` on
  // its own timer, so the bar follows it without a request of its own.
  function saw(node) {
    document.getElementById("node-name").textContent = node.hostname;
    const mode = document.getElementById("node-mode");
    mode.textContent = node.mode;
    mode.className = "badge badge-" + node.mode;
  }

  function isAdmin(me) {
    return me.role === "admin";
  }

  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await API.post("/auth/logout");
    } finally {
      // What the panels of this tab were showing goes with the session. The next
      // person to sign in on this browser starts from the machines.
      try {
        sessionStorage.clear();
      } catch (error) {
        /* Nothing was stored either. */
      }
      window.location.assign("login");
    }
  });

  return { saw, isAdmin, current: () => signedIn };
})();
