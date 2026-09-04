// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The top bar, shared by every signed in page: who is here, which node this
// is, and the way out.

const Chrome = (function () {
  async function load() {
    try {
      const [me, node] = await Promise.all([
        API.get("/auth/me"),
        API.get("/node"),
      ]);
      document.getElementById("identity").textContent =
        me.username + " (" + me.role + ")";
      document.getElementById("node-name").textContent = node.hostname;
      const mode = document.getElementById("node-mode");
      mode.textContent = node.mode;
      mode.className = "badge badge-" + node.mode;
      return { me, node };
    } catch (failure) {
      if (failure.status === 401) {
        window.location.assign("login");
      }
      throw failure;
    }
  }

  function isAdmin(me) {
    return me.role === "admin";
  }

  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await API.post("/auth/logout");
    } finally {
      window.location.assign("login");
    }
  });

  return { load, isAdmin };
})();
