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
from app.runs.models import RunRecord, RunState
from app.runs.store import RunStore

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
