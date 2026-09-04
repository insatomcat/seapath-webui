# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The read only adapter as implemented against a real SEAPATH machine.

Everything comes from `/proc`, `/sys` and one read only command, `ip -j addr
show`, because sysfs does not carry IPv4 addresses. The filesystem root is a
parameter so the parsers can be exercised against a recorded tree, which is
what lets the test suite run on a laptop.

Two facts about the container shape this file. The quadlet is unprivileged and
bind mounts the host `/sys` read only, and it uses the host network namespace,
so `/proc/net` and `/sys/class/net` describe the host. What is not visible from
there is reported as unavailable with the reason, never guessed.

What this reading is *for* is the boundary worth holding. It answers the
questions the inventory form asks about hardware: which NICs, which disks by
their stable name, how many CPUs, is there a PTP clock. Live state, meaning
unit states, the journal and the clock offset, is not read here at all: every
node runs prometheus-node-exporter, and a second source of truth for it was
worth neither the mounts nor the code. See docs/deployment.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
from datetime import UTC, datetime
from pathlib import Path

from app.hosts.models import (
    BlockDevice,
    CpuReading,
    CpuTopologyEntry,
    DisksReading,
    InterfaceAddress,
    NetworkInterface,
    NetworkReading,
    NodeIdentity,
    NodeMode,
    PtpClock,
)
from app.hosts.reader import CommandRunner, SubprocessRunner

logger = logging.getLogger(__name__)

# Block devices that are never OSD candidates and only add noise to the view.
_IGNORED_BLOCK_PREFIXES = ("loop", "ram", "zram", "sr", "dm-", "md")

_SECTOR_BYTES = 512


