# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What every machine of the inventory, and each workload on it, is consuming.

One reading asks three exporters on every machine, in parallel: node_exporter
for the machine, libvirt-exporter for its guests and prometheus-podman-exporter
for its containers. What they answer is counters, and a rate is two readings.

The recorder takes a reading every five seconds and keeps the last five
minutes of what each pair of readings says, per second: the machine's CPU,
memory, disks and physical ports, and each workload's share. It reads only
while somebody signed in has asked this service anything in the last quarter
of an hour, and it keeps nothing past the window and nothing on disk. D67
records why the memory sits here.

Its readings never go through the scrape window the other pages share. A rate
divides by the time between two readings, and an answer the window kept
carries the time it was first read: two readings a second apart inside it
would be the same counters over a second, which is a machine that did nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from app.cluster import usage
from app.cluster.exporters import Exposition, MetricsClient, read_all
from app.cluster.usage import ContainerUsage, GuestUsage, NodeUsage
from app.core.activity import Activity
from app.inventory.service import InventoryService

logger = logging.getLogger(__name__)

_NO_INVENTORY = (
    "There is no inventory yet, so there is no machine to ask. The Inventory "
    "page is where the machines are described."
)
_NO_HOSTS = "The inventory names no machine with an address, so there is none to ask."
_NOT_NODE = "it answered, and not with node_exporter's series"

# Two readings further apart than this many periods span a time the recorder
# was not reading, and are not divided: one flat average over a quarter of an
# hour nobody watched would be drawn as if it had been measured.
_GAP_PERIODS = 3


class ExporterReach(BaseModel):
    """Whether one exporter of one machine answered, and why not."""

    reachable: bool = False
    error: str = ""


class MachineUsage(BaseModel):
    """One machine, and the workloads its own exporters report on it."""

    host: str
    address: str
    node: NodeUsage | None = None
    node_reach: ExporterReach = Field(default_factory=ExporterReach)
    guests: list[GuestUsage] = Field(default_factory=list)
    guests_reach: ExporterReach = Field(default_factory=ExporterReach)
    """libvirt-exporter runs on the `hypervisors` group only, so a machine
    outside it answers nothing here, and that is its ordinary state."""
    guests_read_at: float = 0.0
    """When the libvirt answer arrived, on this service's clock. The exporter
    publishes no clock of its own."""
    containers: list[ContainerUsage] = Field(default_factory=list)
    containers_reach: ExporterReach = Field(default_factory=ExporterReach)
    containers_read_at: float = 0.0


class UsageView(BaseModel):
    """One reading of every machine."""

    machines: list[MachineUsage] = Field(default_factory=list)
    this_host: str | None = None
    note: str = ""
    inventory_commit: str | None = None


# What a pair of readings says


class CpuPoint(BaseModel):
    """CPUs busy, a housekeeping and an isolated share, between two readings."""

    housekeeping_cpus: int
    isolated_cpus: int
    housekeeping: float
    isolated: float


class MemoryPoint(BaseModel):
    total: int
    used: int
    """Everything but `MemAvailable`, the hugepage pool included."""
    reserved: int
    """The hugepage pool."""


class WorkloadPoint(BaseModel):
    cpu: float | None = None
    """CPUs busy."""
    memory: int | None = None


class UsagePoint(BaseModel):
    """One moment of a machine: the reading, and the rates since the one before."""

    at: float
    """When the reading was taken, on this service's clock."""
    cpu: CpuPoint | None = None
    """None for the first reading after a gap, which has nothing to divide."""
    memory: MemoryPoint | None = None
    disk_read: float | None = None
    disk_write: float | None = None
    network_in: float | None = None
    """Bytes per second over the physical ports."""
    network_out: float | None = None
    workloads: dict[str, WorkloadPoint] = Field(default_factory=dict)
    """By key, `vm:<name>` or `container:<name>`, running ones only."""


class WorkloadRates(BaseModel):
    key: str
    kind: str
    """`vm` or `container`."""
    name: str
    vcpus: int | None = None
    cpu: float | None = None
    memory: int | None = None
    disk_read: float | None = None
    disk_write: float | None = None
    network_in: float | None = None
    network_out: float | None = None


class DiskRates(BaseModel):
    device: str
    read: float | None = None
    write: float | None = None
    busy: float | None = None
    """The share of the interval the device had I/O in flight."""


class InterfaceRates(BaseModel):
    device: str
    receive: float | None = None
    transmit: float | None = None


