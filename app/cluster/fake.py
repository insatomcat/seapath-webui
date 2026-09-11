# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Exporters for three machines that do not exist.

What the whole test suite and the `use_fakes` development switch read. Three
expositions per machine, because a SEAPATH node serves three: the CPU pool and
the real time tuning on `node_exporter`, Pacemaker and Corosync on
`ha_cluster_exporter`, and Ceph on the manager's own module.

Nothing here is healthy on purpose. A cluster where every machine answers the
same thing exercises one branch: one node carries VMs and a slot, one is idle,
one is unreachable, one member is in standby, a VM has failed, an OSD is down.
Those are the readings the pages are built against, and a fake that only ever
answered "everything is fine" would let all of them go untested.
"""

from __future__ import annotations

import time
from pathlib import Path


def _detail(**labels: str) -> str:
    full = {
        "cpu": "0",
        "isolated": "0",
        "ht_pair": "0",
        "ht_sibling": "0",
        "state": "housekeeping",
        "slot": "",
        "member_count": "0",
        "members": "",
        "label": "",
        "group": "",
        "scheduler": "",
        "priority": "0",
    }
    full.update(labels)
    rendered = ",".join(f'{name}="{value}"' for name, value in full.items())
    return "seapath_alloc_cpu_detail{" + rendered + "} 1"


def _units(*states: tuple[str, str]) -> str:
    """What node_exporter's systemd collector publishes about a few units.

    One series per state per unit, exactly one of them 1, which is the shape
    the collector emits. The units are the quadlets the golden inventory
    uploads, so the fake cluster runs the containers that inventory declares.
    """
    lines: list[str] = []
    for unit, current in states:
        for state in ("activating", "active", "deactivating", "failed", "inactive"):
            lines.append(
                f'node_systemd_unit_state{{name="{unit}",state="{state}",'
                f'type="notify"}} {1 if state == current else 0}'
            )
        if current == "active":
            lines.append(
                f'node_systemd_unit_start_time_seconds{{name="{unit}"}} 1772000000'
            )
    return "\n".join(lines) + "\n"


def _uname(release: str, version: str) -> str:
    return (
        f'node_uname_info{{domainname="(none)",machine="x86_64",'
        f'nodename="seapath",release="{release}",sysname="Linux",'
        f'version="{version}"}} 1\n'
    )


def _tuning(
    profile: str = "seapath-rt-host",
    installed: str = "1",
    cmdline: str = (
        "BOOT_IMAGE=/vmlinuz root=UUID=0 isolcpus=3-11 nohz_full=3-11 "
        "rcu_nocbs=3-11 intel_idle.max_cstate=0"
    ),
    runtime: str = "-1",
    thp: str = "never",
    irqs_on_isolated: int = 0,
) -> str:
    """The `seapath_rt_*` block `seapath-alloc` writes beside the pool.

    Rendered as text rather than as a model, because what the reader has to
    survive is an exposition another program wrote: the escaping, the empty
    labels, the families a node may not publish at all.
    """
    lines = [
        f'seapath_rt_tuned_info{{profile="{profile}",'
        f'source="/etc/tuned/active_profile",installed="{installed}"}} 1',
        f'seapath_rt_kernel_cmdline_info{{cmdline="{cmdline}"}} 1',
        f"seapath_rt_sched_rt_runtime_us {runtime}",
        "seapath_rt_sched_rt_period_us 1000000",
        'seapath_rt_hugepages_total{size_kb="1048576",node=""} 16',
        'seapath_rt_hugepages_free{size_kb="1048576",node=""} 8',
        f'seapath_rt_transparent_hugepages_info{{enabled="{thp}",'
        f'defrag="{thp}"}} 1',
        'seapath_rt_smt_info{control="on"} 1',
        "seapath_rt_smt_active 1",
        "seapath_rt_acpi_present 1",
        "seapath_rt_irqs_total 214",
        f"seapath_rt_irqs_on_isolated_cpus {irqs_on_isolated}",
    ]
    for number in range(irqs_on_isolated):
        lines.append(
            f'seapath_rt_irq_on_isolated_info{{irq="{181 + number}",'
            f'name="eno1-TxRx-{number}",cpus="{4 + number}"}} 1'
        )
    return "\n".join(lines) + "\n"


def _machine(occupied: dict[int, dict[str, str]], isolated: range) -> str:
    """Twelve cores, two threads each, in the reference topology."""
    lines = [
        "# HELP seapath_alloc_cpu_detail Per-CPU detail",
        "# TYPE seapath_alloc_cpu_detail gauge",
    ]
    for cpu in range(24):
        core = cpu % 12
        sibling = cpu + 12 if cpu < 12 else cpu - 12
        is_isolated = core in isolated
        extra = occupied.get(cpu, {})
        lines.append(
            _detail(
                cpu=str(cpu),
                isolated="1" if is_isolated else "0",
                ht_pair=str(core),
                ht_sibling=str(sibling),
                state=extra.get("state", "free" if is_isolated else "housekeeping"),
                **{k: v for k, v in extra.items() if k != "state"},
            )
        )
    lines.append(f"seapath_alloc_scrape_timestamp_seconds {time.time() - 4:.3f}")
    return "\n".join(lines) + "\n"


# elabo1: a NIC IRQ in its own slot, and a pinned container beside it.
_NODE1 = _machine(
    {
        3: {
            "state": "irq_slot",
            "slot": "irq12419",
            "label": "irq12419",
            "group": "slot",
            "members": "eno12419/181-189/irq FIFO/50",
            "member_count": "1",
        },
        4: {
            "state": "quadlet",
            "label": "nginxquadlet",
            "group": "quadlet",
            "scheduler": "FIFO",
            "priority": "5",
        },
    },
    range(3, 12),
)

# elabo2: two guests, one of them holding four cores with their HT siblings
# reserved, which is what exclusive_physical looks like from outside.
_NODE2 = _machine(
    {
        3: {
            "state": "irq_slot",
            "slot": "irq12419",
            "label": "irq12419",
            "group": "slot",
            "members": "eno12419/181-189/irq FIFO/50",
            "member_count": "1",
        },
        15: {"state": "vm", "label": "vhostvm", "group": "vhost"},
        4: {"state": "vm", "label": "centos", "group": "CPU 0"},
        5: {"state": "vm", "label": "centos", "group": "CPU 1"},
        6: {"state": "vm", "label": "centos", "group": "CPU 2"},
        7: {"state": "vm", "label": "centos", "group": "CPU 3"},
        8: {"state": "vm", "label": "vm", "group": "CPU 0"},
        9: {"state": "vm", "label": "vm", "group": "CPU 1"},
        10: {"state": "vm", "label": "vm", "group": "CPU 2"},
        11: {"state": "vm", "label": "vhostvm", "group": "vhost"},
        16: {"state": "reserved", "label": "4"},
        17: {"state": "reserved", "label": "5"},
        18: {"state": "reserved", "label": "6"},
        19: {"state": "reserved", "label": "7"},
        20: {"state": "reserved", "label": "8"},
        21: {"state": "reserved", "label": "9"},
        22: {"state": "reserved", "label": "10"},
    },
    range(3, 12),
)

_BUSY = _NODE2 + 'seapath_alloc_active_fallbacks{severity="soft"} 1\n'

# The tuning each machine publishes beside its pool, and the two are as unalike
# as the pools. elabo1 is the machine an inventory describes and a convergence
# reached. elabo2 is the finding this whole reading exists for: transparent
# hugepages left on, RT throttling still enabled, and three interrupts the
# kernel is still allowed to deliver on isolated cores. None of that is visible
# from the pool, and none of it used to be visible from anywhere but the node
# the browser happened to be pointed at.
_NODE1 = (
    _NODE1
    + _uname("6.1.0-rt-amd64", "#1 SMP PREEMPT_RT Debian 6.1.0-1 (2026-01-01)")
    + _tuning()
    # The containers of the golden inventory: the exporter's own quadlet runs
    # on every machine, and the site's nginx is the one Pacemaker holds, so it
    # runs here and is inactive on the other member.
    + _units(("node-exporter.service", "active"), ("nginxquadlet.service", "active"))
)
_BUSY = (
    _BUSY
    + _uname("6.1.0-amd64", "#1 SMP PREEMPT_DYNAMIC Debian 6.1.0-1 (2026-01-01)")
    + _tuning(
        cmdline="BOOT_IMAGE=/vmlinuz root=UUID=0 isolcpus=3-11",
        runtime="950000",
        thp="madvise",
        irqs_on_isolated=3,
    )
    # Its exporter quadlet failed, which is the row the Containers page exists
    # to show: the file was uploaded, the unit exists, and the container is
    # down on one machine of three.
    + _units(("node-exporter.service", "failed"), ("nginxquadlet.service", "inactive"))
)

# Keyed by what the reader actually puts in the URL, which is the inventory's
# `ansible_host`. The fake node the rest of the suite serves lives at
# 192.168.200.125, so that address answers too: keying on hostnames alone left
# the pool pane empty on the development switch, which is the one place it is
# looked at by hand.
EXPORTERS = {
    "192.168.200.125": _NODE1,
    "192.168.200.126": _BUSY,
    "elabo1": _NODE1,
    "elabo2": _BUSY,
    "seapath-machine": _NODE1,
}


# A three node cluster, as ha_cluster_exporter publishes it.
#
# Named after the machines the rest of these fakes serve, so one inventory
# describes one cluster: the node the suite runs as is `seapath-machine`, and
# it is the coordinator, which is what a browser pointed at this node sees.
#
# Deliberately not healthy. A cluster where everything is online, every
# resource is started and nothing has ever failed exercises the one branch that
# needs no page: `elabo2` is in standby, one VM has failed where it last ran,
# and a location constraint pins another. Those are the rows an operator opens
# this view to find.
_CLUSTER_NODES = ("seapath-machine", "elabo1", "elabo2")
_DC = _CLUSTER_NODES[0]


def _pacemaker(dc: str = _DC) -> str:
    """One member's exposition, which describes the whole cluster.

    Parameterised by the coordinator because that is the choice the reader
    makes: every member answers, and the one that names itself DC is believed.
    """
    lines = [
        "# HELP ha_cluster_pacemaker_nodes Cluster nodes",
        "# TYPE ha_cluster_pacemaker_nodes gauge",
    ]
    for index, node in enumerate(_CLUSTER_NODES):
        statuses = {
            "online": 1,
            "expected_up": 1,
            "standby": 1 if node == _CLUSTER_NODES[2] else 0,
            "unclean": 0,
            "shutdown": 0,
            "maintenance": 0,
            "pending": 0,
            "dc": 1 if node == dc else 0,
        }
        for status, value in statuses.items():
            lines.append(
                f'ha_cluster_pacemaker_nodes{{node="{node}",type="member",'
                f'status="{status}"}} {value}'
            )
        lines.append(
            f'ha_cluster_corosync_member_votes{{node="{node}",'
            f'node_id="{index + 1}",local="{"true" if node == dc else "false"}"}} 1'
        )
        lines.append(
            f'ha_cluster_pacemaker_node_attributes{{node="{node}",'
            f'name="seapath_role",value="hypervisor"}} 1'
        )

    first, second, third = _CLUSTER_NODES
    resources = [
        # id, node, role, agent, status
        ("vm-guest1", first, "started", "ocf::seapath:VirtualDomain", "active"),
        ("vm-guest2", second, "started", "ocf::seapath:VirtualDomain", "active"),
        # The finding: running nowhere, and failed where it last tried.
        ("vm-guest3", first, "stopped", "ocf::seapath:VirtualDomain", "failed"),
        ("fence-" + first, second, "started", "stonith:fence_ipmilan", "active"),
        ("fence-" + second, first, "started", "stonith:fence_ipmilan", "active"),
        # A container the cluster holds, through Pacemaker's systemd agent.
        # `extra_crm_cmd_to_run` in the golden inventory creates exactly this
        # primitive, so the fake cluster runs what that file declares.
        ("nginxquadlet", first, "started", "systemd:nginxquadlet.service", "active"),
    ]
    for name, node, role, agent, status in resources:
        lines.append(
            f'ha_cluster_pacemaker_resources{{node="{node}",resource="{name}",'
            f'role="{role}",managed="true",status="{status}",agent="{agent}",'
            f'group="",clone=""}} 1'
        )
    # A clone, which runs one instance per member and is placed by Pacemaker
    # rather than by anybody. The connectivity check a cluster commonly carries,
    # and the one resource shape that has no node to be sent to.
    for node in _CLUSTER_NODES:
        lines.append(
            f'ha_cluster_pacemaker_resources{{node="{node}",resource="ping",'
            'role="started",managed="true",status="active",'
            'agent="ocf::pacemaker:ping",group="",clone="ping-clone"} 1'
        )
    # A resource that has failed three times on the node it ran on, which is
    # its migration threshold, so Pacemaker will not start it there again.
    lines += [
        f'ha_cluster_pacemaker_fail_count{{node="{first}",resource="vm-guest3"}} 3',
        f'ha_cluster_pacemaker_migration_threshold{{node="{first}",'
        'resource="vm-guest3"} 3',
        # The two shapes a placement takes, because they behave differently
        # and only their names say which is which. `cli-prefer` is what both
        # `preferred_host` and an operator's move write, and a clear removes
        # it; `pin-` is what `pinned_host` writes, and nothing short of
        # rebuilding the resource removes that one.
        'ha_cluster_pacemaker_location_constraints{constraint="cli-prefer-vm-guest1"'
        f',node="{first}",resource="vm-guest1",role="Started"}} 1000000',
        'ha_cluster_pacemaker_location_constraints{constraint="pin-vm-guest2-on'
        f'{second}",node="{second}",resource="vm-guest2",role="Started"}} 1000000',
        "ha_cluster_pacemaker_stonith_enabled 1",
        "ha_cluster_pacemaker_config_last_change 1772000000",
        "ha_cluster_corosync_quorate 1",
        'ha_cluster_corosync_quorum_votes{type="expected_votes"} 3',
        'ha_cluster_corosync_quorum_votes{type="highest_expected"} 3',
        'ha_cluster_corosync_quorum_votes{type="total_votes"} 3',
        'ha_cluster_corosync_quorum_votes{type="quorum"} 2',
        'ha_cluster_corosync_ring_errors{ring_id="0"} 0',
        'ha_cluster_sbd_devices{device="/dev/vdb",status="healthy"} 1',
    ]
    return "\n".join(lines) + "\n"


def _ceph() -> str:
    """What the active manager's Prometheus module publishes.

    A cluster in `HEALTH_WARN` with one OSD down, for the same reason the
    Pacemaker fake is not healthy: the page has to be built against the reading
    an operator opens it for.
    """
    version = "ceph version 17.2.7 (abcdef) quincy (stable)"
    lines = [
        "# HELP ceph_health_status Cluster health status",
        "ceph_health_status 1",
        'ceph_health_detail{name="OSD_DOWN",severity="HEALTH_WARN"} 1',
        'ceph_health_detail{name="PG_DEGRADED",severity="HEALTH_WARN"} 1',
        "ceph_cluster_total_bytes 3298534883328",
        "ceph_cluster_total_used_bytes 659706976665",
        "ceph_cluster_total_used_raw_bytes 989560464998",
    ]
    for index, host in enumerate(_CLUSTER_NODES):
        lines += [
            f'ceph_mon_metadata{{ceph_daemon="mon.{host}",hostname="{host}",'
            f'ceph_version="{version}",rank="{index}"}} 1',
            f'ceph_mon_quorum_status{{ceph_daemon="mon.{host}"}} 1',
        ]
    active, standby = _CLUSTER_NODES[0], _CLUSTER_NODES[1]
    lines += [
        f'ceph_mgr_metadata{{ceph_daemon="mgr.{active}",hostname="{active}",'
        f'ceph_version="{version}"}} 1',
        f'ceph_mgr_metadata{{ceph_daemon="mgr.{standby}",hostname="{standby}",'
        f'ceph_version="{version}"}} 1',
        f'ceph_mgr_status{{ceph_daemon="mgr.{active}"}} 1',
        f'ceph_mgr_status{{ceph_daemon="mgr.{standby}"}} 0',
    ]
    # Six OSDs, two per machine, and the fifth is down and out: the case the
    # health message above is about.
    for osd in range(6):
        host = _CLUSTER_NODES[osd // 2]
        down = osd == 4
        lines += [
            f'ceph_osd_metadata{{ceph_daemon="osd.{osd}",hostname="{host}",'
            f'device_class="ssd",devices="nvme0n{1 + osd % 2}",'
            f'ceph_version="{version}"}} 1',
            f'ceph_osd_up{{ceph_daemon="osd.{osd}"}} {0 if down else 1}',
            f'ceph_osd_in{{ceph_daemon="osd.{osd}"}} {0 if down else 1}',
            f'ceph_osd_stat_bytes{{ceph_daemon="osd.{osd}"}} 549755813888',
            f'ceph_osd_stat_bytes_used{{ceph_daemon="osd.{osd}"}} '
            f"{109951162778 + osd * 2000000000}",
            f'ceph_osd_numpg{{ceph_daemon="osd.{osd}"}} {0 if down else 96}',
            f'ceph_osd_apply_latency_ms{{ceph_daemon="osd.{osd}"}} {2 + osd}',
            f'ceph_osd_commit_latency_ms{{ceph_daemon="osd.{osd}"}} {1 + osd}',
        ]
    for pool_id, name, kind in (
        ("1", "rbd", "replicated"),
        ("2", "cephfs", "replicated"),
    ):
        lines += [
            f'ceph_pool_metadata{{pool_id="{pool_id}",name="{name}",'
            f'type="{kind}",description="replica:3"}} 1',
            f'ceph_pool_stored{{pool_id="{pool_id}"}} 219902325555',
            f'ceph_pool_bytes_used{{pool_id="{pool_id}"}} 659706976665',
            f'ceph_pool_max_avail{{pool_id="{pool_id}"}} 733007751577',
            f'ceph_pool_objects{{pool_id="{pool_id}"}} 52481',
            f'ceph_pool_percent_used{{pool_id="{pool_id}"}} 0.2',
        ]
    lines += [
        "ceph_pg_total 192",
        "ceph_pg_active 192",
        "ceph_pg_clean 160",
        "ceph_pg_degraded 32",
        "ceph_pg_undersized 32",
        "ceph_pg_peering 0",
    ]
    return "\n".join(lines) + "\n"


# Keyed by port as well as by address, because a machine serves three exporters
# and they say entirely different things. The pool answers on 9100, the cluster
# on 9664 and Ceph on 9283, and only the active manager answers on the last of
# those: the second machine deliberately does not, which is the case the reader
# has to walk past to find the manager.
HA_EXPORTERS = {
    "192.168.200.125": _pacemaker(),
    "192.168.200.126": _pacemaker(),
    "elabo1": _pacemaker(),
    "elabo2": _pacemaker(),
    "seapath-machine": _pacemaker(),
}

CEPH_EXPORTERS = {
    "192.168.200.125": _ceph(),
    "elabo1": _ceph(),
    "seapath-machine": _ceph(),
    # elabo2 runs a standby manager, which answers and publishes nothing.
    "192.168.200.126": "# a standby manager serves no metrics\n",
    "elabo2": "# a standby manager serves no metrics\n",
}

# What libvirt reports on the machine outside the cluster. Recorded from a real
# standalone hypervisor, in `tests/expositions/libvirt-exporter.txt`, and kept
# there rather than written out here: a parser tested against invented series
# is a parser tested against itself.
#
# One of its domains, `VMUADMIN`, is running on the machine and absent from
# every inventory, which is the guest most likely to need stopping and the one
# nothing else here can reach.
_LIBVIRT = (
    Path(__file__).resolve().parents[2] / "tests/expositions/libvirt-exporter.txt"
)
# Keyed by address, the way the others are: the fan out builds its URL from
# `ansible_host`, so a key that is a host name matches nothing.
LIBVIRT_EXPORTERS = (
    {"192.168.200.125": _LIBVIRT.read_text(), "elabo1": _LIBVIRT.read_text()}
    if _LIBVIRT.is_file()
    else {}
)

# Every port this service asks about, and what answers on it.
BY_PORT = {
    9100: EXPORTERS,
    9177: LIBVIRT_EXPORTERS,
    9664: HA_EXPORTERS,
    9283: CEPH_EXPORTERS,
}


class FakeMetricsClient:
    """Answers for the machines above, and refuses for every other.

    Refusing is a case worth serving rather than an oversight: a node whose
    exporter is not up yet is the ordinary state of a cluster being built, and
    the page has to render it beside the nodes that answered.

    Keyed by port as well as by address. One machine serves three exporters
    saying three different things, and a fake that answered the pool on every
    port would have let a reader ask the wrong one and never notice.
    """

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        for port, exporters in BY_PORT.items():
            for host, text in exporters.items():
                if f"{host}:{port}/" in url:
                    return text, ""
        return None, "No route to host"


# The metadata `vm_manager` writes at creation, for the guests the fake cluster
# runs. Enough of the real keys that the page can be built against it: one
# guest pinned, one carrying a pinning profile, and a site's own label beside
# them so the two kinds are told apart on screen.
IMAGE_METADATA: dict[str, dict[str, str]] = {
    "system_vm-guest1": {
        "vm_name": "vm-guest1",
        "_live_migration": "true",
        "_priority": "10",
        "_preferred_host": _CLUSTER_NODES[0],
        "site_owner": "protection",
    },
    "system_vm-guest2": {
        "vm_name": "vm-guest2",
        "_pinned_host": _CLUSTER_NODES[1],
        "_seapath_alloc": (
            "version: 1\nvcpus:\n  isolation: exclusive_physical\n"
            "  scheduler: FIFO\n  priority: 90\n"
        ),
    },
    "system_vm-guest3": {"vm_name": "vm-guest3"},
    # Taken out of the cluster: Ceph holds its disk and its group, and
    # Pacemaker has no resource for it. The row the Enable button is for.
    "system_vm-guest4": {"vm_name": "vm-guest4"},
}


class FakeRbdClient:
    """An image store in memory, with the metadata above already on it.

    Writes land here and the next read sees them, which is what makes the whole
    edit path exercisable on a laptop with no Ceph: the double read that
    decides whether anything actually changed is the same code either way.
    """

    def __init__(self, images: dict[str, dict[str, str]] | None = None) -> None:
        self.images = {
            name: dict(metadata)
            for name, metadata in (
                images if images is not None else IMAGE_METADATA
            ).items()
        }

    def list_metadata(self, image: str) -> dict[str, str]:
        # An image the cluster does not have is a guest declared and never
        # deployed, which is an ordinary state and answers with nothing.
        return dict(self.images.get(image, {}))

    def set_metadata(self, image: str, key: str, value: str) -> None:
        self.images.setdefault(image, {})[key] = value

    def remove_metadata(self, image: str, key: str) -> None:
        self.images.setdefault(image, {}).pop(key, None)

    def list_groups(self) -> list[str]:
        # One group per guest, named after it, which is what `vm_manager`
        # creates beside the system image.
        return sorted(
            name[len("system_") :] for name in self.images if name.startswith("system_")
        )
