# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The pages, checked for the things a screenshot would not catch."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "path", ["/", "/inventory", "/deployment", "/realtime", "/runs"]
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


def test_the_inventory_page_says_what_saving_does_and_does_not_do(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text

    # The two acts are separate and now live on separate pages, which is the
    # whole reason for the split: deciding what a machine should be, and making
    # it so.
    assert "Every change is a commit" in body
    assert "that is the Deployment page" in body


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


def test_the_history_is_beside_the_editor_and_bounded(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    css = signed_in.get("/static/style.css").text

    # The history asks for twenty commits, and twenty rows is most of a screen
    # on a repository that has seen some use. It scrolls in a window of about
    # ten, under the folder rather than across the page, so the page stays the
    # height of the file being edited.
    assert '<section class="card" id="history-card">' in body
    assert "#history-card .table-scroll" in css
    assert "#editor-card {\n  grid-row: span 2;" in css


def test_the_ssh_credentials_are_a_state_line_once_they_hold(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    css = signed_in.get("/static/style.css").text

    # Set once, then read. A key upload above the playbook an operator came for
    # is a form they scroll past every time and fill in never.
    assert 'id="reach-details"' in body
    assert 'id="reach-state"' in body
    assert ".reach-state.warn" in css
    # Last on the page, and it takes the width it needs when opened.
    assert body.index('id="main-playbook"') < body.index('id="reach-details"')
    assert body.index('id="playbook-choice"') < body.index('id="reach-details"')


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
    assert '<div class="modal" id="confirm" hidden>' in body
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
    assert "html {\n  background: var(--bg);\n}" in css


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


def test_the_real_time_page_shows_one_panel_at_a_time(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/realtime").text

    # An application layout rather than a document, and one panel of it on
    # screen. Three panels sharing one screen each got a third of the room
    # their content needs, and every one of them answered by truncating: the
    # conformance values, the cluster's fourth node, the histogram's axis.
    assert 'class="page realtime"' in body
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
    "path", ["/", "/inventory", "/deployment", "/realtime", "/runs", "/login"]
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
