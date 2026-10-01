# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Restoring a container workload a removal took out of the inventory.

The removal's last commit takes the entry and its files out, and its parent
still holds both. A restore from the Backup page brings the workload back
whole: the entry and the files as they were before it was marked absent,
committed first, then one run that restores the RBD image and imports
`deploy_containers_cluster`, which creates the resource on that image.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.core.settings import Settings
from tests.test_backup import CONFIGURED, _listed, _wait
from tests.test_container_removal import (
    _remove,
    _settle,
    _store,
    _stored,
    _workloads,
)
from tests.test_containers import WORKLOADS, _import
from tests.test_deliveries import _reach_the_members

QUADLET = b"[Container]\nImage=public.ecr.aws/nginx/nginx:1.31.5\n"
NETWORK = b"[Network]\n"

# The workloads, with the backup settings beside them on `cluster_machines`.
DOCUMENT = WORKLOADS.replace(
    "        cluster_containers:\n", CONFIGURED + "        cluster_containers:\n"
)


def _restore(client: TestClient, name: str = "nginxquadlet"):
    return client.post(
        "/api/v1/backup/restore/container",
        json={"name": name, "full_date": "202603110733", "date": "202603110836"},
    )


def _removed_nginx(client: TestClient, settings: Settings, tmp_path: Path) -> dict:
    """nginxquadlet removed by a run that succeeded, its entry as it was."""
    _import(client, DOCUMENT)
    _store(client, "files/nginxquadlet.container.j2", QUADLET)
    _store(client, "files/nginxquadlet.network.j2", NETWORK)
    _reach_the_members(client, settings, tmp_path)
    entry = _workloads(client)["nginxquadlet"]
    run_id = _remove(client, "nginxquadlet", remove_rbd=True).json()["run_id"]
    _settle(client, run_id)
    assert "nginxquadlet" not in _workloads(client)
    return entry


def _plays(settings: Settings, run_id: str) -> list[dict]:
    path = (
        settings.runs_dir
        / run_id
        / "collections/ansible_collections/seapath/ansible/playbooks"
        / "backup_restore_container.yaml"
    )
    return yaml.safe_load(path.read_text())


def test_the_backup_page_lists_the_removed_workloads(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _removed_nginx(signed_in, settings, tmp_path)

    removed = signed_in.get("/api/v1/backup").json()["removed"]

    assert sorted(removed) == ["nginxquadlet", "retired"]


def test_restoring_a_removed_workload_declares_it_again_and_deploys_it(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    entry = _removed_nginx(signed_in, settings, tmp_path)
    _listed(remote_runner)

    response = _restore(signed_in)

    assert response.status_code == 202, response.text
    assert _workloads(signed_in)["nginxquadlet"] == entry
    stored = _stored(signed_in)
    assert {
        "files/nginxquadlet.container.j2",
        "files/nginxquadlet.network.j2",
    } <= stored
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"] == "containers: declare nginxquadlet again"
    run_id = response.json()["run_id"]
    record = signed_in.get(f"/api/v1/runs/{run_id}").json()
    assert record["inventory_commit"] == history[0]["hash"]

    restore, deploy = _plays(settings, run_id)
    assert restore["tasks"][0]["ansible.builtin.command"]["argv"][0] == (
        "/usr/local/bin/restore_container.sh"
    )
    # A restore that fails ends the run before the deployment reaches the
    # other members with no image.
    assert restore["any_errors_fatal"] is True
    assert deploy == {"import_playbook": "seapath.ansible.deploy_containers_cluster"}
    assert record["playbook"].endswith(".backup_restore_container")
    _wait(signed_in, run_id)


def test_a_declared_workload_is_only_restored(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    _import(signed_in, DOCUMENT)
    _reach_the_members(signed_in, settings, tmp_path)
    _listed(remote_runner)
    head = signed_in.get("/api/v1/inventory/history").json()[0]["hash"]

    response = _restore(signed_in)

    assert response.status_code == 202, response.text
    assert signed_in.get("/api/v1/inventory/history").json()[0]["hash"] == head
    plays = _plays(settings, response.json()["run_id"])
    assert len(plays) == 1
    assert "any_errors_fatal" not in plays[0]


def test_a_removed_workload_whose_archive_is_gone_is_refused(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    # protect loads its image from an archive of the artefacts, which git
    # does not hold. A removal keeps it; one deleted since has to come back
    # before a run could deploy the workload.
    _import(signed_in, DOCUMENT)
    _reach_the_members(signed_in, settings, tmp_path)
    signed_in.put("/api/v1/inventory/artefacts/files/protect-1.0.tar", content=b"\0")
    _settle(signed_in, _remove(signed_in, "protect").json()["run_id"])
    assert (settings.artefacts_dir / "files/protect-1.0.tar").exists()
    signed_in.delete("/api/v1/inventory/artefacts/files/protect-1.0.tar")
    _listed(remote_runner)
    remote_runner.answers = {
        "cd ": remote_runner.answers["cd "].replace(
            "containers/nginxquadlet/", "containers/protect/"
        )
    }
    head = signed_in.get("/api/v1/inventory/history").json()[0]["hash"]

    response = _restore(signed_in, "protect")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_container"
    assert "protect-1.0.tar" in response.json()["error"]["message"]
    assert signed_in.get("/api/v1/inventory/history").json()[0]["hash"] == head


def test_a_run_that_cannot_start_leaves_the_workload_removed(
    signed_in: TestClient,
    settings: Settings,
    tmp_path: Path,
    remote_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _removed_nginx(signed_in, settings, tmp_path)
    _listed(remote_runner)

    def busy(*args, **kwargs):
        raise ApiError("run_in_progress", "Another run holds the lock.", 409)

    monkeypatch.setattr(signed_in.app.state.run_service._store, "acquire", busy)
    response = _restore(signed_in)

    assert response.status_code == 409
    assert "nginxquadlet" not in _workloads(signed_in)
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"].startswith("Revert")
