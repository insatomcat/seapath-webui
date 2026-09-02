# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The pages, checked for the things a screenshot would not catch."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("path", ["/", "/inventory", "/system", "/runs"])
def test_every_page_needs_a_session(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize(
    ("path", "script"),
    [
        ("/", "node.js"),
        ("/inventory", "inventory.js"),
        ("/system", "system.js"),
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
    assert "that is the System page" in body


def test_the_inventory_page_is_an_editor_over_the_folder(
    signed_in: TestClient,
) -> None:
    inventory = signed_in.get("/inventory").text
    system = signed_in.get("/system").text

    # The desired state of these machines is a folder, so the page is the
    # shape of one: the files on the left, the open file on the right.
    assert 'id="tree"' in inventory
    assert 'id="editor"' in inventory
    assert 'class="page split"' in inventory
    # And editing it happens here and nowhere else.
    assert 'id="editor"' not in system
    assert "inventory.js" not in system


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
    assert "/api/v1/inventory/proposed" in script
    assert "nothing is committed until you do" in script


def test_the_inventory_file_is_saved_against_the_commit_it_was_read_at(
    signed_in: TestClient,
) -> None:
    script = signed_in.get("/static/inventory.js").text

    # Two operators editing the same file from two browsers is the ordinary
    # case, and refusing the second save beats merging it silently.
    assert '"If-Match"' in script


def test_the_system_page_carries_the_credentials_and_the_button(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/system").text

    assert "Reaching the other machines" in body
    assert 'id="site-key-file"' in body
    assert 'id="host-keys-scan"' in body
    assert 'id="main-playbook"' in body
    # And edits nothing: the desired state has one page and it is the other one.
    assert 'id="tree"' not in body


def test_the_commissioning_playbook_is_the_page_and_the_rest_is_a_list(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/system").text
    script = signed_in.get("/static/system.js").text

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
    script = signed_in.get("/static/system.js").text

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


def test_the_ssh_credentials_are_a_state_line_once_they_hold(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/system").text
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
    assert response.headers["location"] == "/inventory"


def test_the_apply_confirmation_says_what_it_will_disturb(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/system").text

    # The single most dangerous button in the product asks once, in a modal
    # naming the disruption and the machines. Typing the host name was here
    # too, and an operator who applies twenty times a day types it twenty times
    # without reading the sentence above it.
    assert 'id="confirm"' in body
    assert 'id="confirm-disruption"' in body
    assert 'id="confirm-reboot"' in body
    assert "confirm-input" not in body


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
    body = signed_in.get("/system").text
    css = signed_in.get("/static/style.css").text

    # Everything in this UI is shown and dismissed with the `hidden` attribute,
    # and a `display` rule in the stylesheet beats the attribute. The
    # confirmation modal is `display: grid`, so without this rule it is on
    # screen from the moment the page loads, over a page nobody asked to leave,
    # and Cancel does not dismiss it.
    assert '<div class="modal" id="confirm" hidden>' in body
    assert "[hidden]" in css
    assert "display: none !important" in css


def test_a_table_scrolls_inside_its_card_rather_than_over_the_next_one(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/").text
    css = signed_in.get("/static/style.css").text

    # A `by-path` name is longer than half a page, and the cards sit in
    # flexible grid tracks, so nothing widens a card to fit its content. The
    # tables of machine values take the whole width, and scroll inside their
    # card when even that is not enough.
    assert body.count('class="card wide"') == 2
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
        "system.js",
        "runs.js",
        "style.css",
    ):
        assert signed_in.get(f"/static/{asset}").status_code == 200


def test_the_system_page_says_once_why_nothing_can_run(
    signed_in: TestClient, settings, tmp_path
) -> None:
    # A node running from source, or from an image built without the
    # collection, has every entry unavailable for the same reason. Nine dimmed
    # rows each repeating it in small print is how an operator ends up asking
    # why the buttons are greyed out.
    body = signed_in.get("/system").text

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
    # substation hypervisor has no route to a CDN.
    assert "/static/vendor/xterm.js" in body
    assert "/static/vendor/xterm.css" in body
    assert "/static/console.js" in body
    assert signed_in.get("/static/vendor/xterm.js").status_code == 200

    # A shell is the one place in this UI where what an operator does is
    # neither recorded nor part of the desired state, and the panel says so
    # every time it opens.
    assert "passwordless <code>sudo</code>" in body
    assert "undone by the next run that touches it" in body
