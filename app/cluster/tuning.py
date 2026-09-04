# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The real time tuning of a machine this service cannot read, from its exporter.

`app/hosts/local.py` reads this machine: `/etc/tuned/active_profile` through
the host `/etc` PAM already brought in, the sysctls in `/proc`, the hugepage
pools and the interrupt masks. None of that reaches another node, and D27
records the two ways out that were on the table. Running the checks over SSH
turns a page refresh into a command execution on live substation hypervisors.
Publishing the readings turns it into one HTTP GET of a document a node is
already serving.

So `seapath-alloc` publishes them, in the same textfile that carries the pool,
under `seapath_rt_*`. This module turns that back into the same
`RealtimeReading` the local reader produces, so one implementation of the
checks answers for every machine.

Two conventions come from the exporter and are relied on here:

- An info family is published even when the reading came back empty, so an
  empty label means "read, and there was nothing" while an absent family means
  "this node runs a collector too old to publish the block".
  `seapath_rt_tuned_info` is what identifies the block.
- A numeric gauge is omitted rather than defaulted, because every value it
  could carry is a legitimate one: `-1` is a correctly tuned
  `sched_rt_runtime_us`, and a hugepage pool of `0` is a real answer.

Nothing here judges. A reading that arrived and a reading that could not be
made are kept apart all the way to the check, which is what lets the page say
"unreadable" instead of drawing a pass over a machine nobody looked at.
"""

from __future__ import annotations

from app.cluster import metrics
from app.hosts.models import HugepagePool, IrqOnIsolatedCpu, RealtimeReading

# The family that says the block is there. An exporter predating it publishes
# the pool and nothing else, which is a node to upgrade rather than a node
# that failed a check.
PUBLISHED = "seapath_rt_tuned_info"

_CMDLINE = "seapath_rt_kernel_cmdline_info"
_SCHED_RUNTIME = "seapath_rt_sched_rt_runtime_us"
_SCHED_PERIOD = "seapath_rt_sched_rt_period_us"
_HUGEPAGES_TOTAL = "seapath_rt_hugepages_total"
_HUGEPAGES_FREE = "seapath_rt_hugepages_free"
_THP = "seapath_rt_transparent_hugepages_info"
_SMT_INFO = "seapath_rt_smt_info"
_SMT_ACTIVE = "seapath_rt_smt_active"
_ACPI = "seapath_rt_acpi_present"
_IRQS_TOTAL = "seapath_rt_irqs_total"
_IRQS_ON_ISOLATED = "seapath_rt_irqs_on_isolated_cpus"
_IRQ_INFO = "seapath_rt_irq_on_isolated_info"

# node_exporter's own, which is how the kernel of a machine this service cannot
# read comes back. `version` is where the PREEMPT_RT build flag appears.
_UNAME = "node_uname_info"


def published(series: dict[str, list[metrics.Sample]]) -> bool:
    return bool(series.get(PUBLISHED))


def kernel(series: dict[str, list[metrics.Sample]]) -> tuple[str, str]:
    """The release and the preemption model, from `node_uname_info`.

    Read from node_exporter rather than from the seapath-alloc block: it is
    already there on every machine that runs an exporter at all, including one
    where `deploy_seapath_alloc` has not run yet.
    """
    samples = series.get(_UNAME, [])
    if not samples:
        return "", ""
    labels = samples[0].labels
    return labels.get("release", ""), preemption(labels.get("version", ""))


def preemption(version: str) -> str:
    """PREEMPT_RT before PREEMPT, or every RT kernel reads as an ordinary one."""
    for marker in ("PREEMPT_RT", "PREEMPT_DYNAMIC", "PREEMPT", "VOLUNTARY"):
        if marker in version:
            return marker
    return "" if not version else "none"


def read(series: dict[str, list[metrics.Sample]]) -> tuple[RealtimeReading | None, str]:
    """The tuning this node published, and the command line it booted with.

    Returns `(None, "")` when the node publishes no `seapath_rt_*` block at
    all, which the caller reports as a node to upgrade rather than as a node
    with no tuning.

    The command line comes back beside the reading rather than inside it
    because it belongs to the CPU reading, which is where the boot parameter
    check looks for it.
    """
    if not published(series):
        return None, ""

    tuned = _labels(series, PUBLISHED)
    thp = _labels(series, _THP)
    smt = _labels(series, _SMT_INFO)
    release, model = kernel(series)

    return (
        RealtimeReading(
            tuned_profile=tuned.get("profile") or None,
            tuned_profile_source=tuned.get("source") or None,
            tuned_profile_installed=_optional_bool(tuned.get("installed")),
            # The uname build string, which is the part of /proc/version the
            # preemption model is carried in. The local reader keeps the whole
            # line; both are only ever shown as the model derived from them.
            kernel_version=release or None,
            preemption=model or None,
            smt_active=_optional_flag(series, _SMT_ACTIVE),
            smt_control=smt.get("control") or None,
            sched_rt_runtime_us=_optional_int(series, _SCHED_RUNTIME),
            sched_rt_period_us=_optional_int(series, _SCHED_PERIOD),
            hugepages=_hugepages(series),
            transparent_hugepages=thp.get("enabled") or None,
            transparent_hugepage_defrag=thp.get("defrag") or None,
            acpi_present=_optional_flag(series, _ACPI),
            irq_count=_optional_int(series, _IRQS_TOTAL),
            irqs_on_isolated=_optional_int(series, _IRQS_ON_ISOLATED),
            irqs_on_isolated_cpus=_irqs(series),
        ),
        _labels(series, _CMDLINE).get("cmdline", ""),
    )


def _labels(series: dict, name: str) -> dict[str, str]:
    samples = series.get(name, [])
    return samples[0].labels if samples else {}


def _optional_int(series: dict, name: str) -> int | None:
    """The gauge, or None when the node did not publish it.

    None rather than a default, because the exporter omits what it could not
    read and every value it does publish is a legitimate one.
    """
    samples = series.get(name, [])
    if not samples:
        return None
    try:
        return int(samples[0].value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _optional_flag(series: dict, name: str) -> bool | None:
    value = _optional_int(series, name)
    return None if value is None else bool(value)


def _optional_bool(raw: str | None) -> bool | None:
    """`"1"`, `"0"`, or the empty label the exporter writes for a non-reading."""
    if raw == "1":
        return True
    if raw == "0":
        return False
    return None


def _hugepages(series: dict) -> list[HugepagePool]:
    """The pools, machine wide and per NUMA node, paired with their free count.

    Two families rather than one because a free count is a gauge that moves
    and a total is one that does not. They are joined here on the size and the
    node, which is the pair that names a pool.
    """
    free = {
        (sample.labels.get("size_kb"), sample.labels.get("node")): sample.value
        for sample in series.get(_HUGEPAGES_FREE, [])
    }
    pools: list[HugepagePool] = []
    for sample in series.get(_HUGEPAGES_TOTAL, []):
        size = sample.labels.get("size_kb")
        node = sample.labels.get("node")
        if size is None:
            continue
        try:
            size_kb = int(size)
        except ValueError:
            continue
        pools.append(
            HugepagePool(
                size_kb=size_kb,
                total=int(sample.value),
                free=int(free.get((size, node), 0)),
                node=int(node) if node else None,
            )
        )
    return pools


def _irqs(series: dict) -> list[IrqOnIsolatedCpu]:
    """The interrupts the exporter named, which may be fewer than it counted.

    A machine that keeps nothing off its isolated cores has every interrupt on
    this list, so the exporter caps what it describes and publishes the true
    number beside it. The count is what the check reports; this is what it
    names.
    """
    found: list[IrqOnIsolatedCpu] = []
    for sample in series.get(_IRQ_INFO, []):
        number = sample.labels.get("irq")
        if not number:
            continue
        found.append(
            IrqOnIsolatedCpu(
                number=number,
                name=sample.labels.get("name") or None,
                cpus=_cpus(sample.labels.get("cpus", "")),
            )
        )
    return found


def _cpus(raw: str) -> list[int]:
    found: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            found.append(int(part))
    return found
