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
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.inventory import delivery
from app.trust import known_hosts
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
                    "src": "../inventories/vied/site/instance/model-1.cid",
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
    # The example, and the site's copy the RBD image is seeded from.
    assert (folder / "examples/instance/model-1.cid").read_text() == "<SCL 1/>"
    assert (folder / "site/instance/model-1.cid").read_text() == "<SCL 1/>"
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


def test_a_new_version_keeps_the_site_values_and_files_and_takes_the_old_ones_away(
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
    # The example of the new version replaces the old one, and the site's file
    # the RBD image is seeded from stays the site's.
    folder = settings.inventory_dir / "inventories/vied"
    assert sorted(path.name for path in (folder / "examples/instance").iterdir()) == [
        "model-2.cid"
    ]
    assert entry["rbd"]["files"] == [
        {
            "src": "../inventories/vied/site/instance/model-1.cid",
            "dest": "instance/model.cid",
        }
    ]
    assert (folder / "site/instance/model-1.cid").read_text() == "<SCL 1/>"
    assert not (settings.artefacts_dir / "files/vied-1.tar").exists()
    assert (settings.artefacts_dir / "files/vied-2.tar").is_file()


def _entry(client: TestClient, name: str = "vied") -> dict:
    document = yaml.safe_load(client.get("/api/v1/inventory/raw").text)
    return document["all"]["children"]["cluster_machines"]["vars"][
        "cluster_containers"
    ][name]


def _install(client: TestClient, staged: dict, **extra: object):
    return client.post(
        f"/api/v1/containers/deliveries/{staged['id']}/install",
        json={"values": SITE, **extra},
    )


# Placement


def test_the_form_offers_the_members_and_writes_the_placement_chosen(
    signed_in: TestClient, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    staged = _stage(signed_in, _build(tmp_path))

    assert staged["nodes"] == ["elabo1", "elabo2", "seapath-machine"]
    assert staged["placement"] == {"preferred_host": None, "pinned_host": None}

    response = _install(
        signed_in, staged, placement={"preferred_host": "elabo2", "pinned_host": None}
    )

    assert response.status_code == 201, response.text
    assert _entry(signed_in)["preferred_host"] == "elabo2"
    assert "pinned_host" not in _entry(signed_in)


@pytest.mark.parametrize(
    ("placement", "reason"),
    [
        ({"preferred_host": "elabo1", "pinned_host": "elabo2"}, "not both"),
        ({"pinned_host": "observer"}, "not a member"),
    ],
)
def test_a_placement_the_cluster_cannot_follow_is_refused(
    signed_in: TestClient, tmp_path: Path, placement: dict, reason: str
) -> None:
    _import(signed_in, CLUSTER)
    before = signed_in.get("/api/v1/inventory/raw").text
    staged = _stage(signed_in, _build(tmp_path))

    response = _install(signed_in, staged, placement=placement)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_placement"
    assert reason in response.json()["error"]["message"]
    assert signed_in.get("/api/v1/inventory/raw").text == before


def test_a_new_version_takes_the_placement_answered_and_drops_other_keys(
    signed_in: TestClient, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    _install(
        signed_in,
        _stage(signed_in, _build(tmp_path / "one")),
        placement={"pinned_host": "elabo1"},
    )
    document = yaml.safe_load(signed_in.get("/api/v1/inventory/raw").text)
    document["all"]["children"]["cluster_machines"]["vars"]["cluster_containers"][
        "vied"
    ]["stale"] = 1
    _import(signed_in, yaml.safe_dump(document, sort_keys=False))
    assert _entry(signed_in)["stale"] == 1

    second = _stage(signed_in, _build(tmp_path / "two", version="vied-2"))
    assert second["placement"] == {"preferred_host": None, "pinned_host": "elabo1"}

    # Sent without a placement, the one it has is kept.
    _install(signed_in, second)
    assert _entry(signed_in)["pinned_host"] == "elabo1"
    assert "stale" not in _entry(signed_in)

    third = _stage(signed_in, _build(tmp_path / "three", version="vied-3"))
    _install(signed_in, third, placement={"preferred_host": None, "pinned_host": None})
    assert "pinned_host" not in _entry(signed_in)


def test_the_files_a_workload_declared_by_hand_named_go_unless_still_named(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    # The workload was first written by hand, its quadlets beside the
    # inventory. One file is also uploaded by upload_extra_files.
    _import(signed_in, CLUSTER)
    for path in ("inventories/vied.pod.j2", "files/nginxquadlet.container"):
        response = signed_in.put(
            f"/api/v1/inventory/files/{path}",
            content=b"[Unit]\n",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code < 300, response.text
    raw = signed_in.get("/api/v1/inventory/raw").text
    by_hand = raw.replace(
        "        extra_crm_cmd_to_run:",
        "        cluster_containers:\n"
        "          vied:\n"
        "            quadlets:\n"
        "              - ../inventories/vied.pod.j2\n"
        "              - ../files/nginxquadlet.container\n"
        "        extra_crm_cmd_to_run:",
    )
    _import(signed_in, by_hand)

    response = _install(signed_in, _stage(signed_in, _build(tmp_path)))

    assert response.status_code == 201, response.text
    assert not (settings.inventory_dir / "inventories/vied.pod.j2").exists()
    assert (settings.inventory_dir / "files/nginxquadlet.container").is_file()


# Starting again from nothing


def _reach_the_members(client: TestClient, settings: Settings, tmp_path: Path) -> None:
    """The site key and the members' host keys, which a run of the cluster needs."""
    key = tmp_path / "site_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    client.put("/api/v1/trust/site-key", json={"material": key.read_text()})
    known_hosts.accept_peers(
        settings.known_hosts_file,
        {
            "192.168.200.126": ["ssh-ed25519 AAAAelabo1"],
            "192.168.200.127": ["ssh-ed25519 AAAAelabo2"],
        },
    )


def test_recreating_launches_the_run_with_the_workload_named(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)
    _reach_the_members(signed_in, settings, tmp_path)
    _install(signed_in, _stage(signed_in, _build(tmp_path / "one")))

    response = _install(
        signed_in,
        _stage(signed_in, _build(tmp_path / "two", version="vied-2")),
        recreate=True,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["commit"]
    run = signed_in.get(f"/api/v1/runs/{body['run_id']}").json()
    assert run["playbook_id"] == "deploy_containers_cluster"
    assert run["variables"] == {"deploy_containers_cluster_recreate": "vied"}


def test_a_workload_to_recreate_is_one_the_inventory_declares(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    _reach_the_members(signed_in, settings, tmp_path)

    response = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "deploy_containers_cluster",
            "variables": {"deploy_containers_cluster_recreate": "nothere"},
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_variable"


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


# Configuration files


CONFIG_APP = """[Unit]
Description=vied app

[Container]
Pod=vied.pod
Image={{ container.images[0].name }}
Environment=CLOCK={{ container.clock }}
Volume=/etc/seapath-containers/vied:/etc/vied:ro
Volume=/mnt/rbd/vied:/var/lib/vied
"""

CHECKS = [
    {
        "file": "model.cid",
        "xpath": "//scl:ConnectedAP/scl:Address/scl:P[@type='IP']",
        "namespaces": {"scl": "http://www.iec.ch/61850/2003/SCL"},
        "value": "sbus_ip",
    }
]


def _build_config(
    tmp_path: Path,
    version: str = "vied-1",
    examples: dict[str, str] | None = None,
    change: Callable[[Path], None] | None = None,
) -> bytes:
    """A delivery with `examples/` and `checks.yaml`."""

    def configure(root: Path) -> None:
        shutil.rmtree(root / "files")
        (root / "quadlets/vied-app.container.j2").write_text(CONFIG_APP)
        for name, text in (examples or {"model.cid": "<SCL example/>"}).items():
            (root / "examples" / name).parent.mkdir(parents=True, exist_ok=True)
            (root / "examples" / name).write_text(text)
        (root / "checks.yaml").write_text(yaml.safe_dump(CHECKS))
        example = yaml.safe_load((root / "inventory-example.yaml").read_text())
        spec = example["cluster_containers"]["vied"]
        spec["rbd"] = {"size": "64M"}
        spec["config"] = [f"examples/{name}" for name in examples or ["model.cid"]]
        (root / "inventory-example.yaml").write_text(yaml.safe_dump(example))
        if change:
            change(root)

    return _build(tmp_path, version, configure)


def test_a_first_installation_starts_the_site_from_the_examples(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)
    staged = _stage(signed_in, _build_config(tmp_path))
    assert staged["findings"] == []
    assert staged["site"] == [
        {
            "example": "model.cid",
            "path": "inventories/vied/site/model.cid",
            "origin": "example",
        }
    ]
    assert staged["checks"] == 1

    response = _install(signed_in, staged)

    assert response.status_code == 201, response.text
    entry = _entry(signed_in)
    assert entry["config"] == ["../inventories/vied/site/model.cid"]
    assert entry["checks"] == CHECKS
    assert entry["rbd"] == {"size": "64M"}
    folder = settings.inventory_dir / "inventories/vied"
    assert (folder / "site/model.cid").read_text() == "<SCL example/>"
    assert (folder / "examples/model.cid").read_text() == "<SCL example/>"
    assert yaml.safe_load((folder / "checks.yaml").read_text()) == CHECKS


def test_a_new_version_never_replaces_the_site_configuration(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    # What happened on ccv on 2026-09-27: the import put the reference CID of
    # the delivery where the site's was.
    _import(signed_in, CLUSTER)
    _install(signed_in, _stage(signed_in, _build_config(tmp_path / "one")))
    signed_in.put(
        "/api/v1/inventory/files/inventories/vied/site/model.cid",
        content=b"<SCL site/>",
    )

    staged = _stage(
        signed_in,
        _build_config(tmp_path / "two", "vied-2", {"model.cid": "<SCL reference 2/>"}),
    )
    assert [item["origin"] for item in staged["site"]] == ["site"]
    response = _install(signed_in, staged)

    assert response.status_code == 201, response.text
    folder = settings.inventory_dir / "inventories/vied"
    assert (folder / "site/model.cid").read_text() == "<SCL site/>"
    assert (folder / "examples/model.cid").read_text() == "<SCL reference 2/>"
    assert _entry(signed_in)["config"] == ["../inventories/vied/site/model.cid"]


def test_a_template_the_site_chose_takes_the_place_of_the_example(
    signed_in: TestClient, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    _install(signed_in, _stage(signed_in, _build_config(tmp_path / "one")))
    signed_in.put(
        "/api/v1/inventory/files/inventories/vied/site/model.cid.j2",
        content=b"<SCL {{ container.sbus_ip }}/>",
    )
    signed_in.delete("/api/v1/inventory/files/inventories/vied/site/model.cid")

    staged = _stage(signed_in, _build_config(tmp_path / "two", "vied-2"))

    assert staged["site"] == [
        {
            "example": "model.cid",
            "path": "inventories/vied/site/model.cid.j2",
            "origin": "site",
        }
    ]
    _install(signed_in, staged)
    assert _entry(signed_in)["config"] == ["../inventories/vied/site/model.cid.j2"]


def test_a_file_a_new_version_expects_is_copied_only_when_accepted(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)
    _install(signed_in, _stage(signed_in, _build_config(tmp_path / "one")))
    two = {"model.cid": "<SCL/>", "settings.json": "{}"}

    staged = _stage(signed_in, _build_config(tmp_path / "two", "vied-2", two))
    assert [(item["example"], item["origin"]) for item in staged["site"]] == [
        ("model.cid", "site"),
        ("settings.json", "example"),
    ]
    response = _install(signed_in, staged, examples=[])

    assert response.status_code == 201, response.text
    folder = settings.inventory_dir / "inventories/vied"
    assert not (folder / "site/settings.json").exists()
    # Still named: the run stops until the site adds it.
    assert _entry(signed_in)["config"][1] == "../inventories/vied/site/settings.json"
    missing = {
        item["value"]
        for item in signed_in.get("/api/v1/inventory/references").json()
        if item["variable"] == "cluster_containers" and not item["found"]
    }
    assert missing == {"../inventories/vied/site/settings.json"}

    staged = _stage(signed_in, _build_config(tmp_path / "three", "vied-3", two))
    _install(signed_in, staged, examples=["settings.json"])
    assert (folder / "site/settings.json").read_text() == "{}"


def test_an_installation_made_before_site_moves_its_files_there(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    # A workload installed from files/, whose site file the operator replaced
    # in place: that file, and not the new example, becomes the site's.
    _import(signed_in, CLUSTER)
    _install(signed_in, _stage(signed_in, _build(tmp_path / "one")))
    signed_in.put(
        "/api/v1/inventory/files/inventories/vied/files/instance/model.cid",
        content=b"<SCL ccv/>",
    )
    document = signed_in.get("/api/v1/inventory/raw").text.replace(
        "../inventories/vied/site/instance/model-1.cid",
        "../inventories/vied/files/instance/model.cid",
    )
    assert (
        signed_in.put("/api/v1/inventory/raw", json={"document": document}).status_code
        == 200
    )

    # The example is a template: the site's file keeps its own name, plain.
    staged = _stage(
        signed_in,
        _build_config(
            tmp_path / "two",
            "vied-2",
            {"model.cid.j2": "<SCL {{ container.sbus_ip }}/>"},
        ),
    )
    assert staged["site"] == [
        {
            "example": "model.cid.j2",
            "path": "inventories/vied/site/model.cid",
            "origin": "current",
        }
    ]
    _install(signed_in, staged)

    folder = settings.inventory_dir / "inventories/vied"
    assert (folder / "site/model.cid").read_text() == "<SCL ccv/>"
    assert [
        path for path in folder.rglob("*") if path.is_file() and "files" in path.parts
    ] == []
    assert _entry(signed_in)["config"] == ["../inventories/vied/site/model.cid"]


def test_a_check_on_a_value_the_delivery_does_not_describe_is_refused(
    signed_in: TestClient, tmp_path: Path
) -> None:
    def wrong(root: Path) -> None:
        (root / "checks.yaml").write_text(
            yaml.safe_dump([{**CHECKS[0], "value": "vlan", "match": "near"}])
        )

    _import(signed_in, CLUSTER)
    staged = _stage(signed_in, _build_config(tmp_path, change=wrong))

    assert staged["findings"] == [
        "checks.yaml, check 1 compares vlan, which values.yaml does not describe.",
        "checks.yaml, check 1: match is one of equal, in_list.",
    ]


def test_an_example_template_reads_the_site_values(
    signed_in: TestClient, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    staged = _stage(
        signed_in,
        _build_config(
            tmp_path,
            examples={"model.cid.j2": "<SCL {{ container.vlan }}/>"},
            change=lambda root: (root / "checks.yaml").unlink(),
        ),
    )

    assert staged["findings"] == [
        "The templates read container.vlan, which values.yaml does not describe."
    ]


@pytest.mark.parametrize(
    ("checks", "finding"),
    [
        ({"not": "a list"}, "checks.yaml must be a list of checks."),
        (["text"], "checks.yaml, check 1 is not a mapping."),
        ([{**CHECKS[0], "xpath": ""}], "checks.yaml, check 1 has no xpath."),
        (
            [{**CHECKS[0], "file": "other.cid"}],
            "checks.yaml, check 1 reads other.cid, which is not one of the examples.",
        ),
        (
            [{**CHECKS[0], "base": "16"}],
            "checks.yaml, check 1: base is an integer, 16 for hexadecimal.",
        ),
        (
            [{**CHECKS[0], "namespaces": ["scl"]}],
            "checks.yaml, check 1: namespaces maps each prefix to its URI.",
        ),
    ],
)
def test_each_rule_of_a_check_is_held(
    signed_in: TestClient, tmp_path: Path, checks: object, finding: str
) -> None:
    def wrong(root: Path) -> None:
        (root / "checks.yaml").write_text(yaml.safe_dump(checks))

    _import(signed_in, CLUSTER)
    staged = _stage(signed_in, _build_config(tmp_path, change=wrong))

    assert staged["findings"] == [finding]


def test_the_configuration_is_shown_with_the_files_of_the_workload(
    signed_in: TestClient, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    _install(signed_in, _stage(signed_in, _build_config(tmp_path)))

    files = signed_in.get("/api/v1/containers/vied/files").json()["files"]

    [config] = [item for item in files if item["config"]]
    assert config["src"] == "../inventories/vied/site/model.cid"
    assert config["dest"] == "/etc/seapath-containers/vied/model.cid"
    assert config["content"] == "<SCL example/>"


# Applying now


def test_applying_an_update_launches_the_run_that_restarts_the_workload(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)
    _reach_the_members(signed_in, settings, tmp_path)
    _install(signed_in, _stage(signed_in, _build(tmp_path / "one")))

    response = _install(
        signed_in,
        _stage(signed_in, _build(tmp_path / "two", version="vied-2")),
        apply=True,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["commit"]
    run = signed_in.get(f"/api/v1/runs/{body['run_id']}").json()
    assert run["playbook_id"] == "deploy_containers_cluster"
    assert run["variables"] == {"deploy_containers_cluster_restart": "vied"}


def test_resetting_wins_over_applying(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)
    _reach_the_members(signed_in, settings, tmp_path)
    _install(signed_in, _stage(signed_in, _build(tmp_path / "one")))

    body = _install(
        signed_in,
        _stage(signed_in, _build(tmp_path / "two", version="vied-2")),
        apply=True,
        recreate=True,
    ).json()

    run = signed_in.get(f"/api/v1/runs/{body['run_id']}").json()
    assert run["variables"] == {"deploy_containers_cluster_recreate": "vied"}


def test_site_values_saved_and_applied_launch_the_restarting_run(
    signed_in: TestClient, tmp_path: Path, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)
    _reach_the_members(signed_in, settings, tmp_path)
    _install(signed_in, _stage(signed_in, _build(tmp_path)))

    response = signed_in.put(
        "/api/v1/containers/vied/values",
        json={"values": {**SITE, "clock": 1}, "apply": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit"]
    run = signed_in.get(f"/api/v1/runs/{body['run_id']}").json()
    assert run["variables"] == {"deploy_containers_cluster_restart": "vied"}


def test_a_workload_to_restart_is_one_the_inventory_declares(
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    _import(signed_in, CLUSTER)
    _reach_the_members(signed_in, settings, tmp_path)

    response = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "deploy_containers_cluster",
            "variables": {"deploy_containers_cluster_restart": "nothere"},
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_variable"
