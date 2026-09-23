# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What a machine and its workloads have consumed, as counters.

Three exporters answer, one per kind of thing that uses a machine:
`node_exporter` for the machine as a whole, `libvirt-exporter` for each guest
and `prometheus-podman-exporter` for each container. All three are deployed by
`deploy_prometheus_exporters` on every hypervisor, and the page reads them the
way the others read theirs: one GET each, on a port that is already open.

What comes out of here is counters and the moment they were read.
A rate needs two readings and a memory of the first, and that memory lives in
the browser that asked for both: this service answers each reading and forgets
it. D67 says why the line sits there.

Every figure is kept as the exporter said it, in bytes and seconds. A counter
the exporter did not publish is None rather than 0, because 0 is a value a
counter has, and the browser divides by the difference between two of them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.cluster import metrics
from app.cluster.exporters import Exposition, value

# Where `deploy_prometheus_exporters` publishes podman's, from the quadlet it
# templates: `PublishPort={{ listen_address }}:9882:9882`.
PODMAN_PORT = 9882

# The CPU modes in which a CPU is doing nothing. `iowait` is idle time spent
# with I/O outstanding: the CPU itself was free to run anything else.
_IDLE_MODES = frozenset({"idle", "iowait"})

# Filesystems that hold no data of their own: memory, the kernel's views, and
# the layers podman stacks for its containers. A page listing them lists every
# tmpfs of every container, and none of them is a disk that can fill.
_PSEUDO_FILESYSTEMS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "securityfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
)

# Block devices that are another device seen again. A logical volume's I/O is
# already counted on the disk under it, and a loop or RAM device is not a disk.
_LAYERED_DISKS = ("dm-", "loop", "ram", "zram", "md", "nbd")

# Interfaces that carry a workload's traffic rather than the machine's own.
# A guest's `vnet` and a container's `veth` are counted again on the bridge or
# the physical port behind them, and their traffic is reported per workload
# from the libvirt and podman exporters, where it has a name.
_VIRTUAL_INTERFACES = ("veth", "vnet", "tap", "podman", "virbr", "cni", "docker")
_VIRTUAL_EXACT = frozenset({"ovs-system"})

# podman's own codes, from the exporter's HELP line.
_CONTAINER_STATES = {
    -1: "unknown",
    0: "created",
    1: "initialized",
    2: "running",
    3: "stopped",
    4: "paused",
    5: "exited",
    6: "removing",
    7: "stopping",
}


class CpuCounters(BaseModel):
    """CPU time of a set of CPUs, summed over every mode, and the idle part of it."""

    cpus: int = 0
    total_seconds: float = 0.0
    idle_seconds: float = 0.0


class MemoryReading(BaseModel):
    """`/proc/meminfo`, as node_exporter publishes it. A gauge, read as it is."""

    total_bytes: int | None = None
    available_bytes: int | None = None
    free_bytes: int | None = None
    buffers_bytes: int | None = None
    cached_bytes: int | None = None
    hugepages_total_bytes: int | None = None
    """The hugepage pool, whether or not a guest holds it. The kernel takes it
    out of `MemAvailable` whole, so a page that did not show it would report
    the memory it reserves as used by nothing."""
    hugepages_free_bytes: int | None = None
    swap_total_bytes: int | None = None
    swap_free_bytes: int | None = None


class Filesystem(BaseModel):
    mountpoint: str
    device: str
    fstype: str
    size_bytes: int
    available_bytes: int
    readonly: bool = False


class DiskCounters(BaseModel):
    device: str
    read_bytes: float | None = None
    written_bytes: float | None = None
    io_time_seconds: float | None = None
    """Time the device had I/O in flight. Its rate is how busy the device was."""


class InterfaceCounters(BaseModel):
    device: str
    operstate: str = ""
    speed_bytes: int | None = None
    """Bytes per second, None where the link reports none (down, or virtual)."""
    receive_bytes: float | None = None
    transmit_bytes: float | None = None
    kind: str = "logical"
    """`physical`, a port with an address burnt into it; `logical`, a team, a
    bond, a bridge or a VLAN over ports; `workload`, the end of a guest's or a
    container's link; or `loopback`. The machine's traffic is the sum of its
    physical ports, since everything else is that traffic counted again."""


