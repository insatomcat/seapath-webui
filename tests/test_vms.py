# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The VMs view: the definition, its files, and Pacemaker's line for it.

Three readings that live on three other pages, joined on the name. What makes
the join legitimate is that `vm_manager` names the Pacemaker resource after the
VM, and the VM after its host key in the inventory, so the same string is the
entry, the domain and the resource.

Read only, and the tests hold that: there is no route here that changes a
guest. Its definition is a commit on `/inventory` and its deployment is a run
on `/runs`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.settings import Settings

# The fake cluster runs `vm-guest1` and `vm-guest2`, and `vm-guest3` failed
# where it last ran. Declaring two of the three is what lets one row be a guest
# the cluster runs and the inventory does not.
GUESTS = """
VMs:
  hosts:
    vm-guest1:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest1.qcow2"
    vm-guest3:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest3.qcow2"
      force: true
      enable: false
"""


def _declare(client: TestClient) -> None:
    document = client.get("/api/v1/inventory/raw").text
    response = client.post(
        "/api/v1/inventory/import", json={"document": document + GUESTS}
    )
    assert response.status_code == 200, response.text


def test_a_node_with_no_guest_says_where_one_is_written(
    signed_in: TestClient,
) -> None:
    payload = signed_in.get("/api/v1/vms").json()

    assert payload["guests"] == []
    assert "`VMs` group" in payload["note"]
    # And which playbook would deploy them, which is the seeded machine's mode.
    assert payload["playbook"] == "deploy_vms_standalone"


def test_the_guests_are_the_members_of_the_vms_group(signed_in: TestClient) -> None:
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()

    assert [item["name"] for item in payload["guests"]] == ["vm-guest1", "vm-guest3"]
    guest = payload["guests"][1]
    assert guest["vm_disk"] == "../files/guest3.qcow2"
    # `force` destroys and recreates the guest on the next deployment run, and
    # `enable` decides whether it is started. Both are on the row an operator
    # reads before launching one.
    assert guest["force"] is True
    assert guest["enable"] is False


def test_a_guest_carries_pacemakers_line_for_it(signed_in: TestClient) -> None:
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()
    running, failed = payload["guests"]

    assert running["resource"]["role"] == "started"
    assert running["resource"]["node"] == "seapath-machine"
    assert failed["resource"]["failed"] is True
    assert failed["resource"]["fail_count"] == 3


def test_a_guest_the_cluster_runs_and_the_inventory_ignores_is_named(
    signed_in: TestClient,
) -> None:
    # It keeps running, a convergence will not touch it, and a guest added
    # under that name later would collide with what is already there.
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()

    assert [item["id"] for item in payload["undeclared"]] == ["vm-guest2"]


def test_a_fencing_device_is_not_a_guest(signed_in: TestClient) -> None:
    # The exporter reports every resource of the cluster. Only the ones
    # `vm_manager` created are VMs, and a stonith device listed among them
    # would be the page saying something false about the inventory.
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()

    names = [item["id"] for item in payload["undeclared"]]
    assert not [name for name in names if name.startswith("fence-")]


def test_a_file_no_run_would_find_is_flagged_where_the_guest_is(
    signed_in: TestClient, settings: Settings
) -> None:
    # With `any_errors_fatal`, a copy that cannot find its source ends the
    # deployment on every host at once, three minutes in. The answer belongs
    # before the run.
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()
    missing = {
        reference["value"]
        for guest in payload["guests"]
        for reference in guest["files"]
        if not reference["found"]
    }
    assert "../files/guest1.qcow2" in missing

    # Uploaded to the store that holds the large files, and the same page says
    # so without a commit anywhere.
    (settings.artefacts_dir / "files").mkdir(parents=True, exist_ok=True)
    (settings.artefacts_dir / "files/guest1.qcow2").write_bytes(b"not really an image")

    payload = signed_in.get("/api/v1/vms").json()
    found = {
        reference["value"]: reference
        for guest in payload["guests"]
        for reference in guest["files"]
    }
    assert found["../files/guest1.qcow2"]["found"] is True
    assert found["../files/guest1.qcow2"]["where"] == "artefacts"


def test_the_page_says_where_the_runtime_column_comes_from(
    signed_in: TestClient,
) -> None:
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()

    assert "ha_cluster_exporter" in payload["runtime_note"]
    assert "vm_manager" in payload["runtime_note"]


def test_a_viewer_may_read_the_guests(signed_in_viewer: TestClient) -> None:
    assert signed_in_viewer.get("/api/v1/vms").status_code == 200


def test_the_guests_need_a_session(client: TestClient) -> None:
    assert client.get("/api/v1/vms").status_code == 401
