# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Container deliveries: uploaded, checked, installed as one commit.

The delivery is the shape `DELIVERY.md` in seapath-ansible describes, built
here file by file so that each test breaks exactly one rule of it. What the
tests hold is that nothing reaches the inventory before every rule passes, and
that an installation, or an update, is one commit whose entry points at files
a run will find.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.inventory import delivery
from tests.test_containers import CLUSTER, _import

POD = """[Unit]
Description=vied

[Pod]
PodName=vied
Network=vied-sbus.network:ip={{ container.sbus_ip }},mac={{ container.sbus_mac }}
"""

APP = """[Unit]
Description=vied app

[Container]
Pod=vied.pod
Image={{ container.images[0].name }}
Environment=CLOCK={{ container.clock }}
Volume=/mnt/rbd/vied/instance:/etc/vied:ro
"""

VALUES = {
    "sbus_ip": {
        "description": "The address of the IED.",
        "format": "ipv4",
        "example": "192.0.2.21",
    },
    "sbus_mac": {
        "description": "The MAC of the IED.",
        "format": "mac",
        "example": "02:00:00:00:00:02",
    },
    "clock": {
        "description": "1 once PTP is checked.",
        "format": "integer",
        "min": 0,
        "max": 1,
        "default": 0,
        "example": 1,
    },
}


def _image(path: Path, name: str) -> None:
    """A docker archive holding nothing but the manifest naming its image."""
    data = json.dumps([{"Config": "c.json", "RepoTags": [name], "Layers": []}])
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data.encode()))


def _build(
    tmp_path: Path,
    version: str = "vied-1",
    change: Callable[[Path], None] | None = None,
) -> bytes:
    """A delivery archive, `change` breaking it before it is packed."""
    tag = version.rsplit("-", 1)[1]
    root = tmp_path / "src" / version
    for sub in ("quadlets", "images", "files/instance"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "quadlets/vied.pod.j2").write_text(POD)
    (root / "quadlets/vied-app.container.j2").write_text(APP)
    (root / f"files/instance/model-{tag}.cid").write_text(f"<SCL {tag}/>")
    (root / "README.md").write_text("The test vIED.\n")
    (root / "values.yaml").write_text(yaml.safe_dump(VALUES))
    archive = f"vied-{tag}.tar"
    _image(root / "images" / archive, f"localhost/vied:{tag}")
    digest = hashlib.sha256((root / "images" / archive).read_bytes()).hexdigest()
    (root / "images/SHA256SUMS").write_text(f"{digest}  {archive}\n")
    example = {
        "cluster_containers": {
            "vied": {
                "unit": "vied-pod.service",
                "images": [
                    {"name": f"localhost/vied:{tag}", "archive": f"images/{archive}"}
                ],
                "quadlets": ["quadlets/vied.pod.j2", "quadlets/vied-app.container.j2"],
                "rbd": {
                    "size": "64M",
                    "files": [
                        {
                            "src": f"files/instance/model-{tag}.cid",
                            "dest": "instance/model.cid",
                        }
                    ],
                },
                "sbus_ip": "192.0.2.21",
                "sbus_mac": "02:00:00:00:00:02",
                "clock": 1,
            }
        }
    }
    (root / "inventory-example.yaml").write_text(yaml.safe_dump(example))
    if change:
        change(root)
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as tar:
        tar.add(root, arcname=version)
    return packed.getvalue()


def _stage(client: TestClient, archive: bytes) -> dict:
    response = client.post("/api/v1/containers/deliveries", content=archive)
    assert response.status_code == 201, response.text
    return response.json()


SITE = {"sbus_ip": "10.0.0.21", "sbus_mac": "02:56:49:45:44:02"}


# Staging


