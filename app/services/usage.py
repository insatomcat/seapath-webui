# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What every machine of the inventory, and each workload on it, is consuming.

One reading asks three exporters on every machine, in parallel: node_exporter
for the machine, libvirt-exporter for its guests and prometheus-podman-exporter
for its containers. What it answers is their counters and the moment each was
read. The browser that asked keeps the previous answer and turns the two into
rates, which is where the memory a rate needs lives, and the only place. D67
records the line and why it is drawn there.

This reading never goes through the scrape window the other pages share. A
rate divides by the time between two readings, and an answer the window kept
carries the time it was first read: two page readings a second apart inside it
would be the same counters over a second, which is a machine that did nothing.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from app.cluster import usage
from app.cluster.exporters import Exposition, MetricsClient, read_all
from app.cluster.usage import ContainerUsage, GuestUsage, NodeUsage
from app.inventory.service import InventoryService

_NO_INVENTORY = (
    "There is no inventory yet, so there is no machine to ask. The Inventory "
    "page is where the machines are described."
)
_NO_HOSTS = (
    "The inventory names no machine with an address, so there is no machine " "to ask."
)
_NOT_NODE = "it answered, and not with node_exporter's series"


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
    """What `GET /api/v1/usage` answers."""

    machines: list[MachineUsage] = Field(default_factory=list)
    this_host: str | None = None
    note: str = ""
    inventory_commit: str | None = None


class UsageService:
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
