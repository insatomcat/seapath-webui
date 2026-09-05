# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What Ceph is doing, read from the manager's own Prometheus module.

`roles/cephadm` enables `ceph mgr module enable prometheus`, and the active
manager then publishes the whole cluster on port 9283: health, the daemons,
the OSD map, the pools and the placement groups. That exposition is Ceph's own
view of itself, computed by the manager that holds it, which is why this reads
it rather than running `ceph -s` over SSH and parsing the result.

**Which node answers.** Only the active manager serves this. A standby answers
the request and publishes nothing, or redirects to the active one, so the
readings are asked of every machine of the inventory and the first exposition
carrying `ceph_health_status` is the cluster's. The page names it.

**Ceph is optional.** A Pacemaker cluster with local storage is a supported
SEAPATH configuration, and no node answering here means exactly that: no Ceph,
which is a sentence rather than a failure. See `docs/ceph.md`.

What is deliberately not read: throughput and IOPS. Both are counters, a rate
needs two scrapes and a memory of the first, and that is the monitoring system
D13 sent to Prometheus. The dashboard this is modelled on draws them because it
has a time series database behind it. This page has one scrape.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field

from app.cluster import metrics
from app.cluster.exporters import Exposition, value

# Where `ceph mgr module enable prometheus` listens. Not a SEAPATH choice: it
# is the module's own default, and cephadm opens it on every manager host.
DEFAULT_PORT = 9283

_HEALTH = "ceph_health_status"
_HEALTH_DETAIL = "ceph_health_detail"
_TOTAL_BYTES = "ceph_cluster_total_bytes"
_USED_BYTES = "ceph_cluster_total_used_bytes"
_RAW_USED_BYTES = "ceph_cluster_total_used_raw_bytes"

_MON_METADATA = "ceph_mon_metadata"
_MON_QUORUM = "ceph_mon_quorum_status"
_MGR_METADATA = "ceph_mgr_metadata"
_MGR_STATUS = "ceph_mgr_status"
_MDS_METADATA = "ceph_mds_metadata"

_OSD_METADATA = "ceph_osd_metadata"
_OSD_UP = "ceph_osd_up"
_OSD_IN = "ceph_osd_in"
_OSD_BYTES = "ceph_osd_stat_bytes"
_OSD_BYTES_USED = "ceph_osd_stat_bytes_used"
_OSD_NUMPG = "ceph_osd_numpg"
_OSD_APPLY_LATENCY = "ceph_osd_apply_latency_ms"
_OSD_COMMIT_LATENCY = "ceph_osd_commit_latency_ms"

_POOL_METADATA = "ceph_pool_metadata"
_POOL_STORED = "ceph_pool_stored"
_POOL_USED = "ceph_pool_bytes_used"
_POOL_AVAILABLE = "ceph_pool_max_avail"
_POOL_OBJECTS = "ceph_pool_objects"
_POOL_PERCENT_USED = "ceph_pool_percent_used"

_PG_TOTAL = "ceph_pg_total"
# The states the page names when a placement group is in one. Clean and active
# are the two that mean nothing is wrong, and the rest are what `ceph -s`
# prints after the count. Read from a fixed list rather than by prefix, because
# `ceph_pg_*` also holds families that are not states at all.
_PG_STATES = (
    "active",
    "clean",
    "peering",
    "degraded",
    "undersized",
    "unknown",
    "stale",
    "inconsistent",
    "incomplete",
    "down",
    "recovering",
    "recovery_wait",
    "backfilling",
    "backfill_wait",
    "backfill_toofull",
    "forced_recovery",
    "forced_backfill",
    "remapped",
    "repair",
    "scrubbing",
    "deep",
    "snaptrim",
    "snaptrim_wait",
)
# What `ceph_health_status` counts in.
_HEALTH_WORDS = {0: "HEALTH_OK", 1: "HEALTH_WARN", 2: "HEALTH_ERR"}


class CephMessage(BaseModel):
    """One entry of `ceph health detail`, which is why the health is not OK."""

    name: str
    severity: str = ""
    """`HEALTH_WARN` or `HEALTH_ERR`, when the release publishes it."""


