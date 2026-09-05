# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The guests the inventory declares, and what the cluster does with them.

A guest is one object whose three parts live in three places. Its definition is
an entry in the `VMs` group of the inventory, versioned in git. The files that
entry names are a libvirt XML in the same repository and a disk image in the
artefacts beside it, which git does not carry. What it is doing right now is
Pacemaker's, one resource per guest, published by the exporter every cluster
already runs.

This assembles the three into one answer, and writes nothing anywhere. The
definition is changed on the Inventory page, one commit; the guest is deployed
by a run of `deploy_vms_cluster` or `deploy_vms_standalone`; and starting,
stopping or migrating one is the runtime plane, which arrives with `vm_manager`
and is not here yet.

The read costs one HTTP GET per machine. It is the same exposition the Cluster
page reads, asked again rather than cached, for the reason D29 gives: this
service holds no second source of truth for what the cluster is doing.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.cluster.ha import PacemakerResource
from app.inventory.model import Mode
from app.inventory.references import Reference
from app.inventory.repository import Commit
from app.inventory.service import InventoryService
from app.services.cluster import ClusterService

# What `vm_manager` names the agent of the resource it creates per guest, and
# the only resources on this page. A cluster carries others, `ha_cluster_exporter`
# reports all of them, and a fencing device listed among the VMs would be a
# page saying something false about the inventory.
VM_AGENT = "VirtualDomain"

# The playbook that deploys the group, per mode. Both loop over `VMs` whole:
# neither takes a guest to deploy, so a page cannot offer to deploy one.
DEPLOY_PLAYBOOK = {
    Mode.CLUSTER: "deploy_vms_cluster",
    Mode.STANDALONE: "deploy_vms_standalone",
}

# The name is the host key, the libvirt domain name and the Pacemaker resource
# id at once, so it has to survive all three. The same shape a machine's key
# has, for the same reason.
_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

_NO_INVENTORY = (
    "There is no inventory yet, so no guest is declared. The Inventory page is "
    "where a VM is described."
)
_NO_GUESTS = (
    "This inventory declares no guest. A VM is an entry in the `VMs` group, "
    "naming its disk image and its libvirt XML, and the Inventory page is "
    "where it is written."
)
# Appended to whatever the cluster service says when it has nothing to report,
# because on this page the consequence is the part that matters: the table is
# then the desired state alone.
_DESIRED_STATE_ONLY = (
    "The guests below are what the inventory declares, with no report of what "
    "is running."
)
_FROM_PACEMAKER = (
    "The state and node columns are read from ha_cluster_exporter on each "
    "machine of the inventory, which publishes what crm_mon said. Nothing here "
    "moves a guest: starting, stopping and migrating one is the runtime plane, "
    "and it arrives with vm_manager."
)


class GuestView(BaseModel):
    """One guest: what the inventory declares, and what Pacemaker reports."""

    name: str
    """The host key, which is also the libvirt domain name and the resource id."""

    vm_disk: str | None = None
    vm_template: str | None = None
    xml_path: str | None = None
    force: bool = False
    """The guest is destroyed and recreated on every deployment run."""
    enable: bool = True

    files: list[Reference] = Field(default_factory=list)
    """The paths this guest names, and whether a run would find each one."""

    resource: PacemakerResource | None = None
    """Pacemaker's line for it, absent when nothing reports one."""

    @property
    def missing_files(self) -> list[Reference]:
        return [reference for reference in self.files if not reference.found]


class GuestsView(BaseModel):
    mode: str = Mode.STANDALONE.value
    guests: list[GuestView] = Field(default_factory=list)
    undeclared: list[PacemakerResource] = Field(default_factory=list)
    """Guests the cluster runs and the inventory does not declare.

    Worth a line of its own rather than a silent omission: a VM deployed by
    hand, or one left behind by an inventory somebody edited, keeps running and
    keeps a name that a later deployment would collide with.
    """
    playbook: str = ""
    """The catalogue entry that deploys the group in this mode."""
    runtime_note: str = ""
    """Why the runtime column says what it says."""
    note: str = ""
    """Said when there is nothing to list at all."""
    inventory_commit: str | None = None