class LocalHostReader:
    """Reads the local machine. Writes nothing, ever."""

    def __init__(self, root: Path, runner: CommandRunner | None = None) -> None:
        self._root = Path(root)
        self._runner = runner or SubprocessRunner()
        # Previous /proc/stat snapshot, so per CPU busy time is a rate between
        # two polls rather than a meaningless number since boot.
        self._previous_cpu_times: dict[int, tuple[int, int]] = {}

    # Filesystem helpers

    def _path(self, *parts: str) -> Path:
        return self._root.joinpath(*[part.lstrip("/") for part in parts])

    def _read_text(self, *parts: str) -> str | None:
        try:
            return self._path(*parts).read_text(errors="replace").strip()
        except (OSError, UnicodeError):
            return None

    def _read_int(self, *parts: str) -> int | None:
        raw = self._read_text(*parts)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    # Node identity

    def node_identity(self) -> NodeIdentity:
        warnings: list[str] = []

        hostname = self._read_text("etc/hostname")
        if not hostname:
            hostname = socket.gethostname()
            warnings.append(
                "The node hostname was read from the container, not from "
                "/etc/hostname. Check that the quadlet mounts it."
            )

        os_release = self._parse_os_release()
        uptime_seconds = None
        boot_time = None
        raw_uptime = self._read_text("proc/uptime")
        if raw_uptime:
            try:
                uptime_seconds = float(raw_uptime.split()[0])
                boot_time = datetime.fromtimestamp(
                    datetime.now(tz=UTC).timestamp() - uptime_seconds,
                    tz=UTC,
                )
            except (ValueError, IndexError):
                warnings.append("/proc/uptime could not be parsed.")

        # The presence of a corosync configuration is what tells a cluster
        # member from a standalone machine, and it is the same signal the other
        # SEAPATH tooling uses. The file only appears once cluster_setup_ha.yaml
        # has run, so its absence is the normal state of a standalone node.
        if self._path("etc/corosync/corosync.conf").exists():
            mode = NodeMode.CLUSTER
        elif self._path("etc/corosync").is_dir():
            mode = NodeMode.STANDALONE
        else:
            mode = NodeMode.UNKNOWN
            warnings.append(
                "Cluster membership is unknown: /etc/corosync is not mounted "
                "into this container."
            )

        admin_account = self._admin_account()
        if admin_account is None:
            warnings.append(
                "No account holds UID 1000, so the administration account "
                "could not be read from the machine."
            )

        return NodeIdentity(
            hostname=hostname,
            kernel_release=self._read_text("proc/sys/kernel/osrelease"),
            distribution=os_release.get("PRETTY_NAME"),
            distribution_id=os_release.get("ID"),
            distribution_version=os_release.get("VERSION_ID"),
            seapath_distro=_seapath_distro(os_release),
            uptime_seconds=uptime_seconds,
            boot_time=boot_time,
            mode=mode,
            admin_account=admin_account,
            warnings=warnings,
        )

    def _admin_account(self) -> str | None:
        """The account holding UID 1000, which the installer created.

        The image symlinks /etc/passwd to the host's, for PAM, so this is the
        machine's own account list. UID 1000 is the question the
        `configure_seapath_distro` role itself asks with `getent passwd 1000`,
        and asking it the same way is what keeps the seed inventory from
        naming an account that would then be deleted.
        """
        raw = self._read_text("etc/passwd")
        if not raw:
            return None
        for line in raw.splitlines():
            fields = line.split(":")
            if len(fields) > 2 and fields[2] == "1000":
                return fields[0]
        return None

    def _parse_os_release(self) -> dict[str, str]:
        raw = self._read_text("etc/os-release")
        if not raw:
            return {}
        values: dict[str, str] = {}
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip().strip('"')
        return values

    # CPU

    def cpu(self) -> CpuReading:
        warnings: list[str] = []
        base = "sys/devices/system/cpu"

        present = parse_cpu_list(self._read_text(base, "present"))
        online = parse_cpu_list(self._read_text(base, "online"))

        isolated = parse_cpu_list(self._read_text(base, "isolated"))
        isolated_source = "sysfs" if isolated else None
        cmdline = self._read_text("proc/cmdline")
        if not isolated and cmdline:
            isolated = parse_cpu_list(_kernel_parameter(cmdline, "isolcpus"))
            isolated_source = "cmdline" if isolated else None
        if not isolated:
            warnings.append(
                "No isolated CPUs. On a hypervisor this usually means "
                "seapath_setup_main.yaml has not been applied yet."
            )

        nohz_full = parse_cpu_list(self._read_text(base, "nohz_full"))
        busy = self._cpu_busy_percent()

        topology = []
        for cpu_id in present or online:
            topology.append(
                CpuTopologyEntry(
                    cpu=cpu_id,
                    socket=self._read_int(
                        base, f"cpu{cpu_id}/topology/physical_package_id"
                    ),
                    core=self._read_int(base, f"cpu{cpu_id}/topology/core_id"),
                    online=cpu_id in online if online else True,
                    isolated=cpu_id in isolated,
                    busy_percent=busy.get(cpu_id),
                )
            )
        if not topology:
            warnings.append("CPU topology is not readable under /sys.")

        load_average = None
        raw_load = self._read_text("proc/loadavg")
        if raw_load:
            try:
                load_average = [float(value) for value in raw_load.split()[:3]]
            except ValueError:
                warnings.append("/proc/loadavg could not be parsed.")

        return CpuReading(
            model=self._cpu_model(),
            present=len(present) or None,
            online=len(online) or None,
            isolated=isolated,
            isolated_source=isolated_source,
            nohz_full=nohz_full,
            housekeeping=[cpu for cpu in (present or online) if cpu not in isolated],
            load_average=load_average,
            topology=topology,
            kernel_cmdline=cmdline,
            warnings=warnings,
        )

    def _cpu_model(self) -> str | None:
        raw = self._read_text("proc/cpuinfo")
        if not raw:
            return None
        for line in raw.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in ("model name", "Model"):
                return value.strip()
        return None

    def _cpu_busy_percent(self) -> dict[int, float]:
        """Busy ratio per CPU between this poll and the previous one.

        The first call after a start returns nothing rather than the average
        since boot, which on a machine up for months says nothing useful.
        """
        raw = self._read_text("proc/stat")
        if not raw:
            return {}
        current: dict[int, tuple[int, int]] = {}
        for line in raw.splitlines():
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            fields = line.split()
            try:
                cpu_id = int(fields[0][3:])
                values = [int(value) for value in fields[1:]]
            except (ValueError, IndexError):
                continue
            total = sum(values)
            idle = sum(values[3:5]) if len(values) >= 5 else values[3]
            current[cpu_id] = (total, idle)

        busy: dict[int, float] = {}
        for cpu_id, (total, idle) in current.items():
            previous = self._previous_cpu_times.get(cpu_id)
            if previous is None:
                continue
            total_delta = total - previous[0]
            idle_delta = idle - previous[1]
            if total_delta > 0:
                busy[cpu_id] = round(
                    100.0 * (total_delta - idle_delta) / total_delta, 1
                )
        self._previous_cpu_times = current
        return busy

    # Network

    def network(self) -> NetworkReading:
        warnings: list[str] = []
        addresses = self._interface_addresses(warnings)

        interfaces: list[NetworkInterface] = []
        net_root = self._path("sys/class/net")
        try:
            names = sorted(entry.name for entry in net_root.iterdir())
        except OSError:
            return NetworkReading(
                warnings=["/sys/class/net is not readable from this container."]
            )

        for name in names:
            device = net_root / name
            speed = self._read_int("sys/class/net", name, "speed")
            interfaces.append(
                NetworkInterface(
                    name=name,
                    kind=_interface_kind(device),
                    mac=self._read_text("sys/class/net", name, "address"),
                    operstate=self._read_text("sys/class/net", name, "operstate"),
                    carrier=_optional_bool(
                        self._read_int("sys/class/net", name, "carrier")
                    ),
                    mtu=self._read_int("sys/class/net", name, "mtu"),
                    # Virtual devices report -1, which is not a speed.
                    speed_mbps=speed if speed and speed > 0 else None,
                    driver=_symlink_name(device / "device" / "driver"),
                    master=_symlink_name(device / "master"),
                    addresses=addresses.get(name, []),
                )
            )

        route_interface, gateway = self._default_route()
        return NetworkReading(
            interfaces=interfaces,
            default_route_interface=route_interface,
            default_gateway=gateway,
            warnings=warnings,
        )

    def _interface_addresses(
        self, warnings: list[str]
    ) -> dict[str, list[InterfaceAddress]]:
        """Addresses come from iproute2: sysfs does not carry IPv4 addresses."""
        result = self._runner.run(["ip", "-j", "addr", "show"])
        if not result.ok:
            warnings.append(
                "Interface addresses are unavailable: "
                + (result.stderr.strip() or "ip returned an error")
            )
            return {}
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            warnings.append("The output of `ip -j addr show` could not be parsed.")
            return {}

        addresses: dict[str, list[InterfaceAddress]] = {}
        for entry in payload:
            name = entry.get("ifname")
            if not name:
                continue
            addresses[name] = [
                InterfaceAddress(
                    address=info.get("local", ""),
                    prefix_length=info.get("prefixlen"),
                    family=info.get("family", ""),
                )
                for info in entry.get("addr_info", [])
                if info.get("local")
            ]
        return addresses

    def _default_route(self) -> tuple[str | None, str | None]:
        raw = self._read_text("proc/net/route")
        if not raw:
            return None, None
        for line in raw.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 3 or fields[1] != "00000000":
                continue
            return fields[0], _hex_to_ipv4(fields[2])
        return None, None

    # PTP

    def ptp_clocks(self) -> list[PtpClock]:
        """The hardware clocks this machine carries.

        Whether the clock is disciplined, and by how far it is off, is a metric
        the node exporter already publishes. What no exporter answers is the
        question discovery asks: does this machine have a PTP capable clock at
        all, which is what decides whether the form offers a `ptp_interface`.
        """
        try:
            entries = sorted(self._path("sys/class/ptp").iterdir())
        except OSError:
            # No clock, or no /sys. Both mean the same to the form, and a
            # machine with no PTP hardware is an ordinary observer node.
            return []
        return [
            PtpClock(
                device=entry.name,
                clock_name=self._read_text("sys/class/ptp", entry.name, "clock_name"),
            )
            for entry in entries
        ]

    # Disks

    def disks(self) -> DisksReading:
        # A warning is something that went wrong on this machine, and an
        # operator learns to read the banner only as long as that stays true.
        # How the claim state is derived, and that a whole disk filesystem is
        # invisible from /sys, is a permanent property of the reading: it
        # belongs next to the table, not in the banner. The disks card says it.
        warnings: list[str] = []
        by_path = self._by_path_index()
        if not by_path:
            warnings.append(
                "Stable /dev/disk/by-path names are unavailable, and those are "
                "the names ceph_osd_disks must use."
            )

        devices: list[BlockDevice] = []
        block_root = self._path("sys/block")
        try:
            names = sorted(entry.name for entry in block_root.iterdir())
        except OSError:
            return DisksReading(warnings=["/sys/block is not readable."])

        for name in names:
            if name.startswith(_IGNORED_BLOCK_PREFIXES):
                continue
            device = block_root / name
            partitions = sorted(
                entry.name
                for entry in _iterdir(device)
                if (entry / "partition").exists()
            )
            holders = sorted(entry.name for entry in _iterdir(device / "holders"))
            size_sectors = self._read_int("sys/block", name, "size")
            claim_reason = None
            if partitions:
                claim_reason = "The device carries partitions."
            elif holders:
                claim_reason = f"The device is held by {', '.join(holders)}."
            devices.append(
                BlockDevice(
                    name=name,
                    path=f"/dev/{name}",
                    by_path=by_path.get(name),
                    size_bytes=size_sectors * _SECTOR_BYTES if size_sectors else None,
                    model=self._read_text("sys/block", name, "device/model"),
                    serial=self._read_text("sys/block", name, "device/serial"),
                    rotational=_optional_bool(
                        self._read_int("sys/block", name, "queue/rotational")
                    ),
                    removable=_optional_bool(
                        self._read_int("sys/block", name, "removable")
                    ),
                    read_only=_optional_bool(self._read_int("sys/block", name, "ro")),
                    partitions=partitions,
                    holders=holders,
                    claimed=bool(partitions or holders),
                    claim_reason=claim_reason,
                )
            )
        return DisksReading(devices=devices, warnings=warnings)

    def _by_path_index(self) -> dict[str, str]:
        """Map a kernel device name to its stable `by-path` name.

        `ceph_osd_disks` is written in `by-path` form in the reference
        inventory, and for good reason: `sda` is not stable across a reboot,
        and an OSD created on the wrong disk destroys its contents.
        """
        index: dict[str, str] = {}
        directory = self._path("dev/disk/by-path")
        for entry in sorted(_iterdir(directory), key=lambda path: path.name):
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            # A partition link points at sda1 and never names a whole disk, so
            # the first link found for a device name wins and later ones, which
            # are aliases of the same disk, do not overwrite it.
            index.setdefault(Path(target).name, f"/dev/disk/by-path/{entry.name}")
        return index


