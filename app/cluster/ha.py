# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What Pacemaker and Corosync are doing, read from `ha_cluster_exporter`.

The same move as the CPU pool, one layer up. `configure_ha` deploys
`ha_cluster_exporter` on every cluster member, where it runs `crm_mon`,
`corosync-quorumtool` and `cibadmin` and publishes what they said. This service
asks it over HTTP and shapes the answer for a page. It runs none of those
commands itself, which is the rule that decides every question in this module:
a resource is *reported* here, and moved by Pacemaker.

**Whose exposition is authoritative.** Every member's exporter reports the
whole cluster, because `crm_mon` does. They agree while the cluster is healthy
and stop agreeing exactly when the page matters most: a node cut off from the
others reports itself online and everything else lost. The designated
coordinator holds the CIB the cluster is acting on, so its exposition is the
one this reads, and the page says which node answered. The SUSE dashboard this
is modelled on takes the same view, through a `dc_instance` variable.

What is deliberately not read: anything with a rate in it. A counter needs two
scrapes and a memory of the first, which is a monitoring system, and D13 sent
that to Prometheus. Every value here is what one scrape said.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from pydantic import BaseModel, Field, computed_field

from app.cluster import metrics
from app.cluster.exporters import Exposition, value

# What `configure_ha` puts ha_cluster_exporter on, and what PROMETHEUS.md tells
# a site to scrape.
DEFAULT_PORT = 9664

_NODES = "ha_cluster_pacemaker_nodes"
_RESOURCES = "ha_cluster_pacemaker_resources"
_FAIL_COUNT = "ha_cluster_pacemaker_fail_count"
_MIGRATION_THRESHOLD = "ha_cluster_pacemaker_migration_threshold"
_LAST_CHANGE = "ha_cluster_pacemaker_config_last_change"
_STONITH = "ha_cluster_pacemaker_stonith_enabled"
_LOCATION = "ha_cluster_pacemaker_location_constraints"
_ATTRIBUTES = "ha_cluster_pacemaker_node_attributes"
_QUORATE = "ha_cluster_corosync_quorate"
_VOTES = "ha_cluster_corosync_quorum_votes"
_MEMBER_VOTES = "ha_cluster_corosync_member_votes"
_RING_ERRORS = "ha_cluster_corosync_ring_errors"
_SBD_DEVICES = "ha_cluster_sbd_devices"

# The statuses a node carries, worst first. A node holds several at once -
# online and expected_up and dc are all true of a healthy coordinator - so the
# one word the page leads with is the first of these the node answers to.
_NODE_STATES = (
    "unclean",
    "shutdown",
    "pending",
    "standby_onfail",
    "standby",
    "maintenance",
    "online",
)

# The statuses a resource carries. `active` is the good one and the other four
# are findings, so the word the page leads with is the first match here.
_RESOURCE_STATES = ("failed", "blocked", "orphaned", "failure_ignored", "active")


class PacemakerNode(BaseModel):
    """One member of the cluster, as the coordinator sees it."""

    name: str
    type: str = "member"
    """`member`, `remote` or `ping`, straight from the exporter."""
    state: str = "unknown"
    """The worst thing true of this node, which is what the page leads with."""
    online: bool = False
    dc: bool = False
    expected_up: bool = False
    flags: list[str] = Field(default_factory=list)
    """Every status the exporter asserted, including the one in `state`."""
    votes: int | None = None
    """Corosync votes this member carries, when it is a Corosync member."""
    attributes: dict[str, str] = Field(default_factory=dict)
    """Node attributes, which is where a fencing agent and `configure_ha` leave
    what they know about a machine."""


class PacemakerResource(BaseModel):
    """One resource instance, and the node it is running on.

    In SEAPATH a resource is very often a VM: `vm_manager` creates one per
    guest, and the migration an operator watches on this page is Pacemaker
    moving it. The exporter names it and says where it is; nothing here moves
    it.
    """

    id: str
    agent: str = ""
    node: str = ""
    """Empty when the resource is not assigned to any node."""
    role: str = ""
    """`started`, `stopped`, `promoted`, `unpromoted`, as the exporter spells
    them."""
    managed: bool = True
    state: str = "unknown"
    flags: list[str] = Field(default_factory=list)
    group: str = ""
    clone: str = ""
    fail_count: int = 0
    fail_count_infinite: bool = False
    """Pacemaker's INFINITY, which is what makes a resource unable to run here.

    Kept as a flag rather than as a float: the exporter publishes `+Inf`, and
    JSON has no way of writing it, so a page fed the number directly received a
    body it could not parse at all.
    """
    migration_threshold: int | None = None

    # A computed field rather than a plain property: the page leads with this
    # and a property is invisible to `model_dump`, so the browser would have to
    # decide again what counts as failed, in a second place, in another
    # language.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed(self) -> bool:
        """Pacemaker will not keep this running where it is.

        Blocked as well as failed, and an infinite failure count as well as
        either: all three mean the resource is not going to come back on its
        own, which is the line an operator reads first.
        """
        return self.state in {"failed", "blocked"} or self.fail_count_infinite


