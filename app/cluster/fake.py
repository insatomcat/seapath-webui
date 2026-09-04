# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Exporters for three machines that do not exist.

What the whole test suite and the `use_fakes` development switch read. The
three nodes are deliberately unalike, because a cluster where every machine
answers the same thing exercises one branch: one carries VMs and a slot, one is
idle, and one is unreachable, which is the ordinary state of a cluster being
built.
"""

from __future__ import annotations

import time


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


class FakeMetricsClient:
    """Answers for the machines above, and refuses for every other.

    Refusing is a case worth serving rather than an oversight: a node whose
    exporter is not up yet is the ordinary state of a cluster being built, and
    the page has to render it beside the nodes that answered.
    """

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        for host, text in EXPORTERS.items():
            if host in url:
                return text, ""
        return None, "No route to host"