class MachineHistory(BaseModel):
    host: str
    address: str
    latest: MachineUsage
    """The last reading as the exporters gave it: the filesystems, the
    interfaces and whether each exporter answered."""
    points: list[UsagePoint] = Field(default_factory=list)
    """Oldest first, the ones after `since` when the caller gave one."""
    workloads: list[WorkloadRates] = Field(default_factory=list)
    """Every running workload, at the last reading."""
    disks: list[DiskRates] = Field(default_factory=list)
    interfaces: list[InterfaceRates] = Field(default_factory=list)


class UsageHistory(BaseModel):
    """What `GET /api/v1/usage` answers."""

    now: float
    """This service's clock, which `at` is on. A chart is laid out against it
    rather than against the browser's, which need not agree."""
    period_seconds: float
    window_seconds: float
    machines: list[MachineHistory] = Field(default_factory=list)
    this_host: str | None = None
    note: str = ""
    inventory_commit: str | None = None


def rate(before: float | None, after: float | None, seconds: float) -> float | None:
    """A counter's increase per second, or None when there is none to take.

    A counter that went down was reset, by a reboot or a guest started again,
    and the interval holds nothing that can be divided.
    """
    if before is None or after is None or seconds <= 0 or after < before:
        return None
    return (after - before) / seconds