# Parsing helpers, kept module level so the tests can hit them directly.


def read_hostname(root: Path) -> str:
    """The node's name, which is not the name this process runs under.

    The container has its own UTS namespace, and `Network=host` does not share
    it, so `socket.gethostname()` returns a container id. Anything an operator
    reads as the machine's name has to come from the mounted `/etc/hostname`,
    including the common name of the certificate whose fingerprint they are
    asked to compare.
    """
    try:
        hostname = (Path(root) / "etc/hostname").read_text(errors="replace").strip()
    except (OSError, UnicodeError):
        hostname = ""
    return hostname or socket.gethostname()


def read_admin_address(root: Path, runner: CommandRunner | None = None) -> str | None:
    """The administration address of this machine, or None if it has none.

    Defined exactly as the inventory form defines `ip_addr`: the IPv4 address
    of the interface carrying the default route. Reading it here rather than
    from the inventory is what lets a fresh ISO bind a precise address at first
    boot, when no inventory exists yet and the machine still has to be reached.
    """
    network = LocalHostReader(root=root, runner=runner).network()
    if not network.default_route_interface:
        return None
    for interface in network.interfaces:
        if interface.name != network.default_route_interface:
            continue
        for address in interface.addresses:
            if address.family == "inet" and address.address:
                return address.address
    return None


