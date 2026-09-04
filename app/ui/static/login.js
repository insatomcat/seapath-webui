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
      window.location.assign("/");
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
