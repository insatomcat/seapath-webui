# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The PTP wizard: where its variables land, and what it refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.inventory.editor import edit, set_variables
from app.inventory.fidelity import unintended_changes
from app.inventory.ptp import (
    PORT_ENTRY,
    PtpRefused,
    PtpSetup,
    plan,
    port_network,
    survey,
    vlan_netdev,
)
from app.inventory.resolve import resolve

GOLDEN = Path(__file__).parent / "golden"

CLUSTER = """\
all:
  hosts:
    node1:
      ansible_host: 10.0.0.1
    node2:
      ansible_host: 10.0.0.2
    node3:
      ansible_host: 10.0.0.3
      ptp_interface: eno9 # a different card
  children:
    hypervisors:
      hosts:
        node1:
        node2:
        node3:
      vars:
        network_interface: eno1
        custom_network:
          10-extra:
            - Match:
                - Name: eno7
    VMs:
      hosts:
        guest1:
          vm_template: guest.xml.j2
"""

THREE = ["node1", "node2", "node3"]


def applied(document: str, setup: PtpSetup) -> str:
    """The file the plan produces, held to the fidelity check a commit gets."""
    planned = plan(document, setup)
    edited = document
    for scope, variables in planned.writes:
        edited = set_variables(edited, scope, variables)
    dropped = {host: dict.fromkeys(names) for host, names in planned.removals.items()}
    if dropped:
        edited = edit(edited, dropped)
    assert unintended_changes(document, edited, planned.intended) == []
    return edited


def test_a_group_setup_writes_the_recipe_once_beside_the_sites_entries() -> None:
    edited = applied(
        CLUSTER,
        PtpSetup(
            group="hypervisors",
            interfaces=dict.fromkeys(THREE, "eno12419"),
            vlan=800,
        ),
    )
    hosts = resolve(edited)

    for host in THREE:
        assert hosts[host]["ptp_interface"] == "eno12419"
        assert hosts[host]["ptp_vlanid"] == 800
        # The site's own entry survives: Ansible replaces a dictionary, so the
        # PTP entries have to go into the one the machines already receive.
        assert hosts[host]["custom_network"] == {
            "10-extra": [{"Match": [{"Name": "eno7"}]}],
            PORT_ENTRY: port_network(),
        }
        assert hosts[host]["custom_netdev"] == {"00-vlan800": vlan_netdev()}
    # The override on node3 would have hidden the group's port, so it goes.
    assert "eno9" not in edited
    assert edited.count("ptp_vlanid:") == 1
    assert "guest1" in edited and "ptp" not in resolve(edited)["guest1"]


def test_different_ports_on_a_group_are_written_on_each_machine() -> None:
    edited = applied(
        CLUSTER,
        PtpSetup(
            group="hypervisors",
            interfaces={"node1": "eno1", "node2": "eno2", "node3": "eno3"},
            vlan=800,
        ),
    )
    hosts = resolve(edited)

    assert [hosts[host]["ptp_interface"] for host in THREE] == ["eno1", "eno2", "eno3"]
    assert edited.count("ptp_vlanid:") == 1


def test_chosen_machines_each_receive_their_own_copy() -> None:
    edited = applied(CLUSTER, PtpSetup(interfaces={"node1": "eno5"}, vlan=100))
    hosts = resolve(edited)

    assert hosts["node1"]["ptp_vlanid"] == 100
    assert "10-extra" in hosts["node1"]["custom_network"]
    assert PORT_ENTRY in hosts["node1"]["custom_network"]
    # The others are untouched, the group's dictionary included.
    assert PORT_ENTRY not in hosts["node2"]["custom_network"]
    assert "ptp_vlanid" not in hosts["node2"]


def test_running_it_again_replaces_the_previous_vlan_device() -> None:
    first = applied(
        CLUSTER,
        PtpSetup(
            group="hypervisors", interfaces=dict.fromkeys(THREE, "eno5"), vlan=800
        ),
    )
    second = applied(
        first,
        PtpSetup(
            group="hypervisors", interfaces=dict.fromkeys(THREE, "eno5"), vlan=801
        ),
    )

    netdev = resolve(second)["node1"]["custom_netdev"]
    assert list(netdev) == ["00-vlan801"]


def test_going_untagged_takes_the_vlan_and_its_device_out() -> None:
    tagged = applied(
        CLUSTER,
        PtpSetup(
            group="hypervisors", interfaces=dict.fromkeys(THREE, "eno5"), vlan=800
        ),
    )
    untagged = applied(
        tagged, PtpSetup(group="hypervisors", interfaces=dict.fromkeys(THREE, "eno5"))
    )
    hosts = resolve(untagged)

    assert "ptp_vlanid" not in hosts["node1"]
    assert "custom_netdev" not in hosts["node1"]
    assert list(hosts["node1"]["custom_network"]) == ["10-extra"]


def test_a_machine_with_its_own_dictionary_receives_the_entries_there() -> None:
    document = CLUSTER.replace(
        "      ptp_interface: eno9 # a different card\n",
        "      custom_netdev:\n        50-mine:\n          - NetDev:\n"
        "              - Name: br9\n",
    )
    edited = applied(
        document,
        PtpSetup(group="hypervisors", interfaces=dict.fromkeys(THREE, "eno5"), vlan=7),
    )

    assert set(resolve(edited)["node3"]["custom_netdev"]) == {"50-mine", "00-vlan7"}
    assert set(resolve(edited)["node1"]["custom_netdev"]) == {"00-vlan7"}


