# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The cluster and storage views over the API.

The reading is tested next door. What these hold is the surface: a viewer may
read it, the payload carries what the page draws, and the readings themselves
take no write.

One act is offered beside them, and its bounds are here too. Refreshing a
resource clears the operation history Pacemaker keeps for it, and it reaches
the machine as a generated one task run over the SSH path a convergence uses,
which is the exception D30 already makes for starting a guest. What stays out
is everything that would be a `crm` or a `ceph` command running inside this
container: standby a node, migrate a resource, evict an OSD. AGENTS.md forbids
that in the same words it forbids writing `corosync.conf`.
"""

from __future__ import annotations

import pytest
import yaml
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
def test_the_readings_themselves_take_no_write(
    signed_in: TestClient, path: str
) -> None:
    # A cluster is changed by Pacemaker, by Ceph, or by an inventory edit and a
    # run. The one act offered here is on a path of its own, against one named
    # resource, and it is a run.
    for method in (signed_in.post, signed_in.put, signed_in.delete):
        assert method(path).status_code == 405


# Refreshing one resource, which is the runtime plane.

# The seeded machine, put into `cluster_machines`. The addresses have to stay
# the seeded ones, because those are what the fake exporters answer for, and
# the group has to exist, because the generated play targets
# `cluster_machines[0]`: a file with no such group is a run with no host. That
# is why refreshing carries the cluster precondition.
_CLUSTER_GROUP = """
cluster_machines:
  hosts:
    seapath-machine:
"""


def _cluster(client: TestClient) -> TestClient:
    document = client.get("/api/v1/inventory/raw").text
    response = client.post(
        "/api/v1/inventory/import", json={"document": document + _CLUSTER_GROUP}
    )
    assert response.status_code == 200, response.text
    return client


def test_refreshing_a_resource_is_a_run_like_any_other(
    signed_in: TestClient,
) -> None:
    response = _cluster(signed_in).post("/api/v1/cluster/resources/vm-guest3/refresh")

    assert response.status_code == 202, response.json()
    body = response.json()
    assert body["resource"] == "vm-guest3"
    # Watched on the Runs page, with the same event stream and the same record
    # a convergence has.
    assert signed_in.get(f"/api/v1/runs/{body['run_id']}").status_code == 200


def test_the_refresh_play_runs_crm_on_a_cluster_member(
    signed_in: TestClient, settings
) -> None:
    # One task, and the command an operator would type on the machine. No
    # `crm` runs inside this container, which is the line that matters.
    run = _cluster(signed_in).post("/api/v1/cluster/resources/vm-guest3/refresh").json()

    written = list((settings.runs_dir / run["run_id"]).rglob("vm_refresh.yaml"))
    assert len(written) == 1
    document = yaml.safe_load(written[0].read_text())
    assert len(document) == 1
    assert document[0]["hosts"] == "{{ groups['cluster_machines'][0] }}"
    tasks = document[0]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["ansible.builtin.command"] == {
        "argv": ["crm", "resource", "refresh", "vm-guest3"]
    }


def test_a_resource_the_cluster_does_not_report_is_refused(
    signed_in: TestClient,
) -> None:
    # The name reaches a command argument, so it is one the cluster answered
    # for rather than whatever was typed into a URL.
    response = _cluster(signed_in).post(
        "/api/v1/cluster/resources/not-a-resource/refresh"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_resource"


def test_a_viewer_reads_the_cluster_and_never_refreshes_it(
    signed_in_viewer: TestClient,
) -> None:
    # Refreshing writes to the CIB of a live cluster, which is an operator's
    # act, exactly as starting a guest is.
    assert signed_in_viewer.get("/api/v1/cluster").status_code == 200

    refused = signed_in_viewer.post("/api/v1/cluster/resources/vm-guest3/refresh")

    assert refused.status_code == 403


def test_both_views_are_in_the_openapi_document(signed_in: TestClient) -> None:
    document = signed_in.get("/api/v1/openapi.json").json()

    assert set(document["paths"]["/api/v1/cluster"]) == {"get"}
    assert set(document["paths"]["/api/v1/storage"]) == {"get"}
    assert set(document["paths"]["/api/v1/cluster/resources/{name}/refresh"]) == {
        "post"
    }
