# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Which version of this service the inventory asks for.

The other half of D23. The collection is a file a node can be handed; this
service is an image, and replacing it is a change to the machine, so it is a
variable and an apply. What is tested here is the part that belongs to a node:
saying what the inventory names, what answers, and whether the two agree.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.runs import catalogue
from app.runs.fake import FakeRunAdapter
from app.runs.models import RunRecord, RunState
from app.runs.store import RunStore
from app.services.registry import FakeTagSource

_INVENTORY = """
all:
  vars:
    seapath_webui_image: {image}
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      network_interface: eno1
"""


# The same file with the variable taken out, which is what a site that removed
# it has, and what every inventory written before the seed carried it has.
_INVENTORY_WITHOUT_IMAGE = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      network_interface: eno1
"""


def _with_image(client: TestClient, image: str) -> None:
    response = client.put(
        "/api/v1/inventory/raw", json={"document": _INVENTORY.format(image=image)}
    )
    assert response.status_code == 200, response.text


def _interrupted(root, playbook_id: str) -> RunRecord:
    store = RunStore(root)
    record = RunRecord(
        id="20260904-abcdef",
        playbook=f"seapath.ansible.{playbook_id}",
        playbook_id=playbook_id,
        launched_by="admin",
        state=RunState.RUNNING,
        started_at=datetime.now(tz=UTC),
    )
    store.create(record)
    store.save(record)
    return store.reconcile()[0]


def test_the_seeded_machine_already_names_the_version_that_answers(
    signed_in: TestClient,
) -> None:
    # The ordinary state of a machine nobody has edited. The seed reads the
    # image from the quadlet the machine boots on and resolves the moving tag
    # the ISO installs to the version answering, so the inventory says which
    # code this node is meant to run without anybody typing it.
    body = signed_in.get("/api/v1/node/update").json()

    assert body["running"] == __version__
    assert body["wanted"] == __version__
    assert body["pending"] is False


def test_an_inventory_naming_no_image_asks_for_nothing(signed_in: TestClient) -> None:
    # Saying "up to date" here would be inventing an answer.
    response = signed_in.put(
        "/api/v1/inventory/raw", json={"document": _INVENTORY_WITHOUT_IMAGE}
    )
    assert response.status_code == 200, response.text

    body = signed_in.get("/api/v1/node/update").json()

    assert body["running"] == __version__
    assert body["wanted"] is None
    assert body["pending"] is False
    assert "seapath_webui_image" in body["reason"]


def test_the_image_the_inventory_names_is_read_for_this_machine(
    signed_in: TestClient,
) -> None:
    # Set once under `all`, which is how a fleet pins a version. The resolver
    # applies group variables before host variables, the way Ansible does.
    _with_image(signed_in, "docker.io/insatomcat/seapath-webui:9.9.9")

    body = signed_in.get("/api/v1/node/update").json()

    assert body["wanted"] == "9.9.9"
    assert body["pending"] is True
    assert body["image"] == "docker.io/insatomcat/seapath-webui:9.9.9"


def test_the_version_that_answers_and_the_one_asked_for_can_agree(
    signed_in: TestClient,
) -> None:
    _with_image(signed_in, f"docker.io/insatomcat/seapath-webui:{__version__}")

    body = signed_in.get("/api/v1/node/update").json()

    assert body["wanted"] == __version__
    assert body["pending"] is False


@pytest.mark.parametrize(
    "image",
    [
        # A registry port is a colon as well, and the tag is the one after the
        # last slash.
        "registry.substation.local:5000/seapath-webui",
        # A digest carries no version to compare against.
        "docker.io/insatomcat/seapath-webui@sha256:" + "a" * 64,
    ],
)
def test_a_reference_with_no_readable_tag_says_so(
    signed_in: TestClient, image: str
) -> None:
    _with_image(signed_in, image)

    body = signed_in.get("/api/v1/node/update").json()

    assert body["wanted"] is None
    assert body["pending"] is False
    assert body["image"] == image


def test_reading_the_update_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/v1/node/update").status_code == 401


def test_a_viewer_reads_it(signed_in_viewer: TestClient) -> None:
    # Which version is answering is a reading, like the rest of the node view.
    assert signed_in_viewer.get("/api/v1/node/update").status_code == 200


def test_the_catalogue_entry_that_applies_it_says_what_it_costs() -> None:
    # The run that replaces this service is recorded by the service being
    # replaced, so it ends without a final status. The entry says so before an
    # operator confirms, rather than after.
    entry = catalogue.get("seapath_setup_deploy_seapath_webui")

    assert entry is not None
    assert entry.restarts_service is True
    assert "without a final status" in entry.disruption
    assert "seapath_webui_image" in entry.notes


def test_a_run_that_replaced_this_service_is_reported_as_that(tmp_path) -> None:
    recovered = _interrupted(tmp_path / "runs", "seapath_setup_deploy_seapath_webui")

    assert recovered.state is RunState.INTERRUPTED
    assert "That is what applying it looks like" in recovered.message


def test_any_other_run_that_never_came_back_still_says_relaunch(tmp_path) -> None:
    recovered = _interrupted(tmp_path / "runs", "seapath_setup_main")

    assert recovered.state is RunState.INTERRUPTED
    assert "relaunchable" in recovered.message


# Two machines naming two repositories, which is the case that decides what a
# pin may touch: a site mirroring the image on its own registry keeps its
# mirror, and only the tag moves.
_TWO_MACHINES = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      network_interface: eno1
      seapath_webui_image: docker.io/insatomcat/seapath-webui:0.3.0
    seapath-second:
      ansible_host: 192.168.200.126
      network_interface: eno1
      seapath_webui_image: registry.substation.local:5000/seapath-webui:0.3.0
cluster_machines:
  hosts:
    seapath-machine:
    seapath-second:
"""


