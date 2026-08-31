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


def test_the_two_ways_of_editing_are_on_the_inventory_page_and_only_there(
    signed_in: TestClient,
) -> None:
    inventory = signed_in.get("/inventory").text
    system = signed_in.get("/system").text

    # The guided form and the file, side by side, because a form that models a
    # dozen variables cannot be the only way to change a file holding fifty.
    assert "One machine, guided" in inventory
    assert 'id="raw-editor"' in inventory
    assert 'id="import-file"' in inventory
    assert "raw-editor" not in system
    assert "node-form" not in system


def test_the_system_page_carries_the_credentials_and_the_button(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/system").text

    assert "Reaching the other machines" in body
    assert 'id="site-key-file"' in body
    assert 'id="host-keys-scan"' in body
    assert 'id="playbooks"' in body
    # And edits nothing: the desired state has one page and it is the other one.
    assert 'id="import-file"' not in body


def test_the_old_configuration_url_still_leads_somewhere(
    signed_in: TestClient,
) -> None:
    response = signed_in.get("/setup", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "/inventory"


def test_the_real_time_fields_are_behind_a_collapsed_expert_section(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/inventory").text

    # The rule is that the UI never makes a real time relevant change look
    # routine.
    assert '<details class="expert">' in body
    assert "isolcpus" in body
    assert "Latency is the product" in body


def test_the_apply_confirmation_makes_the_machine_be_typed_out(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/system").text

    # This is the single most dangerous button in the product, and it has to
    # look like it.
    assert "confirm-input" in body
    assert "to confirm" in body


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
