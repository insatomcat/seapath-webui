# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Restoring a container workload the inventory does not declare.

A backup is enough to put a workload back on a cluster that never had it, as
it is for a guest: `deploy_containers_cluster` records the definition of the
workload in the metadata of its RBD image, `backup_full.sh` exports those
metadata and saves the container images beside them. A restore of a workload
the inventory does not declare reads that definition from the backup, commits
the entry and its files, and launches one run that restores the image, fetches
the image archives the artefacts lack and deploys the workload.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.core.settings import Settings
from tests.test_backup import CONFIGURED, LISTING, _wait
from tests.test_container_removal import _remove, _settle, _store, _workloads
from tests.test_containers import WORKLOADS, _import
from tests.test_deliveries import _reach_the_members

QUADLET = b"[Container]\nImage={{ container.images[0].name }}\n"
NETWORK = b"[Network]\n"

# The workloads, with the backup settings beside them on `cluster_machines`.
DOCUMENT = WORKLOADS.replace(
    "        cluster_containers:\n", CONFIGURED + "        cluster_containers:\n"
)

NGINX = {
    "images": [{"name": "public.ecr.aws/nginx/nginx:1.31.5"}],
    "quadlets": [
        "../files/nginxquadlet.container.j2",
        "../files/nginxquadlet.network.j2",
    ],
    "rbd": {"size": "1G"},
    "ip": "192.0.2.13",
}

PROTECT = {
    "unit": "protect-pod.service",
    "images": [
        {"name": "localhost/protect:1.0", "archive": "../files/protect-1.0.tar"}
    ],
    "quadlets": ["../inventories/protect/protect.pod.j2"],
    "rbd": {"size": "128M"},
}


def _metadata(name: str, entry: dict, files: dict[str, bytes]) -> str:
    """What `rbd image-meta list --format json` exported for one date."""
    definition = {
        "format": 1,
        "name": name,
        "entry": entry,
        "files": {
            path: base64.b64encode(content).decode() for path, content in files.items()
        },
    }
    return json.dumps(
        {
            "seapath.images": " ".join(image["name"] for image in entry["images"]),
            "seapath.definition": json.dumps(definition, sort_keys=True),
        }
    )


def _backup_holds(remote_runner, name: str, metadata: str) -> None:
    listing = LISTING.replace("containers/nginxquadlet/", f"containers/{name}/")
    remote_runner.answers = {"cd ": listing, "cat ": metadata}


def _restore(client: TestClient, name: str = "nginxquadlet"):
    return client.post(
        "/api/v1/backup/restore/container",
        json={"name": name, "full_date": "202603110733", "date": "202603110836"},
    )


def _without(client: TestClient, settings: Settings, tmp_path: Path, name: str):
    """The inventory, with `name` removed by a run that succeeded."""
    _import(client, DOCUMENT)
    _reach_the_members(client, settings, tmp_path)
    _settle(client, _remove(client, name).json()["run_id"])
    assert name not in _workloads(client)


def _plays(settings: Settings, run_id: str) -> list[dict]:
    path = (
        settings.runs_dir
        / run_id
        / "collections/ansible_collections/seapath/ansible/playbooks"
        / "backup_restore_container.yaml"
    )
    return yaml.safe_load(path.read_text())


def _head(client: TestClient) -> dict:
    return client.get("/api/v1/inventory/history").json()[0]