def _with_document(client: TestClient, document: str) -> None:
    response = client.put("/api/v1/inventory/raw", json={"document": document})
    assert response.status_code == 200, response.text


def _image_of(client: TestClient, machine: str) -> str:
    payload = client.get("/api/v1/inventory").json()
    return payload["inventory"]["hosts"][machine]["extra"]["seapath_webui_image"]


def test_the_registry_is_asked_about_the_repository_the_inventory_names(
    signed_in: TestClient, tag_source: FakeTagSource
) -> None:
    # The repository comes from the inventory, because a site builds and hosts
    # its own image. "Is there a newer one" is a question about the repository
    # the machines actually pull from.
    _with_image(signed_in, "registry.substation.local:5000/seapath-webui:0.3.0")
    tag_source.tags_returned = ["latest", "0.3.0", "0.4.0"]

    body = signed_in.get("/api/v1/node/update/latest").json()

    assert tag_source.asked == ["registry.substation.local:5000/seapath-webui"]
    assert body["repository"] == "registry.substation.local:5000/seapath-webui"
    assert body["latest"] == "0.4.0"
    assert body["pinned"] == "0.3.0"
    assert body["newer"] is True


def test_versions_are_compared_as_numbers_and_not_as_text(
    signed_in: TestClient, tag_source: FakeTagSource
) -> None:
    _with_image(signed_in, "docker.io/insatomcat/seapath-webui:0.3.9")
    tag_source.tags_returned = ["0.3.9", "0.3.10"]

    assert signed_in.get("/api/v1/node/update/latest").json()["latest"] == "0.3.10"


def test_a_moving_tag_is_never_offered_as_a_version(
    signed_in: TestClient, tag_source: FakeTagSource
) -> None:
    # `latest` is in every repository and names no version. Pinning a machine
    # to it is what the seed already refuses to do.
    _with_image(signed_in, "docker.io/insatomcat/seapath-webui:0.3.0")
    tag_source.tags_returned = ["latest", "stable", "main"]

    body = signed_in.get("/api/v1/node/update/latest").json()

    assert body["latest"] is None
    assert body["newer"] is False
    assert "no tag naming a version" in body["reason"]


def test_a_registry_that_cannot_be_reached_says_so_and_breaks_nothing(
    signed_in: TestClient, tag_source: FakeTagSource
) -> None:
    # A substation hypervisor may have no route off its administration
    # network. That deployment is supported, and this page keeps working.
    _with_image(signed_in, "docker.io/insatomcat/seapath-webui:0.3.0")
    tag_source.failure = "registry-1.docker.io could not be reached: timed out."

    response = signed_in.get("/api/v1/node/update/latest")

    assert response.status_code == 200
    assert response.json()["reason"] == tag_source.failure
    assert response.json()["newer"] is False


