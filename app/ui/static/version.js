// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The version button of the top bar: whether a newer seapath-webui exists, and
// the upgrade to it in one click.
//
// Three steps, each one the endpoint the Deployment page uses. The registry is
// asked once when a session opens, then on a click, because asking leaves the
// machine. That first question is a quiet one: a node with no route to a
// registry is a supported state, so what it answers goes in the title of the
// button rather than in a banner greeting every sign in. A newer version turns
// the button into the upgrade, which pins it in the inventory as one commit
// and launches the catalogue entry that deploys this service on every machine
// naming an image. That run restarts this service and no guest, which is why
// one click is enough here while every other convergence is confirmed.
//
// A newer version the registry answered is kept for this tab, so the button
// keeps its word from one page to the next. It is keyed by the version
// answering, so an upgrade that landed starts over from a question. The
// answer that there is nothing to do is kept for a few seconds and never
// across pages: it is true only until the registry holds something newer, and
// a button stuck on it hides that it can ask again.
//
// The question and that answer are glyphs, beside the other switches of the
// bar. A word is spent on the version the button offers to deploy, and on the
// step it is running, because those name something an operator has to read.

(function () {
  const PLAYBOOK = "seapath_setup_deploy_seapath_webui";
  const button = document.getElementById("version-button");
  const banner = document.getElementById("version-error");
  if (!button || !Chrome.isAdmin(Chrome.current())) {
    return;
  }
  const glyphs = {
    ask: button.querySelector('[data-version-glyph="ask"]'),
    settled: button.querySelector('[data-version-glyph="settled"]'),
  };
  const word = button.querySelector("[data-version-label]");
  const running = document.querySelector('meta[name="version"]').content;
  const KEY = "seapath-webui-latest:" + running;
  // Set once the session asked on its own. Signing in clears the storage of
  // the tab, so the next session asks again, including one that follows an
  // expiry rather than a sign out.
  const ASKED = "seapath-webui-asked:" + running;
  const SETTLED_MS = 4000;

  // What the button would do, once the registry and the inventory answered:
  // `pin` a newer version then run, `apply` the version already pinned, or
  // nothing.
  let answer = recall();
  // What the last check found when it found nothing to do, said in the title
  // of the button once it asks again.
  let settled = "";
  let settling = null;

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

  function askedAlready() {
    try {
      if (sessionStorage.getItem(ASKED)) {
        return true;
      }
      sessionStorage.setItem(ASKED, "1");
      return false;
    } catch (error) {
      // No storage means no way to ask only once: the click stays the way.
      return true;
    }
  }

  // A glyph alone, which the name carries for anything that reads the button
  // rather than looks at it.
  function showGlyph(name, named) {
    glyphs.ask.hidden = name !== "ask";
    glyphs.settled.hidden = name !== "settled";
    word.hidden = true;
    word.textContent = "";
    button.classList.add("version-glyph");
    button.setAttribute("aria-label", named);
  }

  // A word alone, which is its own name.
  function showWord(text) {
    glyphs.ask.hidden = true;
    glyphs.settled.hidden = true;
    word.textContent = text;
    word.hidden = false;
    button.classList.remove("version-glyph");
    button.removeAttribute("aria-label");
  }

  function say(message) {
    banner.textContent = message;
    banner.hidden = !message;
  }

  function draw() {
    button.hidden = false;
    button.classList.remove("version-due");
    if (answer && answer.act === "pin") {
      showWord("Upgrade to " + answer.version);
      button.title =
        "Pins " + answer.version + " in the inventory for " +
        answer.machines.join(", ") + ", then deploys it there. This " +
        "service restarts on each of them, and no guest does.";
      button.classList.add("version-due");
      return;
    }
    if (answer && answer.act === "apply") {
      showWord("Apply " + answer.version);
      button.title =
        "The inventory already names " + answer.version + ": deploys it, " +
        "which restarts this service on the machines naming an image.";
      button.classList.add("version-due");
      return;
    }
    if (settling !== null) {
      showGlyph("settled", "Up to date");
      button.title = settled;
      return;
    }
    showGlyph("ask", "Check version");
    button.title =
      (settled ? settled + " " : "") +
      "Asks the registry for a seapath-webui newer than " + running + ".";
  }

  // An empty text leaves the button as it is drawn and only takes it out of
  // reach, which is what the question does: its glyph already says what is
  // running.
  function busy(text, on) {
    button.disabled = on;
    if (on) {
      if (text) {
        showWord(text);
      }
      button.setAttribute("aria-busy", "true");
    } else {
      button.removeAttribute("aria-busy");
    }
  }

  async function check(quiet) {
    say("");
    settled = "";
    window.clearTimeout(settling);
    settling = null;
    showGlyph("ask", "Checking");
    busy("", true);
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
        if (quiet) {
          settled = latest.reason;
        } else {
          say(latest.reason);
        }
      } else {
        answer = null;
        settled =
          "The newest version " + latest.repository + " holds is " +
          latest.latest + ", which the inventory names.";
        settling = window.setTimeout(() => {
          settling = null;
          draw();
        }, SETTLED_MS);
      }
    } catch (failure) {
      answer = null;
      if (quiet) {
        settled = failure.message;
      } else {
        say(failure.message);
      }
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
        showWord("Launching");
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
      check(false);
    }
  });

  draw();
  if (!answer && !askedAlready()) {
    check(true);
  }
})();
