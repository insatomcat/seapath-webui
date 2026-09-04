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
from app.hosts.reader import CommandRunner, SubprocessRunner

logger = logging.getLogger(__name__)

# Block devices that are never OSD candidates and only add noise to the view.
_IGNORED_BLOCK_PREFIXES = ("loop", "ram", "zram", "sr", "dm-", "md")

_SECTOR_BYTES = 512

# The unit file that declares this service on the machine, under the host's
# /etc. Both the ISO and `deploy_seapath_webui` install it at this path.
_QUADLET_PATH = "containers/systemd/seapath-webui.container"


class LocalHostReader:
    """Reads the local machine. Writes nothing, ever."""

    def __init__(
        self,
        root: Path,
        runner: CommandRunner | None = None,
        etc_root: Path | None = None,
    ) -> None:
        self._root = Path(root)
        # Where the host's whole /etc is readable. The quadlet bind mounts
        # /etc/hostname and /etc/os-release directly, because identity is
        # needed before anything else, and it mounts the rest at
        # /run/host/etc for PAM. The tuned profile lives in that second one,
        # so reading it costs no new mount: that is the whole reason this
        # parameter exists rather than a Volume line.
        self._etc_root = Path(etc_root) if etc_root else self._root / "etc"
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

    def _read_etc(self, *parts: str) -> str | None:
        return _text_at(self._etc_root.joinpath(*[p.lstrip("/") for p in parts]))

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

        # The authkey, and never corosync.conf. Debian ships a default
        # corosync.conf in the package itself, which every SEAPATH machine
        # installs, so a standalone node has that file too: reading it as
        # membership made the badge say "cluster" on a machine that had never
        # formed one. The authkey is written by `corosync-keygen` in
        # `configure_ha` and distributed to the members by the same role, so it
        # exists exactly where a cluster was formed.
        #
        # Its presence, never its content. This service must never read the
        # authkey, and a stat does not.
        if not self._path("etc/corosync").is_dir():
            mode = NodeMode.UNKNOWN
            warnings.append(
                "Cluster membership is unknown: /etc/corosync is not mounted "
                "into this container."
            )
        elif self._path("etc/corosync/authkey").exists():
            mode = NodeMode.CLUSTER
        else:
            mode = NodeMode.STANDALONE

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
            # Missing raises no warning. Outside a deployed node there is no
            # unit file to read, and the consequence is already said where it
            # is actionable: a seed that pins nothing, and a System page that
            # reports the inventory naming no image for this machine.
            service_image=self._service_image(),
            warnings=warnings,
        )

    def _service_image(self) -> str | None:
        """The image reference the installed quadlet names for this service.

        Read from the unit file, which is what the machine boots on and what an
        Ansible run rewrites. The running container would be the other source,
        and this service is given no route to podman for it. The file sits in
        the host's /etc that PAM already brought in, so this costs no mount.
        """
        content = self._read_etc(_QUADLET_PATH)
        if content is None:
            return None
        image: str | None = None
        for raw in content.splitlines():
            line = raw.strip()
            if line.startswith(("#", ";")) or "=" not in line:
                continue
            key, _, value = line.partition("=")
            # systemd tolerates spaces around the separator, and a later
            # assignment of the same key wins, so the file is read to its end.
            if key.strip().lower() == "image" and value.strip():
                image = value.strip()
        return image

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

    # Real time

    def realtime(self) -> RealtimeReading:
        """The tuning this machine carries, read from files it already shows.

        Read once, judged nowhere: whether a value is right for a SEAPATH
        hypervisor is `app/services/realtime.py`'s decision, because that is
        where the inventory is, and half the answers are "it matches what you
        declared" rather than an absolute.
        """
        warnings: list[str] = []

        profile = self._read_etc("tuned/active_profile")
        profile_source = "/etc/tuned/active_profile" if profile else None
        installed: bool | None = None
        if profile:
            # `configure_hypervisor` writes the profile under `profiles/`. The
            # distribution ships its own under `/usr/lib/tuned`, so a name
            # found in neither is a profile that does not exist.
            installed = (
                any(
                    self._etc_root.joinpath("tuned", directory, profile).is_dir()
                    for directory in ("profiles", ".")
                )
                or self._path("usr/lib/tuned", profile).is_dir()
            )
        else:
            warnings.append(
                "The tuned profile could not be read. /etc/tuned/active_profile "
                "is absent, which on a converged hypervisor means "
                "configure_hypervisor has not run."
            )

        version = self._read_text("proc/version")
        if version is None:
            warnings.append("/proc/version could not be read.")

        smt_control = self._read_text("sys/devices/system/cpu/smt/control")
        smt_active = _optional_bool(self._read_int("sys/devices/system/cpu/smt/active"))

        isolated = set(
            parse_cpu_list(self._read_text("sys/devices/system/cpu/isolated"))
        )
        if not isolated:
            cmdline = self._read_text("proc/cmdline") or ""
            isolated = set(parse_cpu_list(_kernel_parameter(cmdline, "isolcpus")))
        irq_count, irqs = self._irq_affinity(isolated)

        return RealtimeReading(
            tuned_profile=profile,
            tuned_profile_source=profile_source,
            tuned_profile_installed=installed,
            kernel_version=version,
            preemption=_preemption_model(version),
            smt_active=smt_active,
            smt_control=smt_control,
            sched_rt_runtime_us=self._read_int("proc/sys/kernel/sched_rt_runtime_us"),
            sched_rt_period_us=self._read_int("proc/sys/kernel/sched_rt_period_us"),
            hugepages=self._hugepages(),
            transparent_hugepages=_bracketed(
                self._read_text("sys/kernel/mm/transparent_hugepage/enabled")
            ),
            transparent_hugepage_defrag=_bracketed(
                self._read_text("sys/kernel/mm/transparent_hugepage/defrag")
            ),
            acpi_present=self._path("sys/firmware/acpi").is_dir(),
            irq_count=irq_count,
            irqs_on_isolated_cpus=irqs,
            warnings=warnings,
        )

    def _hugepages(self) -> list[HugepagePool]:
        """Every pool the kernel exposes, machine wide and per NUMA node.

        Both, because the two answer different questions. A VM pinned to one
        socket takes its pages from that socket's pool, so a machine with
        enough pages in total and none on the node the guest sits on fails to
        start with the total looking correct.
        """
        pools: list[HugepagePool] = []
        for base, node in self._hugepage_roots():
            for entry in sorted(_iterdir(base)):
                size = _hugepage_size_kb(entry.name)
                if size is None:
                    continue
                total = _int_at(entry / "nr_hugepages")
                free = _int_at(entry / "free_hugepages")
                if total is None:
                    continue
                pools.append(
                    HugepagePool(size_kb=size, total=total, free=free or 0, node=node)
                )
        return pools

    def _hugepage_roots(self) -> list[tuple[Path, int | None]]:
        roots: list[tuple[Path, int | None]] = [
            (self._path("sys/kernel/mm/hugepages"), None)
        ]
        for entry in sorted(_iterdir(self._path("sys/devices/system/node"))):
            match = re.fullmatch(r"node(\d+)", entry.name)
            if match:
                roots.append((entry / "hugepages", int(match.group(1))))
        return roots

    def _irq_affinity(
        self, isolated: set[int]
    ) -> tuple[int | None, list[IrqOnIsolatedCpu]]:
        """Which interrupts may still be delivered to an isolated CPU.

        `/proc/irq` is the container's own and is not namespaced, so this costs
        no mount. An affinity mask is a permission rather than an observation:
        a interrupt allowed on an isolated CPU is a latency source whether or
        not it has fired there yet, which is exactly the kind of thing that is
        true of the machine rather than of this second.
        """
        root = self._path("proc/irq")
        entries = [entry for entry in _iterdir(root) if entry.name.isdigit()]
        if not entries:
            return None, []
        if not isolated:
            return len(entries), []

        offenders: list[IrqOnIsolatedCpu] = []
        for entry in sorted(entries, key=lambda path: int(path.name)):
            raw = _text_at(entry / "smp_affinity_list")
            if raw is None:
                continue
            overlap = sorted(isolated.intersection(parse_cpu_list(raw)))
            if overlap:
                offenders.append(
                    IrqOnIsolatedCpu(
                        number=entry.name,
                        name=_irq_name(entry),
                        cpus=overlap,
                    )
                )
        return len(entries), offenders

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