class LocationConstraint(BaseModel):
    """A rule saying where a resource may or may not run."""

    id: str
    resource: str = ""
    node: str = ""
    role: str = ""
    score: str = "0"
    """`INFINITY`, `-INFINITY` or a number, written the way `crm` writes it."""


class SbdDevice(BaseModel):
    device: str
    status: str = "unknown"
    healthy: bool = False


class Corosync(BaseModel):
    """Quorum, which is the question a two node cluster is always asking."""

    quorate: bool | None = None
    expected_votes: int | None = None
    highest_expected: int | None = None
    total_votes: int | None = None
    quorum: int | None = None
    """How many votes quorum takes."""
    ring_errors: int = 0


class NodeReach(BaseModel):
    """One machine of the inventory, and whether its exporter answered."""

    host: str
    address: str
    reachable: bool
    reporting: bool = False
    """Whether this node published Pacemaker metrics at all."""
    error: str = ""


class PacemakerCluster(BaseModel):
    """What `GET /api/v1/cluster` answers."""

    available: bool = False
    """A coordinator answered and the reading below is a cluster's."""
    error: str = ""
    """Why there is nothing to show, said as what an operator can act on."""
    source: str | None = None
    """Which node's exporter this was read from."""
    from_dc: bool = False
    """Whether that node is the coordinator.

    False means the members disagreed or the coordinator could not be reached,
    and the page says so: a reading taken from a node that is not the DC is
    still worth showing and is not worth trusting blindly.
    """
    dc: str | None = None
    nodes: list[PacemakerNode] = Field(default_factory=list)
    resources: list[PacemakerResource] = Field(default_factory=list)
    constraints: list[LocationConstraint] = Field(default_factory=list)
    corosync: Corosync = Field(default_factory=Corosync)
    stonith_enabled: bool | None = None
    sbd_devices: list[SbdDevice] = Field(default_factory=list)
    config_last_change: datetime | None = None
    """When the CIB last changed, which dates the configuration on screen."""
    reach: list[NodeReach] = Field(default_factory=list)
    this_host: str | None = None
    inventory_commit: str | None = None


class PacemakerReader:
    """One exposition, turned into the cluster above."""

    def read(self, exposition: Exposition) -> PacemakerCluster:
        series = exposition.series or {}
        nodes = _nodes(series)
        dc = next((node.name for node in nodes if node.dc), None)
        return PacemakerCluster(
            available=True,
            source=exposition.host,
            from_dc=dc is not None and dc == exposition.host,
            dc=dc,
            nodes=nodes,
            resources=_resources(series),
            constraints=_constraints(series),
            corosync=_corosync(series),
            stonith_enabled=_flag(series, _STONITH),
            sbd_devices=_sbd(series),
            config_last_change=_moment(value(series, _LAST_CHANGE)),
        )


def _nodes(series: dict[str, list[metrics.Sample]]) -> list[PacemakerNode]:
    found: dict[str, PacemakerNode] = {}
    for sample in series.get(_NODES, []):
        name = sample.labels.get("node", "")
        if not name:
            continue
        node = found.setdefault(
            name, PacemakerNode(name=name, type=sample.labels.get("type", "member"))
        )
        status = sample.labels.get("status", "")
        # A sample with a zero value is the exporter saying the node does *not*
        # carry that status. Both shapes are in the wild, one series per status
        # and one series per asserted status, and reading the value covers the
        # pair of them.
        if not status or not sample.value:
            continue
        node.flags.append(status)
        if status == "online":
            node.online = True
        elif status == "dc":
            node.dc = True
        elif status == "expected_up":
            node.expected_up = True

    votes = {
        sample.labels.get("node", ""): int(sample.value)
        for sample in series.get(_MEMBER_VOTES, [])
    }
    attributes: dict[str, dict[str, str]] = {}
    for sample in series.get(_ATTRIBUTES, []):
        node = sample.labels.get("node", "")
        name = sample.labels.get("name", "")
        if node and name:
            attributes.setdefault(node, {})[name] = sample.labels.get("value", "")

    for node in found.values():
        node.flags.sort()
        node.state = _worst(node.flags, _NODE_STATES, "offline")
        node.votes = votes.get(node.name)
        node.attributes = attributes.get(node.name, {})
    return sorted(found.values(), key=lambda node: node.name)