def parse_cpu_list(raw: str | None) -> list[int]:
    """Parse the kernel's `0-3,8` CPU list syntax."""
    if not raw:
        return []
    cpus: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start, separator, end = chunk.partition("-")
        try:
            if separator:
                cpus.extend(range(int(start), int(end) + 1))
            else:
                cpus.append(int(start))
        except ValueError:
            # `isolcpus=nohz,domain,4-7` carries flags before the list.
            continue
    return sorted(set(cpus))


# What `detect_seapath_distro` answers, worked out from `/etc/os-release`.
#
# The role reads `ansible_distribution`, a fact Ansible builds from the same
# file, and matches it with the regexes below, then falls back to grepping
# `CPE_NAME` for openembedded to recognise Yocto. Read here rather than asked
# of Ansible because this answer is needed to decide whether to offer a button,
# which happens long before any run.
#
# Kept in the order the role uses, since Oracle Linux carries `ID_LIKE` naming
# Red Hat and would otherwise answer CentOS.
_SEAPATH_DISTROS = (
    ("Debian", re.compile(r"debian", re.I)),
    ("OracleLinux", re.compile(r"oracle", re.I)),
    ("CentOS", re.compile(r"centos|red\s*hat|rhel", re.I)),
    ("SLES", re.compile(r"sles|suse", re.I)),
)