class CephDaemon(BaseModel):
    """A monitor, a manager or a metadata server."""

    kind: str
    name: str
    """The `ceph_daemon` label, such as `mon.ccv1`."""
    host: str = ""
    version: str = ""
    state: str = ""
    """`in quorum`, `out of quorum`, `active`, `standby`, or empty."""
    ok: bool = True


class CephOsd(BaseModel):
    name: str
    id: int | None = None
    host: str = ""
    device_class: str = ""
    devices: str = ""
    version: str = ""
    up: bool = False
    in_cluster: bool = False
    total_bytes: float = 0.0
    used_bytes: float = 0.0
    pgs: int = 0
    apply_latency_ms: float | None = None
    commit_latency_ms: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def used_ratio(self) -> float | None:
        """A computed field rather than a property, so the page receives it."""
        if self.total_bytes <= 0:
            return None
        return self.used_bytes / self.total_bytes


class CephPool(BaseModel):
    id: int | None = None
    name: str
    type: str = ""
    stored_bytes: float = 0.0
    used_bytes: float = 0.0
    """The raw capacity the pool occupies, replication included."""
    available_bytes: float = 0.0
    objects: int = 0
    percent_used: float | None = None


class CephCluster(BaseModel):
    """What `GET /api/v1/storage` answers."""

    available: bool = False
    error: str = ""
    """Why there is nothing to show, said as what an operator can act on."""
    source: str | None = None
    """Which node's manager answered."""
    health: str = "unknown"
    health_status: int | None = None
    messages: list[CephMessage] = Field(default_factory=list)
    """What Ceph itself says is wrong, when the release publishes the detail."""

    total_bytes: float = 0.0
    used_bytes: float = 0.0
    raw_used_bytes: float = 0.0
    objects: int = 0

    monitors: list[CephDaemon] = Field(default_factory=list)
    managers: list[CephDaemon] = Field(default_factory=list)
    metadata_servers: list[CephDaemon] = Field(default_factory=list)
    osds: list[CephOsd] = Field(default_factory=list)
    pools: list[CephPool] = Field(default_factory=list)

    placement_groups: int = 0
    pg_states: dict[str, int] = Field(default_factory=dict)
    """Every state some placement group is in, and how many are in it."""
    versions: dict[str, int] = Field(default_factory=dict)
    """How many daemons run each Ceph version, which is what an upgrade moves."""

    this_host: str | None = None
    inventory_commit: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def used_ratio(self) -> float | None:
        if self.total_bytes <= 0:
            return None
        return self.used_bytes / self.total_bytes

    # Computed fields rather than plain properties: these four are the numbers
    # the page leads with, and a property is invisible to `model_dump`, so the
    # browser would have to count them again, in a second place, in another
    # language.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def monitors_in_quorum(self) -> int:
        return sum(1 for daemon in self.monitors if daemon.ok)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def osds_up(self) -> int:
        return sum(1 for osd in self.osds if osd.up)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def osds_in(self) -> int:
        return sum(1 for osd in self.osds if osd.in_cluster)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pgs_not_clean(self) -> int:
        """How many placement groups are not clean.

        The subtraction rather than a sum over the unhealthy states: a group
        that is degraded and undersized at the same time is in two of them, and
        adding the states up reports twice as many groups as the cluster has.
        """
        if not self.placement_groups:
            return 0
        return max(0, self.placement_groups - self.pg_states.get("clean", 0))