def test_a_workload_comes_back_whole_from_its_backup(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    _without(signed_in, settings, tmp_path, "nginxquadlet")
    files = {
        "../files/nginxquadlet.container.j2": QUADLET,
        "../files/nginxquadlet.network.j2": NETWORK,
    }
    _backup_holds(
        remote_runner, "nginxquadlet", _metadata("nginxquadlet", NGINX, files)
    )

    response = _restore(signed_in)

    assert response.status_code == 202, response.text
    assert _workloads(signed_in)["nginxquadlet"] == NGINX
    folder = signed_in.app.state.inventory_service
    assert folder.read_file("files/nginxquadlet.container.j2") == QUADLET
    assert folder.read_file("files/nginxquadlet.network.j2") == NETWORK
    head = _head(signed_in)
    assert head["message"] == "containers: declare nginxquadlet from its backup"
    run_id = response.json()["run_id"]
    record = signed_in.get(f"/api/v1/runs/{run_id}").json()
    assert record["inventory_commit"] == head["hash"]

    # The metadata of the date the restore goes back to, on the backup server.
    asked = remote_runner.requests[-1].command
    assert "/srv/seapath-backups/202603110733/containers/nginxquadlet/" in asked
    assert "202603110836.json" in asked

    restore, deploy = _plays(settings, run_id)
    assert restore["tasks"][0]["ansible.builtin.command"]["argv"][0] == (
        "/usr/local/bin/restore_container.sh"
    )
    # The image comes from a registry: nothing to fetch.
    assert len(restore["tasks"]) == 1
    # A restore that fails ends the run before the deployment reaches the
    # other members with no image.
    assert restore["any_errors_fatal"] is True
    assert deploy == {"import_playbook": "seapath.ansible.deploy_containers_cluster"}
    _wait(signed_in, run_id)


def test_an_archive_the_artefacts_lack_comes_from_the_backup(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    _import(signed_in, DOCUMENT.replace("          protect:\n", "          kept:\n"))
    _reach_the_members(signed_in, settings, tmp_path)
    files = {"../inventories/protect/protect.pod.j2": b"[Pod]\n"}
    _backup_holds(remote_runner, "protect", _metadata("protect", PROTECT, files))

    response = _restore(signed_in, "protect")

    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    restore, _ = _plays(settings, run_id)
    assert restore["tasks"][1] == {
        "name": "Bring back ../files/protect-1.0.tar from the backup",
        # rsync, compressed, rather than a `fetch` that base64s the archive.
        "become": False,
        "ansible.posix.synchronize": {
            "mode": "pull",
            "src": "/var/lib/seapath-restore/images/localhost_protect_1.0.tar",
            "dest": "{{ playbook_dir }}/../files/protect-1.0.tar",
            "archive": False,
            "compress": True,
            "rsync_path": "sudo -n rsync",
            "use_ssh_args": True,
        },
    }
    # Written into a directory of the run's own, never through a symlink
    # into the artefacts or the collection.
    root = (
        settings.runs_dir / run_id / "collections/ansible_collections/seapath/ansible"
    )
    assert (root / "files").is_dir() and not (root / "files").is_symlink()

    _wait(signed_in, run_id)
    record = signed_in.app.state.run_service.get(run_id)
    (root / "files/protect-1.0.tar").write_bytes(b"saved image")
    backup = signed_in.app.state.backup_service
    backup._fetched[run_id] = ["files/protect-1.0.tar"]
    backup.keep_fetched(record)

    kept = signed_in.app.state.inventory_service.artefact_path("files/protect-1.0.tar")
    assert kept.read_bytes() == b"saved image"


def test_an_archive_the_artefacts_hold_is_not_fetched(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    _import(signed_in, DOCUMENT.replace("          protect:\n", "          kept:\n"))
    _reach_the_members(signed_in, settings, tmp_path)
    signed_in.put("/api/v1/inventory/artefacts/files/protect-1.0.tar", content=b"\0")
    _backup_holds(remote_runner, "protect", _metadata("protect", PROTECT, {}))

    response = _restore(signed_in, "protect")

    assert response.status_code == 202, response.text
    restore, _ = _plays(settings, response.json()["run_id"])
    assert len(restore["tasks"]) == 1


def test_a_backup_without_a_definition_is_refused(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    # Taken before the role recorded definitions: the image alone would come
    # back with nothing running on it.
    _without(signed_in, settings, tmp_path, "nginxquadlet")
    _backup_holds(remote_runner, "nginxquadlet", '{"seapath.images": "x"}')
    head = _head(signed_in)["hash"]

    response = _restore(signed_in)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_definition"
    assert _head(signed_in)["hash"] == head


def test_a_declared_workload_is_only_restored(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    _import(signed_in, DOCUMENT)
    _reach_the_members(signed_in, settings, tmp_path)
    _backup_holds(remote_runner, "nginxquadlet", "")
    head = _head(signed_in)["hash"]

    response = _restore(signed_in)

    assert response.status_code == 202, response.text
    assert _head(signed_in)["hash"] == head
    plays = _plays(settings, response.json()["run_id"])
    assert len(plays) == 1
    assert "any_errors_fatal" not in plays[0]
    assert not any("cat " in request.command for request in remote_runner.requests)


def test_a_file_the_folder_holds_otherwise_is_refused(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    _without(signed_in, settings, tmp_path, "nginxquadlet")
    _store(signed_in, "files/nginxquadlet.container.j2", b"someone else's")
    files = {"../files/nginxquadlet.container.j2": QUADLET}
    _backup_holds(
        remote_runner, "nginxquadlet", _metadata("nginxquadlet", NGINX, files)
    )
    head = _head(signed_in)["hash"]

    response = _restore(signed_in)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_container"
    assert _head(signed_in)["hash"] == head


def test_a_run_that_cannot_start_leaves_the_workload_undeclared(
    signed_in: TestClient,
    settings: Settings,
    tmp_path: Path,
    remote_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without(signed_in, settings, tmp_path, "nginxquadlet")
    _backup_holds(remote_runner, "nginxquadlet", _metadata("nginxquadlet", NGINX, {}))

    def busy(*args, **kwargs):
        raise ApiError("run_in_progress", "Another run holds the lock.", 409)

    monkeypatch.setattr(signed_in.app.state.run_service._store, "acquire", busy)
    response = _restore(signed_in)

    assert response.status_code == 409
    assert "nginxquadlet" not in _workloads(signed_in)
    assert _head(signed_in)["message"].startswith("Revert")
