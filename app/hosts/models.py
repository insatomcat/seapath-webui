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
    admin_account: str | None = None
    """The account the installer created, meaning the one holding UID 1000.

    It is what `admin_user` has to be seeded with. `configure_seapath_distro`
    reads UID 1000 on the machine and deletes that account when `admin_user`
    names a different one, so a seed inventory that guessed the name wrong
    would remove the operator's own account on the first convergence.
    """
    service_image: str | None = None
    """The image reference the installed quadlet names for this service.

    Read from the unit file on the host, so it is the reference this machine
    actually boots on, whether the ISO installed it or an Ansible run did. It
    is what the seed inventory pins `seapath_webui_image` from, which is how a
    machine that was never edited by hand can still say which code answers on
    it.
    """


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


class HugepagePool(BaseModel):
    """One hugepage size the kernel exposes, and what is allocated in it."""

    size_kb: int
    total: int
    free: int
    node: int | None = None
    """The NUMA node, or None for the machine wide pool."""


class IrqOnIsolatedCpu(BaseModel):
    """An interrupt whose affinity mask still reaches an isolated CPU."""

    number: str
    name: str | None = None
    cpus: list[int] = Field(default_factory=list)


class RealtimeReading(Reading):
    """The real time tuning of this machine, as configured rather than as felt.

    Everything here answers "what is this machine", which is the side of D13
    that stays: the tuned profile that was selected, the preemption model the
    kernel was built with, the pages that were reserved. What the machine is
    *feeling* under load is `prometheus-node-exporter`'s, and what it actually
    delivers in latency is a `cyclictest` run, which is a measurement and has a
    run record of its own.

    Every field comes from a file the container already sees: its own `/proc`,
    which is not namespaced for any of this, the read only `/sys`, and the
    host's `/etc` that PAM already brought in. No mount is added for this
    reading, and none may be: a value that needs a route to tuned, to systemd
    or to the journal is on the far side of the line D13 drew.
    """

    tuned_profile: str | None = None
    tuned_profile_source: str | None = None
    """Which file the profile was read from, because the answer matters.

    `/etc/tuned/active_profile` is what `configure_hypervisor` writes and what
    survives a reboot, so it is the configured profile and the one an
    inventory can be held against. The running daemon's own
    `/run/tuned/active_profile` is live state and is not read here.
    """
    tuned_profile_installed: bool | None = None
    """The profile directory exists under `/etc/tuned/profiles`.

    A machine naming a profile it does not have is a machine tuned by nothing,
    and `tuned-adm active` on the host would still report the name.
    """

    kernel_version: str | None = None
    """`/proc/version`, which is where the PREEMPT_RT build flag appears."""
    preemption: str | None = None
    """`PREEMPT_RT`, `PREEMPT`, `PREEMPT_DYNAMIC`, `voluntary` or `none`."""

    smt_active: bool | None = None
    smt_control: str | None = None

    sched_rt_runtime_us: int | None = None
    sched_rt_period_us: int | None = None

    hugepages: list[HugepagePool] = Field(default_factory=list)
    transparent_hugepages: str | None = None
    """The bracketed choice of `/sys/kernel/mm/transparent_hugepage/enabled`."""
    transparent_hugepage_defrag: str | None = None

    acpi_present: bool | None = None

    irq_count: int | None = None
    irqs_on_isolated_cpus: list[IrqOnIsolatedCpu] = Field(default_factory=list)
    """The interrupts whose `smp_affinity_list` still reaches an isolated CPU.

    Counted rather than described: on a machine with `isolcpus=managed_irq` the
    kernel keeps managed interrupts off the isolated set by itself, so a long
    list here is a finding and an empty one is the expected shape. The
    per-device affinity is `configure_nic_irq_affinity`'s to write, never this
    service's.
    """
    irqs_on_isolated: int | None = None
    """How many there are, which the list above may only summarise.

    The local reader names every one it found and this is simply their number.
    A reading that arrived from an exporter is capped: a machine keeping
    nothing off its isolated cores would otherwise cost one series per
    interrupt, per node, on every scrape. The count stays true either way, and
    it is the count the check reports."""
