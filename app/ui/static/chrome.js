// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The top bar, shared by every signed in page: who is here, which node this
// is, the way out, and the two gestures that shut a window.

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

  // Who is signed in, as the last reading of this page saw them. The run
  // window is opened from a click rather than from a page's own start, and it
  // has a control only an admin may press: it reads the answer this already
  // has instead of every page handing it over.
  let signedIn = null;

  async function load() {
    try {
      const [me, node] = await Promise.all([
        API.get("/auth/me"),
        API.get("/node"),
      ]);
      const identity = me.username + " (" + me.role + ")";
      document.getElementById("identity").textContent = identity;
      document.getElementById("node-name").textContent = node.hostname;
      const mode = document.getElementById("node-mode");
      mode.textContent = node.mode;
      mode.className = "badge badge-" + node.mode;
      // What the next page of this visit paints its header with, before it
      // asks. The document's own script reads it back; the key it uses is
      // built there, from the same cookie name.
      //
      // The identity is stored rendered rather than as a pair, so the string
      // is formed here and nowhere else: two places building it is two places
      // to change when the role stops being a parenthesis.
      remember({ hostname: node.hostname, mode: node.mode, identity });
      signedIn = me;
      return { me, node };
    } catch (failure) {
      if (failure.status === 401) {
        forget();
        window.location.assign("login");
      }
      throw failure;
    }
  }

  function key() {
    const name = document.querySelector('meta[name="csrf-cookie"]').content;
    return "seapath-chrome-" + name;
  }

  function remember(seen) {
    try {
      sessionStorage.setItem(key(), JSON.stringify(seen));
    } catch (error) {
      /* A browser refusing storage asks on every page, as it always did. */
    }
  }

  // Signing out, and being signed out. Both end this visit, and the header of
  // the next one belongs to whoever signs in then: a name left behind here
  // would be painted over their first page until the API answered.
  function forget() {
    try {
      sessionStorage.removeItem(key());
    } catch (error) {
      /* Nothing was stored either. */
    }
  }

  function isAdmin(me) {
    return me.role === "admin";
  }

  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await API.post("/auth/logout");
    } finally {
      forget();
      window.location.assign("login");
    }
  });

  return { load, isAdmin, current: () => signedIn };
})();
