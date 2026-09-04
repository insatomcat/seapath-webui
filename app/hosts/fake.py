# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A fake SEAPATH machine.

This is what the whole test suite runs against, and what the `use_fakes`
development switch serves, so the UI can be built on a laptop with no cluster,
no libvirt and no container. The values describe a plausible freshly installed
standalone hypervisor: converged enough to be interesting, not yet isolated,
with one spare disk that could become an OSD.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.hosts.models import (
    BlockDevice,
    CpuReading,
    CpuTopologyEntry,
    DisksReading,
    HugepagePool,
    InterfaceAddress,
    IrqOnIsolatedCpu,
    NetworkInterface,
    NetworkReading,
    NodeIdentity,
    NodeMode,
    PtpClock,
    RealtimeReading,
)

_BOOT_TIME = datetime(2026, 8, 11, 6, 0, 0, tzinfo=UTC)
_ISOLATED = [4, 5, 6, 7]


class FakeHostReader:
    """Deterministic readings. No randomness, so golden comparisons hold."""

    def __init__(
        self,
        hostname: str = "seapath-machine",
        mode: NodeMode = NodeMode.STANDALONE,
        seapath_distro: str = "Debian",
    ) -> None:
        self.hostname = hostname
        self.mode = mode
        # Which of the five SEAPATH distributions this machine claims to run.
        # A parameter because it decides which prerequisites playbook may be
        # launched, and a test has to be able to be a Yocto machine.
        self.seapath_distro = seapath_distro
        # The tag the ISO installs, so the fake starts where a freshly
        # installed machine does and the seed has an unpinned reference to
        # resolve, which is the case worth exercising.
        self.service_image = "docker.io/insatomcat/seapath-webui:latest"

    def node_identity(self) -> NodeIdentity:
        return NodeIdentity(
            hostname=self.hostname,
            kernel_release="6.1.0-18-rt-amd64",
            distribution="Debian GNU/Linux 12 (bookworm)",
            distribution_id="debian",
            distribution_version="12",
            seapath_distro=self.seapath_distro,
            uptime_seconds=7200.0,
            boot_time=_BOOT_TIME,
            mode=self.mode,
            admin_account="admin",
            service_image=self.service_image,
        )

    def cpu(self) -> CpuReading:
        topology = [
            CpuTopologyEntry(
                cpu=cpu,
                socket=0,
                core=cpu // 2,
                online=True,
                isolated=cpu in _ISOLATED,
                busy_percent=float(cpu),
            )
            for cpu in range(8)
        ]
        return CpuReading(
            model="Intel(R) Xeon(R) Silver 4310 CPU @ 2.10GHz",
            present=8,
            online=8,
            isolated=list(_ISOLATED),
            isolated_source="sysfs",
            nohz_full=list(_ISOLATED),
            housekeeping=[0, 1, 2, 3],
            load_average=[0.15, 0.10, 0.05],
            topology=topology,
            kernel_cmdline=(
                "BOOT_IMAGE=/vmlinuz root=/dev/mapper/main-root ro "
                "isolcpus=4-7 nohz_full=4-7 rcu_nocbs=4-7"
            ),
        )

    def realtime(self) -> RealtimeReading:
        """A converged hypervisor with two findings left on it.

        Deliberately not a clean bill of health. A fake that passes every check
        makes the page look finished while only ever exercising one branch, and
        the two kept here are the two a real machine most often still carries:
        SMT left on by the firmware, and transparent hugepages on `madvise`
        rather than off. One interrupt is left reaching an isolated CPU for the
        same reason.
        """
        return RealtimeReading(
            tuned_profile="seapath-rt-host",
            tuned_profile_source="/etc/tuned/active_profile",
            tuned_profile_installed=True,
            kernel_version=(
                "Linux version 6.1.0-18-rt-amd64 (debian-kernel@lists.debian.org) "
                "#1 SMP PREEMPT_RT Debian 6.1.76-1 (2026-02-01)"
            ),
            preemption="PREEMPT_RT",
            smt_active=True,
            smt_control="on",
            sched_rt_runtime_us=-1,
            sched_rt_period_us=1000000,
            hugepages=[
                HugepagePool(size_kb=1048576, total=8, free=8),
                HugepagePool(size_kb=2048, total=0, free=0),
                HugepagePool(size_kb=1048576, total=8, free=8, node=0),
            ],
            transparent_hugepages="madvise",
            transparent_hugepage_defrag="madvise",
            acpi_present=True,
            irq_count=112,
            irqs_on_isolated_cpus=[
                IrqOnIsolatedCpu(number="34", name="ahci0", cpus=[4]),
            ],
        )

    def network(self) -> NetworkReading:
        return NetworkReading(
            interfaces=[
                NetworkInterface(
                    name="eno1",
                    kind="physical",
                    mac="3c:ec:ef:00:11:22",
                    operstate="up",
                    carrier=True,
                    mtu=1500,
                    speed_mbps=1000,
                    driver="igb",
                    addresses=[
                        InterfaceAddress(
                            address="192.168.200.125", prefix_length=24, family="inet"
                        )
                    ],
                ),
                NetworkInterface(
                    name="eno12419",
                    kind="physical",
                    mac="3c:ec:ef:00:11:33",
                    operstate="up",
                    carrier=True,
                    mtu=1500,
                    speed_mbps=10000,
                    driver="ice",
                ),
                NetworkInterface(
                    name="eno2",
                    kind="physical",
                    mac="3c:ec:ef:00:11:44",
                    operstate="down",
                    carrier=False,
                    mtu=1500,
                    driver="igb",
                ),
                NetworkInterface(
                    name="lo",
                    kind="loopback",
                    operstate="unknown",
                    mtu=65536,
                    addresses=[
                        InterfaceAddress(
                            address="127.0.0.1", prefix_length=8, family="inet"
                        )
                    ],
                ),
            ],
            default_route_interface="eno1",
            default_gateway="192.168.200.1",
        )

    def ptp_clocks(self) -> list[PtpClock]:
        return [PtpClock(device="ptp0", clock_name="ice-ptp")]

    def disks(self) -> DisksReading:
        return DisksReading(
            devices=[
                BlockDevice(
                    name="sda",
                    path="/dev/sda",
                    by_path="/dev/disk/by-path/pci-0000:03:00.0-scsi-0:2:0:0",
                    size_bytes=480_103_981_056,
                    model="PERC H730P",
                    rotational=False,
                    removable=False,
                    read_only=False,
                    partitions=["sda1", "sda2"],
                    holders=[],
                    claimed=True,
                    claim_reason="The device carries partitions.",
                ),
                BlockDevice(
                    name="sdb",
                    path="/dev/sdb",
                    by_path="/dev/disk/by-path/pci-0000:03:00.0-scsi-0:2:1:0",
                    size_bytes=1_920_383_410_176,
                    model="PERC H730P",
                    rotational=False,
                    removable=False,
                    read_only=False,
                    partitions=[],
                    holders=[],
                    claimed=False,
                ),
            ]
        )