def test_a_group_must_be_answered_for_every_machine_it_holds() -> None:
    with pytest.raises(PtpRefused, match="node3"):
        plan(
            CLUSTER,
            PtpSetup(
                group="hypervisors", interfaces={"node1": "eno5", "node2": "eno5"}
            ),
        )


def test_a_group_holding_guests_is_refused() -> None:
    with pytest.raises(PtpRefused, match="guests"):
        plan(CLUSTER, PtpSetup(group="all", interfaces=dict.fromkeys(THREE, "eno5")))


def test_a_guest_is_refused() -> None:
    with pytest.raises(PtpRefused, match="guest"):
        plan(CLUSTER, PtpSetup(interfaces={"guest1": "eth0"}))


def test_a_name_the_kernel_would_refuse_is_refused() -> None:
    with pytest.raises(PtpRefused, match="not an interface name"):
        plan(CLUSTER, PtpSetup(interfaces={"node1": "a name/with slash"}))


def test_a_dictionary_shared_with_machines_left_out_is_refused() -> None:
    # zz_site is applied after hypervisors, so node1 receives custom_network
    # from it, and so does node4, which is not a hypervisor.
    document = CLUSTER.replace(
        "    VMs:\n",
        "    zz_site:\n      hosts:\n        node1:\n        node4:\n"
        "      vars:\n        custom_network:\n          20-site: []\n    VMs:\n",
    )
    with pytest.raises(PtpRefused, match="node4"):
        plan(
            document,
            PtpSetup(
                group="hypervisors", interfaces=dict.fromkeys(THREE, "eno5"), vlan=5
            ),
        )


def test_a_value_on_a_group_that_overrides_the_chosen_one_is_refused() -> None:
    document = CLUSTER.replace(
        "    VMs:\n",
        "    zz_site:\n      hosts:\n        node1:\n      vars:\n"
        "        ptp_vlanid: 3\n    VMs:\n",
    )
    with pytest.raises(PtpRefused, match="zz_site"):
        plan(
            document,
            PtpSetup(
                group="hypervisors", interfaces=dict.fromkeys(THREE, "eno5"), vlan=9
            ),
        )


def test_the_survey_says_what_each_machine_receives_and_where() -> None:
    found = survey(CLUSTER)

    assert [machine.host for machine in found.machines] == THREE
    assert found.machines[2].interface == "eno9"
    assert found.machines[2].interface_on == "the machine"
    assert found.machines[0].interface is None
    # `all` holds the guest, so it is not offered.
    assert [group.name for group in found.groups] == ["hypervisors"]


def test_the_adopted_cluster_takes_the_recipe_on_its_group() -> None:
    document = (GOLDEN / "adopted-cluster.yaml").read_text()
    edited = applied(
        document,
        PtpSetup(
            group="cluster_machines",
            interfaces=dict.fromkeys(["node1", "node2", "node3"], "eno12419"),
            vlan=800,
        ),
    )
    assert "00-vlan800:" in edited


# The API


def test_the_wizard_previews_then_commits(signed_in: TestClient) -> None:
    before = signed_in.get("/api/v1/inventory").json()["commit"]
    setup = {"interfaces": {"seapath-machine": "eno12419"}, "vlan": 800}

    preview = signed_in.post("/api/v1/inventory/ptp/preview", json=setup)
    assert preview.status_code == 200
    assert "+      ptp_vlanid: 800" in preview.text
    assert "00-vlan800" in preview.text
    assert signed_in.get("/api/v1/inventory").json()["commit"] == before

    committed = signed_in.post(
        "/api/v1/inventory/ptp", json=setup, headers={"If-Match": before}
    )
    assert committed.status_code == 200
    assert committed.json()["message"].startswith(
        "time: PTP on eno12419, VLAN 800, for seapath-machine"
    )

    found = signed_in.get("/api/v1/inventory/ptp").json()
    assert found["machines"][0]["interface"] == "eno12419"
    assert found["machines"][0]["vlan_entries"] is True


def test_a_stale_wizard_is_refused(signed_in: TestClient) -> None:
    stale = signed_in.get("/api/v1/inventory").json()["commit"]
    signed_in.post(
        "/api/v1/inventory/ptp",
        json={"interfaces": {"seapath-machine": "eno1"}},
        headers={"If-Match": stale},
    )

    response = signed_in.post(
        "/api/v1/inventory/ptp",
        json={"interfaces": {"seapath-machine": "eno2"}},
        headers={"If-Match": stale},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_write"


def test_a_refused_setup_says_why(signed_in: TestClient) -> None:
    response = signed_in.post(
        "/api/v1/inventory/ptp/preview",
        json={"interfaces": {"nowhere": "eno1"}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ptp_refused"


def test_a_viewer_may_preview_and_not_commit(signed_in_viewer: TestClient) -> None:
    setup = {"interfaces": {"seapath-machine": "eno1"}}
    assert (
        signed_in_viewer.post("/api/v1/inventory/ptp/preview", json=setup).status_code
        == 200
    )
    assert signed_in_viewer.post("/api/v1/inventory/ptp", json=setup).status_code == 403
