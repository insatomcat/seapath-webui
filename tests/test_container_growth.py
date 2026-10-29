# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Growing a container workload's RBD image: a larger `rbd.size`, and a run.

The size is desired state like the rest of the entry, and
`deploy_containers_cluster` grows the image and its filesystem to it. What the
tests hold is that the gesture is a commit of that one key and a run limited
to the workload, and that every size the role would refuse, or would take and
leave the image as it was, is refused before anything is written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.inventory.quadlets import rbd_bytes
from app.runs import catalogue
from tests.test_containers import WORKLOADS, _by_name, _containers, _import
from tests.test_deliveries import _reach_the_members


def _workloads_at(client: TestClient, commit: str) -> dict:
    document = yaml.safe_load(client.app.state.inventory_service.raw_at(commit))
    return document["all"]["children"]["cluster_machines"]["vars"]["cluster_containers"]


def _grow(client: TestClient, name: str, size: str):
    return client.post(f"/api/v1/containers/{name}/grow", json={"size": size})


def _history(client: TestClient) -> list[str]:
    return [item["hash"] for item in client.get("/api/v1/inventory/history").json()]


@pytest.mark.parametrize(
    "size,expected",
    [
        ("128M", 128 * 1024**2),
        ("1G", 1024**3),
        ("20g", 20 * 1024**3),
        ("10GiB", 10 * 1024**3),
        # rbd reads a bare number as MiB.
        ("1024", 1024**3),
        ("2T", 2 * 1024**4),
        ("10B", None),
        ("big", None),
        ("", None),
    ],
)
def test_a_size_is_read_as_rbd_reads_it(size: str, expected: int | None) -> None:
    assert rbd_bytes(size) == expected


def test_the_page_shows_the_size_and_how_full_the_image_is(
    signed_in: TestClient,
) -> None:
    _import(signed_in, WORKLOADS)

    rows = _by_name(_containers(signed_in))

    assert rows["nginxquadlet"]["rbd_size"] == "1G"
    # Mounted where Pacemaker runs it, and read from that node's exporter.
    assert rows["nginxquadlet"]["volume"] == {
        "host": "seapath-machine",
        "size_bytes": 1040441344,
        "available_bytes": 260110336,
    }
    # Mounted nowhere: stopped, or on a node that did not answer.
    assert rows["protect"]["rbd_size"] == "128M"
    assert rows["protect"]["volume"] is None


def test_growing_commits_the_size_and_launches_the_run(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _import(signed_in, WORKLOADS)
    _reach_the_members(signed_in, settings, tmp_path)

    response = _grow(signed_in, "nginxquadlet", " 4 gib")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["size"] == "4G"
    assert body["message"] == "containers: grow the RBD image of nginxquadlet to 4G"
    bodies = dict(signed_in.app.state.inventory_service.messages("containers: grow"))
    assert "\n\nFrom 1G. " in bodies[body["commit"]]
    entry = _workloads_at(signed_in, body["commit"])["nginxquadlet"]
    # The one key, every other kept.
    assert entry["rbd"] == {"size": "4G"}
    assert entry["quadlets"]
    run = signed_in.get(f"/api/v1/runs/{body['run_id']}").json()
    assert run["playbook_id"] == "deploy_containers_cluster"
    assert run["variables"] == {"deploy_containers_cluster_only": "nginxquadlet"}


@pytest.mark.parametrize(
    "name,size,reason",
    [
        # The entry's own size, and less: the role never shrinks an image.
        ("nginxquadlet", "1G", "is not more"),
        ("nginxquadlet", "512M", "is not more"),
        ("nginxquadlet", "1.5G", "whole number"),
        ("nginxquadlet", "lots", "not a size rbd reads"),
    ],
)
def test_a_size_the_role_would_not_grow_to_is_refused(
    signed_in: TestClient, name: str, size: str, reason: str
) -> None:
    _import(signed_in, WORKLOADS)
    before = _history(signed_in)

    response = _grow(signed_in, name, size)

    assert response.status_code == 400, response.text
    assert reason in response.json()["error"]["message"]
    assert _history(signed_in) == before


def test_a_size_no_larger_than_the_filesystem_is_refused(
    signed_in: TestClient,
) -> None:
    # The entry says 100M, the image was grown to 1 GiB by hand: 512M is more
    # than the entry and would still be a shrink.
    _import(signed_in, WORKLOADS.replace("size: 1G", "size: 100M"))
    before = _history(signed_in)

    response = _grow(signed_in, "nginxquadlet", "512M")

    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "already holds 992.2 MiB on seapath-machine" in message
    assert _history(signed_in) == before


def test_a_workload_with_no_rbd_image_is_refused(signed_in: TestClient) -> None:
    unsized = WORKLOADS.replace("            rbd:\n              size: 1G\n", "")
    _import(signed_in, unsized)

    response = _grow(signed_in, "nginxquadlet", "2G")

    assert response.status_code == 400, response.text
    assert "keeps no RBD image" in response.json()["error"]["message"]


def test_an_undeclared_workload_is_unknown(
    signed_in: TestClient,
) -> None:
    _import(signed_in, WORKLOADS)

    response = _grow(signed_in, "nowhere", "2G")

    assert response.status_code == 404, response.text


def test_a_role_that_only_creates_the_image_is_refused_before_the_commit(
    signed_in: TestClient, collections_path: Path
) -> None:
    _import(signed_in, WORKLOADS)
    before = _history(signed_in)
    present = collections_path.joinpath(
        "ansible_collections/seapath/ansible/roles",
        "deploy_containers_cluster/tasks/cluster_present.yml",
    )
    present.unlink()
    assert not catalogue.role_grows_rbd(collections_path)

    response = _grow(signed_in, "nginxquadlet", "4G")

    assert response.status_code == 409, response.text
    assert "reads rbd.size only when it creates" in response.json()["error"]["message"]
    assert _history(signed_in) == before


def test_a_viewer_cannot_grow(signed_in_viewer: TestClient) -> None:
    response = _grow(signed_in_viewer, "nginxquadlet", "4G")

    assert response.status_code == 403