class NodeUsage(BaseModel):
    """The machine as a whole, from its node_exporter."""

    read_at: float
    """The machine's own clock at the scrape, `node_time_seconds`. Rates of
    the counters below are taken over the difference of two of these."""
    boot_time: float | None = None
    housekeeping: CpuCounters = Field(default_factory=CpuCounters)
    isolated: CpuCounters = Field(default_factory=CpuCounters)
    """The CPUs the kernel was booted with in `isolcpus`, kept apart because
    on a SEAPATH hypervisor they belong to real time guests, which may spin on
    them at full load by design. One figure for the whole machine mixes the
    two, and answers for neither."""
    load1: float | None = None
    load5: float | None = None
    load15: float | None = None
    memory: MemoryReading = Field(default_factory=MemoryReading)
    filesystems: list[Filesystem] = Field(default_factory=list)
    disks: list[DiskCounters] = Field(default_factory=list)
    interfaces: list[InterfaceCounters] = Field(default_factory=list)


class GuestUsage(BaseModel):
    """One libvirt domain, from the exporter on the machine running it."""

    name: str
    running: bool
    vcpus: int | None = None
    cpu_seconds: float | None = None
    """Every thread of the domain's QEMU process, vCPUs and emulator alike."""
    memory_bytes: int | None = None
    """What the guest holds on the machine: the resident set of its QEMU
    process when libvirt has one to report, its current allocation
    otherwise. A guest backed by hugepages holds them outside its resident set,
    and they are in the machine's hugepage pool instead."""
    disk_read_bytes: float | None = None
    disk_written_bytes: float | None = None
    network_receive_bytes: float | None = None
    network_transmit_bytes: float | None = None
    interfaces: list[str] = Field(default_factory=list)
    """The machine side device of each of its interfaces, `vnet0` and its
    family, or whatever `<target dev>` the domain names. They are the
    machine's interfaces that carry this guest's traffic."""


class ContainerUsage(BaseModel):
    """One podman container, from the exporter on the machine running it."""

    name: str
    id: str
    image: str = ""
    state: str = "unknown"
    running: bool = False
    cpu_seconds: float | None = None
    memory_bytes: int | None = None
    disk_read_bytes: float | None = None
    disk_written_bytes: float | None = None
    network_receive_bytes: float | None = None
    """Zero for a container on the host's network, whose traffic is the
    machine's own and is counted on its interfaces."""
    network_transmit_bytes: float | None = None


def node(exposition: Exposition) -> NodeUsage | None:
    """The machine's counters, or None when this is not a node_exporter."""
    series = exposition.series or {}
    if "node_cpu_seconds_total" not in series:
        return None
    clock = value(series, "node_time_seconds")
    usage = NodeUsage(
        read_at=clock if clock is not None else exposition.read_at,
        boot_time=value(series, "node_boot_time_seconds"),
        load1=value(series, "node_load1"),
        load5=value(series, "node_load5"),
        load15=value(series, "node_load15"),
        memory=_memory(series),
        filesystems=_filesystems(series),
        disks=_disks(series),
        interfaces=_interfaces(series),
    )
    usage.housekeeping, usage.isolated = _cpus(series)
    return usage


def guests(exposition: Exposition) -> list[GuestUsage]:
    """Every domain the machine's libvirt reports, running or not."""
    series = exposition.series or {}
    running = {
        sample.labels.get("domain", ""): int(sample.value) in (1, 2)
        for sample in series.get("libvirt_domain_info_state", [])
    }
    cpu = _by_domain(series, "libvirt_domain_info_cpu_time_seconds_total")
    vcpus = _by_domain(series, "libvirt_domain_info_virtual_cpus")
    rss = _by_domain(series, "libvirt_domain_memory_stats_rss_bytes")
    allocated = _by_domain(series, "libvirt_domain_info_memory_usage_bytes")
    read = _device_sums(series, "libvirt_domain_block_stats_read_bytes_total")
    written = _device_sums(series, "libvirt_domain_block_stats_write_bytes_total")
    received = _device_sums(
        series, "libvirt_domain_interface_stats_receive_bytes_total"
    )
    sent = _device_sums(series, "libvirt_domain_interface_stats_transmit_bytes_total")
    devices: dict[str, list[str]] = {}
    for sample in series.get("libvirt_domain_interface_stats_receive_bytes_total", []):
        device = sample.labels.get("target_device", "")
        if device:
            devices.setdefault(sample.labels.get("domain", ""), []).append(device)
    # A scrape that meets another one on a busy exporter publishes every block
    # series of a domain as 0, which is how the VMs page once read a guest's
    # disks as empty. As a counter it is worse: the next reading looks like
    # the whole life of the domain happening in one interval. No running
    # domain has read and written nothing, so both at 0 is no reading.
    stale = {
        name
        for name in read
        if read.get(name) == 0 and written.get(name) == 0 and running.get(name)
    }

    found: list[GuestUsage] = []
    for name, is_running in running.items():
        if not name:
            continue
        memory = rss.get(name) or allocated.get(name)
        found.append(
            GuestUsage(
                name=name,
                running=is_running,
                vcpus=_whole(vcpus.get(name)),
                cpu_seconds=cpu.get(name) if is_running else None,
                memory_bytes=_whole(memory) if is_running else None,
                disk_read_bytes=None if name in stale else read.get(name),
                disk_written_bytes=None if name in stale else written.get(name),
                network_receive_bytes=received.get(name),
                network_transmit_bytes=sent.get(name),
                interfaces=sorted(devices.get(name, [])),
            )
        )
    return sorted(found, key=lambda guest: guest.name)