def _seapath_distro(os_release: dict[str, str]) -> str | None:
    # Yocto first: a SEAPATH Yocto image names itself in ways the regexes above
    # do not catch, and `CPE_NAME` is the signal the role itself trusts.
    if "cpe:/o:openembedded" in os_release.get("CPE_NAME", ""):
        return "Yocto"

    # ID before NAME: `ID=debian` is the machine readable one, and a
    # PRETTY_NAME can carry a vendor string that names two distributions.
    for field in ("ID", "NAME", "PRETTY_NAME"):
        value = os_release.get(field)
        if not value:
            continue
        for distro, pattern in _SEAPATH_DISTROS:
            if pattern.search(value):
                return distro
    return None


def _kernel_parameter(cmdline: str, name: str) -> str | None:
    for token in cmdline.split():
        key, separator, value = token.partition("=")
        if separator and key == name:
            return value
    return None


def _optional_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _symlink_name(path: Path) -> str | None:
    try:
        return Path(os.readlink(path)).name
    except OSError:
        return None


def _iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _interface_kind(device: Path) -> str | None:
    for marker, kind in (
        ("bridge", "bridge"),
        ("bonding", "bond"),
        ("tun_flags", "tun"),
    ):
        if (device / marker).exists():
            return kind
    if (device / "device").exists():
        return "physical"
    if device.name == "lo":
        return "loopback"
    return "virtual"


def _hex_to_ipv4(value: str) -> str | None:
    """`/proc/net/route` stores addresses as little endian hexadecimal."""
    try:
        raw = int(value, 16)
    except ValueError:
        return None
    return ".".join(str((raw >> shift) & 0xFF) for shift in (0, 8, 16, 24))