def _text_at(path: Path) -> str | None:
    """Read a path that is already absolute.

    The reader's own `_read_text` joins its argument onto the filesystem root,
    which is what makes every parser testable against a recorded tree. A path
    walked out of that tree, one entry of `/proc/irq` for instance, is already
    rooted and must not be joined a second time.
    """
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, UnicodeError):
        return None


def _int_at(path: Path) -> int | None:
    raw = _text_at(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _preemption_model(version: str | None) -> str | None:
    """The preemption the kernel was built with, from `/proc/version`.

    PREEMPT_RT first, because a fully preemptible kernel also carries PREEMPT
    in the same string and matching the shorter one first would report every
    RT kernel as an ordinary preemptible one.
    """
    if not version:
        return None
    for marker in ("PREEMPT_RT", "PREEMPT_DYNAMIC", "PREEMPT", "VOLUNTARY"):
        if marker in version:
            return marker
    return "none"


def _bracketed(value: str | None) -> str | None:
    """The selected entry of a sysfs list, `always [madvise] never`."""
    if not value:
        return None
    match = re.search(r"\[([^\]]+)\]", value)
    return match.group(1) if match else value.strip() or None


def _hugepage_size_kb(name: str) -> int | None:
    match = re.fullmatch(r"hugepages-(\d+)kB", name)
    return int(match.group(1)) if match else None


def _irq_name(entry: Path) -> str | None:
    """The device behind an interrupt, which is the only useful part of it.

    `/proc/irq/<n>/` holds one subdirectory named after the handler. A number
    alone tells an operator nothing, and the name is what says whether an
    interrupt on an isolated CPU is the storage controller or a USB port
    nobody uses.
    """
    for child in _iterdir(entry):
        if child.is_dir() and child.name not in ("smp_affinity_list",):
            return child.name
    return None
