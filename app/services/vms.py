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
stopping or placing one is the runtime plane, which reaches a machine as a
generated run and never from here.

The read costs one HTTP GET per machine. It is the same exposition the Cluster
page reads, asked again rather than cached, for the reason D29 gives: this
service holds no second source of truth for what the cluster is doing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.cluster.exporters import MetricsClient, UrllibMetricsClient, read_all
from app.cluster.ha import LocationConstraint, PacemakerCluster, PacemakerResource
from app.cluster.libvirt import DEFAULT_PORT, LibvirtDomain
from app.cluster.libvirt import read as read_libvirt
from app.cluster.libvirt import reporting as libvirt_reporting
from app.cluster.rbd import RbdClient, RbdUnavailable
from app.inventory.model import (
    CLUSTER_GUEST_GROUP,
    GUEST_GROUP,
    STANDALONE_GUEST_GROUP,
    Inventory,
    Mode,
)
from app.inventory.references import Reference
from app.inventory.repository import Commit
from app.inventory.service import InventoryService, InventoryState
from app.services.cluster import ClusterService

logger = logging.getLogger(__name__)
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
    "The state and node columns are read from the exporters each machine of "
    "the inventory already runs: ha_cluster_exporter, which publishes what "
    "crm_mon said, and libvirt-exporter, which publishes what libvirt says "
    "about its own domains. A guest with no Pacemaker resource is reported by "
    "the second, which is the only reading a guest on a standalone machine "
    "has."
)


class GuestView(BaseModel):
    """One guest: what the inventory declares, and what Pacemaker reports."""

    name: str
    """The host key, which is also the libvirt domain name and the resource id."""

    deployment: str = Mode.STANDALONE.value
    """Which of the two playbooks creates it, from the group it is in.

    A file with one flat `VMs` group says nothing, and every guest then takes
    the file's own mode: the group is claimed whole by whichever playbook is
    run.
    """
    declared: bool = False
    """Whether the file put it in a deployment group, rather than defaulting."""
    playbook: str = ""
    """The catalogue entry that creates this guest."""

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

    preferred_host: str | None = None
    """Where the entry says Pacemaker should run it, when it says."""
    pinned_host: str | None = None
    """Where the entry says it runs or does not run at all."""
    constraints: list[LocationConstraint] = Field(default_factory=list)
    """The location rules the cluster holds for this guest, right now.

    The pair above is the desired state and this is what Pacemaker is acting
    on, and they are worth showing together because the second can be an
    operator's doing: a move writes the same `cli-prefer` constraint
    `preferred_host` writes, so the only way to see that a guest is being held
    somewhere it was not declared to be is to compare the two. See D34.
    """

    disabled: bool = False
    """Ceph holds the guest and Pacemaker has no resource for it.

    What `cluster_vm disable` leaves behind, and what `cluster_vm status`
    calls Disabled: the RBD group and image are there, so `enable` brings the
    guest back and a deployment run skips it. Told apart from a guest never
    deployed by asking Ceph for its groups, and only said when the cluster
    answered, since an empty reading from a cluster that did not answer says
    nothing about any resource.
    """

    domain: LibvirtDomain | None = None
    """What libvirt says about it, from the exporter on its own machine.

    The only reading a guest on a standalone machine has, since it has no
    Pacemaker resource at all. A cluster guest carries both where its node
    publishes the exporter, and the two agree because they describe the same
    domain.
    """

    @property
    def missing_files(self) -> list[Reference]:
        return [reference for reference in self.files if not reference.found]


