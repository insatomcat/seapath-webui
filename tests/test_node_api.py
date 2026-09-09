# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The read only node API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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
