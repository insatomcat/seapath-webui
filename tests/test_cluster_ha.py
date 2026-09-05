# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading Pacemaker and Corosync from `ha_cluster_exporter`.

The exposition is written by another program on another machine, so what these
hold is that the reader survives it: the two shapes the status series comes in,
Pacemaker's INFINITY where JSON expects a number, a member that disagrees with
the coordinator, and a machine whose exporter is up and in no cluster at all.
"""

from __future__ import annotations

from app.cluster import ha, metrics
from app.cluster.exporters import Exposition, read_all
from app.cluster.fake import FakeMetricsClient
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService
from app.services.cluster import ClusterService


class Silent:
    """Every machine unreachable, which is a cluster that has not been built."""

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        return None, "Connection refused"


def exposition(host: str, text: str) -> Exposition:
    return Exposition(host=host, address=host, series=metrics.parse(text))


def read(text: str, host: str = "ccv1") -> ha.PacemakerCluster:
    return ha.PacemakerReader().read(exposition(host, text))


# The node list


def test_a_node_carries_every_status_and_leads_with_the_worst() -> None:
    # A healthy coordinator is online and expected_up and dc at once, and a
    # node in standby is still online: the page has one word to print, and
    # printing "online" for a machine nobody may put a VM on is the wrong one.
    cluster = read(
        'ha_cluster_pacemaker_nodes{node="ccv1",type="member",status="online"} 1\n'
        'ha_cluster_pacemaker_nodes{node="ccv1",type="member",status="standby"} 1\n'
        'ha_cluster_pacemaker_nodes{node="ccv1",type="member",status="dc"} 1\n'
    )

    node = cluster.nodes[0]
    assert node.state == "standby"
    assert node.online is True
    assert node.dc is True
    assert node.flags == ["dc", "online", "standby"]


def test_a_status_the_exporter_publishes_as_zero_is_not_asserted() -> None:
    # Both shapes are in the wild: one series per status with a zero for the
    # ones that are false, and one series per status that is true. Reading the
    # value rather than the presence of the label covers the pair of them, and
    # without it every node came back unclean.
    cluster = read(
        'ha_cluster_pacemaker_nodes{node="ccv1",type="member",status="online"} 1\n'
        'ha_cluster_pacemaker_nodes{node="ccv1",type="member",status="unclean"} 0\n'
        'ha_cluster_pacemaker_nodes{node="ccv1",type="member",status="standby"} 0\n'
    )

    assert cluster.nodes[0].state == "online"
    assert cluster.nodes[0].flags == ["online"]


def test_a_node_with_no_status_at_all_is_reported_offline() -> None:
    cluster = read(
        'ha_cluster_pacemaker_nodes{node="ccv3",type="member",status="online"} 0\n'
    )

    assert cluster.nodes[0].state == "offline"
    assert cluster.nodes[0].online is False


# Resources


def test_a_clone_keeps_one_row_per_node_it_runs_on() -> None:
    # Keyed by the resource alone, a clone running on three members collapsed
    # to one row and the page reported one of the copies.
    text = "\n".join(
        f'ha_cluster_pacemaker_resources{{node="{node}",resource="ping",'
        f'role="started",managed="true",status="active",agent="ocf::pacemaker:ping",'
        f'group="",clone="ping-clone"}} 1'
        for node in ("ccv1", "ccv2", "ccv3")
    )

    cluster = read(text)

    assert [resource.node for resource in cluster.resources] == [
        "ccv1",
        "ccv2",
        "ccv3",
    ]
    assert {resource.clone for resource in cluster.resources} == {"ping-clone"}


def test_an_infinite_fail_count_is_a_flag_and_not_a_number() -> None:
    # The exporter publishes Pacemaker's INFINITY as `+Inf`, which JSON cannot
    # carry: serialising it produced a body the page could not parse at all,
    # and the whole panel went blank on the one resource worth looking at.
    cluster = read(
        'ha_cluster_pacemaker_resources{node="ccv1",resource="vm-guest1",'
        'role="stopped",managed="true",status="failed",agent="ocf::seapath:'
        'VirtualDomain",group="",clone=""} 1\n'
        'ha_cluster_pacemaker_fail_count{node="ccv1",resource="vm-guest1"} +Inf\n'
    )

    resource = cluster.resources[0]
    assert resource.fail_count_infinite is True
    assert resource.fail_count == 0
    assert resource.failed is True
    assert "Infinity" not in cluster.model_dump_json()


def test_a_resource_leads_with_its_failure_rather_than_with_being_active() -> None:
    cluster = read(
        'ha_cluster_pacemaker_resources{node="ccv1",resource="vm-guest1",'
        'role="started",managed="true",status="active",agent="a",group="",'
        'clone=""} 1\n'
        'ha_cluster_pacemaker_resources{node="ccv1",resource="vm-guest1",'
        'role="started",managed="true",status="failed",agent="a",group="",'
        'clone=""} 1\n'
    )

    assert cluster.resources[0].state == "failed"
    assert cluster.resources[0].failed is True


def test_an_unmanaged_resource_is_reported_as_one() -> None:
    cluster = read(
        'ha_cluster_pacemaker_resources{node="ccv1",resource="vm-guest1",'
        'role="started",managed="false",status="active",agent="a",group="",'
        'clone=""} 1\n'
    )

    assert cluster.resources[0].managed is False


# Constraints and quorum


def test_a_constraint_score_is_written_the_way_crm_writes_it() -> None:
    # What an operator reads from a location rule is whether it is a preference
    # or a prohibition, and 1000000 is neither word.
    cluster = read(
        'ha_cluster_pacemaker_location_constraints{constraint="ban-vm1",'
        'node="ccv2",resource="vm1",role="Started"} -1000000\n'
        'ha_cluster_pacemaker_location_constraints{constraint="prefer-vm2",'
        'node="ccv1",resource="vm2",role="Started"} 100\n'
    )

    assert [item.score for item in cluster.constraints] == ["-INFINITY", "100"]


def test_the_ring_errors_of_every_ring_are_added_up() -> None:
    # One ring per interface, and what an operator needs first is whether any
    # of them is faulty rather than which.
    cluster = read(
        'ha_cluster_corosync_ring_errors{ring_id="0"} 0\n'
        'ha_cluster_corosync_ring_errors{ring_id="1"} 2\n'
        "ha_cluster_corosync_quorate 0\n"
    )

    assert cluster.corosync.ring_errors == 2
    assert cluster.corosync.quorate is False


def test_a_quorum_the_exporter_never_published_stays_unknown() -> None:
    # False would be drawn as a cluster that has lost quorum, which is an
    # evacuation, and an exporter that published nothing has said nothing.
    cluster = read('ha_cluster_pacemaker_nodes{node="ccv1",status="online"} 1\n')

    assert cluster.corosync.quorate is None
    assert cluster.stonith_enabled is None


# Which member is believed


def test_the_coordinator_is_the_member_that_names_itself_dc() -> None:
    text = (
        'ha_cluster_pacemaker_nodes{node="ccv1",type="member",status="dc"} 1\n'
        'ha_cluster_pacemaker_nodes{node="ccv1",type="member",status="online"} 1\n'
    )

    assert ha.is_coordinator(exposition("ccv1", text)) is True
    # The same exposition, fetched from another member. It describes the same
    # cluster, from a machine that does not hold the CIB the cluster acts on.
    assert ha.is_coordinator(exposition("ccv2", text)) is False


def test_an_exporter_on_a_machine_in_no_cluster_is_not_reporting() -> None:
    # A different sentence from a machine that could not be reached, and the
    # page has to tell the two apart: one is a standalone node, the other is a
    # member that is down.
    assert ha.reporting(exposition("ccv1", "up 1\n")) is False
    assert ha.reporting(Exposition("ccv1", "ccv1", None, "No route to host")) is False


# The service, over the fakes


def test_the_service_asks_every_machine_and_reports_the_coordinator_s_view(
    signed_in,
) -> None:
    service = signed_in.app.state.cluster_service
    cluster = service.pacemaker()

    assert cluster.available is True
    assert cluster.from_dc is True
    assert cluster.dc == "seapath-machine"
    assert [node.name for node in cluster.nodes] == [
        "elabo1",
        "elabo2",
        "seapath-machine",
    ]
    # Which machines were asked, members or not: on a cluster half joined, the
    # machine that failed to answer is the finding.
    assert [item.host for item in cluster.reach] == ["seapath-machine"]
    assert cluster.reach[0].reachable is True
    assert cluster.reach[0].reporting is True


def test_a_machine_that_cannot_be_reached_is_a_row_and_not_a_failure() -> None:
    expositions = read_all(
        FakeMetricsClient(), [("nowhere", "10.0.0.9")], ha.DEFAULT_PORT
    )

    assert expositions[0].answered is False
    assert expositions[0].error == "No route to host"


def test_a_standalone_inventory_is_told_it_has_no_cluster(tmp_path, reader) -> None:
    inventory = InventoryService(InventoryRepository(tmp_path / "inventory"), reader)
    inventory.ensure_seed()
    service = ClusterService(inventory, client=Silent(), port=ha.DEFAULT_PORT)

    cluster = service.pacemaker()

    # A sentence rather than an empty table, and one that says what a cluster
    # would take: a standalone machine is a supported SEAPATH configuration.
    assert cluster.available is False
    assert "standalone" in cluster.error
    assert "cluster_setup_ha" in cluster.error
