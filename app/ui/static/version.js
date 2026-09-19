// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The version button of the top bar: whether a newer seapath-webui exists, and
// the upgrade to it in one click.
//
// Three steps, each one the endpoint the Deployment page uses. The registry is
// asked on a click, because asking leaves the machine. A newer version turns
// the button into the upgrade, which pins it in the inventory as one commit
// and launches the catalogue entry that deploys this service on every machine
// naming an image. That run restarts this service and no guest, which is why
// one click is enough here while every other convergence is confirmed.
//
// What the registry answered is kept for this tab, so the button keeps its
// word from one page to the next. It is keyed by the version answering, so an
// upgrade that landed starts over from a question.

(function () {
  const PLAYBOOK = "seapath_setup_deploy_seapath_webui";
  const button = document.getElementById("version-button");
  const banner = document.getElementById("version-error");
  if (!button || !Chrome.isAdmin(Chrome.current())) {
    return;
  }
  const running = document.querySelector('meta[name="version"]').content;
  const KEY = "seapath-webui-latest:" + running;

  // What the button would do, once the registry and the inventory answered:
  // `pin` a newer version then run, `apply` the version already pinned, or
  // nothing.
  let answer = recall();

  function recall() {
    try {
      return JSON.parse(sessionStorage.getItem(KEY)) || null;
    } catch (error) {
      return null;
    }
  }

  function remember(value) {
    try {
      if (value) {
        sessionStorage.setItem(KEY, JSON.stringify(value));
      } else {
        sessionStorage.removeItem(KEY);
      }
    } catch (error) {
      /* Kept for this page only. */
    }
  }

  function say(message) {
    banner.textContent = message;
    banner.hidden = !message;
  }

  function draw() {
    button.hidden = false;
    button.classList.remove("version-due");
    if (answer && answer.act === "pin") {
      button.textContent = "Upgrade to " + answer.version;
      button.title =
        "Pins " + answer.version + " in the inventory for " +
        answer.machines.join(", ") + ", then deploys it there. This " +
        "service restarts on each of them, and no guest does.";
      button.classList.add("version-due");
      return;
    }
    if (answer && answer.act === "apply") {
      button.textContent = "Apply " + answer.version;
      button.title =
        "The inventory already names " + answer.version + ": deploys it, " +
        "which restarts this service on the machines naming an image.";
      button.classList.add("version-due");
      return;
    }
    if (answer && answer.act === "none") {
      button.textContent = "Up to date";
      button.title = answer.note + " Click to ask again.";
      return;
    }
    button.textContent = "Check version";
    button.title =
      "Asks the registry for a seapath-webui newer than " + running + ".";
  }

  function busy(text, on) {
    button.disabled = on;
    if (on) {
      button.textContent = text;
      button.setAttribute("aria-busy", "true");
    } else {
      button.removeAttribute("aria-busy");
    }
  }

  async function check() {
    say("");
    busy("Checking", true);
    try {
      const [latest, update] = await Promise.all([
        API.get("/node/update/latest"),
        API.get("/node/update"),
      ]);
      if (!latest.reason && latest.newer) {
        answer = { act: "pin", version: latest.latest, machines: latest.machines };
      } else if (update.pending) {
        answer = { act: "apply", version: update.wanted };
      } else if (latest.reason) {
        answer = null;
        say(latest.reason);
      } else {
        answer = {
          act: "none",
          note: "The newest version " + latest.repository + " holds is " +
            latest.latest + ", which the inventory names.",
        };
      }
    } catch (failure) {
      answer = null;
      say(failure.message);
    } finally {
      busy("", false);
    }
    remember(answer);
    draw();
  }

  async function upgrade() {
    say("");
    busy(answer.act === "pin" ? "Pinning" : "Launching", true);
    try {
      if (answer.act === "pin") {
        await API.post("/node/update", { version: answer.version });
        // The commit stands on its own: should the run be refused, what is
        // left is applying the version the inventory now names.
        answer = { act: "apply", version: answer.version };
        remember(answer);
        button.textContent = "Launching";
      }
      const launched = await API.post("/runs", { playbook: PLAYBOOK });
      remember(null);
      answer = null;
      RunWatch.open(launched.run_id);
    } catch (failure) {
      say(failure.message);
    } finally {
      busy("", false);
      draw();
    }
  }

  button.addEventListener("click", () => {
    if (answer && (answer.act === "pin" || answer.act === "apply")) {
      upgrade();
    } else {
      check();
    }
  });

  draw();
})();