def _sum(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _workloads(
    machine: MachineUsage,
) -> dict[str, tuple[str, GuestUsage | ContainerUsage, float]]:
    found: dict[str, tuple[str, GuestUsage | ContainerUsage, float]] = {}
    for guest in machine.guests:
        if guest.running:
            found[f"vm:{guest.name}"] = ("vm", guest, machine.guests_read_at)
    for container in machine.containers:
        if container.running:
            found[f"container:{container.name}"] = (
                "container",
                container,
                machine.containers_read_at,
            )
    return found


class Step(BaseModel):
    """What one reading says against the one before it."""

    point: UsagePoint
    workloads: list[WorkloadRates]
    disks: list[DiskRates]
    interfaces: list[InterfaceRates]


def step(previous: MachineUsage | None, current: MachineUsage, at: float) -> Step:
    """The point and the rates one reading gives against the one before.

    `previous` is None for the first reading, or the first after a gap: the
    memory is a gauge and is there anyway, everything per second is not.
    """
    point = UsagePoint(at=at)
    node = current.node
    if node is not None and node.memory.total_bytes:
        memory = node.memory
        if memory.available_bytes is not None:
            point.memory = MemoryPoint(
                total=memory.total_bytes,
                used=memory.total_bytes - memory.available_bytes,
                reserved=memory.hugepages_total_bytes or 0,
            )

    now = _workloads(current)
    was = _workloads(previous) if previous is not None else {}
    loads: list[WorkloadRates] = []
    for key, (kind, item, read_at) in now.items():
        entry = WorkloadRates(
            key=key,
            kind=kind,
            name=item.name,
            vcpus=getattr(item, "vcpus", None),
            memory=item.memory_bytes,
        )
        old = was.get(key)
        if old is not None:
            before, seconds = old[1], read_at - old[2]
            entry.cpu = rate(before.cpu_seconds, item.cpu_seconds, seconds)
            entry.disk_read = rate(
                before.disk_read_bytes, item.disk_read_bytes, seconds
            )
            entry.disk_write = rate(
                before.disk_written_bytes, item.disk_written_bytes, seconds
            )
            entry.network_in = rate(
                before.network_receive_bytes, item.network_receive_bytes, seconds
            )
            entry.network_out = rate(
                before.network_transmit_bytes, item.network_transmit_bytes, seconds
            )
        loads.append(entry)
        point.workloads[key] = WorkloadPoint(cpu=entry.cpu, memory=entry.memory)

    disks: list[DiskRates] = []
    interfaces: list[InterfaceRates] = []
    old_node = previous.node if previous is not None else None
    if node is not None:
        seconds = node.read_at - old_node.read_at if old_node is not None else 0.0
        if old_node is not None:
            housekeeping = _busy(old_node.housekeeping, node.housekeeping, seconds)
            isolated = _busy(old_node.isolated, node.isolated, seconds)
            if housekeeping is not None:
                point.cpu = CpuPoint(
                    housekeeping_cpus=node.housekeeping.cpus,
                    isolated_cpus=node.isolated.cpus,
                    housekeeping=housekeeping,
                    isolated=isolated or 0.0,
                )
        old_disks = {disk.device: disk for disk in (old_node.disks if old_node else [])}
        for disk in node.disks:
            before = old_disks.get(disk.device)
            disks.append(
                DiskRates(
                    device=disk.device,
                    read=(
                        rate(before.read_bytes, disk.read_bytes, seconds)
                        if before
                        else None
                    ),
                    write=(
                        rate(before.written_bytes, disk.written_bytes, seconds)
                        if before
                        else None
                    ),
                    busy=(
                        rate(before.io_time_seconds, disk.io_time_seconds, seconds)
                        if before
                        else None
                    ),
                )
            )
        old_links = {
            link.device: link for link in (old_node.interfaces if old_node else [])
        }
        physical: list[InterfaceRates] = []
        for link in node.interfaces:
            before = old_links.get(link.device)
            rates = InterfaceRates(
                device=link.device,
                receive=(
                    rate(before.receive_bytes, link.receive_bytes, seconds)
                    if before
                    else None
                ),
                transmit=(
                    rate(before.transmit_bytes, link.transmit_bytes, seconds)
                    if before
                    else None
                ),
            )
            interfaces.append(rates)
            if link.kind == "physical":
                physical.append(rates)
        if old_node is not None:
            point.disk_read = _sum([disk.read for disk in disks])
            point.disk_write = _sum([disk.write for disk in disks])
            point.network_in = _sum([link.receive for link in physical])
            point.network_out = _sum([link.transmit for link in physical])
    return Step(point=point, workloads=loads, disks=disks, interfaces=interfaces)


def _busy(
    before: usage.CpuCounters, after: usage.CpuCounters, seconds: float
) -> float | None:
    """CPUs busy on one side, which is its CPU time less its idle time, per second."""
    if after.cpus == 0:
        return 0.0
    total = rate(before.total_seconds, after.total_seconds, seconds)
    idle = rate(before.idle_seconds, after.idle_seconds, seconds)
    if total is None or idle is None:
        return None
    return max(total - idle, 0.0)


class UsageService:
    """One reading of every machine's three exporters."""

    def __init__(
        self,
        inventory: InventoryService,
        client: MetricsClient,
        node_port: int,
        libvirt_port: int,
        podman_port: int,
        timeout: float = 2.0,
    ) -> None:
        self._inventory = inventory
        # Never the client the other pages share: see the module docstring.
        self._client = client
        self._ports = (node_port, libvirt_port, podman_port)
        self._timeout = timeout

    def usage(self) -> UsageView:
        state = self._inventory.state()
        if state.inventory is None:
            return UsageView(note=_NO_INVENTORY, inventory_commit=state.commit)
        targets = [
            (name, node.ansible_host)
            for name, node in state.inventory.hosts.items()
            if node.ansible_host
        ]
        view = UsageView(this_host=state.this_host, inventory_commit=state.commit)
        if not targets:
            view.note = _NO_HOSTS
            return view

        # The three exporters at once rather than one after the other: each
        # fan out waits on its slowest machine, and a machine that is down
        # costs the whole timeout on every port it is asked on.
        with ThreadPoolExecutor(max_workers=len(self._ports)) as pool:
            nodes, libvirts, podmans = pool.map(
                lambda port: read_all(
                    self._client, targets, port, timeout=self._timeout
                ),
                self._ports,
            )
        for node, libvirt, podman in zip(nodes, libvirts, podmans, strict=True):
            view.machines.append(_machine(node, libvirt, podman))
        return view


class _Track:
    """One machine's last reading and the points of the window."""

    def __init__(self, reading: MachineUsage, at: float, result: Step) -> None:
        self.reading = reading
        self.at = at
        self.last = result
        self.points: deque[UsagePoint] = deque([result.point])


class UsageRecorder:
    """The last few minutes of every machine, read while somebody is here.

    A thread takes a reading every period for as long as a signed in request
    arrived within `idle_seconds`, and keeps what the pairs of readings say
    for `window_seconds`. The page reads what it kept, so a tab switched away
    and back finds the minutes it missed, and several browsers cost the
    machines one reading between them.
    """

    def __init__(
        self,
        service: UsageService,
        activity: Activity,
        period_seconds: float = 5.0,
        window_seconds: float = 300.0,
        idle_seconds: float = 900.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._service = service
        self._activity = activity
        self._period = period_seconds
        self._window = window_seconds
        self._idle = idle_seconds
        self._clock = clock
        self._tracks: dict[str, _Track] = {}
        self._view: UsageView | None = None
        self._taken_at: float | None = None
        # One reading at a time: the thread's and the one a page asks for
        # when nothing recent is kept must not both scrape and both append.
        self._reading = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> None:
        """Take one reading, unless one was taken a moment ago."""
        with self._reading:
            if self._fresh(self._period / 2):
                return
            view = self._service.usage()
            at = self._clock()
            self._record(view, at)

    def _fresh(self, seconds: float) -> bool:
        return self._taken_at is not None and self._clock() - self._taken_at < seconds

    def _record(self, view: UsageView, at: float) -> None:
        present = set()
        for machine in view.machines:
            present.add(machine.host)
            track = self._tracks.get(machine.host)
            previous = None
            if track is not None and at - track.at <= self._period * _GAP_PERIODS:
                previous = track.reading
            result = step(previous, machine, at)
            if track is None:
                self._tracks[machine.host] = _Track(machine, at, result)
                continue
            track.reading, track.at, track.last = machine, at, result
            track.points.append(result.point)
            while track.points and track.points[0].at < at - self._window:
                track.points.popleft()
        # A machine taken out of the inventory takes its minutes with it.
        for host in list(self._tracks):
            if host not in present:
                del self._tracks[host]
        self._view = view
        self._taken_at = at

    def history(self, since: float | None = None) -> UsageHistory:
        """What the window holds, the points after `since` when one is given.

        The first page after a quiet spell finds nothing recent kept, since the
        thread was not reading, and takes a reading itself rather than
        answering with an empty page for a period.
        """
        if not self._fresh(self._period * _GAP_PERIODS):
            try:
                self.sample()
            except Exception:  # pragma: no cover - defensive
                logger.exception("The usage reading failed")
        now = self._clock()
        view = self._view or UsageView()
        answer = UsageHistory(
            now=now,
            period_seconds=self._period,
            window_seconds=self._window,
            this_host=view.this_host,
            note=view.note,
            inventory_commit=view.inventory_commit,
        )
        for machine in view.machines:
            track = self._tracks.get(machine.host)
            if track is None:
                continue
            answer.machines.append(
                MachineHistory(
                    host=machine.host,
                    address=machine.address,
                    latest=track.reading,
                    points=[
                        point
                        for point in track.points
                        if point.at >= now - self._window
                        and (since is None or point.at > since)
                    ],
                    workloads=track.last.workloads,
                    disks=track.last.disks,
                    interfaces=track.last.interfaces,
                )
            )
        return answer

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="usage-recorder", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=self._period + 5)
            self._thread = None

    def tick(self) -> bool:
        """One period of the thread: a reading if somebody is here, else none."""
        if not self._activity.within(self._idle):
            return False
        self.sample()
        return True

    def _run(self) -> None:
        while not self._stopping.wait(self._period):
            try:
                self.tick()
            except Exception:
                # A reading that failed is a gap in the charts, and the next
                # period tries again. Nothing here may end the thread.
                logger.exception("The usage reading failed")


def _machine(node: Exposition, libvirt: Exposition, podman: Exposition) -> MachineUsage:
    machine = MachineUsage(
        host=node.host,
        address=node.address,
        guests_read_at=libvirt.read_at,
        containers_read_at=podman.read_at,
    )
    machine.node = usage.node(node) if node.answered else None
    machine.node_reach = _reach(node, machine.node is not None)
    if libvirt.has("libvirt_domain_info_state"):
        machine.guests = usage.guests(libvirt)
    if machine.node is not None:
        # A guest's interface may carry any name its domain gives it, and one
        # named after the guest reads as a port of the machine. libvirt says
        # which devices are its, and their traffic is the guest's.
        tapped = {device for guest in machine.guests for device in guest.interfaces}
        for interface in machine.node.interfaces:
            if interface.device in tapped:
                interface.kind = "workload"
    machine.guests_reach = _reach(libvirt, True)
    if podman.answered:
        machine.containers = usage.containers(podman)
    machine.containers_reach = _reach(podman, True)
    return machine


def _reach(exposition: Exposition, understood: bool) -> ExporterReach:
    if not exposition.answered:
        return ExporterReach(error=exposition.error)
    if not understood:
        return ExporterReach(error=_NOT_NODE)
    return ExporterReach(reachable=True)
