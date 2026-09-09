# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The pages, checked for the things a screenshot would not catch."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import __version__


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/inventory",
        "/deployment",
        "/vms",
        "/containers",
        "/cluster",
        "/realtime",
        "/runs",
    ],
)
def test_every_page_needs_a_session(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "login"


@pytest.mark.parametrize(
    ("path", "script"),
    [
        ("/", "node.js"),
        ("/inventory", "inventory.js"),
        ("/deployment", "deployment.js"),
        ("/vms", "vms.js"),
        ("/containers", "containers.js"),
        ("/cluster", "cluster.js"),
        ("/realtime", "realtime.js"),
        ("/runs", "runs.js"),
    ],
)
def test_each_page_loads_its_own_script_and_the_shared_chrome(
    signed_in: TestClient, path: str, script: str
) -> None:
    body = signed_in.get(path).text

    assert script in body
    assert "chrome.js" in body
    assert "api.js" in body


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/inventory",
        "/deployment",
        "/vms",
        "/containers",
        "/cluster",
        "/realtime",
        "/runs",
    ],
)
def test_every_script_a_page_loads_names_the_version_that_served_it(
    signed_in: TestClient, path: str
) -> None:
    """A browser must never pair a script from one version with a page from another.

    Both halves are served `no-cache`, and that check compares the copy a
    browser holds against the file the same service has on disk, so a copy kept
    from another version of this service is reported as current. The two then
    disagree about the elements they name, the page script dies on the first
    one that is missing, and the whole page renders and does nothing. That cost
    an afternoon on a node once. The version in the URL makes the halves two
    different resources.
    """
    body = signed_in.get(path).text
    sources = re.findall(r'<script src="([^"]+)"', body)

    assert sources
    for source in sources:
        assert source.endswith(f"?v={__version__}"), source


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/inventory",
        "/deployment",
        "/vms",
        "/containers",
        "/cluster",
        "/realtime",
        "/runs",
    ],
)
def test_every_page_can_say_that_its_own_script_never_ran(
    signed_in: TestClient, path: str
) -> None:
    """The one failure a page cannot report by itself.

    Every screen here is built by a script, so a script that dies before its
    first statement leaves a page with placeholders on it and no message
    anywhere. The handler is in the head, ahead of the scripts it watches, and
    the element it writes to is outside the page body, because the page's own
    banner is built by the script that just died.
    """
    body = signed_in.get(path).text

    assert 'id="script-error"' in body
    assert 'window.addEventListener("error"' in body
    assert "did not load" in body


def test_the_login_page_reports_a_script_that_never_ran_too(
    client: TestClient,
) -> None:
    body = client.get("/login").text

    assert 'id="script-error"' in body
    assert f"login.js?v={__version__}" in body


def test_a_form_control_is_the_size_of_the_text_beside_it(
    signed_in: TestClient,
) -> None:
    css = signed_in.get("/static/style.css").text

    # A browser gives a button, a field and a select a font of its own, in
    # pixels, which the root font size does not reach. Without this every
    # button on screen is drawn a size larger than the page around it.
    assert "html {\n  font-size: 80%;" in css
    assert "button,\ninput,\nselect,\ntextarea {\n  font: inherit;\n}" in css


def test_the_two_inventory_lines_stay_inside_their_column(
    signed_in: TestClient,
) -> None:
    css = signed_in.get("/static/style.css").text

    # A button is not stretched by the cross axis of the column that holds it,
    # so each of these was drawn at the width of its own text and painted over
    # the editor beside it.
    block = css.split("#inventory-panels .panel-open {")[1].split("}")[0]
    assert "width: 100%;" in block
    assert "min-width: 0;" in block