def containers(exposition: Exposition) -> list[ContainerUsage]:
    """Every container podman reports on the machine, running or not.

    The exporter names a container by its short id on every series but one,
    `podman_container_info`, which carries the name and the image. The name is
    the one an operator knows: `systemd-<quadlet>` for a quadlet, and the
    `ceph-<fsid>-<daemon>` of a Ceph daemon.
    """
    series = exposition.series or {}
    info = {
        sample.labels.get("id", ""): sample.labels
        for sample in series.get("podman_container_info", [])
    }
    state = _by_id(series, "podman_container_state")
    cpu = _by_id(series, "podman_container_cpu_seconds_total")
    memory = _by_id(series, "podman_container_mem_usage_bytes")
    read = _by_id(series, "podman_container_block_input_total")
    written = _by_id(series, "podman_container_block_output_total")
    received = _by_id(series, "podman_container_net_input_total")
    sent = _by_id(series, "podman_container_net_output_total")

    found: list[ContainerUsage] = []
    for ident, labels in info.items():
        if not ident:
            continue
        code = int(state[ident]) if ident in state else -1
        running = code == 2
        found.append(
            ContainerUsage(
                name=labels.get("name") or ident,
                id=ident,
                image=labels.get("image", ""),
                state=_CONTAINER_STATES.get(code, "unknown"),
                running=running,
                cpu_seconds=cpu.get(ident) if running else None,
                memory_bytes=_whole(memory.get(ident)) if running else None,
                disk_read_bytes=read.get(ident) if running else None,
                disk_written_bytes=written.get(ident) if running else None,
                network_receive_bytes=received.get(ident) if running else None,
                network_transmit_bytes=sent.get(ident) if running else None,
            )
        )
    return sorted(found, key=lambda container: container.name)


def _cpus(series: dict[str, list[metrics.Sample]]) -> tuple[CpuCounters, CpuCounters]:
    isolated = {
        sample.labels.get("cpu", "")
        for sample in series.get("node_cpu_isolated", [])
        if sample.value
    }
    housekeeping = CpuCounters()
    spinning = CpuCounters()
    seen: dict[str, CpuCounters] = {}
    for sample in series.get("node_cpu_seconds_total", []):
        cpu = sample.labels.get("cpu", "")
        side = spinning if cpu in isolated else housekeeping
        if cpu not in seen:
            seen[cpu] = side
            side.cpus += 1
        side.total_seconds += sample.value
        if sample.labels.get("mode") in _IDLE_MODES:
            side.idle_seconds += sample.value
    return housekeeping, spinning


def _memory(series: dict[str, list[metrics.Sample]]) -> MemoryReading:
    def read(name: str) -> int | None:
        return _whole(value(series, name))

    pages = value(series, "node_memory_HugePages_Total")
    free_pages = value(series, "node_memory_HugePages_Free")
    size = value(series, "node_memory_Hugepagesize_bytes")
    return MemoryReading(
        total_bytes=read("node_memory_MemTotal_bytes"),
        available_bytes=read("node_memory_MemAvailable_bytes"),
        free_bytes=read("node_memory_MemFree_bytes"),
        buffers_bytes=read("node_memory_Buffers_bytes"),
        cached_bytes=read("node_memory_Cached_bytes"),
        hugepages_total_bytes=(
            int(pages * size) if pages is not None and size is not None else None
        ),
        hugepages_free_bytes=(
            int(free_pages * size)
            if free_pages is not None and size is not None
            else None
        ),
        swap_total_bytes=read("node_memory_SwapTotal_bytes"),
        swap_free_bytes=read("node_memory_SwapFree_bytes"),
    )


