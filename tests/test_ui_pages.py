# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The pages, checked for the things a screenshot would not catch."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.ui.routes
from app import __version__
from app.hosts.fake import FakeHostReader
from app.ui.routes import stamp


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

    An unstamped URL is served `no-cache`, and that check compares the copy a
    browser holds against the file the same service has on disk, so a copy kept
    from another version of this service is reported as current. The two then
    disagree about the elements they name, the page script dies on the first
    one that is missing, and the whole page renders and does nothing. That cost
    an afternoon on a node once. The stamp in the URL makes the halves two
    different resources, and that is also what lets the stamped one be held
    without revalidating on every hop.

    The release is in the stamp, and so is the file's own timestamp: a stamped
    URL is served immutable, so two builds of one version would otherwise be one
    URL with different bytes, held for a year.
    """
    body = signed_in.get(path).text
    sources = re.findall(r'<script src="([^"]+)"', body)

    assert sources
    for source in sources:
        name, _, query = source.partition("?")
        assert query.startswith(f"v={__version__}-"), source
        assert query == "v=" + stamp(name.removeprefix("static/")), source


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


def test_the_header_arrives_with_the_document_that_carries_it(
    signed_in: TestClient,
    reader: FakeHostReader,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/chrome.js").text

    # The name, the mode and who is signed in are the same three strings on
    # every page between two runs. Asking for them put two requests in front of
    # every screen and blinked the header through its placeholders on the way
    # back to the values it had a second ago. The service holds all three.
    assert f'id="node-name" class="node-name">{reader.hostname}<' in body
    assert f'class="badge badge-{reader.mode.value}">{reader.mode.value}<' in body
    assert 'id="identity">admin (admin)<' in body
    # The two halves apart as well as rendered, because a page that gates an
    # action on the role compares it.
    assert 'data-username="admin" data-role="admin"' in body
    # And the requests are gone rather than merely covered up.
    assert 'API.get("/auth/me")' not in script
    assert 'API.get("/node")' not in script


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


def test_the_host_key_scan_reaches_the_guests_that_carry_an_address(
    signed_in: TestClient,
) -> None:
    """Every run checks host keys against this node's own known_hosts.

    A convergence never connects to a guest, so a guest key nobody accepted
    holds nothing back. The measurement that runs inside a guest is the one run
    that needs it, and while the scan named only the machines that run could
    not succeed at all: it ended on `Host key verification failed`, counted as
    unreachable.
    """
    body = signed_in.get("/deployment").text
    script = signed_in.get("/static/deployment.js").text

    assert "function guestPeers()" in script
    assert "peers().concat(guestPeers())" in script
    # The address is read from `extra`: `ansible_host` is not a field of a
    # guest entry and this service never writes one.
    assert "(guest.extra || {}).ansible_host" in script
    assert "Host key verification failed" in body


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
    assert "confirmRun(item, false)" in script


def test_a_version_the_inventory_already_names_is_offered_as_an_apply(
    signed_in: TestClient,
) -> None:
    """The gap the registry cannot close.

    The tag is committed and this machine is still answering with the old
    version, which is the state an operator lands in by cancelling the
    confirmation once. It is a run away, and the panel that says so offers it.
    """
    script = signed_in.get("/static/deployment.js").text

    # Rendered from this node's own answer, so the button is there before
    # anything asks a registry, and hidden from a viewer like every other write
    # on this page.
    assert "if (state.update && state.update.pending)" in script
    assert 'go.textContent = "Apply " + state.update.wanted' in script
    assert "go.hidden = !admin" in script
    # And it launches the catalogue entry the inventory itself names, with the
    # confirmation every other convergence gets.
    assert "applyService(\n      state.update.playbook," in script


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


def test_the_machines_are_chosen_before_apply_and_never_inside_it(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    script = signed_in.get("/static/deployment.js").text

    # An operator reads the list on the card, presses Apply and expects that
    # list to be what runs. A selector inside the confirmation contradicted the
    # line they had just read, so the choice is a button of its own beside
    # Apply, in a window, and the card line follows it.
    assert 'id="machines"' in body
    assert 'id="machines-list"' in body
    assert '"Choose machines"' in script
    assert "chooseMachines(item, () => rerender(item))" in script
    assert 'id="confirm-scope"' not in body
    # And the confirmation restates what was chosen rather than asking again.
    assert "scopeSentence(item, selection)" in script
    assert "scope: selection," in script
    assert 'API.get("/playbooks/scopes")' in script


def test_an_entry_blocked_only_by_a_peer_still_offers_the_way_out(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/deployment.js").text

    # Narrowing is what lifts `peer_reachable`, and a card that draws no button
    # while a neighbour is down leaves the way out on the far side of a button
    # it does not draw. So the chooser is offered there, and Apply comes back
    # once the machines chosen are ones this node can reach.
    assert 'item.unmet_codes.join() !== "peer_reachable"' in script
    assert "availableWith(item, selection)" in script
    assert "state.scopes.unreachable" in script


def test_the_machines_are_checked_rather_than_picked_one_at_a_time(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/deployment").text
    script = signed_in.get("/static/deployment.js").text
    css = signed_in.get("/static/style.css").text

    # Several groups and several machines at once, because `--limit` takes a
    # union and an operator converging two hypervisors should not have to
    # launch twice. Nothing checked is the default, which is how the way back
    # to the playbook's own scope stays a checkbox rather than a fourth
    # control.
    assert 'input.type = "checkbox"' in script
    assert "Nothing checked plays what the " in script
    assert 'id="machines-played"' in body
    # A site with forty guests must not push Cancel and Use these under the
    # fold.
    assert "#machines-list {" in css
    assert "overflow-y: auto;" in css


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
def test_a_page_is_styled_from_a_stylesheet_the_browser_already_holds(
    signed_in: TestClient, path: str
) -> None:
    body = signed_in.get(path).text
    css = signed_in.get("/static/style.css").text

    # The whole stylesheet was carried in every document, because a linked one
    # is a round trip between the navigation and the first paint and these
    # assets answered `no-cache`: every hop between the tabs painted the page
    # unstyled while the conditional request was in flight. The stamp is what
    # replaced it, since a URL naming one release is served immutable, so the
    # first page of a release pays the round trip and no other page does. Sixty
    # seven kilobytes leave every document with it.
    head = body.split("</head>")[0]
    assert '<meta name="color-scheme" content="light dark">' in head
    assert (
        f'<link rel="stylesheet" href="static/style.css?v={stamp("style.css")}">'
        in head
    )
    assert ".card.wide" not in head
    assert css not in head
    assert "html {\n  font-size: 80%;\n  background: var(--bg);\n}" in css


@pytest.mark.parametrize("path", ["/", "/inventory", "/deployment", "/runs", "/login"])
def test_the_palette_is_chosen_before_the_page_is_painted(
    signed_in: TestClient, path: str
) -> None:
    head = signed_in.get(path).text.split("</head>")[0]

    # Inline and in the head: an operator whose system is light and who chose
    # dark would see a white page flash by on every navigation, and this UI is
    # navigated all day. A fetched script cannot promise to run before the
    # first paint.
    assert 'src="static/theme.js"' not in head
    assert 'stored("seapath-theme")' in head
    assert "root.dataset.theme = theme" in head
    # The login page is reached before there is a session, and it is styled by
    # the same head, so it is themed too.
    assert "(prefers-color-scheme: light)" in head


def test_the_two_switches_of_the_bar_are_drawn_in_the_first_paint(
    signed_in: TestClient,
) -> None:
    """The palette and the automatic reading, in the paint that draws the bar.

    Both are settings of this browser, so the document cannot render them the
    way it renders the name beside them. It can say what they resolved to
    before anything appears, which is what the head does, and mark the two
    controls from it under the bar. They used to be drawn off and corrected by
    the scripts at the end of the document, which read as the setting turning
    itself on at every hop.
    """
    body = signed_in.get("/cluster").text
    head = body.split("</head>")[0]

    assert 'stored("seapath-autorefresh")' in head
    assert "root.dataset.autorefresh" in head
    # Under the bar, because these are the elements the head cannot reach.
    marking = body.split("</header>")[1].split("</script>")[0]
    assert 'getElementById("autorefresh")' in marking
    assert "aria-checked" in marking
    assert "dataset.themeChoice" in marking
    # And the two scripts that take the controls over say the same thing, so
    # nothing moves when they arrive and the attributes stay true afterwards.
    assert "dataset.themeChoice = choice" in signed_in.get("/static/theme.js").text
    assert "dataset.autorefresh" in signed_in.get("/static/reread.js").text


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
    assert 'stored("seapath-theme")' in body


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
        "runstream.js",
        "runwatch.js",
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
    # And the recap line carries Ansible's numbers rather than the bare word,
    # in the render this page shares with the window that follows a run.
    assert "recapLine" in signed_in.get("/static/runstream.js").text


@pytest.mark.parametrize(
    "url", ["/static/runs.js", "/static/runs.js?v=0.0.1", "/static/runs.js?x=1"]
)
def test_an_asset_this_release_did_not_stamp_is_revalidated(
    signed_in: TestClient, url: str
) -> None:
    # A node upgraded in place serves new HTML and, without this, an old
    # script: the page is then half from each version, and the symptom looks
    # like a bug in the new code. `no-cache` costs one conditional request and
    # answers 304 while the file is unchanged. It covers every URL this release
    # did not stamp itself, including a stamp from another version, which is
    # exactly the copy a browser upgraded under would be holding.
    response = signed_in.get(url)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag")

    unchanged = signed_in.get(url, headers={"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304


def test_an_asset_stamped_with_this_release_is_held_without_asking_again(
    signed_in: TestClient,
) -> None:
    # The URL every page emits, and the only one this promise is made about: it
    # names one release, so it can only ever answer with one release's bytes.
    # Every navigation used to revalidate the stylesheet and all seven scripts,
    # which is seven round trips to paint a page whose assets had not moved.
    response = signed_in.get(f"/static/runs.js?v={stamp('runs.js')}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_a_file_edited_under_a_running_service_is_a_different_url(
    signed_in: TestClient,
) -> None:
    """Two builds of one version must never be one URL with different bytes.

    A stamped URL is served immutable, so the release alone would have a browser
    hold the first copy it saw for a year: an image rebuilt without a version
    change, or a file edited on a node, would serve a page from one build and a
    script from another. That is the failure the stamp exists to prevent.
    """
    before = stamp("runs.js")
    asset = Path(app.ui.routes.__file__).parent / "static" / "runs.js"
    was = asset.stat().st_mtime

    try:
        os.utime(asset, (was + 10, was + 10))
        after = stamp("runs.js")
        assert after != before
        # And this release's own pages name the new one, so a browser holding the
        # old copy is asked for the file again.
        assert f"runs.js?v={after}" in signed_in.get("/runs").text
        # The old stamp is revalidated rather than held, which is what a browser
        # that kept it needs.
        kept = signed_in.get(f"/static/runs.js?v={before}")
        assert kept.headers["cache-control"] == "no-cache"
    finally:
        os.utime(asset, (was, was))


def test_the_node_page_carries_the_terminal_and_says_what_it_is(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/").text

    # The emulator and its stylesheet are served from this node, because a
    # substation hypervisor has no route to a CDN. Both are stamped with the
    # version that serves them, like the rest of the assets of this service, so
    # a browser fetches them once per release.
    assert f'src="static/vendor/xterm.js?v={stamp("vendor/xterm.js")}"' in body
    assert f'href="static/vendor/xterm.css?v={stamp("vendor/xterm.css")}"' in body
    assert "static/console.js" in body
    assert ".xterm {" in signed_in.get("/static/vendor/xterm.css").text
    assert signed_in.get("/static/vendor/xterm.js").status_code == 200

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


def test_the_guest_latency_panel_says_what_a_guest_needs_to_be_measurable(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/realtime").text

    # One guest at a time, chosen before the run: the playbook plays the whole
    # group, and measuring every guest at once measures the contention between
    # the measurements.
    assert 'id="panel-guest_cyclictest"' in body
    assert 'id="guest-choice"' in body
    # The vCPUs are chosen from a list, like the machine form's CPUs: `smp` is
    # the role's word for "every online one" and says nothing in a field asking
    # for vCPUs, so it is a label with that value behind it.
    assert 'id="guest-affinity-choice"' in body
    # And the key this node connects with, offered to be pasted into the guest.
    # Installing it would be this service writing inside a VM, which it never
    # does, so the panel carries the line and the button that copies it.
    assert 'id="guest-key-line"' in body
    assert 'id="guest-key-copy"' in body


def test_the_real_time_view_bar_carries_each_panel_s_answer(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/realtime").text

    # The bar is the summary before it is a navigation. Only one panel is on
    # screen, so each tab carries its own status dot and the line the panel
    # would lead with, and the page still answers at a glance without an
    # operator opening the three panels that are hidden.
    bar = body.split('<nav class="views"')[1].split("</nav>")[0]
    for view in [
        "checks",
        "pool",
        "cyclictest",
        "guest_cyclictest",
        "hwlatdetect",
    ]:
        assert f'data-view="{view}"' in bar
    assert bar.count('class="view-answer"') == 5
    assert bar.count('<span class="dot ') == 5


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
        "runstream.js",
        "runwatch.js",
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

    # A disabled guest is there as well: Ceph holds it, `cluster_vm status`
    # answers Disabled, and the role skips it.
    assert (
        "const there = Boolean(guest.resource || guest.domain || guest.disabled);"
        in script
    )
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


def test_adding_a_vm_asks_for_the_network_it_will_come_up_on(
    signed_in: TestClient,
) -> None:
    # The section that takes the address out of the qcow2: one image, one entry
    # per guest. It writes an interface, a cloud-init seed and the address a
    # later run reaches the guest at, and the help text says so.
    body = signed_in.get("/vms").text

    for field in ("add-bridge", "add-mac", "add-address", "add-gateway", "add-dns"):
        assert f'id="{field}"' in body
    assert 'id="add-dhcp"' in body
    assert 'id="add-hostname"' in body
    # The trust into the guest, offered checked, and saying what it leaves to
    # the image: the account and its sudo rights, and the host key to accept.
    assert 'id="add-trust" checked' in body
    assert "sudo rights" in body
    assert "host key" in body
    # The two things an operator cannot guess: what the MAC is for, and that
    # cloud-init is read once, when the guest is created.
    assert "ansible_host" in body
    assert "read once, when the guest is created" in body


def test_adding_a_vm_can_reuse_the_image_and_the_xml_this_node_holds(
    signed_in: TestClient,
) -> None:
    # One image and one template for every guest of a site, which is what a
    # seeded image makes possible. The lists are filled from the folder when
    # the form opens, so the page carries the selects and the script reads it.
    body = signed_in.get("/vms").text
    script = signed_in.get("/static/vms.js").text

    assert 'id="add-disk-source"' in body
    assert 'id="add-xml-source"' in body
    assert "Upload an image" in body
    assert 'API.get("/inventory/folder")' in script
    # The address the entry gives each guest, as a column of the guest table.
    assert "<th>Address</th>" in body
    assert "function addressCell(guest)" in script


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
    assert ".modal-body.tall textarea" in signed_in.get("/static/style.css").text


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


@pytest.mark.parametrize(
    ("path", "script"),
    [
        ("/", "node.js"),
        ("/cluster", "cluster.js"),
        ("/vms", "vms.js"),
        ("/containers", "containers.js"),
        ("/realtime", "realtime.js"),
        ("/deployment", "deployment.js"),
        ("/inventory", "inventory.js"),
    ],
)
def test_a_page_paints_what_this_browser_last_read_before_it_asks(
    signed_in: TestClient, path: str, script: str
) -> None:
    """The wait that made this UI unpleasant to move around in.

    A navigation is one round trip for the document and another for the answers
    its panels are drawn from, and on a machine reached through an ssh tunnel an
    operator paid both on every hop, for panels they were reading a moment ago.
    The page draws what this browser last read first, and the reading redraws it.
    See D46.
    """
    body = signed_in.get(path).text
    page = signed_in.get(f"/static/{script}").text

    assert "kept.js" in body
    # The request leaves before anything is painted, so the reading is in flight
    # while the kept answer is drawn.
    assert "API.started(" in page
    assert "Kept.paint(" in page or "Kept.held(" in page
    assert "Kept.keep(" in page


@pytest.mark.parametrize(
    ("path", "script"),
    [
        ("/cluster", "cluster.js"),
        ("/vms", "vms.js"),
        ("/containers", "containers.js"),
        ("/realtime", "realtime.js"),
        ("/deployment", "deployment.js"),
        ("/inventory", "inventory.js"),
    ],
)
def test_a_panel_showing_a_kept_answer_says_so_and_cannot_be_acted_on(
    signed_in: TestClient, path: str, script: str
) -> None:
    """The two rules that make it honest.

    What is on screen is never presented as current, and nothing can be launched
    from it: a resource may have moved, and an operator must not migrate a guest
    off a node that has already failed over. The controls come back when the
    reading lands, and also when it fails, because a panel nobody can act on is
    worse than an old one.
    """
    page = signed_in.get(f"/static/{script}").text
    kept = signed_in.get("/static/kept.js").text

    assert "Kept.rereading(" in page
    assert "Kept.hold(" in page
    assert "Kept.release()" in page
    # The sentence itself, and the age in it.
    assert "What this browser last read, " in kept
    assert "Reading again." in kept
    # The reread control is the one thing left alive: asking for the reading is
    # what an operator may do to a panel in this state.
    assert 'control.classList.contains("reread")' in kept


def test_the_node_page_reads_again_when_an_operator_asks(
    signed_in: TestClient,
) -> None:
    """It polled itself every five seconds, whatever anybody was doing.

    Four requests to the node, forever, on nobody's behalf, over a link an
    operator reaches it through. D37 settled that an automatic reading is the
    switch in the bar and a control on the panel; this page predates it.
    """
    body = signed_in.get("/").text
    page = signed_in.get("/static/node.js").text

    assert 'aria-label="Read this machine again"' in body
    assert "Reread.attach(" in page
    assert "setInterval" not in page
    assert "REFRESH_MS" not in page


def test_the_node_page_says_the_age_of_what_it_shows_and_holds_nothing(
    signed_in: TestClient,
) -> None:
    """Four readings of this machine, and no act aimed at any of them.

    The one control on that page opens a shell on this node, which does not
    depend on a row of a reading, so there is nothing to hold. The line that says
    what is on screen and how old it is still has to be there.
    """
    body = signed_in.get("/").text
    page = signed_in.get("/static/node.js").text

    assert '<p class="loading" id="reading" hidden></p>' in body
    assert 'Kept.rereading(["reading"]' in page
    assert "Kept.hold(" not in page


def test_the_inventory_editor_is_never_drawn_from_a_kept_copy(
    signed_in: TestClient,
) -> None:
    """The one place where stale would mean lost work.

    The tree, the references and the history are drawn from what this browser
    last read. The text in the editor is fetched, every time, because an operator
    is about to commit it and a kept copy is how somebody saves over a change
    they never saw. The commit the page saves against comes from the reading, so
    a save made while the kept folder is on screen is refused by the service
    rather than allowed through, and the writes are held until then anyway.
    """
    page = signed_in.get("/static/inventory.js").text

    kept = page.split("function paintKept()")[1].split("async function")[0]
    assert "folder" in kept
    assert "history" in kept
    assert "inventory/raw" not in kept
    assert 'Kept.hold(["add-file", "new-file", "save", "commit"])' in page
    # The controls named there are the buttons themselves rather than a card
    # around rows, and asking for the descendants of a button finds nothing: the
    # first version of this left every write on that page live.
    kept_js = signed_in.get("/static/kept.js").text
    assert "panel.matches(CONTROLS)" in kept_js
    # The replicas panel too: it is the answer of the other machines, and a kept
    # one would claim they hold a commit nobody asked them about.
    assert "replicas" not in kept


def test_what_a_browser_kept_is_scoped_to_this_node_and_this_release(
    signed_in: TestClient,
) -> None:
    """Two traps, both of which have already been paid for once in this UI.

    A browser reaching two nodes through two ssh tunnels sees one origin, so a
    single key would draw one cluster's table over the other's. And a payload
    kept by one release and drawn by another is a render dying on a field that
    moved, which leaves a page that renders and then does nothing.
    """
    body = signed_in.get("/cluster").text
    kept = signed_in.get("/static/kept.js").text

    assert f'<meta name="version" content="{__version__}">' in body
    assert 'meta[name="csrf-cookie"]' in kept
    assert 'meta[name="version"]' in kept
    assert "held.release !== RELEASE" in kept
    # This tab, so a page opened in a new one is a first load, and closing the
    # tab forgets everything.
    assert "sessionStorage" in kept
    assert "localStorage" not in kept
    # And signing out forgets it, because the next person to sign in on this
    # browser must not be shown what the last one was reading.
    assert "sessionStorage.clear()" in signed_in.get("/static/chrome.js").text


def test_a_page_asks_for_everything_it_needs_at_once(
    signed_in: TestClient,
) -> None:
    """A round trip per stage is what an ssh tunnel charges for.

    The Real time page waited for the inventory, then for the guests, then for
    the rest; the Deployment page waited for the inventory, then for five
    answers, then for the catalogue. None of those answers depends on another,
    and the order that does matter is the order of the renders, which is kept.
    """
    for script, expected in (
        ("realtime.js", 6),
        ("deployment.js", 7),
        ("inventory.js", 4),
    ):
        page = signed_in.get(f"/static/{script}").text
        assert page.count("API.started(") >= expected, script

    # The inventory page leaves one answer out of the wait: the replicas panel
    # asks every other machine whether it holds this commit, which is an ssh to
    # each of them, and it stood in front of the folder and the editor.
    inventory = signed_in.get("/static/inventory.js").text
    order = inventory.split("async function refresh()")[1].split("async function")[0]
    assert order.index("loadReplicas(pending.replicas)") < order.index("await draw(")
    assert "await loadReplicas" not in inventory
    # And the file it opens by itself is fetched beside the folder rather than
    # after it, which was a second round trip before the editor held anything.
    opening = inventory.split("async function start()")[1]
    assert opening.index('fetch("api/v1/inventory/raw"') < opening.index(
        "await refresh()"
    )


def test_the_top_bar_costs_a_page_nothing_before_its_own_reading(
    signed_in: TestClient,
) -> None:
    """Every page opened with two requests for three strings it was given.

    The document carries the bar, so the pages read the role from it rather than
    awaiting an answer that had already arrived.
    """
    chrome = signed_in.get("/static/chrome.js").text

    # The function is gone, and only the comment saying what it cost remains.
    assert "function load(" not in chrome
    assert "API.get(" not in chrome
    for script in ("cluster.js", "vms.js", "containers.js", "runs.js"):
        assert "Chrome.load()" not in signed_in.get(f"/static/{script}").text, script


def test_opening_a_run_does_not_read_the_whole_history_again(
    signed_in: TestClient,
) -> None:
    """Three readings of fifty runs to draw one page.

    The page read the list, opened the newest run and read the list again to
    move the highlight, and the run's own stream then ended, which read the
    record and the list a third time. On a node reached through an ssh tunnel
    that was four seconds and nearly two megabytes on every click. See D47.
    """
    page = signed_in.get("/static/runs.js").text

    opening = page.split("async function show(")[1].split("function confirmRelaunch")[0]
    # Opening a run marks its row from the list already on screen.
    assert "markCurrent()" in opening
    assert "loadList()" not in opening.split("onEnd")[0]
    # The list is read again when a run ends under the page, because the badge of
    # its row has just changed, and only then: a run that was already finished
    # replays its events and ends the moment it is opened.
    assert "if (!wasFinished)" in opening
    # And the run this page opens on arrival is asked for beside the list.
    start = page.split("async function start()")[1]
    assert start.index('API.started("/runs?limit=50")') < start.index("await loadList(")
    assert 'API.started("/runs/"' in start


def test_the_run_list_answers_what_a_list_needs(signed_in: TestClient) -> None:
    """A record carries a duration per task, and a list carried fifty of them.

    On a commissioned node that is six hundred kilobytes for a page that draws
    five fields per row. What a run did stays on the run's own record.
    """
    schema = signed_in.get("/api/v1/openapi.json").json()
    listed = schema["paths"]["/api/v1/runs"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    summary = schema["components"]["schemas"]["RunSummary"]["properties"]

    assert listed["items"]["$ref"].endswith("RunSummary")
    # What situates a run: what it was, when, who launched it, how it ended.
    for field in ("id", "playbook_id", "state", "started_at", "launched_by"):
        assert field in summary
    # What it did, which belongs to the run's own record.
    for field in ("progress", "command", "variables", "files", "machines"):
        assert field not in summary


def test_the_tab_carries_a_mark_this_service_ships(signed_in: TestClient) -> None:
    """A browser asks for /favicon.ico on every page of an origin that has none.

    An operator keeps one tab per node open through several ssh tunnels, so that
    is a round trip and a 404 per visit, for a blank square.
    """
    body = signed_in.get("/cluster").text
    icon = signed_in.get(f"/static/favicon.svg?v={stamp('favicon.svg')}")

    assert f'href="static/favicon.svg?v={stamp("favicon.svg")}"' in body
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert icon.headers["cache-control"] == "public, max-age=31536000, immutable"
    # Drawn for both palettes, because a favicon sits in the tab strip.
    assert "prefers-color-scheme: dark" in icon.text


def test_a_request_that_never_reached_the_node_says_which_link_is_down(
    signed_in: TestClient,
) -> None:
    """ "Failed to fetch" is the browser's word, and it reads as a cluster fault.

    The node is usually reached through an ssh tunnel, and a tunnel that went
    down is the ordinary cause of it. The panel under the banner is still showing
    what this browser last read, which the sentence leaves standing.
    """
    api = signed_in.get("/static/api.js").text

    assert "This node did not answer" in api
    assert "ssh" in api
    # Both ways out of this file, because an upload fails the same way.
    assert api.count("throw unreachable(error)") == 2


def test_reading_a_panel_again_never_empties_it_first(signed_in: TestClient) -> None:
    """The swap is one pass, and a failed reading changes nothing on screen.

    The point of the control is that an operator keeps looking at an answer
    while the next one is fetched. A panel that blanked itself on the way would
    be the page reload it exists to avoid.
    """
    control = signed_in.get("/static/reread.js").text

    # The render happens inside the loader, which is called once the whole
    # reading is in hand, and is told to reach the machines rather than take
    # what the service kept from the page's own load.
    assert "await read(true);" in control
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
    assert wiring.count('attach("members-reread", loadCluster)') == 1
    assert wiring.count('attach("resources-reread", loadCluster)') == 1
    assert wiring.count('attach("storage-reread", loadStorage)') == 1


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
    # Off until it is asked for, and in the bar on every page: it is one
    # setting for this browser, and a control that comes and goes as an
    # operator moves between pages is one they stop reaching for. The pages
    # with no panel to read again arm no timer, which `reread.js` decides.
    assert 'aria-checked="false"' in body
    assert "hidden" not in body.split('id="autorefresh"')[1].split(">")[0]
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


def test_a_page_with_no_panel_to_read_again_arms_no_timer(
    signed_in: TestClient,
) -> None:
    """The switch stands on every page. The timer does not.

    A page whose panels show what this operator has just changed has no control
    to register, and a timer there would wake every ten seconds to walk an
    empty list.
    """
    control = signed_in.get("/static/reread.js").text

    assert "if (!controls.length) {" in control


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


def test_the_assistant_is_a_switch_and_off_means_nothing_is_asked(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/inventory.js").text

    # A site with variables of its own gets a remark about every one of them,
    # so the assistance is something an operator turns on rather than something
    # the page always does.
    assert 'id="assistant"' in body
    assert 'role="switch"' in body
    assert "seapath-assistant" in script
    # Off is not a filter over an answer that arrived anyway: the switch is
    # tested before the call is made.
    assert "!assistantOn()" in script
    assert '"/inventory/raw/assist"' in script


def test_the_assistant_remarks_are_kept_apart_from_the_findings(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/inventory.js").text

    # None of these refuses a commit. Mixed into the findings list they would
    # read as though one of them might.
    assert 'id="editor-remarks"' in body
    assert "Nothing here refuses a commit" in script


def test_the_assistant_reads_while_the_file_is_typed_but_not_per_keystroke(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/inventory.js").text

    # The reading carries the whole document, so a held down key would send one
    # round trip per character.
    assert "ASSISTANT_DELAY_MS" in script
    assert "window.clearTimeout(assistantTimer)" in script


def test_the_editor_completes_a_variable_name_where_one_goes(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/complete.js").text

    # The vocabulary was readable through the API and not while typing the
    # file, which is the one moment it is worth anything.
    assert "complete.js" in body
    assert "Complete.attach" in signed_in.get("/static/inventory.js").text
    # Offered where a key goes and nowhere else: a name sits after the
    # indentation and an optional dash, and anything past the colon is a value.
    assert "/^([ \\t]*)(-[ \\t]+)?([A-Za-z_][A-Za-z0-9_]*)?$/" in script
    # And it says what the variable is, since a list of names an operator could
    # have guessed is a list they stop opening.
    assert "completion-role" in script
    assert "term.caution || term.summary" in script


def test_the_completion_ranks_the_reviewed_entries_first_and_marks_the_rest(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/complete.js").text

    # The list carries every variable the installed collection declares behind
    # the ones a human wrote prose for, and eight rows is what an operator
    # sees. A tie on the prefix goes to the reviewed entry, or those rows fill
    # with role plumbing named after the same prefix.
    assert "if (reviewed(left) !== reviewed(right))" in script
    assert "return term.reviewed !== false;" in script
    # And the row says which half it came from, in the words the deployment
    # page uses for a playbook read off the collection.
    assert '"not reviewed"' in script
    assert "completion-derived" in script


def test_the_completion_offers_what_may_be_written_where_the_caret_is(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/complete.js").text

    # The same boundary the assistant reports after the fact, applied before
    # the mistake: a guest entry is offered vm_disk and a hypervisor is not.
    assert '"VMs", "cluster_VMs", "standalone_VMs"' in script
    assert 'return guests ? "guest" : "host";' in script
    # A connection variable is written wherever a host is, machine or guest.
    assert 'term.scope === "connection"' in script


def test_the_completion_opens_under_the_line_it_completes(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/complete.js").text

    # The mirror is laid over the textarea. Against the card it starts a header
    # and a note too high, and the list drew itself over the line being typed.
    assert 'mirror.style.top = area.offsetTop + "px";' in script
    assert "area.offsetTop + (over ? y - height : y + line)" in script
    # Over the line only when the window leaves no room under it, and the list
    # has to be drawn before it can be measured.
    assert "const over = under < height" in script
    assert script.index("draw();\n      place();") > 0


def test_the_completion_owns_its_keys_while_it_is_open(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text
    script = signed_in.get("/static/complete.js").text

    # An open list is a mode. Tab in that mode takes the highlighted name
    # rather than indenting, which means this listener has to be registered
    # before the one `yamledit.js` attaches.
    assert "stopImmediatePropagation" in script
    assert body.index("complete.js") < body.index("inventory.js")
    inventory = signed_in.get("/static/inventory.js").text
    assert inventory.index("Complete.attach") < inventory.index("YamlEdit.attach")


def test_the_completion_writes_through_the_browsers_own_undo_stack(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/complete.js").text

    # Same reason as `yamledit.js`: one Ctrl+Z after an accepted name would
    # otherwise throw away everything typed before it.
    assert 'document.execCommand("insertText", false, written)' in script
    assert 'term.name + ": "' in script


def test_the_completion_is_the_assistant_switch_too(signed_in: TestClient) -> None:
    script = signed_in.get("/static/inventory.js").text

    # One switch covers the whole assistant. Off means the vocabulary is not
    # even fetched, rather than fetched and unused.
    assert "vocabulary.length || !assistantOn()" in script
    assert "completion.close()" in script


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
def test_every_page_carries_the_window_that_follows_a_run(
    signed_in: TestClient, path: str
) -> None:
    """A run is watched where it was launched, and not on the way to the Runs page.

    Almost every act of this service is a playbook, so launching one was a
    navigation, and reading the result of one's own action meant finding the
    way back to the page, the view and the card it was launched from.
    """
    body = signed_in.get(path).text

    assert 'id="run-watch"' in body
    assert "runstream.js" in body
    assert "runwatch.js" in body
    # And it is the last window in the document, which is what makes it the one
    # drawn over the confirmation it replaces and the one Escape dismisses.
    assert body.rindex('class="modal"') == body.index('id="run-watch"') - len(
        'class="modal" '
    )


@pytest.mark.parametrize(
    "asset",
    ["cluster.js", "containers.js", "vms.js", "deployment.js", "realtime.js"],
)
def test_no_action_navigates_away_to_show_the_run_it_launched(
    signed_in: TestClient, asset: str
) -> None:
    script = signed_in.get(f"/static/{asset}").text

    assert 'location.assign("runs' not in script
    assert "RunWatch.open(started.run_id" in script


def test_the_window_and_the_runs_page_draw_the_same_log(
    signed_in: TestClient,
) -> None:
    """One render for one event stream.

    Two would be two sets of bugs, and the one an operator sees only for the
    length of a convergence is the one nobody would notice going wrong.
    """
    stream = signed_in.get("/static/runstream.js").text
    page = signed_in.get("/static/runs.js").text
    window = signed_in.get("/static/runwatch.js").text

    assert "recapLine" in stream
    assert "RunStream.append" in page
    assert "RunStream.append" in window
    assert "RunStream.follow" in page
    assert "RunStream.follow" in window


def test_the_window_says_what_closing_it_does_and_does_not_do(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/vms").text
    script = signed_in.get("/static/runwatch.js").text
    prose = " ".join(body.split())

    # The window is ignorable, and that is the whole point of it: the run
    # belongs to the service rather than to this browser.
    assert "Closing this window leaves the run going" in prose
    assert 'id="run-watch-open"' in body
    # Closing stops the stream through the control the window names, so the
    # connection is not left open behind a hidden element.
    assert 'data-dismiss="run-watch-close"' in body
    assert 'element("run-watch-close").addEventListener("click", close)' in script


def test_a_run_that_ends_under_the_window_leaves_the_page_up_to_date(
    signed_in: TestClient,
) -> None:
    """What the navigation used to do on the way back.

    The operator who stays to the end stays to see what the run did, and the
    panels underneath it were drawn before it ran.
    """
    window = signed_in.get("/static/runwatch.js").text
    reread = signed_in.get("/static/reread.js").text

    assert "await Reread.readAgain()" in window
    assert "async function readAgain()" in reread
    assert "return { attach, readAgain };" in reread
    # And only for the operator who is still watching. One who closed the
    # window said they were not, and a fan out to every machine of the
    # inventory on nobody's behalf is what D37 is careful about.
    assert 'const watching = !element("run-watch").hidden' in window


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
def test_every_window_names_the_control_that_shuts_it(
    signed_in: TestClient, path: str
) -> None:
    """Escape and a click beside the window both press that control.

    Neither hides the element, so the page's own teardown runs: the console
    closes its socket, a confirmation clears the machine it was about to name,
    the run window stops following its stream. A window without the attribute
    is one those two gestures leave open.
    """
    body = signed_in.get(path).text

    for window in re.findall(r"<div class=\"modal\"[^>]*>", body):
        assert "data-dismiss=" in window, window


def test_a_click_beside_a_window_shuts_it(signed_in: TestClient) -> None:
    script = signed_in.get("/static/chrome.js").text

    # The scrim is the element a click outside the window lands on, and the
    # same control Escape presses is pressed here.
    assert 'scrim.classList.contains("modal")' in script
    assert "dismiss(scrim)" in script
    # Both ends of the click, because a selection that starts on a value inside
    # the window and ends past its edge is released on the scrim.
    assert "scrim !== pressed" in script
