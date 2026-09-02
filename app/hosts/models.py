# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What the read only adapter returns.

Every reading carries `warnings`. A field that could not be read is `None` next
to a sentence saying why, because on a substation hypervisor "unknown" and
"zero" must never look alike.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Reading(BaseModel):
    warnings: list[str] = Field(default_factory=list)


class NodeMode(str, Enum):
    STANDALONE = "standalone"
    CLUSTER = "cluster"
    UNKNOWN = "unknown"


class NodeIdentity(Reading):
    hostname: str
    kernel_release: str | None = None
    distribution: str | None = None
    distribution_id: str | None = None
    distribution_version: str | None = None
    seapath_distro: str | None = None
    """Which of the five SEAPATH distributions this machine runs.

    The same five names `detect_seapath_distro` produces, worked out from
    `/etc/os-release` the way that role works them out from the facts it
    gathers. It is what tells a Debian machine from a Yocto one, and therefore
    which of the five prerequisites playbooks may be launched from here.
    """
    uptime_seconds: float | None = None
    boot_time: datetime | None = None
    mode: NodeMode = NodeMode.UNKNOWN


class CpuTopologyEntry(BaseModel):
    cpu: int
    socket: int | None = None
    core: int | None = None
    online: bool = True
    isolated: bool = False
    busy_percent: float | None = None


class CpuReading(Reading):
    model: str | None = None
    present: int | None = None
    online: int | None = None
    isolated: list[int] = Field(default_factory=list)
    isolated_source: str | None = None
    nohz_full: list[int] = Field(default_factory=list)
    housekeeping: list[int] = Field(default_factory=list)
    # From /proc, which is the container's own and is not namespaced for any of
    # this, so these two cost no mount. That is the line: a live value stays
    # when it is free, and goes when it needs a route to the host.
    load_average: list[float] | None = None
    topology: list[CpuTopologyEntry] = Field(default_factory=list)
    kernel_cmdline: str | None = None


class InterfaceAddress(BaseModel):
    address: str
    prefix_length: int | None = None
    family: str


class NetworkInterface(BaseModel):
    name: str
    kind: str | None = None
    mac: str | None = None
    operstate: str | None = None
    carrier: bool | None = None
    mtu: int | None = None
    speed_mbps: int | None = None
    driver: str | None = None
    master: str | None = None
    addresses: list[InterfaceAddress] = Field(default_factory=list)


class NetworkReading(Reading):
    interfaces: list[NetworkInterface] = Field(default_factory=list)
    default_route_interface: str | None = None
    default_gateway: str | None = None


class PtpClock(BaseModel):
    device: str
    clock_name: str | None = None


class BlockDevice(BaseModel):
    name: str
    path: str
    by_path: str | None = None
    size_bytes: int | None = None
    model: str | None = None
    serial: str | None = None
    rotational: bool | None = None
    removable: bool | None = None
    read_only: bool | None = None
    partitions: list[str] = Field(default_factory=list)
    holders: list[str] = Field(default_factory=list)
    claimed: bool | None = None
    claim_reason: str | None = None


class DisksReading(Reading):
    devices: list[BlockDevice] = Field(default_factory=list)