class GuestsView(BaseModel):
    mode: str = Mode.STANDALONE.value
    guests: list[GuestView] = Field(default_factory=list)
    undeclared: list[PacemakerResource] = Field(default_factory=list)
    undeclared_domains: list[LibvirtDomain] = Field(default_factory=list)
    """The same, for the machines Pacemaker does not answer for."""
    """Guests the cluster runs and the inventory does not declare.

    Worth a line of its own rather than a silent omission: a VM deployed by
    hand, or one left behind by an inventory somebody edited, keeps running and
    keeps a name that a later deployment would collide with.
    """
    split: bool = False
    """Whether the file says which deployment each guest belongs to.

    False for one flat `VMs` group, which is every inventory written before
    `cluster_VMs` and `standalone_VMs` existed and every one that needs only
    one deployment.
    """
    deployments: list[str] = Field(default_factory=list)
    """The deployments this inventory has machines for, so a form can ask."""
    placement_nodes: list[str] = Field(default_factory=list)
    """The machines a move may send a guest to, right now.

    `machines` held against what the cluster reports: a member in standby is
    one Pacemaker will place nothing on, so offering it is offering a
    constraint that holds a guest where it already is. Empty whenever no
    cluster answered, and the page then offers no move at all.
    """
    machines: list[str] = Field(default_factory=list)
    """The machines a guest may be placed on, for the form that asks.

    Cluster members that are hypervisors, and not every host of the file. A
    standalone machine has no Pacemaker to hear a constraint and an observer
    has no libvirt to run the guest, so offering either is offering a guest
    that never starts.
    """
    warnings: list[str] = Field(default_factory=list)
    """What this page could not answer cleanly, in the operator's terms."""
    playbook: str = ""
    """The catalogue entry that deploys the group in this mode."""
    runtime_note: str = ""
    """Why the runtime column says what it says."""
    note: str = ""
    """Said when there is nothing to list at all."""
    inventory_commit: str | None = None


# What each role reads and the other ignores. A variable written for the wrong
# mode is silently inert, which is the worst of the three outcomes: the
# operator asked for something, the file says they got it, and nothing anywhere
# does it. `deploy_vms_standalone` renders the whole domain from the template
# and has no Pacemaker, so placement, priority, migration and the disk bus mean
# nothing there; `deploy_vms_cluster` never reads `autostart` or
# `disk_extract`.
CLUSTER_ONLY = (
    "preferred_host",
    "pinned_host",
    "priority",
    "live_migration",
    "migrate_to_timeout",
    "migration_downtime",
    "disk_bus",
    "nostart",
    "colocated_vms",
    "strong_colocation",
)
STANDALONE_ONLY = ("autostart", "disk_extract")

# The disk buses `cluster_vm` passes through to libvirt. A short list rather
# than free text: the value reaches a domain definition, and a bus libvirt does
# not know is a guest that fails to start with a message about its disk.
DISK_BUSES = ("virtio", "sata", "scsi", "ide", "usb")


def _deployments(inventory: Inventory) -> list[str]:
    """The deployments this file has machines for.

    A guest can only be created where there is something to create it on, so
    this is what a form offers rather than the two names in the abstract.
    """
    found = []
    if inventory.cluster_members:
        found.append(Mode.CLUSTER.value)
    if set(inventory.hosts) - set(inventory.cluster_members):
        found.append(Mode.STANDALONE.value)
    return found


def _warnings(inventory: Inventory) -> list[str]:
    """What one `VMs` group cannot say, and this file needs it to.

    Both deployment playbooks loop over the whole group, `deploy_vms_cluster`
    from a cluster member and `deploy_vms_standalone` on the standalone
    machine, and neither takes a guest to deploy. So a file declaring both
    kinds of machine has no way of saying which deployment a guest belongs to,
    and running the two playbooks would create every guest twice, once in Ceph
    and once in the local pool.

    Said rather than resolved: inventing a per guest answer here would be this
    service adding a variable the roles do not read.
    """
    if not inventory.guests:
        return []
    # A file that says which deployment each guest belongs to has answered
    # this, and the validation refuses the ones it left out.
    if any(guest.deployment is not None for guest in inventory.guests.values()):
        return []
    standalone = [
        name for name in inventory.hosts if name not in inventory.cluster_members
    ]
    if not standalone or not inventory.cluster_members:
        return []
    return [
        "This inventory declares a cluster and "
        + (
            f"the standalone machine {standalone[0]}"
            if len(standalone) == 1
            else f"{len(standalone)} machines outside it"
        )
        + ", and its `VMs` group is one flat group. Both deployment playbooks "
        "loop over all of it, so the guests below are treated as the "
        "cluster's: running deploy_vms_standalone as well would create each of "
        "them a second time, on that machine. Declaring `cluster_VMs` and "
        "`standalone_VMs` as children of `VMs` says which is which."
    ]


