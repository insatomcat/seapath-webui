// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The top bar, shared by every signed in page: who is here, which node this
// is, the way out, and Escape.

const Chrome = (function () {
  // Escape dismisses the window on top, on every page. It clicks the control
  // the window names in `data-dismiss` rather than hiding the element, so the
  // page's own teardown runs: the console closes its socket, a confirmation
  // clears the machine it was about to name, a form empties the file it was
  // holding. Hiding the element would leave all three behind.
  //
  // Last first, because a window later in the document is the one drawn over
  // the others, and it is the one an operator means.
  //
  // One window keeps the key: the console, where Escape is a byte the shell is
  // waiting for and the terminal has already claimed it. That is what the
  // `defaultPrevented` guard leaves alone, and why the console has a Close
  // button of its own.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented) {
      return;
    }
    const windows = Array.from(document.querySelectorAll(".modal[data-dismiss]"));
    const top = windows.reverse().find((modal) => !modal.hidden);
    if (!top) {
      return;
    }
    const control = document.getElementById(top.dataset.dismiss);
    if (control && !control.disabled) {
      control.click();
    }
  });

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

  return { load, isAdmin };
})();
