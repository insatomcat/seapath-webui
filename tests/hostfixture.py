# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A recorded SEAPATH machine, as a directory tree.

The values are taken from a real Debian hypervisor: an eight CPU machine with
`isolcpus=4-7`, one boot disk carrying partitions and one spare disk that could
become an OSD. Building it in code rather than checking in a tree keeps the
symlinks, which `by-path` and the driver names depend on, portable.
"""

from __future__ import annotations

from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def build_host_tree(root: Path) -> Path:
    # Identity
    _write(root / "etc/hostname", "node1\n")
    _write(
        root / "etc/os-release",
        'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
        'NAME="Debian GNU/Linux"\n'
        "ID=debian\n"
        'VERSION_ID="12"\n',
    )
    # The account list as PAM sees it: the image symlinks /etc/passwd to the
    # host's. `admin` holds UID 1000, which is what the ISO installs and what
    # `admin_user` is seeded from.
    _write(
        root / "etc/passwd",
        "root:x:0:0:root:/root:/bin/bash\n"
        "ansible:x:998:998::/home/ansible:/bin/bash\n"
        "admin:x:1000:1000::/home/admin:/bin/bash\n",
    )
    _write(root / "proc/sys/kernel/osrelease", "6.1.0-18-rt-amd64\n")
    _write(root / "proc/uptime", "125430.12 987654.32\n")
    (root / "etc/corosync").mkdir(parents=True, exist_ok=True)

    # CPU
    cpu = root / "sys/devices/system/cpu"
    _write(cpu / "present", "0-7\n")
    _write(cpu / "online", "0-7\n")
    _write(cpu / "isolated", "4-7\n")
    _write(cpu / "nohz_full", "4-7\n")
    for index in range(8):
        _write(cpu / f"cpu{index}/topology/physical_package_id", "0\n")
        _write(cpu / f"cpu{index}/topology/core_id", str(index // 2) + "\n")
    _write(
        root / "proc/cmdline",
        "BOOT_IMAGE=/vmlinuz root=/dev/mapper/main-root ro isolcpus=4-7 "
        "nohz_full=4-7 rcu_nocbs=4-7\n",
    )
    _write(root / "proc/loadavg", "0.15 0.10 0.05 1/512 4242\n")
    _write(
        root / "proc/cpuinfo",
        "processor\t: 0\nmodel name\t: Intel(R) Xeon(R) Silver 4310 CPU @ 2.10GHz\n",
    )
    write_proc_stat(root, busy=1000, idle=9000)

    # Network
    net = root / "sys/class/net"
    _write(net / "eno1/address", "3c:ec:ef:00:11:22\n")
    _write(net / "eno1/operstate", "up\n")
    _write(net / "eno1/carrier", "1\n")
    _write(net / "eno1/mtu", "1500\n")
    _write(net / "eno1/speed", "1000\n")
    (net / "eno1/device").mkdir(parents=True, exist_ok=True)
    _symlink(net / "eno1/device/driver", "../../../../bus/pci/drivers/igb")
    _write(net / "lo/address", "00:00:00:00:00:00\n")
    _write(net / "lo/operstate", "unknown\n")
    _write(net / "lo/mtu", "65536\n")
    # A virtual device reports -1, which the reader must not present as a speed.
    _write(net / "ovsbr0/address", "3c:ec:ef:00:11:99\n")
    _write(net / "ovsbr0/operstate", "up\n")
    _write(net / "ovsbr0/speed", "-1\n")
    _write(
        root / "proc/net/route",
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eno1\t00000000\t01C8A8C0\t0003\t0\t0\t100\t00000000\n"
        "eno1\t00C8A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n",
    )

    # Real time tuning, as configure_hypervisor leaves it. The profile is read
    # from the host's /etc, which the container sees at /run/host/etc, so the
    # tree carries it under `etc/` and the test points `etc_root` there.
    _write(root / "etc/tuned/active_profile", "seapath-rt-host\n")
    (root / "etc/tuned/profiles/seapath-rt-host").mkdir(parents=True, exist_ok=True)
    _write(
        root / "proc/version",
        "Linux version 6.1.0-18-rt-amd64 (debian-kernel@lists.debian.org) "
        "#1 SMP PREEMPT_RT Debian 6.1.76-1 (2026-02-01)\n",
    )
    _write(cpu / "smt/active", "0\n")
    _write(cpu / "smt/control", "off\n")
    _write(root / "proc/sys/kernel/sched_rt_runtime_us", "-1\n")
    _write(root / "proc/sys/kernel/sched_rt_period_us", "1000000\n")
    _write(root / "sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages", "8\n")
    _write(root / "sys/kernel/mm/hugepages/hugepages-1048576kB/free_hugepages", "8\n")
    _write(root / "sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages", "0\n")
    _write(root / "sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages", "0\n")
    node0 = root / "sys/devices/system/node/node0/hugepages/hugepages-1048576kB"
    _write(node0 / "nr_hugepages", "8\n")
    _write(node0 / "free_hugepages", "8\n")
    thp = root / "sys/kernel/mm/transparent_hugepage"
    _write(thp / "enabled", "always madvise [never]\n")
    _write(thp / "defrag", "always defer [never]\n")
    (root / "sys/firmware/acpi").mkdir(parents=True, exist_ok=True)
    # Two interrupts, one of which is still allowed on an isolated CPU. The
    # named subdirectory is how /proc/irq carries the device behind the number.
    _write(root / "proc/irq/22/smp_affinity_list", "0-3\n")
    (root / "proc/irq/22/eno1").mkdir(parents=True, exist_ok=True)
    _write(root / "proc/irq/34/smp_affinity_list", "4\n")
    (root / "proc/irq/34/ahci0").mkdir(parents=True, exist_ok=True)

    # PTP
    _write(root / "sys/class/ptp/ptp0/clock_name", "ice-ptp\n")

    # Disks: sda is the boot disk, sdb is a free OSD candidate.
    block = root / "sys/block"
    _write(block / "sda/size", "937703088\n")
    _write(block / "sda/removable", "0\n")
    _write(block / "sda/ro", "0\n")
    _write(block / "sda/queue/rotational", "0\n")
    _write(block / "sda/device/model", "PERC H730P      \n")
    _write(block / "sda/sda1/partition", "1\n")
    _write(block / "sda/sda2/partition", "2\n")
    _write(block / "sdb/size", "3750748848\n")
    _write(block / "sdb/removable", "0\n")
    _write(block / "sdb/ro", "0\n")
    _write(block / "sdb/queue/rotational", "0\n")
    _write(block / "sdb/device/model", "PERC H730P      \n")
    (block / "sdb/holders").mkdir(parents=True, exist_ok=True)
    # A device under LVM, which must read as claimed.
    _write(block / "sdc/size", "1024\n")
    (block / "sdc/holders/dm-0").mkdir(parents=True, exist_ok=True)
    # Noise the view must drop.
    _write(block / "loop0/size", "0\n")

    by_path = root / "dev/disk/by-path"
    by_path.mkdir(parents=True, exist_ok=True)
    _symlink(by_path / "pci-0000:03:00.0-scsi-0:2:0:0", "../../sda")
    _symlink(by_path / "pci-0000:03:00.0-scsi-0:2:0:0-part1", "../../sda1")
    _symlink(by_path / "pci-0000:03:00.0-scsi-0:2:1:0", "../../sdb")

    return root


def write_proc_stat(root: Path, busy: int, idle: int) -> None:
    """Rewrite `/proc/stat`, so a test can advance time between two polls."""
    lines = [f"cpu  {busy * 8} 0 0 {idle * 8} 0 0 0 0 0 0"]
    for index in range(8):
        lines.append(f"cpu{index} {busy} 0 0 {idle} 0 0 0 0 0 0")
    _write(root / "proc/stat", "\n".join(lines) + "\n")


def _symlink(link: Path, target: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    link.symlink_to(target)