def test_an_image_pinned_by_digest_has_no_repository_to_ask_about(
    signed_in: TestClient, tag_source: FakeTagSource
) -> None:
    _with_image(signed_in, "docker.io/insatomcat/seapath-webui@sha256:" + "a" * 64)

    body = signed_in.get("/api/v1/node/update/latest").json()

    assert tag_source.asked == []
    assert body["repository"] is None
    assert body["newer"] is False


def test_pinning_moves_the_tag_on_every_machine_and_keeps_each_repository(
    signed_in: TestClient,
) -> None:
    # A run plays the whole inventory and the role templates each machine's
    # quadlet from its own variable, so pinning this node alone would converge
    # a cluster onto two versions of this service.
    _with_document(signed_in, _TWO_MACHINES)

    response = signed_in.post("/api/v1/node/update", json={"version": "0.4.0"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["machines"] == ["seapath-machine", "seapath-second"]
    assert body["playbook"] == "seapath_setup_deploy_seapath_webui"
    assert (
        _image_of(signed_in, "seapath-machine")
        == "docker.io/insatomcat/seapath-webui:0.4.0"
    )
    assert (
        _image_of(signed_in, "seapath-second")
        == "registry.substation.local:5000/seapath-webui:0.4.0"
    )


def test_pinning_changes_no_machine(
    signed_in: TestClient, run_adapter: FakeRunAdapter
) -> None:
    # The rule the whole service is built on. A pin is a commit, and a machine
    # changes when a playbook changes it, which is a second act an operator
    # confirms.
    _with_document(signed_in, _TWO_MACHINES)

    signed_in.post("/api/v1/node/update", json={"version": "0.4.0"})

    assert run_adapter.requests == []


def test_the_commit_a_pin_writes_names_the_version_and_the_machines(
    signed_in: TestClient,
) -> None:
    # The inventory is the audit trail, and "update inventory" forty times over
    # is not one.
    _with_document(signed_in, _TWO_MACHINES)

    signed_in.post("/api/v1/node/update", json={"version": "0.4.0"})

    latest = signed_in.get("/api/v1/inventory/history").json()[0]
    assert latest["message"] == "webui: run 0.4.0 on seapath-machine, seapath-second"
    assert latest["author"] == "admin"


def test_a_machine_pinned_by_digest_is_left_as_it_is(signed_in: TestClient) -> None:
    # Somebody decided on an exact image for that machine. A tag written over
    # it would undo that decision silently.
    digest = "docker.io/insatomcat/seapath-webui@sha256:" + "a" * 64
    _with_document(
        signed_in,
        _TWO_MACHINES.replace(
            "registry.substation.local:5000/seapath-webui:0.3.0", digest
        ),
    )

    body = signed_in.post("/api/v1/node/update", json={"version": "0.4.0"}).json()

    assert body["machines"] == ["seapath-machine"]
    assert _image_of(signed_in, "seapath-second") == digest


def test_an_inventory_naming_no_image_has_nothing_to_pin(
    signed_in: TestClient,
) -> None:
    _with_document(signed_in, _INVENTORY_WITHOUT_IMAGE)

    response = signed_in.post("/api/v1/node/update", json={"version": "0.4.0"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_image_variable"


@pytest.mark.parametrize("version", ["", "../../etc/passwd", "0.4.0 && reboot"])
def test_a_version_that_is_not_a_tag_is_refused(
    signed_in: TestClient, version: str
) -> None:
    # What goes into the variable is a reference a machine will pull. Anything
    # else is refused here rather than discovered by a role.
    _with_document(signed_in, _TWO_MACHINES)

    response = signed_in.post("/api/v1/node/update", json={"version": version})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_version"


def test_pinning_is_an_administrators_act(signed_in_viewer: TestClient) -> None:
    # A viewer reads which versions exist and writes none of them.
    assert signed_in_viewer.get("/api/v1/node/update/latest").status_code == 200
    assert (
        signed_in_viewer.post(
            "/api/v1/node/update", json={"version": "0.4.0"}
        ).status_code
        == 403
    )