class InvalidGuest(Exception):
    """The declaration cannot become an entry, and the message says why."""


class VmService:
    def __init__(self, inventory: InventoryService, cluster: ClusterService) -> None:
        self._inventory = inventory
        self._cluster = cluster

    def deploy_playbook(self) -> str:
        state = self._inventory.state()
        mode = state.inventory.mode if state.inventory else Mode.STANDALONE
        return DEPLOY_PLAYBOOK[mode]

    def declare(
        self,
        name: str,
        definition: dict[str, Any],
        author: str,
        expected_head: str | None = None,
    ) -> Commit:
        """Write one guest into the `VMs` group, as a commit.

        The page calls this "add a VM" and the operator never sees the group,
        the commit or the playbook that follows. What happens underneath is the
        ordinary path: a splice into the inventory, checked by `fidelity`, then
        an upstream playbook. See D30.
        """
        if not _NAME.match(name):
            raise InvalidGuest(
                f"{name!r} cannot be a guest name. It becomes the libvirt "
                "domain and the Pacemaker resource, so it takes letters, "
                "digits and dashes."
            )
        variables = {
            key: value for key, value in definition.items() if value not in (None, "")
        }
        commit, _ = self._inventory.declare_guest(
            name, variables, author, expected_head
        )
        return commit

    def guests(self) -> GuestsView:
        state = self._inventory.state()
        if state.inventory is None:
            return GuestsView(note=_NO_INVENTORY, inventory_commit=state.commit)

        mode = state.inventory.mode
        view = GuestsView(
            mode=mode.value,
            playbook=DEPLOY_PLAYBOOK[mode],
            inventory_commit=state.commit,
        )

        files = self._files_by_host()
        resources, view.runtime_note = self._resources()

        for name, guest in state.inventory.guests.items():
            view.guests.append(
                GuestView(
                    name=name,
                    vm_disk=guest.vm_disk,
                    vm_template=guest.vm_template,
                    xml_path=guest.xml_path,
                    force=guest.force,
                    enable=guest.enable,
                    files=files.get(name, []),
                    resource=resources.get(name),
                )
            )

        declared = set(state.inventory.guests)
        view.undeclared = [
            resource for name, resource in resources.items() if name not in declared
        ]

        if not view.guests:
            view.note = _NO_GUESTS
        return view

    def _files_by_host(self) -> dict[str, list[Reference]]:
        """Every path the inventory names, kept under the entry that names it.

        The same reading the Inventory page shows, so a guest whose image has
        not been uploaded says so here as well: with `any_errors_fatal`, a
        `copy` that cannot find its source ends the deployment on every host at
        once, three minutes in.
        """
        found: dict[str, list[Reference]] = {}
        for reference in self._inventory.references():
            found.setdefault(reference.host, []).append(reference)
        return found

    def _resources(self) -> tuple[dict[str, PacemakerResource], str]:
        """What Pacemaker says, by guest name, and one sentence about it.

        The resource id `vm_manager` creates is the VM name itself, which is
        also the host key in the inventory, so the two halves are matched on
        equality rather than on a naming convention this service invents.

        Asked whatever the inventory says the mode is, the way the Cluster page
        asks. A file declaring a standalone machine is a statement about the
        desired state, and this column reports what is actually running: a node
        that is in a cluster its inventory has not caught up with is exactly
        when an operator opens this page.
        """
        cluster = self._cluster.pacemaker()
        if cluster.error:
            return {}, f"{cluster.error} {_DESIRED_STATE_ONLY}"
        return {
            resource.id: resource
            for resource in cluster.resources
            if VM_AGENT in resource.agent
        }, _FROM_PACEMAKER