def _group_for(deployment: Mode | None) -> str:
    """The inventory group a declaration goes into.

    `VMs` itself when the caller names no deployment, which is the file that
    has one flat group and one deployment to send it to.
    """
    if deployment is Mode.CLUSTER:
        return CLUSTER_GUEST_GROUP
    if deployment is Mode.STANDALONE:
        return STANDALONE_GUEST_GROUP
    return GUEST_GROUP


class InvalidGuest(Exception):
    """The declaration cannot become an entry, and the message says why."""


class UnknownGuest(Exception):
    """No guest of that name is declared here or reported by the cluster."""


class VmService:
    def __init__(
        self,
        inventory: InventoryService,
        cluster: ClusterService,
        client: MetricsClient | None = None,
        libvirt_port: int = DEFAULT_PORT,
        timeout: float = 2.0,
        rbd: RbdClient | None = None,
    ) -> None:
        self._inventory = inventory
        self._cluster = cluster
        self._rbd = rbd
        self._client = client or UrllibMetricsClient()
        self._libvirt_port = libvirt_port
        self._timeout = timeout

    def known(self) -> set[str]:
        """The guests this node can act on.

        The declared ones and the ones Pacemaker reports, because a guest the
        cluster runs and the inventory forgot is exactly the one an operator
        needs to be able to stop. What this is for is that a name reaching a
        module argument is a name this node has seen, rather than whatever was
        typed into a URL.
        """
        view = self.guests()
        return {guest.name for guest in view.guests} | {
            resource.id for resource in view.undeclared
        }

    def check_known(self, name: str) -> None:
        if name not in self.known():
            raise UnknownGuest(
                f"No guest called {name!r} is declared in this inventory or "
                "reported by the cluster."
            )

    def in_cluster(self, name: str) -> bool:
        """Whether a guest is one Pacemaker holds or held, and can be told to.

        A guest the file puts in the cluster deployment, or one the cluster
        reports and the inventory does not declare: the second is exactly the
        guest an operator wants out of the cluster, and nothing else here
        reaches it.
        """
        view = self.guests()
        for guest in view.guests:
            if guest.name == name:
                return guest.deployment == Mode.CLUSTER.value or bool(guest.resource)
        return any(resource.id == name for resource in view.undeclared)

    def deploy_playbook(self, guest: str = "") -> str:
        """The playbook that creates a guest, or the file's own default."""
        return DEPLOY_PLAYBOOK[self.deployment_of(guest)]

    def deployment_of(self, guest: str) -> Mode:
        """Which of the two a guest belongs to.

        Its group when the file says, the file's mode otherwise. Everything
        that differs between a Pacemaker guest and a libvirt one asks this: the
        playbook that creates it, the module that starts it, the options its
        entry may carry, and whether it has an RBD image to hold metadata.
        """
        state = self._inventory.state()
        if state.inventory is None:
            return Mode.STANDALONE
        return state.inventory.deployment_of(guest)

    def declare(
        self,
        name: str,
        definition: dict[str, Any],
        author: str,
        expected_head: str | None = None,
        deployment: Mode | None = None,
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
            key: value
            for key, value in definition.items()
            if value not in (None, "", [])
        }
        self._check_definition(name, variables, deployment)
        commit, _ = self._inventory.declare_guest(
            name, variables, author, expected_head, _group_for(deployment)
        )
        return commit

    def _check_definition(
        self, name: str, variables: dict[str, Any], deployment: Mode | None = None
    ) -> None:
        """What the entry says, held against what this inventory declares.

        Every one of these is written once, at creation, into the metadata of
        the guest's RBD image, and changing it afterwards means the metadata
        window and an outage. A typo caught here is worth a great deal more
        than a typo caught there: `preferred_host: nod2` is a guest Pacemaker
        places nowhere, reported as a constraint nobody can read.
        """
        state = self._inventory.state()
        machines = state.inventory.placement_hosts() if state.inventory else []
        guests = set(state.inventory.guests) if state.inventory else set()
        # The guest's own deployment decides which variables mean anything,
        # since a file may hold both kinds and each role reads its own.
        if deployment is None:
            deployment = state.inventory.mode if state.inventory else Mode.STANDALONE
        cluster = deployment is Mode.CLUSTER

        wrong = [
            variable
            for variable in (STANDALONE_ONLY if cluster else CLUSTER_ONLY)
            if variable in variables
        ]
        if wrong:
            role = "deploy_vms_cluster" if cluster else "deploy_vms_standalone"
            raise InvalidGuest(
                f"{', '.join(wrong)} is read by the other deployment role, so "
                f"{role}, which creates {name}, would ignore it. Writing it "
                "would say the guest got something nothing does."
            )

        if "pinned_host" in variables and "preferred_host" in variables:
            raise InvalidGuest(
                "A guest is pinned or preferred, and not both. `cluster_vm` "
                "reads `pinned_host` first and ignores the other, so writing "
                "the pair would hide one of the two decisions."
            )
        for field in ("pinned_host", "preferred_host"):
            host = variables.get(field)
            if host and host not in machines:
                raise InvalidGuest(
                    f"{host!r} is not a machine a guest can be placed on. "
                    "Pacemaker places one on a cluster member that runs "
                    "libvirt, which here means "
                    f"{', '.join(machines) or 'no machine of this inventory'}."
                )

        unknown = [
            item for item in variables.get("colocated_vms", []) if item not in guests
        ]
        if unknown:
            raise InvalidGuest(
                f"{', '.join(unknown)} is not a guest of this inventory, so "
                "there is nothing to keep this one beside."
            )

        bus = variables.get("disk_bus")
        if bus and bus not in DISK_BUSES:
            raise InvalidGuest(
                f"{bus!r} is not a disk bus this service writes. One of "
                f"{', '.join(DISK_BUSES)}."
            )

        profile = variables.get("vm_pinning_profile")
        if profile:
            try:
                parsed = yaml.safe_load(profile)
            except yaml.YAMLError as error:
                raise InvalidGuest(
                    f"The pinning profile is not YAML: {error}"
                ) from error
            if not isinstance(parsed, dict):
                raise InvalidGuest(
                    "The pinning profile is a mapping, the one "
                    "`deploy_seapath_alloc` documents, starting with "
                    "`version: 1`."
                )

    def guests(self) -> GuestsView:
        state = self._inventory.state()
        if state.inventory is None:
            return GuestsView(note=_NO_INVENTORY, inventory_commit=state.commit)

        mode = state.inventory.mode
        view = GuestsView(
            mode=mode.value,
            split=any(
                guest.deployment is not None
                for guest in state.inventory.guests.values()
            ),
            deployments=_deployments(state.inventory),
            machines=state.inventory.placement_hosts(),
            warnings=_warnings(state.inventory),
            playbook=DEPLOY_PLAYBOOK[mode],
            inventory_commit=state.commit,
        )

        files = self._files_by_host()
        reading = self._cluster.pacemaker()
        resources, constraints, view.runtime_note = self._resources(reading)
        # Where a move may send a guest: a machine the inventory allows it on
        # that the cluster also reports as online and out of standby. Both
        # halves are needed. The inventory rules out an observer, which has no
        # libvirt to run the guest, and the cluster rules out a member nothing
        # can be placed on this afternoon.
        view.placement_nodes = [
            node.name
            for node in reading.nodes
            if node.name in view.machines
            and node.online
            and "standby" not in node.flags
        ]
        domains = self._domains(state)
        # Ceph is asked only when a cluster guest has no resource and the
        # cluster did answer: that is the one row the groups can change, and
        # every other page load costs no rbd call at all.
        held_by_ceph = self._groups(
            not reading.error
            and any(
                state.inventory.deployment_of(name) is Mode.CLUSTER
                and name not in resources
                for name in state.inventory.guests
            )
        )

        for name, guest in state.inventory.guests.items():
            deployment = state.inventory.deployment_of(name)
            view.guests.append(
                GuestView(
                    name=name,
                    deployment=deployment.value,
                    declared=guest.deployment is not None,
                    playbook=DEPLOY_PLAYBOOK[deployment],
                    vm_disk=guest.vm_disk,
                    vm_template=guest.vm_template,
                    xml_path=guest.xml_path,
                    force=guest.force,
                    enable=guest.enable,
                    preferred_host=guest.extra.get("preferred_host"),
                    pinned_host=guest.extra.get("pinned_host"),
                    files=files.get(name, []),
                    resource=resources.get(name),
                    constraints=constraints.get(name, []),
                    domain=domains.get(name),
                    disabled=(
                        deployment is Mode.CLUSTER
                        and name not in resources
                        and name in held_by_ceph
                    ),
                )
            )

        declared = set(state.inventory.guests)
        view.undeclared = [
            resource for name, resource in resources.items() if name not in declared
        ]
        # A domain a machine runs and no inventory declares. The same finding
        # as an undeclared Pacemaker resource, for the machines Pacemaker does
        # not answer for, and the one guest nothing else here could reach.
        view.undeclared_domains = [
            domain
            for name, domain in domains.items()
            if name not in declared and name not in resources
        ]

        if not view.guests:
            view.note = _NO_GUESTS
        return view

    def _domains(self, state: InventoryState) -> dict[str, LibvirtDomain]:
        """What libvirt reports, by domain name, across every machine.

        Asked of every machine the inventory declares rather than of the
        standalone ones alone: `deploy_prometheus_exporters` puts the exporter
        on the whole `hypervisors` group, and a cluster member runs domains
        too. Where both answer for a guest, they describe the same domain.

        A machine that does not answer costs its own domains and nothing else,
        which is the ordinary state of a machine being built.
        """
        if state.inventory is None:
            return {}
        targets = [
            (name, node.ansible_host)
            for name, node in state.inventory.hosts.items()
            if node.ansible_host
        ]
        found: dict[str, LibvirtDomain] = {}
        for exposition in read_all(
            self._client, targets, self._libvirt_port, timeout=self._timeout
        ):
            if not libvirt_reporting(exposition):
                continue
            reading = read_libvirt(exposition)
            for domain in reading.domains:
                found.setdefault(domain.name, domain)
        return found

    def _groups(self, wanted: bool) -> set[str]:
        """The guests Ceph holds, or nothing when it was not worth asking.

        Ceph not answering costs the one word it would have added, and the row
        then reads as a guest nothing reports, which is what it was before.
        """
        if not wanted or self._rbd is None:
            return set()
        try:
            return set(self._rbd.list_groups())
        except RbdUnavailable as error:
            logger.info("Could not list the RBD groups: %s", error)
            return set()

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

    def _resources(
        self, cluster: PacemakerCluster
    ) -> tuple[dict[str, PacemakerResource], dict[str, list[LocationConstraint]], str]:
        """What Pacemaker says, by guest name, and one sentence about it.

        The resource id `vm_manager` creates is the VM name itself, which is
        also the host key in the inventory, so the two halves are matched on
        equality rather than on a naming convention this service invents.

        Asked whatever the inventory says the mode is, the way the Cluster page
        asks. A file declaring a standalone machine is a statement about the
        desired state, and this column reports what is actually running: a node
        that is in a cluster its inventory has not caught up with is exactly
        when an operator opens this page.

        The location constraints come back with it, keyed the same way. They
        are what says a guest is being held somewhere, and the page cannot tell
        an operator's move from a declared `preferred_host` without them: the
        two are the same CIB object, so the only reading is the constraint
        against the entry.
        """
        if cluster.error:
            return {}, {}, f"{cluster.error} {_DESIRED_STATE_ONLY}"
        held: dict[str, list[LocationConstraint]] = {}
        for constraint in cluster.constraints:
            held.setdefault(constraint.resource, []).append(constraint)
        return (
            {
                resource.id: resource
                for resource in cluster.resources
                if VM_AGENT in resource.agent
            },
            held,
            _FROM_PACEMAKER,
        )