def test_the_header_paints_the_node_this_browser_already_saw(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/chrome.js").text

    # The name and the mode are the same two strings on every page between two
    # runs, and asking for them again on each navigation blinked the header
    # through its placeholders. The document reads what the last page stored,
    # `chrome.js` writes it back from the API and corrects both.
    assert "seapath-chrome-" in body
    assert "sessionStorage.getItem" in body
    assert "seapath-chrome-" in script
    assert "sessionStorage.setItem" in script
    # Keyed per node: two nodes reached through two ssh tunnels are one origin
    # to the browser, and one key would show one node's name over the other's.
    assert 'name="csrf-cookie"' in script


def test_the_inventory_page_says_what_saving_does_and_does_not_do(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text

    # The two acts are separate and now live on separate pages, which is the
    # whole reason for the split: deciding what a machine should be, and making
    # it so.
    assert "Every change is a commit" in body
    assert "that is the Deployment page" in body


def test_the_inventory_page_carries_the_other_copies_of_the_repository(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    prose = " ".join(body.split())

    # The copies live where the repository is edited, behind a state line that
    # says whether they hold this commit, and the window says what a
    # replication is and what it refuses.
    assert 'id="replicas-open"' in body
    assert 'id="replicas-modal"' in body
    assert 'id="replicate"' in body
    assert "over the connection a run makes" in prose
    assert "is refused rather than overwritten" in prose
    # Overriding that refusal is a checkbox that says what it destroys.
    assert 'id="replicate-force"' in body
    assert "survives there only in its reflog" in prose or "reads its reflog" in prose


def test_the_inventory_page_is_an_editor_over_the_folder(
    signed_in: TestClient,
) -> None:
    inventory = signed_in.get("/inventory").text
    deployment = signed_in.get("/deployment").text

    # The desired state of these machines is a folder, so the page is the
    # shape of one: the files on the left, the open file on the right.
    assert 'id="tree"' in inventory
    assert 'id="editor"' in inventory
    assert 'class="page split"' in inventory
    # And editing it happens here and nowhere else.
    assert 'id="editor"' not in deployment
    assert "inventory.js" not in deployment


def test_the_inventory_page_carries_the_folder_around_the_inventory(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/inventory.js").text

    # An inventory is rarely alone: a dozen roles name a file this machine has
    # to hold, and the page has to be a way of putting one there.
    assert 'id="add-file"' in body
    assert 'id="new-file"' in body
    # The two stores are listed together, since a run overlays them in the same
    # place, and told apart where an operator reads them, since one is in the
    # history and the other is not.
    assert '"Files, versioned with it"' in script
    assert '"Artefacts, kept out of git"' in script
    # And every path the inventory names is one click away.
    assert 'id="references-table"' in body


def test_a_validation_warning_names_the_machine_it_is_about(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/inventory.js").text

    # Almost every rule is per host and the wording is identical across them,
    # so "No PTP interface" on a four machine inventory was four identical
    # lines with nothing saying which machine was missing one. A finding
    # carries the host; the banner has to show it.
    assert 'finding.host + ": " + finding.message' in script


def test_a_file_the_inventory_names_and_the_folder_lacks_is_in_the_list(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/inventory.js").text

    # A missing file stops a convergence at a task that failed on every host at
    # once. It is listed among the files that exist, where an operator is
    # already looking, and clicking it opens it as a file to write.
    assert '"Named by the inventory, not here"' in script
    assert "missingEntries" in script
    assert "tree-item.missing" in signed_in.get("/static/style.css").text
    assert 'id="tree"' in body


def test_the_machine_can_propose_its_own_inventory_into_the_editor(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/inventory.js").text

    # The seed of first boot, on demand: a machine re-cabled since, or one
    # whose discovery failed then, still has the file it would have written one
    # click away. It lands in the editor, and saving it is the operator's act.
    assert 'id="propose"' in body
    assert "api/v1/inventory/proposed" in script
    assert "nothing is committed until you do" in script


def test_the_inventory_file_is_saved_against_the_commit_it_was_read_at(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/inventory.js").text

    # Two operators editing the same file from two browsers is the ordinary
    # case, and refusing the second save beats merging it silently.
    assert '"If-Match"' in script


def test_the_deployment_page_carries_the_credentials_and_the_button(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text

    assert "Reaching the other machines" in body
    assert 'id="site-key-file"' in body
    assert 'id="host-keys-scan"' in body
    assert 'id="main-playbook"' in body
    # And edits nothing: the desired state has one page and it is the other one.
    assert 'id="tree"' not in body


def test_the_deployment_page_says_which_collection_this_node_runs(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    script = signed_in.get("/static/deployment.js").text

    # Which playbooks this node runs is answerable without opening the panel,
    # because the answer is a fact about the machine rather than a form.
    assert "The code this node runs" in body
    assert 'id="collection-state"' in body
    assert 'id="collection-file"' in body
    # And the catalogue is read again after one is installed, so the page
    # offers the collection that just landed.
    assert 'API.upload("/collection", file)' in script


def test_the_deployment_page_offers_the_version_the_registry_holds(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    script = signed_in.get("/static/deployment.js").text

    # Next to the collection, because they are the two halves of "which code
    # this node runs" and they are updated in two different ways.
    assert 'id="update-check"' in body
    assert 'id="update-go"' in body
    # Asked on a click and never on page load: the answer leaves the machine,
    # and a substation hypervisor may have no route to make it.
    assert 'API.get("/node/update/latest")' in script
    assert 'element("update-check").addEventListener("click", loadLatest)' in script
    # And what the button does is a commit, then the confirmation every other
    # convergence gets.
    assert 'API.post("/node/update", { version: state.latest.latest })' in script
    assert "confirmRun(item.entry, false)" in script


def test_the_commissioning_playbook_is_the_page_and_the_rest_is_a_list(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    script = signed_in.get("/static/deployment.js").text

    # Thirteen stacked entries put the one an operator came for below the fold,
    # and the one they came for is never the first. `seapath_setup_main` is the
    # commissioning path and stays a button; everything else is chosen from a
    # list that carries the whole catalogue, unavailable entries included.
    assert 'id="main-playbook"' in body
    assert 'id="playbook-choice"' in body
    assert 'id="playbook-detail"' in body
    assert '"seapath_setup_main"' in script
    # Grouped the way docs/playbooks.md groups it.
    assert '"Machine configuration"' in script
    assert '"Cluster"' in script
    # And an entry the collection does not carry stays in the list, saying why,
    # rather than disappearing from it.
    assert '" (unavailable)"' in script


def test_the_list_says_which_entries_nobody_reviewed(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/deployment.js").text

    # Everything the collection ships is in the list. The entries nobody wrote
    # a sentence for are last, under a heading that says where the description
    # came from, and they carry the counts the reader took from the playbook.
    assert '"Read from the collection, not reviewed"' in script
    assert "entry.reviewed" in script
    assert '"not reviewed"' in script
    assert "playbook-counts" in script
    # Catalogue prose names commands and variables in backticks, the way the
    # documents it was written alongside do, and those sentences are read right
    # before an apply. Rendered as text they showed as literal backticks.
    assert "withCode" in script


def test_the_catalogue_says_it_is_being_read_before_it_is(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    script = signed_in.get("/static/deployment.js").text
    css = signed_in.get("/static/style.css").text

    # Reading the catalogue walks every playbook of the installed collection.
    # Two empty cards for a second is what a node with no collection at all
    # looks like, so the cards say which of the two is happening.
    assert 'id="main-loading"' in body
    assert 'id="other-loading"' in body
    assert '<div id="main-playbook" hidden>' in body
    assert "showPlaybooksLoading" in script
    assert "The catalogue could not be read." in script
    assert ".loading" in css
    # An operator who turned animation off is told by the text alone.
    assert "prefers-reduced-motion" in css


def test_the_disks_sit_beside_the_cpu_rather_than_below_the_fold(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/").text
    css = signed_in.get("/static/style.css").text

    # Three cards of machine facts across, one wide table below. Left to the
    # page's auto-fit, a wide window left a third of the first row empty and
    # pushed the disks off the screen.
    assert '<main class="page node">' in body
    assert '<section class="card" id="card-disks">' in body
    assert body.index('id="card-disks"') < body.index('id="card-network"')
    assert '<section class="card wide" id="card-network">' in body
    assert ".page.node" in css


def test_the_history_is_a_state_line_over_a_window(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    css = signed_in.get("/static/style.css").text

    # The history asks for twenty commits, and twenty rows stacked under the
    # folder made the page twice the height of the file being edited. The line
    # carries the last commit, which is the answer to "did my save land", and
    # the rows scroll inside the window it opens.
    assert 'id="history-open"' in body
    assert 'id="history-state"' in body
    assert '<div class="modal" id="history-modal"' in body
    assert "#history-modal .table-scroll" in css
    assert "#editor-card {\n  grid-row: span 2;" in css


def test_the_ssh_credentials_are_a_state_line_once_they_hold(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    css = signed_in.get("/static/style.css").text

    # Set once, then read. A key upload above the playbook an operator came for
    # is a form they scroll past every time and fill in never.
    assert 'id="reach-open"' in body
    assert 'id="reach-state"' in body
    assert ".reach-state.warn" in css
    # Last on the page, and the form itself is a window rather than a fold, so
    # that opening it cannot push the playbook off the screen.
    assert body.index('id="main-playbook"') < body.index('id="reach-open"')
    assert body.index('id="playbook-choice"') < body.index('id="reach-open"')
    assert '<div class="modal" id="reach-modal"' in body
    assert '<div class="modal" id="collection-modal"' in body


def test_the_old_configuration_url_still_leads_somewhere(
    signed_in: TestClient,
) -> None:
    response = signed_in.get("/setup", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "inventory"


def test_the_page_that_was_called_system_still_leads_somewhere(
    signed_in: TestClient,
) -> None:
    # An operator's bookmark, and the URL every note written before the rename
    # carries.
    response = signed_in.get("/system", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "deployment"


def test_the_apply_confirmation_says_what_it_will_disturb(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text

    # The single most dangerous button in the product asks once, in a modal
    # naming the disruption and the machines. Typing the host name was here
    # too, and an operator who applies twenty times a day types it twenty times
    # without reading the sentence above it.
    assert 'id="confirm"' in body
    assert 'id="confirm-disruption"' in body
    assert 'id="confirm-reboot"' in body
    assert "confirm-input" not in body


def test_the_reboot_is_declined_by_default_on_a_node_the_run_plays(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/deployment.js").text

    # The service usually runs on one of the machines it converges, and a
    # reboot there takes the page, the run and the operator's way back in with
    # it. So the box starts checked whenever this node is in the inventory, and
    # an operator who wants the reboot unchecks it. On a control machine
    # outside the inventory nothing here is at stake, and the upstream default
    # stands.
    assert "const playsThisNode = Boolean(state.thisHost);" in script
    assert 'let skipReboot = entry.reboots === "gated" && playsThisNode;' in script
    assert "box.checked = skipReboot;" in script
    # And every switch is set, since a playbook that reboots in two places
    # needs both. One of them alone reboots the machine anyway.
    assert "entry.reboot_variables.forEach" in script


def test_relaunching_asks_no_more_than_applying_does(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/runs").text

    # The heavier friction was on the lighter act: a relaunch converges again
    # with the same playbook, which is how a failed run is recovered.
    assert 'id="confirm-disruption"' in body
    assert "confirm-input" not in body


def test_a_hidden_element_is_hidden_whatever_its_display_rule(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    css = signed_in.get("/static/style.css").text

    # Everything in this UI is shown and dismissed with the `hidden` attribute,
    # and a `display` rule in the stylesheet beats the attribute. The
    # confirmation modal is `display: grid`, so without this rule it is on
    # screen from the moment the page loads, over a page nobody asked to leave,
    # and Cancel does not dismiss it.
    assert '<div class="modal" id="confirm"' in body
    assert " hidden>" in body
    assert "[hidden]" in css
    assert "display: none !important" in css


@pytest.mark.parametrize("path", ["/", "/inventory", "/deployment", "/runs", "/login"])
def test_a_page_is_styled_without_fetching_anything(
    signed_in: TestClient, path: str
) -> None:
    body = signed_in.get(path).text
    css = signed_in.get("/static/style.css").text

    # A linked stylesheet is a round trip between the navigation and the first
    # paint, and these assets are served `no-cache`, so every hop between the
    # tabs painted the page unstyled while the conditional request was in
    # flight. The head carries the styles themselves, and the schemes the
    # browser paints its own surfaces in.
    head = body.split("</head>")[0]
    assert '<meta name="color-scheme" content="light dark">' in head
    assert '<link rel="stylesheet"' not in body
    assert css in head
    # Read whole, so a selector with a `>` in it survives the templating.
    assert ".card.wide" in head
    assert "html {\n  font-size: 80%;\n  background: var(--bg);\n}" in css


@pytest.mark.parametrize("path", ["/", "/inventory", "/deployment", "/runs", "/login"])
def test_the_palette_is_chosen_before_the_page_is_painted(
    signed_in: TestClient, path: str
) -> None:
    head = signed_in.get(path).text.split("</head>")[0]

    # Inline and in the head, for the same reason the stylesheet is: an
    # operator whose system is light and who chose dark would see a white page
    # flash by on every navigation, and this UI is navigated all day. A fetched
    # script cannot promise to run before the first paint.
    assert 'src="static/theme.js"' not in head
    assert 'localStorage.getItem("seapath-theme")' in head
    assert "document.documentElement.dataset.theme = choice" in head
    # The login page is reached before there is a session, and it is styled by
    # the same head, so it is themed too.
    assert "(prefers-color-scheme: light)" in head


def test_the_two_palettes_are_the_only_place_a_colour_is_written(
    signed_in: TestClient,
) -> None:
    css = signed_in.get("/static/style.css").text

    # The claim the second palette rests on: every rule reads a token, so a
    # theme is a block of values rather than a second stylesheet. Anything
    # outside the two `:root` blocks that names a colour is a rule that will
    # stay dark on a light page.
    palettes, rules = css.split('[data-theme="light"]')[1].split("}", 1)
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rules)
    assert literals == [], literals
    assert "--accent: #0b62c4" in palettes


def test_the_theme_switch_offers_the_system_as_a_third_state(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/").text

    # Two states cannot express "follow the system": once the operator has
    # touched a toggle there is no way back to it, and the system is the
    # default every other application on that laptop honours.
    assert 'role="radiogroup"' in body
    for choice in ("system", "light", "dark"):
        assert f'data-theme-choice="{choice}"' in body


def test_the_sign_in_page_is_themed_without_carrying_the_switch(
    client: TestClient,
) -> None:
    body = client.get("/login").text

    # The switch is chrome, and there is no chrome before there is a session.
    # The palette still applies, because it is decided in the head every page
    # shares rather than by the script that draws the switch.
    assert "data-theme-choice" not in body
    assert 'localStorage.getItem("seapath-theme")' in body


def test_the_console_keeps_its_own_ground_in_both_palettes(
    signed_in: TestClient,
) -> None:
    css = signed_in.get("/static/style.css").text
    script = signed_in.get("/static/console.js").text

    # The terminal draws what a shell and an Ansible run wrote for a terminal,
    # in the sixteen ANSI colours of one, which are chosen against a dark
    # ground. Remapping them for a light page would be rewriting output this
    # service passes through untouched, so the console tokens are declared once
    # and the light palette does not redeclare them.
    light = css.split('[data-theme="light"]')[1].split("}", 1)[0]
    assert "--console-bg:" in css.split('[data-theme="light"]')[0]
    assert "--console-bg:" not in light
    assert 'token("--console-bg")' in script


def test_a_table_scrolls_inside_its_card_rather_than_over_the_next_one(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/").text
    css = signed_in.get("/static/style.css").text

    # A `by-path` name is longer than half a page, and the cards sit in
    # flexible grid tracks, so nothing widens a card to fit its content. Both
    # tables scroll inside their own card rather than over the one next to it,
    # and the six column network table keeps the whole width.
    assert body.count('class="card wide"') == 1
    assert body.count('<div class="table-scroll">') == 2
    assert ".card.wide" in css
    assert "overflow-x: auto" in css


def test_the_login_page_carries_no_navigation(client: TestClient) -> None:
    body = client.get("/login").text

    assert "Sign out" not in body
    assert "chrome.js" not in body


def test_the_static_assets_are_served(signed_in: TestClient) -> None:
    for asset in (
        "api.js",
        "chrome.js",
        "node.js",
        "inventory.js",
        "deployment.js",
        "runs.js",
        "style.css",
    ):
        assert signed_in.get(f"/static/{asset}").status_code == 200


def test_the_deployment_page_says_once_why_nothing_can_run(
    signed_in: TestClient, settings, tmp_path
) -> None:
    # A node running from source, or from an image built without the
    # collection, has every entry unavailable for the same reason. Nine dimmed
    # rows each repeating it in small print is how an operator ends up asking
    # why the buttons are greyed out.
    body = signed_in.get("/deployment").text

    assert 'id="apply-blocked"' in body


def test_the_run_view_shows_the_skipped_column(signed_in: TestClient) -> None:
    # A run of sixteen tasks reporting five ok reads as a truncated log until
    # the eleven skipped ones are visible somewhere.
    body = signed_in.get("/runs").text
    script = signed_in.get("/static/runs.js").text

    assert "<th>skipped</th>" in body
    assert "counts.skipped" in script
    # And the recap line carries Ansible's numbers rather than the bare word.
    assert "recapLine" in script


def test_a_static_asset_is_revalidated_rather_than_held(
    signed_in: TestClient,
) -> None:
    # A node upgraded in place serves new HTML and, without this, an old
    # script: the page is then half from each version, and the symptom looks
    # like a bug in the new code. `no-cache` costs one conditional request and
    # answers 304 while the file is unchanged.
    response = signed_in.get("/static/runs.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag")

    unchanged = signed_in.get(
        "/static/runs.js", headers={"If-None-Match": response.headers["etag"]}
    )
    assert unchanged.status_code == 304


def test_the_node_page_carries_the_terminal_and_says_what_it_is(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/").text

    # The emulator and its stylesheet are served from this node, because a
    # substation hypervisor has no route to a CDN. The stylesheet is in the
    # document, like the rest of the styles of this service, so the first paint
    # of this page waits on no fetch.
    assert "static/vendor/xterm.js" in body
    assert ".xterm {" in body
    assert "static/console.js" in body
    assert signed_in.get("/static/vendor/xterm.js").status_code == 200
    assert signed_in.get("/static/vendor/xterm.css").status_code == 200

    # A shell is the one place in this UI where what an operator does is
    # neither recorded nor part of the desired state, and the panel says so
    # every time it opens.
    assert "passwordless <code>sudo</code>" in body
    assert "undone by the next run that touches it" in body


def test_the_real_time_page_checks_every_machine(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/realtime").text

    # One row per check and one column per machine. The checks used to be about
    # the node the browser happened to be pointed at, which said nothing about
    # the other hypervisors of the same cluster, and the commonest findings in
    # a substation are exactly the ones that hide on the machine nobody is
    # looking at.
    assert 'data-view="checks"' in body
    assert 'id="check-head"' in body

    # A machine that published nothing is not a machine that failed a check,
    # and the legend carries the difference.
    assert "nothing published" in body
    assert "what its own\n        inventory entry asks of it" in body


def test_the_real_time_page_offers_both_measurements(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/realtime").text

    # Both, because they answer complementary questions: what the scheduler
    # delivered and what the firmware took without telling the kernel. Each is
    # launched behind a confirmation, since both load every machine the
    # inventory declares.
    assert 'data-view="cyclictest"' in body
    assert 'data-view="hwlatdetect"' in body
    assert 'id="panel-cyclictest"' in body
    assert 'id="panel-hwlatdetect"' in body
    assert 'id="measure-confirm"' in body


def test_a_firmware_measurement_that_came_back_clean_says_so_in_green(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/realtime.js").text
    css = signed_in.get("/static/style.css").text

    # The clean result was a grey line under the heading, which reads as a
    # panel with nothing in it. The measuring is the expensive part: it loads
    # every machine and holds interrupts off for the whole run, and both of its
    # answers are worth the same box.
    assert 'verdict.className = "clear";' in script
    assert ".clear {" in css
    assert "--ok-wash" in css
    # And the sentence carries the verdict on its own, so the colour is never
    # the only cue.
    assert "No interruption above the threshold." in script


def test_the_real_time_page_shows_one_panel_at_a_time(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/realtime").text

    # An application layout rather than a document, and one panel of it on
    # screen. Three panels sharing one screen each got a third of the room
    # their content needs, and every one of them answered by truncating: the
    # conformance values, the cluster's fourth node, the histogram's axis.
    # `paned` is the layout, shared with the Cluster page, and `realtime` is
    # this page. Both are asserted because the layout is what the test is about
    # and the page is what makes the assertion about this one.
    assert 'class="page paned realtime"' in body
    assert body.count('class="card pane"') == 3
    assert body.count('class="card pane" id="card-map" hidden') == 1
    assert body.count('class="card pane" id="card-measure" hidden') == 1


def test_the_real_time_view_bar_carries_each_panel_s_answer(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/realtime").text

    # The bar is the summary before it is a navigation. Only one panel is on
    # screen, so each tab carries its own status dot and the line the panel
    # would lead with, and the page still answers at a glance without an
    # operator opening the three panels that are hidden.
    bar = body.split('<nav class="views"')[1].split("</nav>")[0]
    for view in ["checks", "pool", "cyclictest", "hwlatdetect"]:
        assert f'data-view="{view}"' in bar
    assert bar.count('class="view-answer"') == 4
    assert bar.count('<span class="dot ') == 4


# The property a reverse proxy depends on: nothing this service serves names a
# path from the root of the origin. A site mounting the UI under a prefix, say
# `/seapath/`, gets a working application only if every link, script, fetch,
# websocket and redirect resolves against the document rather than the host. It
# is a property of the served bytes, so it is asserted on them.
_ROOT_ANCHORED = re.compile(
    r"""(?:href|src)\s*=\s*["']/|"/api/v1|location\.assign\(\s*["']/"""
)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/inventory",
        "/deployment",
        "/vms",
        "/containers",
        "/realtime",
        "/runs",
        "/login",
    ],
)
def test_no_page_anchors_a_url_to_the_root(signed_in: TestClient, path: str) -> None:
    body = signed_in.get(path).text

    assert _ROOT_ANCHORED.search(body) is None


@pytest.mark.parametrize(
    "asset",
    [
        "api.js",
        "chrome.js",
        "console.js",
        "inventory.js",
        "login.js",
        "node.js",
        "realtime.js",
        "runs.js",
        "deployment.js",
        "vms.js",
        "containers.js",
    ],
)
def test_no_script_anchors_a_url_to_the_root(signed_in: TestClient, asset: str) -> None:
    script = signed_in.get(f"/static/{asset}").text

    assert _ROOT_ANCHORED.search(script) is None


@pytest.mark.parametrize("path", ["/", "/system", "/setup"])
def test_a_redirect_stays_inside_the_mount_point(client: TestClient, path: str) -> None:
    # An absolute `Location` would send an operator to the root of the reverse
    # proxy, out of the prefix the service was mounted under.
    response = client.get(path, follow_redirects=False)

    assert response.status_code in (303, 308)
    assert not response.headers["location"].startswith("/")


def test_the_cluster_page_shows_one_panel_at_a_time(signed_in: TestClient) -> None:
    body = signed_in.get("/cluster").text

    # The Real time page's layout, reused for the same reason (D28): three
    # tables that each need the screen, and a bar saying what the two behind it
    # found.
    assert 'class="page paned cluster"' in body
    assert body.count('class="card pane"') == 3
    assert body.count('class="card pane" id="card-resources" hidden') == 1
    assert body.count('class="card pane" id="card-storage" hidden') == 1


def test_the_cluster_view_bar_carries_each_panel_s_answer(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/cluster").text

    bar = body.split('<nav class="views"')[1].split("</nav>")[0]
    for view in ["members", "resources", "storage"]:
        assert f'data-view="{view}"' in bar
    assert bar.count('class="dot status-unknown"') == 3
    assert bar.count("view-answer") == 3


def test_the_cluster_page_says_where_a_cluster_is_changed_from(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/cluster").text
    prose = " ".join(body.split())

    # The one act the page offers says what it is and where it runs, so an
    # operator knows the command reached a machine rather than this container.
    assert "<code>crm resource refresh</code>" in body
    assert "run on a cluster member over the connection a convergence uses" in prose
    # Moving a resource is a button now, and the page says what the button
    # writes: the same constraint `preferred_host` writes, which is why a
    # deliberate placement is legible beside a declared one. See D34.
    assert "Move overrides the decision" in prose
    assert "the same object <code>preferred_host</code> writes" in prose
    assert "Standby is <code>crm node standby</code>, run on a cluster member" in prose
    # And adding storage is the path every other change takes here.
    assert "<code>ceph_osd_disks</code> in the" in body
    assert "cluster_setup_cephadm" in body


def test_the_resources_panel_carries_the_refresh_and_opens_the_constraints(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/cluster").text
    script = signed_in.get("/static/cluster.js").text

    # A confirmation, because this one reaches a live cluster.
    assert 'id="confirm-go"' in body
    assert "/cluster/resources/" in script
    # Both scopes, and the page says which of the two is the smaller act.
    assert 'id="refresh-all"' in body
    assert "The button on a row is the smaller act." in " ".join(body.split())
    # The constraints are a panel of their own under the table, open, and
    # spaced off it, and it says what each prefix means: the ids are the only
    # thing that tells a pin from a preference.
    assert 'class="sub-panel" id="constraints" open' in body
    assert "<code>cli-prefer-</code> is a placement somebody asked for" in body


def test_the_resources_panel_places_a_resource_and_gives_it_back(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/cluster.js").text

    # Move and its inverse, on the same endpoints the VMs page calls, with the
    # destination chosen inside the confirmation.
    assert '"/move"' in script or '/move"' in script
    assert '"/clear"' in script or '/clear"' in script
    assert "choose: {" in script
    # A pinned resource and a clone have no node to be sent to, and neither is
    # offered one.
    assert "if (pinOf(cluster, resource.id) || resource.clone) {" in script


def test_the_membership_panel_empties_a_machine_and_fills_it_again(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/cluster").text
    prose = " ".join(body.split())
    script = signed_in.get("/static/cluster.js").text

    # One button, and it is the one that changes something: a node in standby
    # is offered its way back and nothing else.
    assert '(standby ? "/standby" : "/online")' in script
    assert 'held ? "Bring online" : "Standby"' in script
    # The two things the confirmation has to say before an operator agrees.
    assert "No other member is online and out of standby" in script
    assert "a node in standby still votes, so quorum is unchanged" in prose


def test_the_cluster_page_never_reads_ceph_s_absence_as_a_fault(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/cluster.js").text

    # A Pacemaker cluster with local storage is a supported SEAPATH
    # configuration (docs/ceph.md), so the tab is hollow rather than amber.
    assert 'summarise("storage", "absent", "No Ceph on this cluster")' in script


def test_the_vms_page_joins_the_definition_and_the_state(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/vms").text

    # The two halves are on one row and labelled as two: the definition
    # columns are the desired state, the state and node columns are what
    # Pacemaker reports at this moment.
    for column in ("Guest", "State", "Node", "Disk image", "libvirt XML"):
        assert f"<th>{column}</th>" in body
    # And where each of them is changed, since nothing on this page writes.
    assert 'href="inventory"' in body
    assert 'href="deployment"' in body


def test_every_act_that_cannot_be_undone_is_confirmed_first(
    signed_in: TestClient,
) -> None:
    # Stopping a guest stops what it was serving, and on these machines that
    # is a substation function. Removing a metadata key is a write to an image
    # and nothing here puts back what it took away. One window for both, which
    # names the thing and says what happens.
    body = signed_in.get("/vms").text
    script = signed_in.get("/static/vms.js").text

    assert 'id="confirm"' in body
    assert 'id="confirm-title"' in body
    assert 'id="confirm-disruption"' in body
    assert "function confirmRemove(" in script
    assert 'label: "Remove it"' in script


def test_the_add_form_separates_what_each_deployment_role_reads(
    signed_in: TestClient,
) -> None:
    # Placement, priority, migration and the disk bus are Pacemaker's and
    # vm_manager's. The pinning profile is neither: a standalone deployment
    # writes it to /etc/seapath/alloc.d and the same hook reads it there.
    body = signed_in.get("/vms").text

    assert "data-cluster" in body
    assert "data-standalone" in body
    assert 'id="add-autostart"' in body
    # And the one field whose value is invisible in the XML the operator
    # brings, because vm_manager builds that element itself.
    assert "the RBD image, the Ceph monitors and the libvirt secret" in body


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/inventory",
        "/deployment",
        "/vms",
        "/containers",
        "/cluster",
        "/realtime",
        "/runs",
    ],
)
def test_every_window_names_what_dismisses_it(signed_in: TestClient, path: str) -> None:
    body = signed_in.get(path).text
    script = signed_in.get("/static/chrome.js").text

    # Escape closes the window on top, on every page. It works by clicking the
    # control the window names, so the page's own teardown runs: a window that
    # named nothing would be the one Escape leaves on screen.
    for opening in re.findall(r"<div class=\"modal\"[^>]*>", body):
        assert "data-dismiss=" in opening, (path, opening)
    assert 'querySelectorAll(".modal[data-dismiss]")' in script
    assert 'event.key !== "Escape"' in script


def test_adding_a_vm_is_its_own_window(signed_in: TestClient) -> None:
    # The form asks for three things and shows four steps while it works, so
    # it takes a window rather than growing the page under the guest list.
    body = signed_in.get("/vms").text

    assert '<div class="modal" id="add-modal"' in body
    assert 'id="add-steps"' in body


def test_the_deployment_column_says_what_the_run_does_to_this_guest(
    signed_in: TestClient,
) -> None:
    # Both roles register the hypervisor's own list first and skip their whole
    # creation block for a guest it already has, so the entry alone cannot
    # answer. A guest nothing reports is one the next run creates, and saying
    # "left alone" about it is the one case where this column would mislead
    # the operator who is about to launch that run.
    script = signed_in.get("/static/vms.js").text

    assert "const there = Boolean(guest.resource || guest.domain);" in script
    assert "if (there && !guest.force) {" in script
    assert 'return cell("left alone");' in script
    assert 'there ? "recreated" : "created"' in script
    # And the colour warns about the destruction rather than about a creation.
    assert 'there && guest.force ? "recreated" : ""' in script


def test_the_vms_page_offers_no_snapshot_yet(signed_in: TestClient) -> None:
    # Start, stop and placement are one task calling an upstream module or one
    # `crm` command, which is what D30 and D34 settle. The rest of the runtime
    # plane has no such answer yet, and the page implies none.
    body = signed_in.get("/vms").text.lower()

    for act in (">snapshot", ">clone", ">remove"):
        assert act not in body


def test_the_vms_page_moves_a_guest_and_gives_the_placement_back(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/vms").text
    script = signed_in.get("/static/vms.js").text

    # The destination is chosen in the window that names the disruption, so an
    # operator cannot confirm a node they set on a row some minutes ago.
    assert 'id="confirm-choice"' in body
    assert 'id="confirm-node"' in body
    # The act is on the Pacemaker resource, which is where the one endpoint
    # lives: the guest name is the resource id, so the page has one door.
    assert '"/cluster/resources/" + encodeURIComponent(guest.name) + "/move"' in script
    assert '"/cluster/resources/" + encodeURIComponent(guest.name) + "/clear"' in script
    # And what the confirmation has to say: the same constraint the entry's own
    # placement writes, and what it costs the guest.
    assert "same object preferred_host produces" in script
    assert "without it the guest is stopped where" in script
    # Pacemaker refuses a move to the node the resource is already active on,
    # so that node is not offered and the confirmation says where the wish to
    # keep a guest put belongs instead.
    assert "placementNodes.filter((node) => node !== guest.resource.node)" in script
    assert "declare preferred_host on its " in script


def test_the_vms_page_marks_a_guest_held_somewhere_it_was_not_declared(
    signed_in: TestClient,
) -> None:
    # The only reading that makes an override visible: `preferred_host` and a
    # move write the same `cli-prefer` object, so the CIB cannot say who asked
    # for it and the entry is what the constraint is held against. See D34.
    script = signed_in.get("/static/vms.js").text

    assert 'item.id.startsWith("cli-prefer-")' in script
    assert 'item.id.startsWith("pin-")' in script
    # The three readings, and the subject of each is the placement rather than
    # the guest: "declared" on its own would read as whether the inventory has
    # the guest at all, which is a different question this page also answers.
    assert '", as the inventory declares"' in script
    assert '", inventory declares " + declared' in script
    assert '", inventory declares no placement"' in script
    # And the sentence behind the tag, which carries what to do about it.
    assert "Return writes " in script
    assert "leaves the placement to Pacemaker" in script


def test_the_vms_page_reads_its_two_lists_side_by_side(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/vms").text
    css = signed_in.get("/static/style.css").text
    script = signed_in.get("/static/vms.js").text

    # Two columns where the window is wide enough for both: the guest list,
    # and beside it what the cluster runs and nobody declared. Below that
    # width the two stack, in the order they are read.
    assert '<div class="column">' in body
    assert '<div class="column aside">' in body
    assert "@media (min-width: 83rem) {\n  .page.vms {\n    display: flex;" in css

    # The badge beside the node is a sentence about a placement, and a column
    # narrow enough to break it after every second word made three rows of
    # this table four lines tall.
    assert 'cell(where, "placed")' in script
    assert "td.placed {\n  white-space: nowrap;\n}" in css
    assert "#guest-rows td.acts {\n  white-space: nowrap;\n}" in css


def test_adding_a_vm_asks_for_the_three_things_a_guest_is_made_of(
    signed_in: TestClient,
) -> None:
    # The page performs the whole act rather than sending an operator to two
    # other pages and a group name they have no reason to know. See D30.
    body = signed_in.get("/vms").text

    assert 'id="add-name"' in body
    assert 'id="add-disk"' in body
    assert 'id="add-xml"' in body
    assert "Add and deploy" in body


def test_adding_a_vm_says_what_it_will_do_before_it_does_it(
    signed_in: TestClient,
) -> None:
    # Four requests behind one button, two of which move a file that can be
    # very large. The page says so rather than presenting a spinner.
    body = signed_in.get("/vms").text

    assert "the guest is written into the" in body
    assert "the deployment playbook is run" in body


def test_the_vms_page_edits_the_metadata_of_a_guest(signed_in: TestClient) -> None:
    # A guest's Pacemaker configuration lives as metadata on its RBD image,
    # and `vm_manager` writes those keys at creation and never again. See D31.
    body = signed_in.get("/vms").text

    assert 'id="meta"' in body
    assert 'id="edit-key"' in body
    assert 'id="edit-value"' in body
    assert "rbd image-meta" in body


def test_a_metadata_value_is_edited_in_a_window_wide_enough_for_it(
    signed_in: TestClient,
) -> None:
    # `vm_manager` stores the running libvirt domain under `xml` and the
    # template it was built from under `_base_xml`. Editing one of those in a
    # three line box is how a closing tag goes missing, so the editor is its
    # own window and takes the room.
    body = signed_in.get("/vms").text

    assert '<div class="modal-body wide tall">' in body
    assert "<textarea" in body
    assert ".modal-body.tall textarea" in body


def test_applying_a_metadata_change_is_offered_as_the_outage_it_is(
    signed_in: TestClient,
) -> None:
    # Pacemaker reads those keys when it creates the resource, so applying one
    # stops the guest and starts it again. The button says so rather than
    # calling it a refresh.
    body = signed_in.get("/vms").text

    assert "Stop and restart to apply" in body
    assert "created again" in body


def test_the_inventory_editor_answers_the_keys_an_indented_file_needs(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    editing = signed_in.get("/static/yamledit.js").text

    # A textarea moves the caret out of the field on Tab and drops it in
    # column zero on Enter, which is a poor box to write an inventory in.
    assert "yamledit.js" in body
    assert "YamlEdit.attach" in signed_in.get("/static/inventory.js").text
    # Shifting a block is the binding that pays for itself: six lines one
    # level in, rather than a caret on each of them.
    assert 'event.key === "Tab"' in editing
    assert "event.shiftKey" in editing
    assert 'event.key === "Enter"' in editing
    # And the bindings are said out loud, since nothing about a text area
    # suggests they are there.
    assert 'id="editor-keys"' in body
    assert "<kbd>Shift</kbd>+<kbd>Tab</kbd>" in body


def test_the_editor_writes_through_the_browsers_own_undo_stack(
    signed_in: TestClient,
) -> None:
    editing = signed_in.get("/static/yamledit.js").text

    # `value` and `setRangeText` both empty the undo stack, so one Ctrl+Z
    # after an automatic indent would throw away everything typed before it.
    # `execCommand` is deprecated and is the only write that survives it.
    assert 'document.execCommand("insertText", false, text)' in editing
    assert "area.setRangeText" in editing


def test_the_comment_binding_is_reachable_from_an_azerty_keyboard(
    signed_in: TestClient,
) -> None:
    editing = signed_in.get("/static/yamledit.js").text

    # The slash is typed with Shift there, so a binding that required Shift
    # absent would not exist on the keyboards this service is operated from.
    assert 'event.key === "/" && chord && !event.altKey' in editing


@pytest.mark.parametrize(
    ("path", "controls"),
    [
        ("/vms", ['id="reread"']),
        ("/containers", ['id="reread"']),
        (
            "/cluster",
            [
                'id="members-reread"',
                'id="resources-reread"',
                'id="storage-reread"',
            ],
        ),
        ("/realtime", ['id="pool-reread"']),
    ],
)
def test_a_panel_that_ages_can_be_read_again_where_it_is(
    signed_in: TestClient, path: str, controls: list[str]
) -> None:
    """The panels that report what machines are doing right now carry a control.

    Their answer ages while an operator reads it, and the only way to a fresh
    one was reloading the page: every panel refetched, the view bar back
    through its placeholders, the open panel back to its spinner, and the
    scroll position gone. On the cluster page that is three fan outs to every
    machine of the inventory to see one table again.
    """
    body = signed_in.get(path).text

    for control in controls:
        assert control in body
    # A glyph in a ring, and the name of the reading for whoever hovers it or
    # hears it read out. The control carries no text of its own.
    assert body.count('class="reread"') == len(controls)
    assert body.count('aria-label="Read ') == len(controls)


def test_reading_a_panel_again_never_empties_it_first(signed_in: TestClient) -> None:
    """The swap is one pass, and a failed reading changes nothing on screen.

    The point of the control is that an operator keeps looking at an answer
    while the next one is fetched. A panel that blanked itself on the way would
    be the page reload it exists to avoid.
    """
    control = signed_in.get("/static/reread.js").text

    # The render happens inside the loader, which is called once the whole
    # reading is in hand.
    assert "await read();" in control
    # A reading that failed reports itself and leaves the panel alone.
    assert "onFailure(failure);" in control
    # And one reading at a time, so a run of clicks cannot leave two answers
    # racing to draw the same table.
    assert "button.disabled = true;" in control
    assert 'button.setAttribute("aria-busy", "true");' in control


def test_reading_the_cluster_again_asks_the_machines_once(
    signed_in: TestClient,
) -> None:
    """Membership and resources come out of the same exposition.

    Asking every machine of the inventory twice to see two panels of one
    reading is a cost a substation hypervisor should not pay.
    """
    script = signed_in.get("/static/cluster.js").text

    wiring = script.split("function wireReread()")[1].split("async function start")[0]
    assert wiring.count("loadCluster") == 2
    assert wiring.count("loadStorage") == 1


def test_the_cluster_page_tells_a_reading_from_a_pacemaker_refresh(
    signed_in: TestClient,
) -> None:
    """Two things called refresh would be one thing an operator gets wrong.

    `crm resource refresh` reaches a live cluster and clears an operation
    history. Reading the panel again touches nothing, so the two say what they
    are in different words.
    """
    body = signed_in.get("/cluster").text

    assert "Refresh every resource" in body
    assert 'aria-label="Read the resources again"' in body


def test_a_reading_that_came_back_empty_takes_down_the_one_before_it(
    signed_in: TestClient,
) -> None:
    """A panel read again says one thing, not two.

    These branches only ever ran on a first load until a panel could be read a
    second time. A cluster that stopped answering would have left the table of
    the reading before it standing under the sentence saying there was nothing
    to read.
    """
    cluster = signed_in.get("/static/cluster.js").text
    realtime = signed_in.get("/static/realtime.js").text

    assert 'element("resources-body").hidden = true;' in cluster
    assert 'element("storage-body").hidden = true;' in cluster
    assert 'element("pool").hidden = true;' in realtime
    # And the other way round: a cluster that answers again takes down the
    # sentence that said it could not be read.
    assert 'element("members-blocked").hidden = true;' in cluster
    assert 'element("resources-blocked").hidden = true;' in cluster
    assert 'element("storage-blocked").hidden = true;' in cluster


@pytest.mark.parametrize(
    "path", ["/", "/vms", "/containers", "/cluster", "/realtime", "/runs"]
)
def test_the_automatic_reading_is_switched_from_the_top_bar(
    signed_in: TestClient, path: str
) -> None:
    """One switch, on every page, beside the one that picks the palette.

    Both are a preference of this browser and neither reaches a machine, so
    they live together and an operator sets them in one place.
    """
    body = signed_in.get(path).text

    assert 'id="autorefresh"' in body
    assert 'role="switch"' in body
    # Off until it is asked for, and hidden until the page has a panel that
    # carries the manual control. A page whose panels show what this operator
    # just changed has none, and a switch there would act on nothing.
    assert 'aria-checked="false" hidden' in body
    # It says Read, like the control it drives. Refresh on the cluster page is
    # `crm resource refresh`, and it reaches a live cluster.
    assert "Refresh" not in body.split('id="autorefresh"')[1].split("</button>")[0]
    assert 'aria-label="Automatic reading, every 10 seconds"' in body


def test_the_automatic_reading_is_the_manual_one_on_a_timer(
    signed_in: TestClient,
) -> None:
    """The same code, the same panel, the same banner when it fails.

    A second path to the same table would be a second set of bugs, and the one
    that only runs unattended is the one nobody would see fail.
    """
    control = signed_in.get("/static/reread.js").text

    # Ten seconds, and the switch's label says so.
    assert "const PERIOD_MS = 10000;" in control
    # The timer calls what the button calls.
    assert "await control.run();" in control
    assert 'button.addEventListener("click", control.run);' in control


def test_the_automatic_reading_stops_when_nobody_is_looking(
    signed_in: TestClient,
) -> None:
    """A tab left open overnight asks the machines nothing.

    These readings fan out to every machine of the inventory. A browser
    forgotten on the cluster page would otherwise spend a substation's cycles
    until morning on nobody's behalf, which is why the manual control came
    first and the timer waited for this.
    """
    control = signed_in.get("/static/reread.js").text

    # A hidden tab.
    assert "if (!document.hidden) {" in control
    assert 'document.addEventListener("visibilitychange"' in control
    # A panel in a view that is not open. Only one of the cluster page's three
    # cards is on screen, and the other two fan out to every machine to redraw
    # a table nobody is looking at.
    assert "control.button.offsetParent !== null" in control
    # An open dialog. Every one of these pages names the machine it is about to
    # disturb in a modal, and the row it was opened on is in the table under
    # it: that table must not move while the sentence is being read.
    assert 'document.querySelector(".modal:not([hidden])")' in control
    # And a reading still in flight, so a slow fan out cannot stack requests
    # behind itself on a cluster that is already slow to answer.
    assert "!control.running" in control


def test_the_automatic_reading_is_remembered_by_this_browser(
    signed_in: TestClient,
) -> None:
    """The switch survives a navigation, and a browser that refuses storage.

    An operator watching a failover moves between the cluster and the VMs
    pages, and setting the switch again on each of them would be the reason
    they stopped using it.
    """
    control = signed_in.get("/static/reread.js").text

    assert 'const KEY = "seapath-autorefresh";' in control
    assert 'localStorage.getItem(KEY) === "on"' in control
    # Absent means off, so a cleared storage costs the machines nothing.
    assert "localStorage.removeItem(KEY);" in control
    # A private window, or a policy: the switch still works there.
    assert "} catch (error) {\n      return false;\n    }" in control
