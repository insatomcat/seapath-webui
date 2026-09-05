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

import difflib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.inventory.editor import UneditableInventory, add_guest
from app.inventory.fidelity import unintended_changes
from app.inventory.parser import parse
from app.inventory.resolve import resolve
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


# 3. Declaring one, which is a splice like every other write.


def test_a_guest_is_added_to_a_group_the_file_already_has() -> None:
    document = OURS.read_text() + GUESTS

    edited = add_guest(document, "third", {"vm_disk": "../files/third.qcow2"})

    assert resolve(edited)["third"] == {
        "ansible_user": "ansible",
        "vm_disk": "../files/third.qcow2",
    }
    # Everything that was there is still there, byte for byte. The edit is an
    # insertion and nothing else, which is what a splice has to be: a line
    # changed here is a line of somebody's inventory nobody asked to change.
    removed = [
        line
        for line in difflib.ndiff(document.splitlines(), edited.splitlines())
        if line.startswith("- ")
    ]
    assert removed == []


def test_the_group_is_created_where_the_other_groups_live() -> None:
    # Both shapes are valid Ansible and a hand written file uses either. A
    # file keeping its groups under `all.children` gets one more child.
    nested = """
all:
  hosts:
    node1:
      ansible_host: 10.0.0.1
  children:
    standalone_machine:
      hosts:
        node1:
"""
    edited = add_guest(nested, "guest1", {})

    assert "    VMs:\n" in edited
    assert resolve(edited)["guest1"] == {}
    assert list(resolve(edited)) == ["guest1", "node1"]


def test_a_guest_with_no_variables_is_a_name_and_nothing_else() -> None:
    # Adopting a VM that is already running is exactly that entry: the
    # deployment role skips every task that would read a file when the guest
    # exists and carries no `force`, so it needs no image and no XML.
    edited = add_guest(OURS.read_text(), "ABB15", {})

    # The golden file keeps its groups at the top level, so the group it gains
    # sits there too and its host is two levels in.
    assert "    ABB15:\n" in edited
    assert "{}" not in edited


def test_a_name_the_inventory_already_carries_is_refused() -> None:
    with pytest.raises(UneditableInventory):
        add_guest(OURS.read_text(), "seapath-machine", {})


def test_the_write_changes_nothing_but_the_guest_it_declares() -> None:
    # What `fidelity` is asked on every declaration. A splice that landed in
    # the wrong mapping would show up here as a machine changing, and a group
    # that swallowed a neighbour as a variable lost.
    document = OURS.read_text()
    edited = add_guest(document, "ABB15", {"vm_disk": "../files/ABB15.qcow2"})

    divergences = unintended_changes(
        document, edited, {"ABB15": {"vm_disk": "../files/ABB15.qcow2"}}, {"ABB15"}
    )

    assert divergences == []


def test_a_host_that_appears_without_being_asked_for_is_a_refusal() -> None:
    # The check the declaration relies on, asserted from the other side: a
    # write that invents a host in the `VMs` group has invented a VM the next
    # deployment run creates.
    document = OURS.read_text()
    edited = add_guest(document, "ABB15", {})

    divergences = unintended_changes(document, edited, {}, set())

    assert [d.kind for d in divergences] == ["host_added"]
    assert divergences[0].hosts == ["ABB15"]