class CephReader:
    """One manager's exposition, turned into the cluster above."""

    def read(self, exposition: Exposition) -> CephCluster:
        series = exposition.series or {}
        status = value(series, _HEALTH)
        monitors = _monitors(series)
        managers = _managers(series)
        metadata_servers = _daemons(series, _MDS_METADATA, "mds")
        osds = _osds(series)
        return CephCluster(
            available=True,
            source=exposition.host,
            health=(
                _HEALTH_WORDS.get(int(status), "unknown")
                if status is not None
                else "unknown"
            ),
            health_status=int(status) if status is not None else None,
            messages=_messages(series),
            total_bytes=value(series, _TOTAL_BYTES, 0.0) or 0.0,
            used_bytes=value(series, _USED_BYTES, 0.0) or 0.0,
            raw_used_bytes=value(series, _RAW_USED_BYTES, 0.0) or 0.0,
            objects=int(sum(s.value for s in series.get(_POOL_OBJECTS, []))),
            monitors=monitors,
            managers=managers,
            metadata_servers=metadata_servers,
            osds=osds,
            pools=_pools(series),
            placement_groups=int(value(series, _PG_TOTAL, 0.0) or 0.0),
            pg_states=_pg_states(series),
            versions=_versions([*monitors, *managers, *metadata_servers], osds),
        )


def _messages(series: dict[str, list[metrics.Sample]]) -> list[CephMessage]:
    """The health checks Ceph is currently raising.

    Published from Pacific on. An older cluster answers the status and no
    detail, and the page says the health without pretending to know why.
    """
    return sorted(
        (
            CephMessage(
                name=sample.labels.get("name", ""),
                severity=sample.labels.get("severity", ""),
            )
            for sample in series.get(_HEALTH_DETAIL, [])
            if sample.value and sample.labels.get("name")
        ),
        key=lambda message: message.name,
    )


def _monitors(series: dict[str, list[metrics.Sample]]) -> list[CephDaemon]:
    quorum = _by_daemon(series, _MON_QUORUM)
    monitors = _daemons(series, _MON_METADATA, "mon")
    for daemon in monitors:
        in_quorum = bool(quorum.get(daemon.name, 0.0))
        daemon.state = "in quorum" if in_quorum else "out of quorum"
        daemon.ok = in_quorum
    return monitors


def _managers(series: dict[str, list[metrics.Sample]]) -> list[CephDaemon]:
    """The managers, with the active one named.

    `ceph_mgr_status` is 1 on the active manager. It is published by that same
    manager, so a standby is only ever seen through the active one's metadata,
    which is exactly how `ceph -s` prints it.
    """
    status = _by_daemon(series, _MGR_STATUS)
    managers = _daemons(series, _MGR_METADATA, "mgr")
    for daemon in managers:
        active = bool(status.get(daemon.name, 0.0))
        daemon.state = "active" if active else "standby"
        # A standby is not a finding: a cluster with one active manager and two
        # standbys is the configuration cephadm deploys.
        daemon.ok = True
    return managers


def _daemons(
    series: dict[str, list[metrics.Sample]], family: str, kind: str
) -> list[CephDaemon]:
    return sorted(
        (
            CephDaemon(
                kind=kind,
                name=sample.labels.get("ceph_daemon", ""),
                host=sample.labels.get("hostname", ""),
                version=_version(sample.labels.get("ceph_version", "")),
            )
            for sample in series.get(family, [])
            if sample.labels.get("ceph_daemon")
        ),
        key=lambda daemon: daemon.name,
    )


def _osds(series: dict[str, list[metrics.Sample]]) -> list[CephOsd]:
    found: dict[str, CephOsd] = {}
    for sample in series.get(_OSD_METADATA, []):
        name = sample.labels.get("ceph_daemon", "")
        if not name:
            continue
        found[name] = CephOsd(
            name=name,
            id=_number(name),
            host=sample.labels.get("hostname", ""),
            device_class=sample.labels.get("device_class", ""),
            devices=sample.labels.get("devices", ""),
            version=_version(sample.labels.get("ceph_version", "")),
        )

    # An OSD the map knows and the metadata does not is still an OSD, and a
    # down one is exactly the case this page exists for, so the up and in
    # series create rows of their own rather than only decorating existing ones.
    for family in (_OSD_UP, _OSD_IN):
        for sample in series.get(family, []):
            name = sample.labels.get("ceph_daemon", "")
            if name and name not in found:
                found[name] = CephOsd(name=name, id=_number(name))

    up = _by_daemon(series, _OSD_UP)
    within = _by_daemon(series, _OSD_IN)
    total = _by_daemon(series, _OSD_BYTES)
    used = _by_daemon(series, _OSD_BYTES_USED)
    pgs = _by_daemon(series, _OSD_NUMPG)
    apply_latency = _by_daemon(series, _OSD_APPLY_LATENCY)
    commit_latency = _by_daemon(series, _OSD_COMMIT_LATENCY)
    for name, osd in found.items():
        osd.up = bool(up.get(name, 0.0))
        osd.in_cluster = bool(within.get(name, 0.0))
        osd.total_bytes = total.get(name, 0.0)
        osd.used_bytes = used.get(name, 0.0)
        osd.pgs = int(pgs.get(name, 0.0))
        osd.apply_latency_ms = apply_latency.get(name)
        osd.commit_latency_ms = commit_latency.get(name)
    # An OSD whose number could not be read sorts after the numbered ones
    # rather than crashing the comparison against them.
    return sorted(
        found.values(), key=lambda osd: (osd.id is None, osd.id or 0, osd.name)
    )


