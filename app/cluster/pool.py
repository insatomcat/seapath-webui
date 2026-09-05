# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What every machine in the inventory is, read from their exporters.

Two readings, one request. The CPU pool, which is what the isolated cores are
doing, and the real time tuning, which is what the machine was tuned as. They
arrive in the same exposition because `seapath-alloc` writes them into the same
textfile, and they are read together because they answer one question: does
this machine still match the inventory it was converged from.

This is the one reading in the service that crosses to another machine, and it
is worth being precise about why that is allowed here when D13 sent live state
away.

D13 refused a *second source of truth* for live state: reading a unit state
from inside this container needed a route to the host's systemd, and every node
already runs `prometheus-node-exporter`, so the duplicate earned nothing and
cost eight mounts. This does the opposite. It reads what that same exporter
already publishes, over HTTP, on the port it already listens on, and invents
nothing.

It is also the only way this question can be answered at all. `seapath-alloc`
derives occupancy from the affinity of every QEMU thread in `/proc`, which this
container's PID namespace hides and which AGENTS.md forbids opening with
`--pid=host`. The exporter runs on the host, sees all of it, and writes
`seapath_alloc_cpu_detail` every fifteen seconds. Asking it is the cheap path
to the answer, and the only one that does not enlarge this container.

Reading the cluster rather than this node is deliberate. Every other panel
reads the machine the browser is pointed at, which made the page incoherent the
moment a measurement brought back three machines. The pool is where the
aggregate view starts.

What this is not: a monitoring system. Nothing here is stored, alerted on, or
kept with history. The exporters are the source, Prometheus is where history
and alerting live, and the repository ships two Grafana dashboards over exactly
these metrics. This is the same data, in the page an operator already has open.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field, computed_field

from app.cluster import metrics, tuning
from app.cluster.exporters import (
    Exposition,
    MetricsClient,
    UrllibMetricsClient,
    read_all,
)
from app.hosts.models import RealtimeReading
from app.services.checks import Check

# What `deploy_prometheus_exporters` puts node_exporter on, and what
# PROMETHEUS.md tells a site to scrape.
DEFAULT_PORT = 9100

_CPU_DETAIL = "seapath_alloc_cpu_detail"
_FALLBACKS = "seapath_alloc_active_fallbacks"
_SLOT_WARNING = "seapath_alloc_slot_warning_info"
_SCRAPE_TIME = "seapath_alloc_scrape_timestamp_seconds"

# Said once, because it is the one failure here an operator fixes by upgrading
# rather than by looking at the machine.
_NO_TUNING = (
    "This node's collector publishes the pool but not the tuning. "
    "deploy_seapath_alloc from a collection that ships seapath_rt_* is what "
    "adds it, and until then only the isolated set and the kernel can be "
    "checked here."
)


class CpuSlot(BaseModel):
    """One logical CPU, and what is on it."""

    cpu: int
    isolated: bool = False
    core: int | None = None
    """The physical core, which is what pairs a CPU with its HT sibling."""
    sibling: int | None = None
    state: str = "unknown"
    """`free`, `vm`, `irq`, `quadlet`, `run`, `claim`, `reserved`, `slot`,
    `irq_slot` or `housekeeping`, straight from the exporter."""
    label: str = ""
    """Who is on it: the VM, the interface, the claim, or the slot name."""
    group: str = ""
    scheduler: str = ""
    priority: int = 0
    slot: str = ""
    members: str = ""
    """The actors sharing a slot core, summarised by the exporter."""


class NodePool(BaseModel):
    host: str
    address: str
    reachable: bool = False
    error: str = ""
    """Why this node returned nothing, said as what an operator can act on."""
    cpus: list[CpuSlot] = Field(default_factory=list)
    soft_fallbacks: int = 0
    hard_fallbacks: int = 0
    """Actors that asked for isolation and are running on housekeeping cores.

    The conformance question `seapath-alloc` answers and no reading of `/sys`
    can: a machine that accepted a pinning profile and could not honour it.
    """
    slot_warnings: list[str] = Field(default_factory=list)
    scrape_age_seconds: float | None = None

    declared_isolcpus: str | None = None
    """What the inventory asks this node to isolate.

    The exporter publishes `isolated` per CPU, so the set the kernel actually
    booted with comes back from every node, and the inventory holds what each
    was told. The commonest finding in a cluster is one machine converged and
    never rebooted, which the kernel's boot-time reading of `isolcpus` makes
    invisible any other way.
    """
    observed_isolcpus: str = ""
    kernel: str = ""
    preemption: str = ""

    reading: RealtimeReading | None = None
    """The tuning this node published, which is what its checks are formed from.

    None when the node answered without it, which is a collector to upgrade
    rather than a machine that failed anything. `tuning_error` says so.
    """
    kernel_cmdline: str = ""
    tuning_error: str = ""
    checks: list[Check] = Field(default_factory=list)
    """The same ten checks the local machine gets, run against this node.

    Filled by `RealtimeService`, which is where the inventory is: reading a
    node is this module's job, and holding it against what it was told is not.
    """

    # A computed field rather than a plain property: a property is invisible to
    # `model_dump`, so the page received no answer at all and rendered every
    # node as "nothing declared".
    @computed_field  # type: ignore[prop-decorator]
    @property
    def isolation_matches(self) -> bool | None:
        """None when nothing is declared, so it is never drawn as a pass."""
        if not self.declared_isolcpus or not self.reachable or not self.cpus:
            return None
        return _parse(self.declared_isolcpus) == _parse(self.observed_isolcpus)

    @property
    def isolated(self) -> list[int]:
        return [slot.cpu for slot in self.cpus if slot.isolated]

    @property
    def free(self) -> list[int]:
        return [slot.cpu for slot in self.cpus if slot.state == "free"]