def test_a_delivery_is_read_as_the_workload_and_the_values_it_asks_for(
    signed_in: TestClient, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)

    staged = _stage(signed_in, _build(tmp_path))

    assert staged["findings"] == []
    assert staged["name"] == "vied"
    assert staged["version"] == "vied-1"
    assert staged["update"] is False
    assert staged["images"] == ["localhost/vied:1"]
    fields = {field["key"]: field for field in staged["values"]}
    assert sorted(fields) == ["clock", "sbus_ip", "sbus_mac"]
    assert fields["clock"]["has_default"] is True
    assert fields["sbus_ip"]["format"] == "ipv4"


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        (
            lambda root: (root / "quadlets/vied.pod.j2").write_text(
                POD + "\n[Install]\nWantedBy=multi-user.target\n"
            ),
            "[Install]",
        ),
        (
            lambda root: (root / "images/SHA256SUMS").write_text(
                "0" * 64 + "  vied-1.tar\n"
            ),
            "does not match",
        ),
        (
            lambda root: _image(root / "images/vied-1.tar", "localhost/other:1"),
            "the quadlets use localhost/vied:1",
        ),
        (
            lambda root: (root / "values.yaml").write_text(
                yaml.safe_dump({k: v for k, v in VALUES.items() if k != "clock"})
            ),
            "container.clock",
        ),
        (
            lambda root: (root / "files/instance/model-1.cid").unlink(),
            "files/instance/model-1.cid is missing",
        ),
    ],
)
def test_a_delivery_breaking_a_rule_says_so_before_anything_is_written(
    signed_in: TestClient, tmp_path: Path, change: Callable, finding: str
) -> None:
    _import(signed_in, CLUSTER)
    before = signed_in.get("/api/v1/inventory/raw").text

    staged = _stage(signed_in, _build(tmp_path, change=change))

    assert any(finding in item for item in staged["findings"]), staged["findings"]
    assert signed_in.get("/api/v1/inventory/raw").text == before


def test_an_archive_reaching_outside_itself_is_refused(
    signed_in: TestClient, tmp_path: Path
) -> None:
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w") as tar:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    staged = _stage(signed_in, packed.getvalue())

    assert "outside the delivery" in staged["findings"][0]
    assert not (tmp_path / "escape").exists()


# Installing


