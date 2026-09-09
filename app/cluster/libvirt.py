# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What libvirt says about the domains of one machine.

`ha_cluster_exporter` answers for a guest Pacemaker owns, and a guest on a
standalone machine has no Pacemaker at all. Reading only the first is how a
page reported seven running VMs as absent: nothing it asked knew about them.

`deploy_prometheus_exporters` already deploys `libvirt-exporter` on every
machine of the `hypervisors` group, on port 9177, with the libvirt socket
mounted read only. So this asks the exposition that machine already publishes,
the way the cluster view asks the two others: one HTTP GET, on a port that is
already open, no mount and no second source of truth. D13, D26, D27 and D29 are
that boundary, and this is the same one.

What it reads is a handful of series out of the several hundred the exporter
publishes. The block, interface, memory and vCPU counters are rates and
histories, which belong to Prometheus and to the Grafana dashboards that have a
time series database behind them. What a page here needs is what a guest is
doing right now.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.cluster import metrics
from app.cluster.exporters import Exposition

# What `deploy_prometheus_exporters` publishes it on, from the quadlet it
# templates: `PublishPort={{ listen_address }}:9177:9177`.
DEFAULT_PORT = 9177

# The series this reads, and nothing else.
_STATE = "libvirt_domain_info_state"
_INFO = "libvirt_domain_info"
_MEMORY = "libvirt_domain_info_maximum_memory_bytes"
_VCPUS = "libvirt_domain_info_virtual_cpus"
_UP = "libvirt_up"

# libvirt's own domain state codes, which the exporter publishes as the value
# and spells out in `state_desc`. The word here is the one the state column
# uses for a Pacemaker resource, so the same column reads the same way whether
# a guest is on a cluster or on a standalone machine: libvirt's `shut off` is
# Pacemaker's `stopped`, and the sentence the exporter sends, `the domain is
# running`, is kept as the description rather than shown in a table cell. The
# states libvirt distinguishes and Pacemaker does not are kept as they are:
# a paused or crashed domain is not a stopped one.
_STATES = {
    0: "unknown",
    1: "running",
    2: "blocked",
    3: "paused",
    4: "shutting down",
    5: "stopped",
    6: "crashed",
    7: "suspended",
}

# The states a page reports as a guest that is doing its job. `blocked` is a
# running domain waiting on I/O, which libvirt reports separately and which is
# still a guest that has not stopped.
_RUNNING = frozenset({1, 2})


class LibvirtDomain(BaseModel):
    """One domain, as the machine running it describes it."""

    name: str
    host: str
    """The machine whose exporter reported it."""
    state: str
    """One word, the vocabulary the state column uses for a resource."""
    description: str = ""
    """libvirt's own wording, `the domain is running` and its family."""
    running: bool
    maximum_memory_bytes: int | None = None
    vcpus: int | None = None
    machine_type: str | None = None
    """`pc-q35-10.0` and its family, from the domain's `os_type_machine`."""


class LibvirtReading(BaseModel):
    """What one machine's libvirt exporter answered."""

    host: str
    reachable: bool = False
    available: bool = False
    """The exporter answered and libvirt itself was up."""
    error: str = ""
    domains: list[LibvirtDomain] = Field(default_factory=list)


def read(exposition: Exposition) -> LibvirtReading:
    """One machine's domains, from its exposition."""
    reading = LibvirtReading(
        host=exposition.host,
        reachable=exposition.answered,
        error=exposition.error,
    )
    if not exposition.answered:
        return reading

    series = exposition.series or {}
    # `libvirt_up` is the exporter saying whether it could reach libvirt at
    # all. Absent on an exporter that does not publish it, and then the domains
    # it did send are the answer.
    up = series.get(_UP)
    if up and not up[0].value:
        reading.error = "the exporter could not reach libvirt"
        return reading

    reading.available = True
    reading.domains = _domains(series, exposition.host)
    return reading


def reporting(exposition: Exposition) -> bool:
    """Whether this exposition is a libvirt exporter's at all.

    Asked before an answer is believed, the way `ha.reporting` is: a machine
    that answers on the port with something else has not told us about its
    domains.
    """
    series = exposition.series or {}
    return _UP in series or _STATE in series


def _domains(series: dict[str, list[metrics.Sample]], host: str) -> list[LibvirtDomain]:
    memory = _by_domain(series.get(_MEMORY, []))
    vcpus = _by_domain(series.get(_VCPUS, []))
    machines = {
        sample.labels.get("domain", ""): sample.labels.get("os_type_machine", "")
        for sample in series.get(_INFO, [])
    }

    found: list[LibvirtDomain] = []
    for sample in series.get(_STATE, []):
        name = sample.labels.get("domain", "")
        if not name:
            continue
        code = int(sample.value)
        found.append(
            LibvirtDomain(
                name=name,
                host=host,
                state=_STATES.get(code, "unknown"),
                description=sample.labels.get("state_desc", ""),
                running=code in _RUNNING,
                maximum_memory_bytes=_whole(memory.get(name)),
                vcpus=_whole(vcpus.get(name)),
                machine_type=machines.get(name) or None,
            )
        )
    return sorted(found, key=lambda domain: domain.name)


def _by_domain(samples: list[metrics.Sample]) -> dict[str, float]:
    return {
        sample.labels["domain"]: sample.value
        for sample in samples
        if sample.labels.get("domain")
    }


def _whole(value: float | None) -> int | None:
    return None if value is None else int(value)
