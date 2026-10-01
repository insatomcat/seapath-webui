# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Declaring again a workload a removal took out of the inventory.

The removal's last commit takes the entry and its files out, and its parent
still holds both. What the tests hold is that the workload comes back exactly
as it was before it was marked absent, with nothing of the removal left in it,
and that a workload whose image archives left with it is refused rather than
declared into a run that would stop on them.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.core.settings import Settings
from tests.test_container_removal import (
    _forget,
    _record,
    _remove,
    _settle,
    _store,
    _stored,
    _workloads,
)
from tests.test_containers import WORKLOADS, _containers, _import
from tests.test_deliveries import _reach_the_members

QUADLET = b"[Container]\nImage=public.ecr.aws/nginx/nginx:1.31.5\n"
NETWORK = b"[Network]\n"


def _redeclare(client: TestClient, name: str):
    return client.post(f"/api/v1/containers/{name}/redeclare")


def _forgotten(client: TestClient) -> dict:
    return {item["name"]: item for item in _containers(client)["forgotten"]}


def _removed_nginx(client: TestClient, settings: Settings, tmp_path: Path) -> dict:
    """nginxquadlet removed by a run that succeeded, its entry as it was."""
    _import(client, WORKLOADS)
    _store(client, "files/nginxquadlet.container.j2", QUADLET)
    _store(client, "files/nginxquadlet.network.j2", NETWORK)
    _reach_the_members(client, settings, tmp_path)
    entry = _workloads(client)["nginxquadlet"]
    run_id = _remove(client, "nginxquadlet", remove_rbd=True).json()["run_id"]
    _settle(client, run_id)
    assert "nginxquadlet" not in _workloads(client)
    return entry


def test_a_removed_workload_is_listed_with_the_commit_that_forgot_it(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _removed_nginx(signed_in, settings, tmp_path)
    history = signed_in.get("/api/v1/inventory/history").json()

    forgotten = _forgotten(signed_in)

    assert sorted(forgotten) == ["nginxquadlet", "retired"]
    nginx = forgotten["nginxquadlet"]
    assert nginx["commit"] == history[0]["hash"]
    assert nginx["rbd"] is True
    assert nginx["remove_rbd"] is True
    assert nginx["missing_archives"] == []


def test_declaring_it_again_puts_back_the_entry_and_its_files(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    entry = _removed_nginx(signed_in, settings, tmp_path)

    response = _redeclare(signed_in, "nginxquadlet")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["message"] == "containers: declare nginxquadlet again"
    # As it was before the removal marked it: no state, no remove_rbd.
    assert _workloads(signed_in)["nginxquadlet"] == entry
    service = signed_in.app.state.inventory_service
    assert service.read_file("files/nginxquadlet.container.j2") == QUADLET
    assert service.read_file("files/nginxquadlet.network.j2") == NETWORK
    run = signed_in.get(f"/api/v1/runs/{body['run_id']}").json()
    assert run["playbook_id"] == "deploy_containers_cluster"
    assert "nginxquadlet" not in _forgotten(signed_in)


def test_a_file_stored_again_since_is_left_as_it_is(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _removed_nginx(signed_in, settings, tmp_path)
    _store(signed_in, "files/nginxquadlet.container.j2", b"[Container]\n# newer\n")

    response = _redeclare(signed_in, "nginxquadlet")

    assert response.status_code == 202, response.text
    service = signed_in.app.state.inventory_service
    assert service.read_file("files/nginxquadlet.container.j2") == (
        b"[Container]\n# newer\n"
    )
    assert service.read_file("files/nginxquadlet.network.j2") == NETWORK


def test_a_workload_whose_archive_left_with_it_is_refused(
    signed_in: TestClient, settings: Settings
) -> None:
    _import(
        signed_in,
        WORKLOADS.replace(
            "protect:\n            unit",
            "protect:\n            state: absent\n            unit",
        ),
    )
    _store(signed_in, "files/protect-rt.container")
    signed_in.put("/api/v1/inventory/artefacts/files/protect-1.0.tar", content=b"\x00")
    _forget(signed_in, _record(signed_in))

    assert _forgotten(signed_in)["protect"]["missing_archives"] == ["protect-1.0.tar"]
    response = _redeclare(signed_in, "protect")

    assert response.status_code == 409
    assert "protect-1.0.tar" in response.json()["error"]["message"]
    assert "protect" not in _workloads(signed_in)
    assert "files/protect-rt.container" not in _stored(signed_in)


def test_an_archive_uploaded_again_lets_it_through(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _import(
        signed_in,
        WORKLOADS.replace(
            "protect:\n            unit",
            "protect:\n            state: absent\n            unit",
        ),
    )
    _store(signed_in, "files/protect-rt.container")
    _forget(signed_in, _record(signed_in))
    signed_in.put("/api/v1/inventory/artefacts/files/protect-1.0.tar", content=b"\x00")
    _reach_the_members(signed_in, settings, tmp_path)

    response = _redeclare(signed_in, "protect")

    assert response.status_code == 202, response.text
    assert "state" not in _workloads(signed_in)["protect"]
    assert "files/protect-rt.container" in _stored(signed_in)


def test_a_name_no_removal_took_out_is_unknown(signed_in: TestClient) -> None:
    _import(signed_in, WORKLOADS)

    for name in ("nginxquadlet", "retired", "nothing"):
        assert _redeclare(signed_in, name).status_code == 404


def test_a_run_that_cannot_start_leaves_it_undeclared(
    signed_in: TestClient, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    _removed_nginx(signed_in, settings, tmp_path)

    def refused(*_: object, **__: object) -> None:
        raise ApiError("run_in_progress", "Another run holds the lock.", 409)

    monkeypatch.setattr(signed_in.app.state.run_service, "launch", refused)

    response = _redeclare(signed_in, "nginxquadlet")

    assert response.status_code == 409
    assert "nginxquadlet" not in _workloads(signed_in)
    assert "nginxquadlet" in _forgotten(signed_in)
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"].startswith("Revert")