def _filesystems(series: dict[str, list[metrics.Sample]]) -> list[Filesystem]:
    available = {
        sample.labels.get("mountpoint", ""): sample.value
        for sample in series.get("node_filesystem_avail_bytes", [])
    }
    readonly = {
        sample.labels.get("mountpoint", ""): bool(sample.value)
        for sample in series.get("node_filesystem_readonly", [])
    }
    found: list[Filesystem] = []
    for sample in series.get("node_filesystem_size_bytes", []):
        labels = sample.labels
        mountpoint = labels.get("mountpoint", "")
        fstype = labels.get("fstype", "")
        if not mountpoint or fstype in _PSEUDO_FILESYSTEMS or sample.value <= 0:
            continue
        found.append(
            Filesystem(
                mountpoint=mountpoint,
                device=labels.get("device", ""),
                fstype=fstype,
                size_bytes=int(sample.value),
                available_bytes=int(available.get(mountpoint, 0)),
                readonly=readonly.get(mountpoint, False),
            )
        )
    return sorted(found, key=lambda filesystem: filesystem.mountpoint)


def _disks(series: dict[str, list[metrics.Sample]]) -> list[DiskCounters]:
    read = _by_label(series, "node_disk_read_bytes_total", "device")
    written = _by_label(series, "node_disk_written_bytes_total", "device")
    busy = _by_label(series, "node_disk_io_time_seconds_total", "device")
    devices = sorted(
        device
        for device in set(read) | set(written)
        if device and not device.startswith(_LAYERED_DISKS)
    )
    return [
        DiskCounters(
            device=device,
            read_bytes=read.get(device),
            written_bytes=written.get(device),
            io_time_seconds=busy.get(device),
        )
        for device in devices
    ]


def _interfaces(series: dict[str, list[metrics.Sample]]) -> list[InterfaceCounters]:
    received = _by_label(series, "node_network_receive_bytes_total", "device")
    sent = _by_label(series, "node_network_transmit_bytes_total", "device")
    speed = _by_label(series, "node_network_speed_bytes", "device")
    assigned = _by_label(series, "node_network_address_assign_type", "device")
    state = {
        sample.labels.get("device", ""): sample.labels.get("operstate", "")
        for sample in series.get("node_network_info", [])
    }
    found: list[InterfaceCounters] = []
    for device in sorted(set(received) | set(sent)):
        if not device:
            continue
        # A link that is down reports a negative speed, -1 Mb/s scaled to
        # bytes, which is the kernel saying it does not know.
        link = speed.get(device)
        found.append(
            InterfaceCounters(
                device=device,
                operstate=state.get(device, ""),
                speed_bytes=int(link) if link is not None and link > 0 else None,
                receive_bytes=received.get(device),
                transmit_bytes=sent.get(device),
                kind=_kind(device, assigned),
            )
        )
    return found


def _kind(device: str, assigned: dict[str, float]) -> str:
    """What an interface is, from its name and how its address was given.

    `addr_assign_type` 0 is an address the hardware came with, which a port
    has and a team, a bridge, a VLAN or a veth does not: they take one from a
    port (2) or are given one (1 or 3). An exporter too old to publish it
    leaves the name alone to go by, and then everything that is not plainly a
    workload's is taken for a port.
    """
    if device == "lo":
        return "loopback"
    if device in _VIRTUAL_EXACT or device.startswith(_VIRTUAL_INTERFACES):
        return "workload"
    if not assigned:
        return "physical"
    return "physical" if assigned.get(device) == 0 else "logical"


def _by_label(
    series: dict[str, list[metrics.Sample]], name: str, label: str
) -> dict[str, float]:
    return {
        sample.labels.get(label, ""): sample.value for sample in series.get(name, [])
    }


def _by_domain(series: dict[str, list[metrics.Sample]], name: str) -> dict[str, float]:
    return _by_label(series, name, "domain")


def _by_id(series: dict[str, list[metrics.Sample]], name: str) -> dict[str, float]:
    return _by_label(series, name, "id")


def _device_sums(
    series: dict[str, list[metrics.Sample]], name: str
) -> dict[str, float]:
    """A per device counter of a domain, added up over its devices."""
    found: dict[str, float] = {}
    for sample in series.get(name, []):
        domain = sample.labels.get("domain", "")
        if domain:
            found[domain] = found.get(domain, 0.0) + sample.value
    return found


def _whole(number: float | None) -> int | None:
    return None if number is None else int(number)
