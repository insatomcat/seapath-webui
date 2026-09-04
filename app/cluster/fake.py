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