def _resources(series: dict[str, list[metrics.Sample]]) -> list[PacemakerResource]:
    """Every resource instance, keyed by the resource and the node holding it.

    Keyed by the pair rather than by the resource alone: a clone runs on every
    member at once, and collapsing it to one row would report one of the copies
    and silently drop the rest.
    """
    found: dict[tuple[str, str], PacemakerResource] = {}
    for sample in series.get(_RESOURCES, []):
        name = sample.labels.get("resource", "")
        if not name:
            continue
        node = sample.labels.get("node", "")
        resource = found.setdefault(
            (name, node),
            PacemakerResource(
                id=name,
                node=node,
                agent=sample.labels.get("agent", ""),
                role=sample.labels.get("role", ""),
                managed=sample.labels.get("managed", "true") != "false",
                group=sample.labels.get("group", ""),
                clone=sample.labels.get("clone", ""),
            ),
        )
        status = sample.labels.get("status", "")
        if status and sample.value:
            resource.flags.append(status)

    failures = _by_pair(series.get(_FAIL_COUNT, []))
    thresholds = _by_pair(series.get(_MIGRATION_THRESHOLD, []))
    for (name, node), resource in found.items():
        resource.flags.sort()
        resource.state = _worst(resource.flags, _RESOURCE_STATES, "inactive")
        count = failures.get((name, node))
        if count is not None:
            # Pacemaker's INFINITY arrives as `+Inf`, which is a float JSON
            # cannot carry. It is the state that stops a resource from running
            # on a node at all, so it is kept as a flag rather than dropped.
            resource.fail_count_infinite = math.isinf(count)
            resource.fail_count = 0 if math.isinf(count) else int(count)
        threshold = thresholds.get((name, node))
        if threshold is not None and not math.isinf(threshold):
            resource.migration_threshold = int(threshold)
    return sorted(found.values(), key=lambda item: (item.id, item.node))


def _by_pair(samples: list[metrics.Sample]) -> dict[tuple[str, str], float]:
    return {
        (sample.labels.get("resource", ""), sample.labels.get("node", "")): sample.value
        for sample in samples
    }


def _constraints(series: dict[str, list[metrics.Sample]]) -> list[LocationConstraint]:
    return sorted(
        (
            LocationConstraint(
                id=sample.labels.get("constraint", ""),
                resource=sample.labels.get("resource", ""),
                node=sample.labels.get("node", ""),
                role=sample.labels.get("role", ""),
                score=_score(sample.value),
            )
            for sample in series.get(_LOCATION, [])
        ),
        key=lambda item: (item.resource, item.id),
    )


def _score(raw: float) -> str:
    """A constraint score, written the way `crm configure show` writes it.

    The exporter publishes Pacemaker's INFINITY as a very large float, or as
    `+Inf` outright. Either way the number is not the point: what an operator
    reads is whether the rule is a preference or a prohibition.
    """
    if math.isinf(raw) or abs(raw) >= 1_000_000:
        return "INFINITY" if raw > 0 else "-INFINITY"
    return str(int(raw))


def _corosync(series: dict[str, list[metrics.Sample]]) -> Corosync:
    votes = {
        sample.labels.get("type", ""): int(sample.value)
        for sample in series.get(_VOTES, [])
    }
    return Corosync(
        quorate=_flag(series, _QUORATE),
        expected_votes=votes.get("expected_votes"),
        highest_expected=votes.get("highest_expected"),
        total_votes=votes.get("total_votes"),
        quorum=votes.get("quorum"),
        # Summed over the rings, because a cluster has one ring per interface
        # and what an operator needs first is whether any of them is faulty.
        ring_errors=int(sum(s.value for s in series.get(_RING_ERRORS, []))),
    )


def _sbd(series: dict[str, list[metrics.Sample]]) -> list[SbdDevice]:
    found: dict[str, SbdDevice] = {}
    for sample in series.get(_SBD_DEVICES, []):
        device = sample.labels.get("device", "")
        status = sample.labels.get("status", "")
        if not device or not sample.value:
            continue
        found[device] = SbdDevice(
            device=device, status=status, healthy=status == "healthy"
        )
    return sorted(found.values(), key=lambda item: item.device)


def _flag(series: dict[str, list[metrics.Sample]], name: str) -> bool | None:
    """A gauge that means yes or no, or None when the exporter published none."""
    raw = value(series, name)
    return None if raw is None else bool(raw)


def _worst(flags: list[str], order: tuple[str, ...], default: str) -> str:
    for candidate in order:
        if candidate in flags:
            return candidate
    return default


def _moment(raw: float | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


def is_coordinator(exposition: Exposition) -> bool:
    """Whether this exposition came from the node holding the CIB.

    Every member reports the whole cluster, so the reading is only as good as
    the member it came from: a node cut off from the others reports itself
    online and everything else lost. The designated coordinator holds the CIB
    the cluster is acting on, and it names itself in the series it publishes.

    Matching the DC's name against the machine that answered works because
    Pacemaker names its nodes after the machines, which is what `configure_ha`
    builds the cluster from. When the two do not line up this simply answers
    no, and the caller says which node it fell back to.
    """
    for sample in (exposition.series or {}).get(_NODES, []):
        if (
            sample.labels.get("status") == "dc"
            and sample.value
            and sample.labels.get("node") == exposition.host
        ):
            return True
    return False


def reporting(exposition: Exposition) -> bool:
    """Whether this exporter answered with a cluster at all.

    An `ha_cluster_exporter` on a machine that is not in a cluster answers, and
    publishes nothing under these names. That is a different sentence from a
    node that could not be reached, and the page has to tell them apart.
    """
    return exposition.has(_NODES)
