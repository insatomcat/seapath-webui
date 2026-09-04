# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The CPU pool of every machine in the inventory, read from their exporters.

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

import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from pydantic import BaseModel, Field

from app.cluster import metrics

logger = logging.getLogger(__name__)

# What `deploy_prometheus_exporters` puts node_exporter on, and what
# PROMETHEUS.md tells a site to scrape.
DEFAULT_PORT = 9100

_CPU_DETAIL = "seapath_alloc_cpu_detail"
_FALLBACKS = "seapath_alloc_active_fallbacks"
_SLOT_WARNING = "seapath_alloc_slot_warning_info"
_SCRAPE_TIME = "seapath_alloc_scrape_timestamp_seconds"


class MetricsClient(Protocol):
    """Fetches one exporter's exposition, or explains why it could not.

    Injected for the same reason the command runner is: the whole test suite
    runs with no cluster, and the set of things this service may reach over the
    network stays a short list in one place.
    """

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]: ...


class UrllibMetricsClient:
    """The stdlib, because this is one GET of a text document.

    An HTTP library would be a dependency in a substation image for a request
    `urllib` already makes. The timeout is short and the failure is a sentence:
    a node that cannot be reached is an ordinary state on a cluster being
    built, and the page says which one rather than failing whole.
    """

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                return response.read().decode("utf-8", errors="replace"), ""
        except urllib.error.HTTPError as error:
            return None, f"the exporter answered {error.code}"
        except urllib.error.URLError as error:
            return None, f"{error.reason}"
        except (TimeoutError, OSError) as error:
            return None, str(error)
        except Exception as error:  # pragma: no cover - defensive
            return None, str(error)


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
        """Every node's pool, fetched in parallel.

        In parallel because the page waits on the slowest one, and a node that
        is down costs the whole timeout: three nodes in series with one
        unreachable is six seconds before anything renders.
        """
        if not targets:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            return list(pool.map(lambda item: self._node(*item), targets))

    def _node(self, host: str, address: str) -> NodePool:
        url = f"http://{address}:{self._port}/metrics"
        text, error = self._client.fetch(url, timeout=self._timeout)
        if text is None:
            logger.debug("No metrics from %s: %s", url, error)
            return NodePool(host=host, address=address, reachable=False, error=error)

        series = metrics.parse(text)
        if _CPU_DETAIL not in series:
            return NodePool(
                host=host,
                address=address,
                reachable=True,
                error=(
                    "The exporter answered but publishes no seapath-alloc "
                    "metrics. deploy_seapath_alloc installs the collector that "
                    "writes them."
                ),
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