def test_installing_writes_the_workload_its_files_and_its_images_as_one_commit(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)
    staged = _stage(signed_in, _build(tmp_path))
    history = len(signed_in.get("/api/v1/inventory/history").json())

    response = signed_in.post(
        f"/api/v1/containers/deliveries/{staged['id']}/install",
        json={"values": SITE},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["playbook"] == "deploy_containers_cluster"
    assert body["message"] == "containers: install vied from vied-1"
    assert len(signed_in.get("/api/v1/inventory/history").json()) == history + 1

    document = yaml.safe_load(signed_in.get("/api/v1/inventory/raw").text)
    entry = document["all"]["children"]["cluster_machines"]["vars"][
        "cluster_containers"
    ]["vied"]
    assert entry == {
        "unit": "vied-pod.service",
        "images": [{"name": "localhost/vied:1", "archive": "../files/vied-1.tar"}],
        "quadlets": [
            "../inventories/vied/quadlets/vied.pod.j2",
            "../inventories/vied/quadlets/vied-app.container.j2",
        ],
        "rbd": {
            "size": "64M",
            "files": [
                {
                    "src": "../inventories/vied/files/instance/model-1.cid",
                    "dest": "instance/model.cid",
                }
            ],
        },
        "sbus_ip": "10.0.0.21",
        "sbus_mac": "02:56:49:45:44:02",
        "clock": 0,
    }
    folder = settings.inventory_dir / "inventories/vied"
    assert (folder / "quadlets/vied.pod.j2").read_text() == POD
    assert (folder / "values.yaml").is_file()
    assert (settings.artefacts_dir / "files/vied-1.tar").is_file()
    assert list(settings.imports_dir.iterdir()) == []

    # Every file the entry names is one a run would find.
    missing = [
        item
        for item in signed_in.get("/api/v1/inventory/references").json()
        if item["variable"] == "cluster_containers" and not item["found"]
    ]
    assert missing == []


def test_a_mac_made_of_digits_is_written_as_text(
    signed_in: TestClient, tmp_path: Path
) -> None:
    # Ansible reads the inventory as YAML 1.1, where 12:34:56:12:34:56 left
    # unquoted is a base 60 integer, and the quadlet would get a number.
    _import(signed_in, CLUSTER)
    staged = _stage(signed_in, _build(tmp_path))

    signed_in.post(
        f"/api/v1/containers/deliveries/{staged['id']}/install",
        json={"values": {**SITE, "sbus_mac": "12:34:56:12:34:56"}},
    )

    document = yaml.safe_load(signed_in.get("/api/v1/inventory/raw").text)
    entry = document["all"]["children"]["cluster_machines"]["vars"][
        "cluster_containers"
    ]["vied"]
    assert entry["sbus_mac"] == "12:34:56:12:34:56"


def test_values_that_do_not_fit_are_refused_by_key(
    signed_in: TestClient, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    staged = _stage(signed_in, _build(tmp_path))

    response = signed_in.post(
        f"/api/v1/containers/deliveries/{staged['id']}/install",
        json={"values": {"sbus_ip": "10.0.0.300", "clock": "7"}},
    )

    assert response.status_code == 400
    refused = response.json()["error"]["detail"]["refused"]
    assert sorted(refused) == ["clock", "sbus_ip", "sbus_mac"]


def test_a_new_version_keeps_the_site_values_and_takes_the_old_files_away(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)
    first = _stage(signed_in, _build(tmp_path / "one"))
    signed_in.post(
        f"/api/v1/containers/deliveries/{first['id']}/install",
        json={"values": {**SITE, "clock": 1}},
    )

    second = _stage(signed_in, _build(tmp_path / "two", version="vied-2"))
    assert second["update"] is True
    current = {field["key"]: field["current"] for field in second["values"]}
    assert current == {**SITE, "clock": 1}

    response = signed_in.post(
        f"/api/v1/containers/deliveries/{second['id']}/install",
        json={"values": current},
    )

    assert response.status_code == 201, response.text
    assert response.json()["message"] == "containers: update vied from vied-2"
    document = yaml.safe_load(signed_in.get("/api/v1/inventory/raw").text)
    entry = document["all"]["children"]["cluster_machines"]["vars"][
        "cluster_containers"
    ]["vied"]
    assert entry["images"] == [
        {"name": "localhost/vied:2", "archive": "../files/vied-2.tar"}
    ]
    assert entry["clock"] == 1
    folder = settings.inventory_dir / "inventories/vied/files/instance"
    assert sorted(path.name for path in folder.iterdir()) == ["model-2.cid"]
    assert not (settings.artefacts_dir / "files/vied-1.tar").exists()
    assert (settings.artefacts_dir / "files/vied-2.tar").is_file()


# The site values of an installed workload


def test_the_site_values_of_a_workload_are_edited_against_its_values_file(
    signed_in: TestClient, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    staged = _stage(signed_in, _build(tmp_path))
    signed_in.post(
        f"/api/v1/containers/deliveries/{staged['id']}/install",
        json={"values": SITE},
    )
    containers = {
        item["name"]: item
        for item in signed_in.get("/api/v1/containers").json()["containers"]
    }
    assert containers["vied"]["values_editable"] is True
    assert containers["nginxquadlet"]["values_editable"] is False

    refused = signed_in.put(
        "/api/v1/containers/vied/values", json={"values": {**SITE, "clock": 3}}
    )
    assert refused.status_code == 400

    response = signed_in.put(
        "/api/v1/containers/vied/values", json={"values": {**SITE, "clock": 1}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["message"] == "containers: set the site values of vied"
    values = signed_in.get("/api/v1/containers/vied/values").json()["values"]
    assert {field["key"]: field["current"] for field in values}["clock"] == 1


def test_a_workload_declared_by_hand_has_no_values_form(
    signed_in: TestClient,
) -> None:
    _import(signed_in, CLUSTER)

    response = signed_in.get("/api/v1/containers/nginxquadlet/values")

    assert response.status_code == 404


# Restarting


def test_restarting_a_container_the_cluster_holds_restarts_its_resource(
    signed_in: TestClient, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)

    response = signed_in.post("/api/v1/containers/nginxquadlet/restart")

    assert response.status_code == 202, response.text
    body = response.json()
    written = list((settings.runs_dir / body["run_id"]).rglob("resource_restart.yaml"))
    assert len(written) == 1
    tasks = yaml.safe_load(written[0].read_text())[0]["tasks"]
    assert tasks[0]["ansible.builtin.command"] == {
        "argv": ["crm", "resource", "restart", "nginxquadlet"]
    }


# The format checks


@pytest.mark.parametrize(
    ("fmt", "good", "bad"),
    [
        ("ipv4", "192.0.2.1", "192.0.2.256"),
        ("ipv4_network", "192.0.2.0/24", "192.0.2.1/24"),
        ("mac", "02:00:00:00:00:01", "02:00:00:00:01"),
        ("vlan_list", "100,300", "100,5000"),
        ("name", "processbus", "a-name-longer-than-15"),
    ],
)
def test_each_format_accepts_its_values_and_refuses_the_others(
    fmt: str, good: str, bad: str
) -> None:
    value = delivery.Value(key="k", description="d", format=fmt, example=good)

    assert delivery.check(value, good) == ""
    assert delivery.check(value, bad) != ""
