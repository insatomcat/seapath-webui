// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

(function () {
  const form = document.getElementById("login-form");
  const error = document.getElementById("error");
  const submit = document.getElementById("submit");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    try {
      await API.post("/auth/login", {
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
      });
      // A session that expired sent this tab here without signing out, so
      // what the previous one kept is still in its storage: the panels it was
      // showing, and the mark that stops the version check from asking again.
      // A new session starts from nothing, whichever way the last one ended.
      try {
        sessionStorage.clear();
      } catch (storage) {
        /* Nothing was stored either. */
      }
      window.location.assign("./");
    } catch (failure) {
      // The message is written for the operator, so it is shown as it comes.
      error.textContent = failure.message;
      error.hidden = false;
      submit.disabled = false;
      document.getElementById("password").value = "";
    } finally {
      submit.removeAttribute("aria-busy");
    }
  });
})();
