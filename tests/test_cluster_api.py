# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The cluster and storage views over the API.

The reading is tested next door. What these hold is the surface: a viewer may
read it, the payload carries what the page draws, and there is no way to change
a cluster from here. That last one is the point of the whole feature. Every
button an administration page would grow - standby a node, clean up a failure,
migrate a resource, evict an OSD - is a `crm` or a `ceph` command running inside
this container, and AGENTS.md forbids it in the same words it forbids writing
`corosync.conf`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_the_cluster_is_read_from_the_coordinator_and_says_so(
    signed_in: TestClient,
) -> None:
    payload = signed_in.get("/api/v1/cluster").json()

    assert payload["available"] is True
    assert payload["source"] == "seapath-machine"
    assert payload["from_dc"] is True
    assert payload["dc"] == "seapath-machine"
    assert payload["corosync"]["quorate"] is True
    assert payload["inventory_commit"]


def test_the_cluster_payload_carries_the_failure_the_page_leads_with(
    signed_in: TestClient,
) -> None:
    payload = signed_in.get("/api/v1/cluster").json()

    failed = [item for item in payload["resources"] if item["state"] == "failed"]
    assert [item["id"] for item in failed] == ["vm-guest3"]
    assert failed[0]["fail_count"] == 3
    assert failed[0]["migration_threshold"] == 3
    # And the member nobody may place a VM on, which is a decision rather than
    # a fault and is reported as its own word.
    standby = [item for item in payload["nodes"] if item["state"] == "standby"]
    assert [item["name"] for item in standby] == ["elabo2"]


def test_the_storage_payload_carries_what_ceph_says_is_wrong(
    signed_in: TestClient,
) -> None:
    payload = signed_in.get("/api/v1/storage").json()

    assert payload["health"] == "HEALTH_WARN"
    assert [item["name"] for item in payload["messages"]] == [
        "OSD_DOWN",
        "PG_DEGRADED",
    ]
    assert payload["source"] == "seapath-machine"
    assert len(payload["osds"]) == 6
    assert payload["used_ratio"] == pytest.approx(0.2, abs=0.01)


def test_a_viewer_may_read_both(signed_in_viewer: TestClient) -> None:
    # Reading costs nothing and changes nothing, which is why it is the lowest
    # role. It is also the role an operator on call is likely to have.
    assert signed_in_viewer.get("/api/v1/cluster").status_code == 200
    assert signed_in_viewer.get("/api/v1/storage").status_code == 200


def test_neither_view_may_be_signed_out_of(client: TestClient) -> None:
    assert client.get("/api/v1/cluster").status_code == 401
    assert client.get("/api/v1/storage").status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/cluster", "/api/v1/storage"])
def test_nothing_here_writes(signed_in: TestClient, path: str) -> None:
    # The rule of the whole service, held as a test rather than as a comment:
    # a cluster is changed by Pacemaker, by Ceph, or by an inventory edit and a
    # run. A POST here would be this container running a cluster command.
    for method in (signed_in.post, signed_in.put, signed_in.delete):
        assert method(path).status_code == 405


def test_both_views_are_in_the_openapi_document(signed_in: TestClient) -> None:
    document = signed_in.get("/api/v1/openapi.json").json()

    assert set(document["paths"]["/api/v1/cluster"]) == {"get"}
    assert set(document["paths"]["/api/v1/storage"]) == {"get"}
