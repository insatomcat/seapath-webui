# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What systemd says about a handful of units, read from `node_exporter`.

The same move as the CPU pool, on the same exposition. `node_exporter` runs
with its `systemd` collector on every SEAPATH machine and publishes the state
of each unit; this asks the machine that already answers for the pool and picks
out the units the page is about. No route to the host's systemd, no bus, no
mount, and no second scrape: the exposition is the one `PoolReader` fetches
from the same port.

**Which units, and why not all of them.** Only the units the caller names,
which is the quadlets the inventory declares plus the ones Pacemaker reports as
resources. Listing every unit a machine runs is Cockpit and Prometheus, which
[D13](../../docs/decisions.md) sent away from here on purpose. Naming the units
first keeps this inside the line D26, D27 and D29 drew: one current value, out
of an exposition a node already publishes, about an object this service already
holds the desired state for.

What is deliberately not read: `node_systemd_unit_start_time_seconds` beyond
the one timestamp, the restart counters, and anything that would need a second
scrape to mean something. A rate is a monitoring system.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from app.cluster import metrics
from app.cluster.exporters import Exposition

_STATE = "node_systemd_unit_state"
_START_TIME = "node_systemd_unit_start_time_seconds"

# The states the collector publishes, worst first. It emits one series per
# state per unit and sets exactly one of them to 1, so the state of a unit is
# the one series that answered. Ordered because a collector that publishes two
# at once, or none, must still produce one word rather than a stack trace.
_STATES = ("failed", "activating", "deactivating", "inactive", "active")


class UnitState(BaseModel):
    """One systemd unit on one machine, as its exporter published it."""

    unit: str
    state: str = "unknown"
    """`active`, `inactive`, `failed`, `activating` or `deactivating`."""
    active: bool = False
    failed: bool = False
    started_at: datetime | None = None
    """When systemd last started it, absent for a unit that is not running."""


def reporting(exposition: Exposition) -> bool:
    """Whether this node's exporter runs the systemd collector at all.

    A node answering the pool and nothing here is a collector to turn on, which
    is a different sentence from a node that cannot be reached, and the pages
    say which of the two happened.
    """
    return exposition.has(_STATE)


def read(
    series: dict[str, list[metrics.Sample]], units: set[str]
) -> dict[str, UnitState]:
    """The state of each named unit, keyed by unit name.

    A unit the exposition does not carry is absent from the answer rather than
    reported as stopped. The two are not the same finding: a quadlet whose file
    has never been uploaded has no unit at all, and calling it inactive would
    describe it as deployed and down.
    """
    if not units:
        return {}
    states: dict[str, dict[str, bool]] = {}
    for sample in series.get(_STATE, []):
        unit = sample.labels.get("name", "")
        if unit not in units:
            continue
        states.setdefault(unit, {})[sample.labels.get("state", "")] = sample.value == 1

    started: dict[str, float] = {}
    for sample in series.get(_START_TIME, []):
        unit = sample.labels.get("name", "")
        if unit in units and sample.value > 0:
            started[unit] = sample.value

    found: dict[str, UnitState] = {}
    for unit, published in states.items():
        state = next((name for name in _STATES if published.get(name)), "unknown")
        found[unit] = UnitState(
            unit=unit,
            state=state,
            active=state == "active",
            failed=state == "failed",
            started_at=_moment(started.get(unit)) if state == "active" else None,
        )
    return found


def _moment(seconds: float | None) -> datetime | None:
    if not seconds:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # A clock the exporter published from a machine whose time is wrong.
        # The unit state is still worth reporting without it.
        return None