class ClusterPool(BaseModel):
    """What `GET /api/v1/realtime/pool` answers."""

    nodes: list[NodePool] = Field(default_factory=list)
    this_host: str | None = None
    inventory_commit: str | None = None
    """Which desired state the machines were compared against.

    A conformance report with no idea which commit it was held against is an
    anecdote, the same way a latency figure with no isolation behind it is.
    """
    available: bool = True
    """At least one node answered.

    False is the ordinary state on a machine whose exporters have not been
    deployed yet, and the page says so rather than drawing an empty grid.
    """


class PoolReader:
    def __init__(
        self,
        client: MetricsClient | None = None,
        port: int = DEFAULT_PORT,
        timeout: float = 2.0,
    ) -> None:
        self._client = client or UrllibMetricsClient()
        self._port = port
        self._timeout = timeout

    def read(self, targets: list[tuple[str, str]]) -> list[NodePool]:
        """Every node's pool, fetched in parallel."""
        return [
            self._node(exposition)
            for exposition in read_all(
                self._client, targets, self._port, timeout=self._timeout
            )
        ]

    def _node(self, exposition: Exposition) -> NodePool:
        host, address = exposition.host, exposition.address
        if exposition.series is None:
            return NodePool(
                host=host, address=address, reachable=False, error=exposition.error
            )

        series = exposition.series
        reading, cmdline = tuning.read(series)
        release, model = tuning.kernel(series)
        if _CPU_DETAIL not in series:
            # The kernel still comes back from node_exporter's own series, so
            # a node with no allocator says which kernel it booted rather than
            # nothing at all.
            return NodePool(
                host=host,
                address=address,
                reachable=True,
                error=(
                    "The exporter answered but publishes no seapath-alloc "
                    "metrics. deploy_seapath_alloc installs the collector that "
                    "writes them."
                ),
                kernel=release,
                preemption=model,
            )
        return NodePool(
            host=host,
            address=address,
            reachable=True,
            cpus=sorted(
                (_slot(sample) for sample in series[_CPU_DETAIL]),
                key=lambda slot: slot.cpu,
            ),
            soft_fallbacks=_count(series, _FALLBACKS, "soft"),
            hard_fallbacks=_count(series, _FALLBACKS, "hard"),
            slot_warnings=sorted(
                {
                    f"{sample.labels.get('slot', '?')}: "
                    f"{sample.labels.get('reason', '?')}"
                    for sample in series.get(_SLOT_WARNING, [])
                    if sample.value
                }
            ),
            scrape_age_seconds=_age(series),
            observed_isolcpus=_ranges(
                [
                    int(sample.labels.get("cpu", -1))
                    for sample in series[_CPU_DETAIL]
                    if sample.labels.get("isolated") == "1"
                ]
            ),
            reading=reading,
            kernel_cmdline=cmdline,
            tuning_error="" if reading else _NO_TUNING,
            kernel=release,
            preemption=model,
        )


def _slot(sample: metrics.Sample) -> CpuSlot:
    labels = sample.labels
    return CpuSlot(
        cpu=_int(labels.get("cpu"), 0),
        isolated=labels.get("isolated") == "1",
        core=_optional_int(labels.get("ht_pair")),
        sibling=_optional_int(labels.get("ht_sibling")),
        state=labels.get("state") or "unknown",
        label=labels.get("label", ""),
        group=labels.get("group", ""),
        scheduler=labels.get("scheduler", ""),
        priority=_int(labels.get("priority"), 0),
        slot=labels.get("slot", ""),
        members=labels.get("members", ""),
    )


def _count(series: dict, name: str, severity: str) -> int:
    for sample in series.get(name, []):
        if sample.labels.get("severity") == severity:
            return int(sample.value)
    return 0


def _age(series: dict) -> float | None:
    """How old the exporter's reading is, in seconds.

    Shown rather than hidden. The collector runs on a fifteen second timer, so
    the pool is never quite now, and a page that implies otherwise would be
    read as live during the minute a node has stopped exporting.
    """
    samples = series.get(_SCRAPE_TIME, [])
    if not samples:
        return None
    return max(0.0, time.time() - samples[0].value)


def _int(raw: str | None, default: int) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _optional_int(raw: str | None) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse(raw: str) -> set[int]:
    """A kernel CPU list, as a set, so `4-7` and `4,5,6,7` compare equal."""
    found: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                found.update(range(int(start), int(end) + 1))
            except ValueError:
                continue
        else:
            try:
                found.add(int(part))
            except ValueError:
                continue
    return found


def _ranges(cpus: list[int]) -> str:
    """The kernel's own notation, so the two columns compare by eye."""
    ordered = sorted(cpu for cpu in cpus if cpu >= 0)
    if not ordered:
        return ""
    parts: list[str] = []
    start = previous = ordered[0]
    for cpu in ordered[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = cpu
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)
