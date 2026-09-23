# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The read only node API."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.conftest import sign_in

_ENDPOINTS = [
    "/api/v1/node",
    "/api/v1/node/cpu",
    "/api/v1/node/network",
    "/api/v1/node/disks",
]


@pytest.mark.parametrize("path", _ENDPOINTS)
def test_every_reading_needs_a_session(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_the_summary_reports_the_machine_and_the_collection(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/api/v1/node").json()

    assert body["hostname"] == "seapath-machine"
    assert body["mode"] == "standalone"
    assert body["kernel_release"] == "6.1.0-18-rt-amd64"
    assert body["collection_version"] == "test"
    # No inventory before M1, and the field says so rather than disappearing.
    assert body["inventory_commit"] is None


def test_the_cpu_view_separates_isolated_from_housekeeping(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/api/v1/node/cpu").json()

    assert body["isolated"] == [4, 5, 6, 7]
    assert body["housekeeping"] == [0, 1, 2, 3]
    assert len(body["topology"]) == 8
    assert all(entry["isolated"] for entry in body["topology"][4:])


def test_the_disk_view_carries_the_stable_path_ceph_needs(
    signed_in: TestClient,
) -> None:
    body = signed_in.get("/api/v1/node/disks").json()
    free = [device for device in body["devices"] if not device["claimed"]]

    assert len(free) == 1
    assert free[0]["by_path"].startswith("/dev/disk/by-path/")


def test_live_state_is_not_served_from_here(signed_in: TestClient) -> None:
    # Unit states, the journal and the clock offset were read from this
    # container once, and the mounts that took are the reason this test exists:
    # bringing any of them back is a design decision, not a convenience.
    for path in ("/api/v1/node/services", "/api/v1/node/logs", "/api/v1/node/time"):
        assert signed_in.get(path).status_code == 404


def test_the_openapi_schema_is_where_the_api_document_says(
    client: TestClient,
) -> None:
    schema = client.get("/api/v1/openapi.json")

    assert schema.status_code == 200
    assert "/api/v1/node" in schema.json()["paths"]


def test_the_docs_page_asks_for_the_specification_relatively(
    client: TestClient,
) -> None:
    """A reverse proxy may serve this service under a prefix.

    FastAPI's own docs route writes `openapi_url` into the page as a path from
    the root, so behind a prefix the page asked for `/api/v1/openapi.json`
    while the specification was at `/<prefix>/api/v1/openapi.json`, and Swagger
    UI rendered "Failed to load API definition" over a 404. Every other URL
    this service emits is relative for exactly this reason, and this page sits
    beside the specification it asks for.
    """
    page = client.get("/api/v1/docs")

    assert page.status_code == 200
    assert "url: 'openapi.json'" in page.text
    assert "'/api/v1/openapi.json'" not in page.text


# Rebooting this machine, the one act the page offers.


def _reboot_play(settings, run: dict) -> dict:
    written = list((settings.runs_dir / run["run_id"]).rglob("node_reboot.yaml"))
    assert len(written) == 1, written
    document = yaml.safe_load(written[0].read_text())
    assert len(document) == 1
    return document[0]


def test_a_reboot_is_scheduled_on_this_machine_by_a_run(
    signed_in: TestClient, settings
) -> None:
    """One task, on the machine the inventory says this one is.

    Scheduled rather than done, so the run driven from this machine ends with
    its status written before the machine goes down.
    """
    response = signed_in.post("/api/v1/node/reboot")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["host"] == "seapath-machine"
    assert signed_in.get(f"/api/v1/runs/{body['run_id']}").status_code == 200

    play = _reboot_play(settings, body)
    assert play["hosts"] == "seapath-machine"
    assert play["name"] == "Reboot seapath-machine"
    assert play["become"] is True
    assert play["tasks"] == [
        {
            "name": "Schedule the reboot and end the run",
            "ansible.builtin.command": {
                "argv": ["systemd-run", "--on-active=15", "systemctl", "reboot"]
            },
            "changed_when": True,
        }
    ]


@pytest.mark.parametrize("username", ["operator", "viewer"])
def test_only_an_administrator_reboots_the_machine(
    client: TestClient, username: str
) -> None:
    sign_in(client, username)

    assert client.post("/api/v1/node/reboot").status_code == 403


def test_the_reboot_is_in_the_openapi_document(signed_in: TestClient) -> None:
    document = signed_in.get("/api/v1/openapi.json").json()

    assert set(document["paths"]["/api/v1/node/reboot"]) == {"post"}
