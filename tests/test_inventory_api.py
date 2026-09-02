# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The inventory API, and the first boot sequence behind it."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from tests.conftest import SITE_KEY


def test_the_first_boot_provisions_the_trust_and_seeds_the_inventory(
    signed_in: TestClient, host_tree: Path
) -> None:
    # Starting the application is the first boot. Nothing here needed a form.
    authorized = (host_tree / "home/ansible/.ssh/authorized_keys").read_text()
    assert authorized.splitlines()[0] == SITE_KEY
    assert "seapath-webui:seapath-machine->seapath-machine" in authorized

    state = signed_in.get("/api/v1/inventory").json()
    assert state["seeded"] is True
    assert state["commit"]
    assert list(state["inventory"]["hosts"]) == ["seapath-machine"]


def test_the_host_keys_are_read_from_the_machine_not_from_the_network(
    signed_in: TestClient, settings
) -> None:
    known_hosts = settings.known_hosts_file.read_text()

    # No network is involved, so there is nothing to intercept.
    assert "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIhostkey" in known_hosts
    assert "seapath-machine ssh-ed25519" in known_hosts
    assert "127.0.0.1 ssh-ed25519" in known_hosts


def test_the_seed_is_what_discovery_proposed(signed_in: TestClient) -> None:
    discovery = signed_in.get("/api/v1/inventory/discovery").json()
    node = signed_in.get("/api/v1/inventory").json()["inventory"]["hosts"][
        "seapath-machine"
    ]

    assert node["ansible_host"] == discovery["proposed"]["ansible_host"]
    # Discovery proposes and never decides: the PTP interface is a cabling fact
    # the machine cannot observe.
    assert node["ptp_interface"] is None


def test_the_machine_offers_the_inventory_it_would_write_about_itself(
    signed_in: TestClient,
) -> None:
    before = signed_in.get("/api/v1/inventory").json()["commit"]

    document = signed_in.get("/api/v1/inventory/proposed")
    parsed = yaml.safe_load(document.text)

    assert document.status_code == 200
    assert "seapath-machine" in parsed["all"]["hosts"]
    # Standalone, which is what a machine on its own can honestly claim to be.
    assert "seapath-machine" in parsed["standalone_machine"]["hosts"]
    # And nothing was committed: the operator saves it, or does not.
    assert signed_in.get("/api/v1/inventory").json()["commit"] == before


def test_the_raw_inventory_is_the_file_itself(signed_in: TestClient) -> None:
    raw = signed_in.get("/api/v1/inventory/raw").text

    assert yaml.safe_load(raw)["all"]["hosts"]["seapath-machine"]
    assert "managed by seapath-webui" in raw


def test_a_form_submission_becomes_a_commit_with_a_readable_message(
    signed_in: TestClient,
) -> None:
    before = signed_in.get("/api/v1/inventory").json()["commit"]

    response = signed_in.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={
            "changes": {
                "ptp_interface": "eno12419",
                "gateway_addr": "192.168.200.254",
            }
        },
        headers={"If-Match": before},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["commit"] != before
    # `git log` is the audit trail, and an audit trail of "update inventory"
    # forty times over is not one. The message names the forms and the fields
    # that actually changed, so a week later it still means something.
    assert body["message"] == (
        "network, time: set gateway_addr, ptp_interface on seapath-machine"
    )

    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["author"] == "admin"


def test_a_write_from_a_stale_read_is_refused(signed_in: TestClient) -> None:
    stale = signed_in.get("/api/v1/inventory").json()["commit"]
    signed_in.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={"changes": {"ptp_interface": "eno12419"}},
        headers={"If-Match": stale},
    )

    response = signed_in.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={"changes": {"ptp_interface": "eno2"}},
        headers={"If-Match": stale},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_write"


def test_an_invalid_change_never_reaches_the_repository(
    signed_in: TestClient,
) -> None:
    before = signed_in.get("/api/v1/inventory").json()["commit"]

    response = signed_in.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={"changes": {"isolcpus": "0-3"}},
        headers={"If-Match": before},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_inventory"
    # CPU 0 carries work the kernel cannot move.
    assert "CPU 0" in response.json()["error"]["message"]
    assert signed_in.get("/api/v1/inventory").json()["commit"] == before


def test_a_grub_password_is_hashed_before_it_is_written(
    signed_in: TestClient,
) -> None:
    signed_in.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={"changes": {}, "grub_password_plain": "seapath"},
    )

    raw = signed_in.get("/api/v1/inventory/raw").text

    # The inventory goes into git, so a password in clear is a password in the
    # audit trail forever.
    assert "seapath\n" not in raw
    assert "grub_password: grub.pbkdf2.sha512.65536." in raw


def test_a_field_that_is_not_a_machine_variable_is_refused(
    signed_in: TestClient,
) -> None:
    response = signed_in.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={"changes": {"ansible_user": "root"}},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_field"


def test_a_preview_does_not_change_the_answer(signed_in: TestClient) -> None:
    before = signed_in.get("/api/v1/inventory").json()
    candidate = before["inventory"]
    candidate["hosts"]["seapath-machine"]["ptp_interface"] = "eno12419"

    diff = signed_in.post(
        "/api/v1/inventory/preview", json={"inventory": candidate}
    ).text

    assert "+      ptp_interface: eno12419" in diff
    assert signed_in.get("/api/v1/inventory").json()["commit"] == before["commit"]


def test_a_revert_is_a_new_commit_and_applies_nothing(
    signed_in: TestClient,
) -> None:
    signed_in.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={"changes": {"ptp_interface": "eno12419"}},
    )
    unwanted = signed_in.get("/api/v1/inventory").json()["commit"]

    response = signed_in.post(f"/api/v1/inventory/revert/{unwanted}")

    assert response.status_code == 200
    state = signed_in.get("/api/v1/inventory").json()
    assert state["inventory"]["hosts"]["seapath-machine"]["ptp_interface"] is None
    # Rollback is a new commit, never a rewritten history.
    assert len(signed_in.get("/api/v1/inventory/history").json()) == 3


def test_the_export_is_a_repository_a_control_machine_can_clone(
    signed_in: TestClient,
) -> None:
    response = signed_in.get("/api/v1/inventory/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"
    assert "seapath-inventory.tar.gz" in response.headers["content-disposition"]


def test_a_viewer_may_read_the_inventory_and_not_change_it(
    signed_in_viewer: TestClient,
) -> None:
    assert signed_in_viewer.get("/api/v1/inventory").status_code == 200
    assert signed_in_viewer.get("/api/v1/inventory/history").status_code == 200

    response = signed_in_viewer.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={"changes": {"ptp_interface": "eno12419"}},
    )

    assert response.status_code == 403
    assert response.json()["error"]["detail"]["required"] == "admin"


def test_the_inventory_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/v1/inventory").status_code == 401
