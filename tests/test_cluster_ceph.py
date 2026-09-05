# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading Ceph from the manager's own Prometheus module.

Ceph is optional in SEAPATH, so half of what these hold is the absent case: a
cluster with local storage has to be told it has no Ceph in a sentence, and a
standby manager has to be walked past rather than read as an empty cluster.

The other half is the reading itself, against the exposition an active manager
publishes: which daemons are up, how full the cluster is, and what Ceph itself
says is wrong with it.
"""

from __future__ import annotations

from app.cluster import ceph, metrics
from app.cluster.exporters import Exposition
from app.cluster.fake import CEPH_EXPORTERS
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService
from app.services.storage import StorageService


def exposition(host: str, text: str) -> Exposition:
    return Exposition(host=host, address=host, series=metrics.parse(text))


def read(text: str, host: str = "ccv1") -> ceph.CephCluster:
    return ceph.CephReader().read(exposition(host, text))


FULL = CEPH_EXPORTERS["seapath-machine"]


# Health


def test_the_health_is_the_word_ceph_uses_for_it() -> None:
    # `1` on a page is a number an operator has to translate, and the whole
    # point of this panel is that it says what `ceph -s` says.
    assert read("ceph_health_status 0\n").health == "HEALTH_OK"
    assert read("ceph_health_status 1\n").health == "HEALTH_WARN"
    assert read("ceph_health_status 2\n").health == "HEALTH_ERR"


def test_what_ceph_says_is_wrong_is_carried_through() -> None:
    # The reason behind the word, which is the thing an operator came for. A
    # release too old to publish the detail answers the health alone, and the
    # panel then says the health and claims nothing about why.
    cluster = read(FULL)

    assert cluster.health == "HEALTH_WARN"
    assert [message.name for message in cluster.messages] == [
        "OSD_DOWN",
        "PG_DEGRADED",
    ]
    assert cluster.messages[0].severity == "HEALTH_WARN"
    assert read("ceph_health_status 1\n").messages == []


# Daemons


def test_a_monitor_out_of_quorum_is_reported_as_one() -> None:
    cluster = read(
        'ceph_mon_metadata{ceph_daemon="mon.a",hostname="ccv1",'
        'ceph_version="ceph version 17.2.7 (x) quincy (stable)"} 1\n'
        'ceph_mon_metadata{ceph_daemon="mon.b",hostname="ccv2",'
        'ceph_version="ceph version 17.2.7 (x) quincy (stable)"} 1\n'
        'ceph_mon_quorum_status{ceph_daemon="mon.a"} 1\n'
        'ceph_mon_quorum_status{ceph_daemon="mon.b"} 0\n'
    )

    assert [(item.name, item.state, item.ok) for item in cluster.monitors] == [
        ("mon.a", "in quorum", True),
        ("mon.b", "out of quorum", False),
    ]
    assert cluster.monitors_in_quorum == 1


def test_a_standby_manager_is_a_healthy_state() -> None:
    # One active manager and two standbys is what cephadm deploys, so a standby
    # drawn as a fault would put a red daemon on every healthy cluster.
    cluster = read(FULL)

    assert [(item.name, item.state) for item in cluster.managers] == [
        ("mgr.elabo1", "standby"),
        ("mgr.seapath-machine", "active"),
    ]
    assert all(manager.ok for manager in cluster.managers)


def test_the_version_is_the_release_out_of_the_banner() -> None:
    # `ceph_version` is the whole `ceph version 17.2.7 (sha) quincy (stable)`
    # line, and what is compared across daemons is the number and the name.
    cluster = read(FULL)

    assert cluster.versions == {"17.2.7 quincy": 11}


# OSDs


def test_an_osd_that_is_down_keeps_its_row_with_its_host_and_device() -> None:
    # The row this page exists for. It is also the row that disappears if the
    # OSD map and the metadata are read as one thing.
    cluster = read(FULL)

    down = [osd for osd in cluster.osds if not osd.up]
    assert [osd.name for osd in down] == ["osd.4"]
    assert down[0].host == "elabo2"
    assert down[0].device_class == "ssd"
    assert down[0].in_cluster is False
    assert cluster.osds_up == 5
    assert cluster.osds_in == 5


def test_an_osd_the_metadata_never_described_is_still_reported() -> None:
    cluster = read('ceph_osd_up{ceph_daemon="osd.7"} 0\n')

    assert [osd.name for osd in cluster.osds] == ["osd.7"]
    assert cluster.osds[0].id == 7
    assert cluster.osds[0].up is False
    assert cluster.osds[0].used_ratio is None


# Capacity and placement groups


def test_the_capacity_is_the_ratio_an_operator_reads_against_ceph_s_thresholds() -> (
    None
):
    cluster = read(FULL)

    assert cluster.total_bytes == 3298534883328
    assert round(cluster.used_ratio, 2) == 0.2


def test_a_group_in_two_states_is_counted_once() -> None:
    # Degraded and undersized at the same time is the ordinary shape of a
    # cluster with an OSD down. Adding the states up reported sixty-four groups
    # in trouble on a cluster that has thirty-two.
    cluster = read(FULL)

    assert cluster.placement_groups == 192
    assert cluster.pg_states == {
        "active": 192,
        "clean": 160,
        "degraded": 32,
        "undersized": 32,
    }
    assert cluster.pgs_not_clean == 32


def test_the_states_no_group_is_in_are_left_out() -> None:
    # A cluster at rest publishes twenty-odd families at zero, and a table of
    # zeroes buries the one line that is not one.
    cluster = read(FULL)

    assert "peering" not in cluster.pg_states


# Pools


def test_a_pool_is_named_from_its_metadata_and_filled_from_its_series() -> None:
    cluster = read(FULL)

    assert [pool.name for pool in cluster.pools] == ["cephfs", "rbd"]
    pool = cluster.pools[1]
    assert pool.id == 1
    assert pool.type == "replicated"
    assert pool.stored_bytes == 219902325555
    assert pool.available_bytes == 733007751577
    assert pool.objects == 52481


# Finding the manager


def test_only_the_active_manager_is_read() -> None:
    # A standby answers the request and publishes nothing, which is how the
    # active one is found without asking Ceph who it is.
    assert ceph.reporting(exposition("ccv1", FULL)) is True
    assert ceph.reporting(exposition("ccv2", "# standby\n")) is False


def test_the_service_walks_past_the_standby_to_the_manager(signed_in) -> None:
    service = signed_in.app.state.storage_service

    cluster = service.ceph()

    assert cluster.available is True
    assert cluster.source == "seapath-machine"
    assert cluster.health == "HEALTH_WARN"


def test_a_cluster_with_no_ceph_is_told_so_in_a_sentence(tmp_path, reader) -> None:
    # Local storage is a supported SEAPATH configuration, so this has to read
    # as a fact about the cluster and never as a fault of it.
    inventory = InventoryService(InventoryRepository(tmp_path / "inventory"), reader)
    inventory.ensure_seed()
    service = StorageService(inventory, client=_Silent(), port=ceph.DEFAULT_PORT)

    cluster = service.ceph()

    assert cluster.available is False
    assert "local storage" in cluster.error
    assert "cluster_setup_cephadm" in cluster.error


class _Silent:
    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        return None, "Connection refused"
