# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The `VMs` group, read as guests rather than as machines.

An inventory holds two kinds of entry. `cluster_machines`, `standalone_machine`
and `hypervisors` describe machines, which this service reaches over SSH,
scrapes an exporter on, and holds against the rules that describe a hypervisor.
`VMs` describes guests, which `deploy_vms_cluster` and `deploy_vms_standalone`
loop over and which nothing here connects to.

Reading the second as the first has a cost the operator meets immediately. A
site bringing the inventory of a standalone deployment that runs two VMs
arrived here with three machines, two of them without an administration
interface, and the import was refused by the rule that a standalone inventory
describes exactly one machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.inventory.parser import parse
from app.inventory.validation import validate

GOLDEN = Path(__file__).parent / "golden"
OURS = GOLDEN / "standalone.yaml"

REFERENCE = Path.home() / "dev/seapath-ansible/inventories/examples"

# The shape of the upstream example, which is what a site writes: the two file
# references, one variable the guest template reads, and one this service has
# never heard of.
GUESTS = """
VMs:
  hosts:
    rtvm:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest.qcow2"
      force: true
      vm_features: ["rt", "isolated"]
      cpuset: [4, 5]
    guest2:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest.qcow2"
  vars:
    ansible_user: ansible
"""


def _with_guests() -> str:
    return OURS.read_text() + GUESTS


# 1. Parsing.


def test_a_member_of_the_vms_group_is_a_guest_and_not_a_machine() -> None:
    inventory = parse(_with_guests())

    assert inventory.host_names() == ["seapath-machine"]
    # By name, the way the resolver returns every host of a file.
    assert inventory.guest_names() == ["guest2", "rtvm"]


def test_a_standalone_inventory_with_guests_still_validates() -> None:
    # The regression this module exists for. Counting the guests as machines
    # made `standalone_is_one_machine` refuse the import of every real
    # standalone inventory that runs a VM.
    result = validate(parse(_with_guests()))

    assert result.valid, [f.rule for f in result.errors()]


def test_the_files_a_guest_names_are_read_off_its_entry() -> None:
    guest = parse(_with_guests()).guests["rtvm"]

    assert guest.vm_disk == "../files/guest.qcow2"
    assert guest.vm_template == "../templates/vm/guest.xml.j2"


def test_the_guest_defaults_are_the_ones_the_roles_apply() -> None:
    # `force` destroys and recreates an existing guest, so its default has to
    # be the role's own: `virsh undefine --nvram` is not a default this service
    # gets to choose.
    guest = parse(_with_guests()).guests["guest2"]

    assert guest.force is False
    assert guest.enable is True
    assert parse(_with_guests()).guests["rtvm"].force is True


def test_a_variable_this_service_does_not_model_survives_on_a_guest() -> None:
    # `guest.xml.j2` alone reads some thirty variables. Modelling them would be
    # this service inventing an interface over a template a site is expected to
    # replace, so they are carried whole.
    guest = parse(_with_guests()).guests["rtvm"]

    assert guest.extra["vm_features"] == ["rt", "isolated"]
    assert guest.extra["cpuset"] == [4, 5]
    # Read resolved, group variables included, the way a machine's are.
    assert guest.extra["ansible_user"] == "ansible"


def test_the_group_is_found_under_all_children_too() -> None:
    nested = """
    all:
      hosts:
        seapath-machine:
          ansible_host: 192.168.200.125
          network_interface: eno1
          admin_user: admin
      children:
        standalone_machine:
          hosts:
            seapath-machine:
        VMs:
          hosts:
            rtvm:
              vm_disk: "../files/guest.qcow2"
    """
    inventory = parse(nested)

    assert inventory.host_names() == ["seapath-machine"]
    assert inventory.guest_names() == ["rtvm"]


@pytest.mark.skipif(
    not REFERENCE.is_dir(),
    reason="the seapath-ansible checkout is not next to this one",
)
def test_the_reference_vm_inventory_reads_as_guests() -> None:
    # `seapath-vm-deployement.yaml` is the file upstream tells a site to copy,
    # and it is what this service has to survive meeting.
    document = (REFERENCE / "seapath-standalone.yaml").read_text() + (
        REFERENCE / "seapath-vm-deployement.yaml"
    ).read_text().split("---", 1)[1]

    inventory = parse(document)

    assert inventory.guest_names() == ["rtvm", "vm"]
    assert "rtvm" not in inventory.hosts


# 2. Through the API, which is where the refusal was seen.


def test_an_inventory_that_declares_guests_is_accepted(
    signed_in: TestClient, settings: Settings
) -> None:
    document = _with_guests()

    response = signed_in.post("/api/v1/inventory/import", json={"document": document})

    assert response.status_code == 200
    assert response.json()["hosts"] == ["seapath-machine"]
    assert (settings.inventory_dir / "inventory.yaml").read_text() == document
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"] == (
        "inventory: import a standalone inventory of seapath-machine " "and 2 guests"
    )


def test_editing_a_machine_leaves_the_guests_where_they_are(
    signed_in: TestClient, settings: Settings
) -> None:
    # The file is the renderer's own output plus a `VMs` group, which is the
    # case that would rewrite it wholesale: a file this service produced is
    # rendered from the model, and the model does not render guests. It stops
    # being the renderer's output the moment a guest is in it, so the save is
    # an edit and the group survives.
    signed_in.post("/api/v1/inventory/import", json={"document": _with_guests()})
    commit = signed_in.get("/api/v1/inventory").json()["commit"]

    response = signed_in.patch(
        "/api/v1/inventory/hosts/seapath-machine",
        json={"changes": {"gateway_addr": "192.168.200.254"}},
        headers={"If-Match": commit},
    )

    assert response.status_code == 200
    written = (settings.inventory_dir / "inventory.yaml").read_text()
    assert "gateway_addr: 192.168.200.254" in written
    assert written.endswith(GUESTS)
