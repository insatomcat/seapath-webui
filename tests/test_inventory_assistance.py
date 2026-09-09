# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The assistant, and the silence it has to keep on a correct file.

The rule that decides whether this is worth having: a file that came straight
from upstream produces no remark at all. One false positive on a reference
inventory teaches an operator to ignore the whole thing, and the remarks that
matter go with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.inventory.assistance import assist

REFERENCE = Path.home() / "dev/seapath-ansible/inventories/examples"

CLUSTER = """
all:
  hosts:
    node1:
      ansible_host: 192.168.200.121
      network_interface: eno1
  vars:
    admin_user: admin
    subnet: 24
hypervisors:
  hosts:
    node1:
  vars:
    isolcpus: 4-7
VMs:
  hosts:
    rtvm:
      vm_disk: ../files/guest.qcow2
"""


def _names(assistance, kind: str) -> set[str]:
    return {remark.name for remark in assistance.remarks if remark.kind == kind}


# 1. Silence on what is correct.


def test_a_correct_file_produces_no_remark() -> None:
    assert assist(CLUSTER).remarks == []


def test_an_empty_file_produces_no_remark() -> None:
    # The page opens on a node nobody has seeded, and an empty editor is not a
    # place to start listing what is missing.
    assert assist("").remarks == []
    assert assist("").known == 0


@pytest.mark.skipif(
    not REFERENCE.is_dir(),
    reason="the seapath-ansible checkout is not next to this one",
)
@pytest.mark.parametrize(
    "name",
    [
        "seapath-cluster.yaml",
        "seapath-standalone.yaml",
        "seapath-ovs.yaml",
        "seapath-vm-deployement.yaml",
    ],
)
def test_the_reference_inventories_draw_no_remark(name: str) -> None:
    """The test that decides whether the assistant is worth switching on.

    `seapath-vm-deployement.yaml` is the one that caught the first version of
    this: it writes `ansible_host` and `ansible_user` on its guest entries, for
    the play that waits for the guest to answer over SSH, and calling those
    misplaced was a remark about a file upstream ships.
    """
    assistance = assist((REFERENCE / name).read_text())

    assert assistance.remarks == []
    assert assistance.known > 0


# 2. What it does catch.


def test_a_misspelling_is_named_with_what_was_probably_meant() -> None:
    assistance = assist(
        "all:\n  hosts:\n    node1:\n  vars:\n    cephadm_netwrok: 192.168.55.0/24\n"
    )

    assert _names(assistance, "unknown") == {"cephadm_netwrok"}
    assert assistance.remarks[0].suggestion == "cephadm_network"


def test_a_variable_of_a_sites_own_is_reported_without_a_guess() -> None:
    # Legitimate, and indistinguishable from a misspelling without reading the
    # roles. The remark says so rather than inventing a correction.
    assistance = assist("all:\n  hosts:\n    node1:\n  vars:\n    site_own_thing: 1\n")

    assert _names(assistance, "unknown") == {"site_own_thing"}
    assert assistance.remarks[0].suggestion is None


def test_two_variables_that_merely_look_alike_are_not_offered_for_each_other() -> None:
    # `cluster_next_ip_addr` and `cluster_ip_addr` are two addresses on two
    # machines, and a suggestion swapping one for the other would be a wrong
    # answer written with confidence.
    assistance = assist(
        "all:\n  hosts:\n    node1:\n  vars:\n    cluster_nxt_ip_addr: 1.2.3.4\n"
    )

    suggestion = assistance.remarks[0].suggestion
    assert suggestion in (None, "cluster_next_ip_addr")


def test_a_misspelling_written_once_is_reported_once() -> None:
    # Written on `all`, it reaches three machines. Reporting it per machine
    # would read as three mistakes, and there is one.
    document = (
        "all:\n"
        "  hosts:\n    node1:\n    node2:\n    node3:\n"
        "  vars:\n    cephadm_netwrok: 192.168.55.0/24\n"
    )
    assistance = assist(document)

    assert len(assistance.remarks) == 1
    assert assistance.remarks[0].where == ["the whole inventory"]


def test_a_guest_variable_on_a_machine_is_read_by_nothing() -> None:
    document = CLUSTER.replace(
        "      network_interface: eno1\n",
        "      network_interface: eno1\n      vm_disk: ../files/wrong.qcow2\n",
    )

    assistance = assist(document)

    assert _names(assistance, "misplaced") == {"vm_disk"}
    assert assistance.remarks[0].where == ["node1"]