def _pools(series: dict[str, list[metrics.Sample]]) -> list[CephPool]:
    by_id: dict[str, CephPool] = {}
    for sample in series.get(_POOL_METADATA, []):
        pool_id = sample.labels.get("pool_id", "")
        name = sample.labels.get("name", "")
        if not pool_id or not name:
            continue
        by_id[pool_id] = CephPool(
            id=_int(pool_id),
            name=name,
            type=sample.labels.get("type", ""),
        )

    for family, attribute in (
        (_POOL_STORED, "stored_bytes"),
        (_POOL_USED, "used_bytes"),
        (_POOL_AVAILABLE, "available_bytes"),
    ):
        for sample in series.get(family, []):
            pool = by_id.get(sample.labels.get("pool_id", ""))
            if pool is not None:
                setattr(pool, attribute, sample.value)
    for sample in series.get(_POOL_OBJECTS, []):
        pool = by_id.get(sample.labels.get("pool_id", ""))
        if pool is not None:
            pool.objects = int(sample.value)
    for sample in series.get(_POOL_PERCENT_USED, []):
        pool = by_id.get(sample.labels.get("pool_id", ""))
        if pool is not None:
            pool.percent_used = sample.value
    return sorted(by_id.values(), key=lambda pool: pool.name)


def _pg_states(series: dict[str, list[metrics.Sample]]) -> dict[str, int]:
    """Every state some placement group is in, empty states left out.

    A cluster at rest has twenty-odd families at zero, and a table of zeroes
    buries the one line that is not.
    """
    found: dict[str, int] = {}
    for state in _PG_STATES:
        count = value(series, f"ceph_pg_{state}")
        if count:
            found[state] = int(count)
    return found


def _versions(daemons: list[CephDaemon], osds: list[CephOsd]) -> dict[str, int]:
    found: dict[str, int] = {}
    for version in [daemon.version for daemon in daemons] + [
        osd.version for osd in osds
    ]:
        if version:
            found[version] = found.get(version, 0) + 1
    return dict(sorted(found.items()))


def _by_daemon(
    series: dict[str, list[metrics.Sample]], family: str
) -> dict[str, float]:
    return {
        sample.labels.get("ceph_daemon", ""): sample.value
        for sample in series.get(family, [])
    }


def _version(raw: str) -> str:
    """The release out of the version banner Ceph publishes.

    `ceph_version` is the whole `ceph version 17.2.7 (sha) quincy (stable)`
    line, and what a page compares across daemons is the number and the name.
    """
    parts = raw.split()
    if len(parts) >= 5 and parts[:2] == ["ceph", "version"]:
        # The number, then the release name that follows the commit hash.
        return f"{parts[2]} {parts[4]}"
    return raw


def _number(daemon: str) -> int | None:
    _, _, tail = daemon.partition(".")
    return _int(tail)


def _int(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def reporting(exposition: Exposition) -> bool:
    """Whether this node is the manager serving the cluster's metrics.

    A standby manager answers the request and publishes nothing under this
    name, which is how the active one is found without asking Ceph who it is.
    """
    return exposition.has(_HEALTH)
