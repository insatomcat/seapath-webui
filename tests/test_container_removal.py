# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Removing a container workload: `state: absent`, a run, then the entry goes.

The role takes a workload off the machines when its entry says `state:
absent`, so removing one is a commit and a run of `deploy_containers_cluster`,
like a convergence. What the tests hold is the order: the inventory says
absent first, the run applies it, and only a run that succeeded over the whole
cluster lets the entry and its files leave the inventory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.runs.models import RunRecord, RunState
from app.runs.scope import RunScope
from tests.test_containers import CLUSTER, REAL, WORKLOADS, _containers, _import
from tests.test_deliveries import _reach_the_members
from tests.test_runs import wait_for_listeners

RUN_ID = "20261001T120000-abcdef"


def _workloads(client: TestClient) -> dict:
    document = yaml.safe_load(client.get("/api/v1/inventory/raw").text)
    return document["all"]["children"]["cluster_machines"]["vars"]["cluster_containers"]


def _store(client: TestClient, path: str, content: bytes = b"x") -> None:
    response = client.put(
        f"/api/v1/inventory/files/{path}",
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code in (200, 201), response.text


def _head(client: TestClient) -> str:
    return client.get("/api/v1/inventory/history").json()[0]["hash"]


def _remove(client: TestClient, name: str, **payload: object):
    return client.post(f"/api/v1/containers/{name}/remove", json=payload)


def _marked(client: TestClient, commit: str) -> dict:
    """The workloads as the removal commit wrote them, whatever ran after."""
    document = yaml.safe_load(client.app.state.inventory_service.raw_at(commit))
    return document["all"]["children"]["cluster_machines"]["vars"]["cluster_containers"]


def _body(client: TestClient, prefix: str) -> str:
    return dict(client.app.state.inventory_service.messages(prefix))


def _stored(client: TestClient) -> set[str]:
    return {item.path for item in client.app.state.inventory_service.files()}


def _settle(client: TestClient, run_id: str) -> None:
    wait_for_listeners(client.app.state.run_service, run_id)


def _record(client: TestClient, **fields: object) -> RunRecord:
    values: dict = {
        "id": RUN_ID,
        "playbook": "deploy_containers_cluster.yaml",
        "playbook_id": "deploy_containers_cluster",
        "state": RunState.SUCCESS,
        "launched_by": "operator1",
        "inventory_commit": _head(client),
        "finished_at": datetime.now(tz=UTC),
    }
    values.update(fields)
    return RunRecord(**values)


def _forget(client: TestClient, record: RunRecord):
    return client.app.state.container_service.forget_removed(record)


# Marking a workload absent


def test_removing_marks_the_workload_absent_and_launches_the_run(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _import(signed_in, WORKLOADS)
    _reach_the_members(signed_in, settings, tmp_path)

    response = _remove(signed_in, "protect", remove_rbd=True)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["message"] == "containers: remove protect"
    entry = _marked(signed_in, body["commit"])["protect"]
    # Every key stays: the role reads the quadlets and the images to know what
    # to take off the nodes.
    assert entry["state"] == "absent"
    assert entry["remove_rbd"] is True
    assert entry["quadlets"] == [
        "../files/protect-rt.container",
        "../files/protect.pod.j2",
    ]
    run = signed_in.get(f"/api/v1/runs/{body['run_id']}").json()
    assert run["playbook_id"] == "deploy_containers_cluster"
    # The run is limited to the workload being removed.
    assert run["variables"] == {"deploy_containers_cluster_only": "protect"}


def test_the_rbd_image_is_kept_unless_asked(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _import(signed_in, WORKLOADS)
    _reach_the_members(signed_in, settings, tmp_path)

    response = _remove(signed_in, "nginxquadlet")

    assert response.status_code == 202, response.text
    commit = response.json()["commit"]
    entry = _marked(signed_in, commit)["nginxquadlet"]
    assert entry["state"] == "absent"
    assert "remove_rbd" not in entry
    assert "stays in the pool" in _body(signed_in, "containers: remove")[commit]


def test_the_run_that_removed_it_takes_the_entry_out(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    # The fake runner succeeds, so the whole act is seen through: marked,
    # run, then forgotten by the operator who removed it.
    _import(signed_in, WORKLOADS)
    _reach_the_members(signed_in, settings, tmp_path)

    run_id = _remove(signed_in, "protect").json()["run_id"]
    _settle(signed_in, run_id)

    # `retired` was marked before and the run, limited to protect, left it
    # where it was: its own run is still owed.
    assert sorted(_workloads(signed_in)) == ["nginxquadlet", "retired"]
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"] == "containers: forget protect"
    assert history[0]["author"] == "admin"


def test_a_workload_being_removed_is_listed_apart(signed_in: TestClient) -> None:
    _import(signed_in, WORKLOADS)

    payload = _containers(signed_in)

    assert [item["name"] for item in payload["removing"]] == ["retired"]
    assert "retired" not in {item["name"] for item in payload["containers"]}
    rows = {item["name"]: item for item in payload["containers"]}
    assert rows["protect"]["rbd"] is True


def test_a_quadlet_upload_extra_files_uploads_is_not_removed_here(
    signed_in: TestClient,
) -> None:
    _import(signed_in, REAL.read_text())

    response = _remove(signed_in, "nginxquadlet")

    assert response.status_code == 404
    assert "Inventory page" in response.json()["error"]["message"]


def test_a_workload_already_marked_is_refused(signed_in: TestClient) -> None:
    _import(signed_in, WORKLOADS)

    response = _remove(signed_in, "retired")

    assert response.status_code == 409
    assert "already marked" in response.json()["error"]["message"]


def test_a_workload_another_one_is_colocated_with_is_refused(
    signed_in: TestClient,
) -> None:
    _import(
        signed_in,
        WORKLOADS.replace(
            "          protect:\n",
            "          protect:\n            colocated_with: [nginxquadlet]\n",
        ),
    )

    response = _remove(signed_in, "nginxquadlet")

    assert response.status_code == 409
    assert "protect is kept beside nginxquadlet" in response.json()["error"]["message"]
    assert "state" not in _workloads(signed_in)["nginxquadlet"]


def test_a_run_that_cannot_start_puts_the_workload_back(
    signed_in: TestClient,
) -> None:
    # No site key and no host keys: the run is refused before it starts.
    _import(signed_in, WORKLOADS)

    response = _remove(signed_in, "protect")

    assert response.status_code >= 400
    assert "state" not in _workloads(signed_in)["protect"]
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"].startswith("Revert")


# Forgetting it once the run has taken it away


def test_a_run_that_removed_a_workload_takes_its_entry_and_files_out(
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
    _store(signed_in, "files/protect.pod.j2")
    _store(signed_in, "files/settings.json")
    _store(signed_in, "inventories/protect/values.yaml")
    _store(signed_in, "files/nginxquadlet.container.j2")
    signed_in.put("/api/v1/inventory/artefacts/files/protect-1.0.tar", content=b"\x00")

    commit = _forget(signed_in, _record(signed_in))

    assert commit is not None
    assert commit.message == "containers: forget protect, retired"
    assert RUN_ID in _body(signed_in, "containers: forget")[commit.hash]
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["author"] == "operator1"
    assert sorted(_workloads(signed_in)) == ["nginxquadlet"]
    stored = _stored(signed_in)
    assert "files/nginxquadlet.container.j2" in stored
    assert not stored & {
        "files/protect-rt.container",
        "files/protect.pod.j2",
        "files/settings.json",
        "inventories/protect/values.yaml",
    }
    assert not (settings.artefacts_dir / "files/protect-1.0.tar").exists()


def test_a_file_another_workload_names_stays(signed_in: TestClient) -> None:
    _import(
        signed_in,
        WORKLOADS.replace(
            "              - ../files/retired.container",
            "              - ../files/retired.container\n"
            "              - ../files/nginxquadlet.network.j2",
        ),
    )
    _store(signed_in, "files/retired.container")
    _store(signed_in, "files/nginxquadlet.network.j2")

    _forget(signed_in, _record(signed_in))

    stored = _stored(signed_in)
    assert "files/retired.container" not in stored
    assert "files/nginxquadlet.network.j2" in stored


def test_a_run_that_did_not_succeed_keeps_the_entry(signed_in: TestClient) -> None:
    _import(signed_in, WORKLOADS)

    for state in (RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED):
        assert _forget(signed_in, _record(signed_in, state=state)) is None

    assert "retired" in _workloads(signed_in)


def test_a_preview_a_narrowed_run_or_another_playbook_keeps_the_entry(
    signed_in: TestClient,
) -> None:
    _import(signed_in, WORKLOADS)

    for record in (
        _record(signed_in, check=True),
        _record(signed_in, scope=RunScope(hosts=["elabo1"])),
        _record(signed_in, playbook_id="seapath_setup_main"),
    ):
        assert _forget(signed_in, record) is None

    assert "retired" in _workloads(signed_in)


def test_a_run_limited_to_a_workload_forgets_that_one_alone(
    signed_in: TestClient,
) -> None:
    _import(
        signed_in,
        WORKLOADS.replace(
            "protect:\n            unit",
            "protect:\n            state: absent\n            unit",
        ),
    )

    for variables in (
        # Limited to a workload that stays: nothing was removed.
        {"deploy_containers_cluster_only": "nginxquadlet"},
        {"deploy_containers_cluster_only": ["nginxquadlet"]},
    ):
        assert _forget(signed_in, _record(signed_in, variables=variables)) is None
    commit = _forget(
        signed_in,
        _record(signed_in, variables={"deploy_containers_cluster_only": "protect"}),
    )

    assert commit is not None
    assert commit.message == "containers: forget protect"
    assert sorted(_workloads(signed_in)) == ["nginxquadlet", "retired"]


def test_a_run_may_be_limited_to_a_workload_being_removed_and_to_no_unknown_one(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _import(signed_in, WORKLOADS)
    _reach_the_members(signed_in, settings, tmp_path)

    def launch(name: str, variable: str = "deploy_containers_cluster_only"):
        return signed_in.post(
            "/api/v1/runs",
            json={
                "playbook": "deploy_containers_cluster",
                "variables": {variable: name},
            },
        )

    refused = launch("nothere")
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "invalid_variable"
    # Restarting what is being removed stays refused: only the limit names it.
    assert launch("retired", "deploy_containers_cluster_restart").status_code == 400
    accepted = launch("retired")
    assert accepted.status_code in (201, 202), accepted.text


def test_a_workload_marked_after_the_run_read_the_inventory_is_kept(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    # The run read the inventory before protect was marked: it removed
    # retired, and protect is for the next one.
    _import(signed_in, WORKLOADS)
    before = _head(signed_in)
    _reach_the_members(signed_in, settings, tmp_path)
    service = signed_in.app.state.container_service
    service.remove("protect", False, "admin")

    service.forget_removed(_record(signed_in, inventory_commit=before))

    assert sorted(_workloads(signed_in)) == ["nginxquadlet", "protect"]
    assert _workloads(signed_in)["protect"]["state"] == "absent"


def test_nothing_marked_absent_is_no_commit(signed_in: TestClient) -> None:
    _import(signed_in, CLUSTER)

    assert _forget(signed_in, _record(signed_in)) is None