def test_a_machine_variable_on_a_guest_is_read_by_nothing() -> None:
    document = CLUSTER.replace(
        "      vm_disk: ../files/guest.qcow2\n",
        "      vm_disk: ../files/guest.qcow2\n      isolcpus: 4-7\n",
    )

    assert _names(assist(document), "misplaced") == {"isolcpus"}


# 3. Where the guest boundary is, and where it is not.


def test_a_connection_variable_is_at_home_on_a_guest_and_on_a_machine() -> None:
    """Ansible's own variables describe how it reaches a host, not what it is.

    The reference VM inventory writes `ansible_host` on a guest entry so the
    play can wait for the guest over SSH once it is created.
    """
    document = CLUSTER.replace(
        "      vm_disk: ../files/guest.qcow2\n",
        "      vm_disk: ../files/guest.qcow2\n      ansible_host: 10.132.170.8\n",
    )

    assert assist(document).remarks == []


def test_a_guest_variable_on_all_reaches_the_guests_and_is_left_alone() -> None:
    # Untidy, and it does reach them. Reaching a reader is the test, rather
    # than reaching only readers.
    document = CLUSTER.replace(
        "    admin_user: admin\n", "    admin_user: admin\n    vm_disk: ../files/a\n"
    )

    assert _names(assist(document), "misplaced") == set()


def test_a_guest_group_declared_through_children_is_still_guests() -> None:
    # `cluster_VMs` and `standalone_VMs` are how a file that holds both says
    # which playbook creates a guest, and the boundary has to follow them.
    document = """
all:
  hosts:
    node1:
      ansible_host: 192.168.200.121
      network_interface: eno1
VMs:
  children:
    cluster_VMs:
      hosts:
        rtvm:
          isolcpus: 4-7
"""

    assert _names(assist(document), "misplaced") == {"isolcpus"}


def test_an_empty_group_is_judged_by_nothing() -> None:
    # The reference inventories declare empty groups to keep Ansible from
    # warning, and a variable on one reaches no host at all.
    document = CLUSTER + "\nstandalone_machine:\n  vars:\n    isolcpus: 4-7\n"

    assert _names(assist(document), "misplaced") == set()


def test_a_host_variable_on_a_group_is_a_way_of_writing_an_inventory() -> None:
    # `isolcpus` on `hypervisors` is what the reference cluster does. Judging
    # anything but the guest boundary would report the upstream file.
    assert _names(assist(CLUSTER), "misplaced") == set()


# 4. The endpoint, and the switch that decides whether it is asked.


def test_the_assistant_answers_a_viewer(signed_in: TestClient) -> None:
    response = signed_in.post(
        "/api/v1/inventory/raw/assist", json={"document": CLUSTER}
    )

    assert response.status_code == 200
    assert response.json() == {"remarks": [], "known": 6}


def test_the_assistant_reports_rather_than_refuses(signed_in: TestClient) -> None:
    """No remark ever reaches `validate()`, so none of them blocks a commit.

    A variable of a site's own is a legitimate name, and a service that refused
    the commit would be a service a site has to work around.
    """
    document = CLUSTER.replace(
        "    admin_user: admin\n", "    admin_user: admin\n    site_own_thing: 1\n"
    )

    assist_response = signed_in.post(
        "/api/v1/inventory/raw/assist", json={"document": document}
    )
    check = signed_in.post("/api/v1/inventory/raw/check", json={"document": document})

    assert len(assist_response.json()["remarks"]) == 1
    # The rules never mention it, at any level. The assistant and the
    # validation are two answers about one file, and only one of them decides
    # whether it may be committed.
    assert all(
        "site_own_thing" not in finding["message"]
        for finding in check.json()["findings"]
    ), check.json()


def test_the_assistant_needs_a_session(client: TestClient) -> None:
    response = client.post("/api/v1/inventory/raw/assist", json={"document": ""})

    assert response.status_code == 401


def test_a_file_that_is_not_yaml_is_refused_rather_than_guessed_at(
    signed_in: TestClient,
) -> None:
    response = signed_in.post(
        "/api/v1/inventory/raw/assist", json={"document": "all:\n  hosts:\n   - ["}
    )

    assert response.status_code == 400
