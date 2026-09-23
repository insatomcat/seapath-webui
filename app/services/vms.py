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
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.cluster import ha
from app.cluster.exporters import MetricsClient, UrllibMetricsClient, read_all
from app.cluster.ha import (
    LocationConstraint,
    PacemakerCluster,
    PacemakerResource,
)
from app.cluster.libvirt import DEFAULT_PORT, LibvirtDomain
from app.cluster.libvirt import read as read_libvirt
from app.cluster.libvirt import reporting as libvirt_reporting
from app.cluster.rbd import RbdClient, RbdUnavailable, image_of
from app.core.logging import audit_event
from app.inventory import cloudinit
from app.inventory.editor import Scope, guest_entries
from app.inventory.files import UnsafePath
from app.inventory.model import (
    CLUSTER_GUEST_GROUP,
    GUEST_GROUP,
    STANDALONE_GUEST_GROUP,
    Inventory,
    Mode,
)
from app.inventory.references import Reference, Where, in_folder
from app.inventory.repository import Commit
from app.inventory.service import InventoryService, InventoryState, RefusedFile
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

# The guest template `seapath-ansible` ships, as the reference VM inventory
# names it. Relative to the playbooks, like every path an entry carries.
COLLECTION_TEMPLATE = "../templates/vm/guest.xml.j2"

# What starts a VNC server in a domain. A `<video>` card alone draws a screen
# nothing outside the guest can see, and a SPICE `<graphics>` is a protocol
# noVNC does not speak. See D62.
_VNC_GRAPHICS = re.compile(r"""<graphics\b[^>]*\btype\s*=\s*["']vnc["']""")

# The metadata key `vm_manager` stores a cluster guest's domain XML under, on
# its system image, and the one Pacemaker's agent defines the domain from.
XML_KEY = "xml"

# How many images are read at once for the graphic consoles. Each is one `rbd`
# asking a monitor, so the page waits for the slowest rather than for the sum.
_DISPLAY_READERS = 8


def has_vnc_display(xml: str) -> bool:
    """Whether a domain XML gives the guest a VNC display."""
    return _VNC_GRAPHICS.search(xml) is not None


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


class SourceFile(BaseModel):
    """A file a guest was created from, where this node keeps it."""

    where: Where
    """`inventory`, the versioned folder, or `artefacts`, the store git skips."""
    path: str
    """Relative to that root, as the Inventory page names it."""


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
    ansible_host: str | None = None
    """Where a run reaches inside the guest, when the entry says.

    The address the guest is expected to answer on, read off its entry. What
    it is actually answering on is nothing this page reads: a guest publishes
    no exporter.
    """
    seeded: bool = False
    """The entry carries a `cloud_init` mapping, so a creation attaches a seed.

    Worth the column because the address above means two different things
    depending on it: with a seed it is what the guest will be given, without
    one it is what somebody wrote the image with.
    """

    files: list[Reference] = Field(default_factory=list)
    """The paths this guest names, and whether a run would find each one."""
    creation: list[str] = Field(default_factory=list)
    """The variables of `CREATION_ONLY` the guest's own entry carries.

    What the end of the deployment run that creates it takes out of the entry.
    One it inherits from the `VMs` group is left out, since that line serves
    the other guests too.
    """
    sources: list[SourceFile] = Field(default_factory=list)
    """The files it was created from that this node still holds and nothing names.

    Read back from the commit that forgot how it was created, and offered for
    deletion on its row. A file another entry still names is left out, since
    deleting it would break that guest's next creation.
    """

    resource: PacemakerResource | None = None
    """Pacemaker's line for it, absent when nothing reports one."""

    pinning_profile: str | None = None
    """`vm_pinning_profile` from its entry, the seapath-alloc profile as text.

    Carried for a standalone guest, whose profile the deployment writes to
    `/etc/seapath/alloc.d` on every run, so editing the entry is how it
    changes. A cluster guest's is read from its image, where `vm_manager`
    wrote it once."""
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


class DisplaysView(BaseModel):
    """Which guests have a VNC display, from the XML that defines them."""

    guests: dict[str, bool | None] = Field(default_factory=dict)
    """Keyed by guest, for each one whose definition was found.

    A cluster guest's is the XML Ceph holds on its image, and a standalone
    guest's is the domain its machine's libvirt runs. None where Ceph or the
    machine did not answer, and the page then offers no console.
    """


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
    collection_template: str | None = None
    """SEAPATH's own guest template as an entry names it, where the collection has it.

    `../templates/vm/guest.xml.j2`, the template the reference VM inventory
    names. It takes the name, the disk, the bridges and the MAC from each
    guest's entry, so one file serves every guest of a site, and a guest
    declared with it needs no XML uploaded at all.
    """
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
    # `deploy_vms_standalone` renders `vm_template` and reads nothing else, so
    # a standalone guest naming only `xml_path` fails at the first task that
    # looks the template up. A plain XML is a template with no `{{ }}` in it,
    # and that is how a standalone entry names one.
    "xml_path",
)
STANDALONE_ONLY = ("autostart", "disk_extract")

# What both deployment roles read inside their creation block and nowhere else:
# the block runs for a guest the hypervisor does not have, or one whose entry
# carries `force`. Once the guest exists these lines describe how it was made
# and not what it is, and a later run never opens them, so the deployment run
# that created it takes them out. `bridges` is read there too and stays,
# because the collision checks of the next declaration read the MAC off it.
# See D49 and D50.
CREATION_ONLY = (
    "vm_disk",
    "vm_template",
    "xml_path",
    "additional_disk",
    "disk_extract",
    "cloud_init",
)

# The subject every forgetting commit starts with, and the trailer naming each
# file the guest was created from. The commit is where those names outlive the
# entry that carried them, and where the row reads them back to offer deleting
# the files: this service keeps no other record, and the history already
# answers which guest a file served.
FORGET_SUBJECT = "vms: forget how "
SOURCE_TRAILER = "Source-File"

# How long a guest just created is waited for before its recipe is kept. The
# exporters answer from what libvirt and crm_mon said at their last scrape, so
# a domain the run defined a second ago can be missing from the first reading.
# A guest still unreported after the last attempt keeps its lines, which is
# the reading that fails safe: the next deployment run needs them if the
# creation did not happen.
SETTLE_DELAYS = (0.0, 5.0, 10.0, 15.0, 30.0)

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


def _taken(inventory: Inventory | None) -> tuple[dict[str, str], dict[str, str]]:
    """The addresses and the MACs this inventory already hands out.

    Both keyed by the value and holding the host that has it, because what the
    refusal has to say is which host an operator is about to collide with.
    Machines carry an address and no MAC that this service knows of: a
    hypervisor's interfaces are the machine's own hardware, and nothing in an
    inventory names their MACs.
    """
    addresses: dict[str, str] = {}
    macs: dict[str, str] = {}
    if inventory is None:
        return addresses, macs
    for host, node in inventory.hosts.items():
        if node.ansible_host:
            addresses.setdefault(node.ansible_host, host)
    for guest, entry in inventory.guests.items():
        if entry.ansible_host:
            addresses.setdefault(entry.ansible_host, guest)
        for mac in [
            *(bridge.get("mac_address") for bridge in entry.bridges),
            *_seed_macs(entry.cloud_init),
        ]:
            mac = str(mac or "").strip().lower()
            if mac:
                macs.setdefault(mac, guest)
    return addresses, macs


def _seed_macs(cloud_init: dict[str, Any] | None) -> list[str]:
    """The MACs a guest's seed selects its interfaces by.

    Read beside `bridges` because a guest built from an XML the operator
    brought has no `bridges`: its MAC is in that XML, and the seed's `match` is
    the one place the inventory repeats it. Two guests declared from one such
    XML carry the same MAC there, and that is the collision worth catching.
    """
    network = (cloud_init or {}).get("network")
    ethernets = network.get("ethernets") if isinstance(network, dict) else None
    if not isinstance(ethernets, dict):
        return []
    return [
        str(interface["match"].get("macaddress") or "")
        for interface in ethernets.values()
        if isinstance(interface, dict) and isinstance(interface.get("match"), dict)
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


class NotDisabled(Exception):
    """A guest asked to be deleted while the cluster still holds it."""


class NoSources(Exception):
    """A guest with no source file left to delete."""


class VmService:
    def __init__(
        self,
        inventory: InventoryService,
        cluster: ClusterService,
        client: MetricsClient | None = None,
        libvirt_port: int = DEFAULT_PORT,
        timeout: float = 2.0,
        rbd: RbdClient | None = None,
        settle: Sequence[float] = SETTLE_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inventory = inventory
        self._cluster = cluster
        self._rbd = rbd
        self._settle = tuple(settle)
        self._sleep = sleep
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

    def complete_network(
        self,
        network: cloudinit.GuestNetwork,
        xml_path: str | None,
        vm_template: str | None,
    ) -> cloudinit.GuestNetwork:
        """The network with its MAC, from wherever the domain's interface is.

        A `.j2` template renders `bridges`, so the interface is this entry's to
        declare and a missing MAC is generated. Any other XML declares its
        interfaces itself, whichever variable names it, and the MAC is read off
        the committed file.
        """
        brought = xml_path or (
            vm_template if vm_template and not vm_template.endswith(".j2") else None
        )
        if brought is None:
            return network.completed()

        stored = in_folder(brought)
        try:
            document = self._inventory.read_file(stored) if stored else None
        except (OSError, UnsafePath):
            document = None
        if document is None:
            if network.bridge or network.address or network.dhcp:
                raise InvalidGuest(
                    f"{brought} is not in the inventory folder, and its "
                    "interfaces are where the guest's MAC is read from. Commit "
                    "the XML first."
                )
            return network
        try:
            macs, interfaces = cloudinit.brought_macs(document)
            return cloudinit.against_brought_xml(network, macs, interfaces, brought)
        except cloudinit.BroughtXmlRefused as error:
            raise InvalidGuest(str(error)) from error

    def declare(
        self,
        name: str,
        definition: dict[str, Any],
        author: str,
        expected_head: str | None = None,
        deployment: Mode | None = None,
        replace: bool = False,
        network: cloudinit.GuestNetwork | None = None,
    ) -> Commit | None:
        """Write one guest into the `VMs` group, as a commit.

        The page calls this "add a VM" and the operator never sees the group,
        the commit or the playbook that follows. What happens underneath is the
        ordinary path: a splice into the inventory, checked by `fidelity`, then
        an upstream playbook. See D30.

        `replace` writes over a guest of that name the file already declares,
        which is how a second attempt at adding it goes through.

        None where the file already said exactly this, which `replace` can
        produce: a declaration that went through and a deployment that did not
        is retried with the same form, and an empty commit in the audit trail
        would say a change happened.

        `network` is the guest's network as one form section describes it. The
        variables it produces are already in `definition`, put where they read
        well in the entry; this is the same thing typed, which is what the
        rules are written against. See `app.inventory.cloudinit`.
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
        self._check_definition(name, variables, deployment, network)
        commit, _ = self._inventory.declare_guest(
            name,
            variables,
            author,
            expected_head,
            _group_for(deployment),
            replace=replace,
        )
        return commit

    def undeclare(
        self, name: str, author: str, expected_head: str | None = None
    ) -> Commit:
        """Take a disabled guest out of the inventory, as a commit.

        The first half of deleting it, and the half that has to come first: an
        entry left behind after its images are gone is a guest the next
        deployment run creates afresh. Only a guest `disable` left is accepted.
        One Pacemaker still holds would lose its resource and its disk in a
        single click, and one nothing reports is a declaration the Inventory
        page edits.
        """
        view = self.guests()
        guest = next((item for item in view.guests if item.name == name), None)
        if guest is None:
            raise UnknownGuest(
                f"No guest called {name!r} is declared in this inventory."
            )
        if not guest.disabled:
            raise NotDisabled(
                f"{name} is not out of the cluster. Disable it first: that stops "
                "it and removes its Pacemaker resource, and leaves the disk this "
                "would delete."
            )
        state = self._inventory.state()
        beside = sorted(
            other
            for other, entry in (
                state.inventory.guests.items() if state.inventory else []
            )
            if other != name and name in (entry.extra.get("colocated_vms") or [])
        )
        if beside:
            raise InvalidGuest(
                f"{', '.join(beside)} is kept beside {name} through "
                "`colocated_vms`. Take it out of that list first."
            )
        commit, _ = self._inventory.undeclare_guest(name, author, expected_head)
        return commit

    def forget_created(
        self, playbook_id: str, author: str, run_id: str
    ) -> Commit | None:
        """Take out of the guests a run created what only their creation reads.

        Called when a deployment run ends, whatever its state: a run that failed
        halfway still created the guests it reached. The candidates are the
        guests this playbook creates whose own entry still carries a variable of
        `CREATION_ONLY` and no `force`, and each is judged on its own reading, so
        a guest the run did not create keeps its recipe for the next one. The
        exporters are asked again over `SETTLE_DELAYS` until every candidate is
        reported or the delays run out.

        One commit for all of them, authored by the operator who launched the
        run and naming it, so the line in the history is accounted for by an
        act somebody took. Its trailers name the files each guest was created
        from, which is what the row reads back to offer deleting them. None when
        nothing was left to forget.
        """
        forgotten: dict[str, GuestView] = {}
        for delay in self._settle or (0.0,):
            if delay:
                self._sleep(delay)
            view = self.guests()
            waiting = [
                guest
                for guest in view.guests
                if guest.playbook == playbook_id
                and guest.creation
                and not guest.force
                and guest.name not in forgotten
            ]
            for guest in waiting:
                if guest.resource or guest.domain or guest.disabled:
                    forgotten[guest.name] = guest
            if all(guest.name in forgotten for guest in waiting):
                break
        if not forgotten:
            return None

        names = sorted(forgotten)
        verb = "was" if len(names) == 1 else "were"
        trailers = [
            f"{SOURCE_TRAILER}: {name} {source.where.value} {source.path}"
            for name in names
            for source in self._sources_named_by(forgotten[name])
        ]
        message = (
            f"{FORGET_SUBJECT}{', '.join(names)} {verb} created\n\n"
            f"Deployment run {run_id} created {'it' if len(names) == 1 else 'them'}, "
            "and a deployment run reads these lines only for a guest the "
            "hypervisor does not have."
        )
        if trailers:
            message += "\n\n" + "\n".join(trailers)
        removals = {name: forgotten[name].creation for name in names}
        commit = self._inventory.write_variables(
            [],
            {name: dict.fromkeys(variables) for name, variables in removals.items()},
            message,
            author,
            removals=removals,
        )
        if commit is not None:
            audit_event(
                "vms.creation_forgotten",
                run=run_id,
                guests=",".join(names),
                commit=commit.hash,
                user=author,
            )
        return commit

    def set_pinning_profile(
        self,
        name: str,
        profile: str | None,
        author: str,
        expected_head: str | None = None,
    ) -> Commit | None:
        """Write a standalone guest's `vm_pinning_profile`, or take it out.

        One commit on the guest's own entry. `deploy_vms_standalone` writes
        the variable to `/etc/seapath/alloc.d/<guest>.yaml` on every run, and
        removes the file of a guest that no longer names one, so a run of it
        is what reaches the machine; the seapath-alloc hook reads the file
        when the guest starts. A cluster guest is refused: `deploy_vms_cluster`
        hands the profile to `vm_manager` at creation only, and the one it
        runs with is `_seapath_alloc` in the metadata of its image.
        """
        state = self._inventory.state()
        if state.inventory is None or name not in state.inventory.guests:
            raise UnknownGuest(
                f"No guest called {name!r} is declared in this inventory."
            )
        if state.inventory.deployment_of(name) is Mode.CLUSTER:
            raise InvalidGuest(
                f"{name} is a cluster guest, whose profile is `_seapath_alloc` "
                "in the metadata of its image. The Metadata window edits it."
            )
        text = (profile or "").strip()
        if text:
            _check_profile(text)
            # A block scalar ends with a newline, and the file the role writes
            # is the value as it is.
            text += "\n"
        entry = state.inventory.guests[name].extra.get("vm_pinning_profile")
        if (_profile_text(entry) or "") == text:
            return None
        variable = "vm_pinning_profile"
        subject = (
            f"vms: pinning profile of {name}"
            if text
            else f"vms: no pinning profile for {name}"
        )
        message = (
            f"{subject}\n\n"
            "Written to /etc/seapath/alloc.d by the next deploy_vms_standalone "
            "run, and read by the seapath-alloc hook when the guest starts."
        )
        if text:
            commit = self._inventory.write_variables(
                [(Scope(kind="host", name=name), {variable: text})],
                {name: {variable: text}},
                message,
                author,
                expected_head=expected_head,
            )
        else:
            commit = self._inventory.write_variables(
                [],
                {name: {variable: None}},
                message,
                author,
                removals={name: [variable]},
                expected_head=expected_head,
            )
        if commit is not None:
            audit_event(
                "vms.pinning_profile",
                guest=name,
                commit=commit.hash,
                user=author,
            )
        return commit

    def delete_sources(
        self, name: str, author: str
    ) -> tuple[list[SourceFile], list[Commit]]:
        """Delete the files a guest was created from, where nothing else names them.

        The image from the artefacts, the XML from the versioned folder as a
        commit. What is offered is read again here rather than trusted from the
        page, since another guest may have been declared from the same image in
        between, and a file it names is not deleted.
        """
        guest = next((item for item in self.guests().guests if item.name == name), None)
        if guest is None:
            raise UnknownGuest(
                f"No guest called {name!r} is declared in this inventory."
            )
        if not guest.sources:
            raise NoSources(
                f"This node holds no file {name} was created from that nothing "
                "else names."
            )
        commits: list[Commit] = []
        for source in guest.sources:
            if source.where is Where.INVENTORY:
                commit = self._inventory.remove_file(source.path, author)
                if commit is not None:
                    commits.append(commit)
            else:
                self._inventory.remove_artefact(source.path)
        audit_event(
            "vms.sources_deleted",
            guest=name,
            files=",".join(f"{s.where.value}:{s.path}" for s in guest.sources),
            user=author,
        )
        return guest.sources, commits

    def _sources_named_by(self, guest: GuestView) -> list[SourceFile]:
        """The files this node holds that the guest's own creation lines name."""
        roots = {
            Where.INVENTORY: self._inventory.folder,
            Where.ARTEFACTS: self._inventory.artefacts_root,
        }
        found: list[SourceFile] = []
        for reference in guest.files:
            root = roots.get(reference.where) if reference.where else None
            if (
                reference.variable not in guest.creation
                or root is None
                or not reference.resolved
            ):
                continue
            try:
                resolved = Path(reference.resolved).resolve()
                relative = resolved.relative_to(root.resolve())
            except ValueError:
                continue
            source = SourceFile(where=reference.where, path=relative.as_posix())
            if source not in found:
                found.append(source)
        return found

    def _recorded_sources(self) -> dict[str, list[SourceFile]]:
        """What the newest forgetting commit of each guest says it was created from."""
        recorded: dict[str, list[SourceFile]] = {}
        for _, message in self._inventory.messages(FORGET_SUBJECT):
            lines = message.splitlines()
            subject = lines[0][len(FORGET_SUBJECT) :]
            for ending in (" was created", " were created"):
                subject = subject.removesuffix(ending)
            named = [name.strip() for name in subject.split(",") if name.strip()]
            files: dict[str, list[SourceFile]] = {}
            for line in lines[1:]:
                key, _, value = line.partition(":")
                parts = value.strip().split(" ", 2)
                if key != SOURCE_TRAILER or len(parts) != 3:
                    continue
                try:
                    source = SourceFile(where=Where(parts[1]), path=parts[2])
                except ValueError:
                    continue
                files.setdefault(parts[0], []).append(source)
            for name in named:
                recorded.setdefault(name, files.get(name, []))
        return recorded

    def _held(self, source: SourceFile) -> Path | None:
        """Where this node keeps a source file, when it still does."""
        try:
            path = (
                self._inventory.file_path(source.path)
                if source.where is Where.INVENTORY
                else self._inventory.artefact_path(source.path)
            )
        except (OSError, UnsafePath, RefusedFile):
            return None
        return path.resolve() if path.is_file() else None

    def _check_definition(
        self,
        name: str,
        variables: dict[str, Any],
        deployment: Mode | None = None,
        network: cloudinit.GuestNetwork | None = None,
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

        template = str(variables.get("vm_template") or "")
        if not cluster and template and not template.endswith(".j2"):
            # `community.libvirt.virt define` takes the domain's name from the
            # XML, so a second standalone guest defined from the same plain
            # file redefines the first guest's domain, disk path and MAC
            # included. `vm_manager` rewrites the name in a cluster, which is
            # why this is a standalone rule.
            sharing = sorted(
                other
                for other, entry in (
                    state.inventory.guests.items() if state.inventory else []
                )
                if other != name
                and entry.vm_template == template
                and state.inventory.deployment_of(other) is Mode.STANDALONE
            )
            if sharing:
                raise InvalidGuest(
                    f"{template} already defines {', '.join(sharing)}. A plain "
                    "XML names one domain, and on a standalone machine a second "
                    "guest defined from it replaces the first one's definition. "
                    "Bring a .j2 template, SEAPATH's guest.xml.j2 among them, "
                    "which takes the name, the disk and the MAC from each "
                    "guest's entry."
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

        if network is not None and network.asked_for:
            # The address, the MAC and the bridge, held against what this file
            # already hands out. A duplicate here is a guest that half works on
            # somebody else's network, and the entry is written once.
            addresses, macs = _taken(state.inventory)
            wrong = cloudinit.refusal(name, network, addresses, macs)
            if wrong:
                raise InvalidGuest(wrong)

        bus = variables.get("disk_bus")
        if bus and bus not in DISK_BUSES:
            raise InvalidGuest(
                f"{bus!r} is not a disk bus this service writes. One of "
                f"{', '.join(DISK_BUSES)}."
            )

        profile = variables.get("vm_pinning_profile")
        if profile:
            _check_profile(profile)

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
            collection_template=(
                COLLECTION_TEMPLATE
                if self._inventory.collection_holds(COLLECTION_TEMPLATE)
                else None
            ),
            inventory_commit=state.commit,
        )

        named = self._inventory.references()
        files = self._files_by_host(named)
        entries = guest_entries(self._inventory.raw())
        # A file still named by any entry, the guest's own included, is one a
        # creation may read, so it is never offered for deletion.
        in_use = {
            Path(reference.resolved).resolve()
            for reference in named
            if reference.resolved
        }
        recorded = self._recorded_sources()
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
                    ansible_host=guest.ansible_host,
                    seeded=guest.cloud_init is not None,
                    pinning_profile=_profile_text(
                        guest.extra.get("vm_pinning_profile")
                    ),
                    preferred_host=guest.extra.get("preferred_host"),
                    pinned_host=guest.extra.get("pinned_host"),
                    files=files.get(name, []),
                    creation=[
                        variable
                        for variable in CREATION_ONLY
                        if variable in entries.get(name, {})
                    ],
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

        for guest in view.guests:
            if guest.creation:
                continue
            for source in recorded.get(guest.name, []):
                held = self._held(source)
                if held is not None and held not in in_use:
                    guest.sources.append(source)

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

        Two machines describing the same domain is the same moment as two
        Pacemaker lines for one resource: a guest that is moving is defined on
        both ends of the migration, and the machine that answers first is the
        first of the inventory rather than the one running it. The domain that
        is running wins, for the reason `ha.running` gives.
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
                kept = found.get(domain.name)
                if kept is None or (domain.running and not kept.running):
                    found[domain.name] = domain
        return found

    def displays(self) -> DisplaysView:
        """Which cluster guests carry a VNC `<graphics>` in their domain XML.

        Read from the `xml` metadata of each guest's system image, which is the
        definition Pacemaker's agent gives libvirt at every start, whatever
        created the guest: this service, `vm-mgr` by hand, or an import. The
        inventory forgets the XML a guest was created from once it exists, so
        it has nothing to say here. Asked on its own request, since it is one
        `rbd` per guest, and the page draws the table without waiting for it.
        """
        state = self._inventory.state()
        view = DisplaysView()
        if state.inventory is None or self._rbd is None:
            return view
        rbd = self._rbd
        names = [
            name
            for name in state.inventory.guests
            if state.inventory.deployment_of(name) is Mode.CLUSTER
        ]

        def read(name: str) -> tuple[str, str | None]:
            try:
                return name, rbd.list_metadata(image_of(name)).get(XML_KEY, "")
            except RbdUnavailable as error:
                logger.info("Could not read the XML of %s: %s", name, error)
                return name, None

        with ThreadPoolExecutor(max_workers=_DISPLAY_READERS) as pool:
            for name, xml in pool.map(read, names):
                # An empty answer is an image that does not exist yet, or one
                # `vm_manager` did not write: a guest with no domain to show.
                if xml != "":
                    view.guests[name] = None if xml is None else has_vnc_display(xml)
        return view

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

    def _files_by_host(
        self, named: list[Reference] | None = None
    ) -> dict[str, list[Reference]]:
        """Every path the inventory names, kept under the entry that names it.

        The same reading the Inventory page shows, so a guest whose image has
        not been uploaded says so here as well: with `any_errors_fatal`, a
        `copy` that cannot find its source ends the deployment on every host at
        once, three minutes in.
        """
        found: dict[str, list[Reference]] = {}
        for reference in named if named is not None else self._inventory.references():
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
        # Through `ha.running`: Pacemaker sends one line per node it holds a
        # record for, and the page leads with the one that says where the
        # guest is.
        named = {
            resource.id for resource in cluster.resources if VM_AGENT in resource.agent
        }
        found = {name: ha.running(cluster, name) for name in named}
        return (
            {name: line for name, line in found.items() if line is not None},
            held,
            _FROM_PACEMAKER,
        )


def _check_profile(profile: str) -> None:
    """Refuse a profile the seapath-alloc hook could not read."""
    try:
        parsed = yaml.safe_load(profile)
    except yaml.YAMLError as error:
        raise InvalidGuest(f"The pinning profile is not YAML: {error}") from error
    if not isinstance(parsed, dict):
        raise InvalidGuest(
            "The pinning profile is a mapping, the one "
            "`deploy_seapath_alloc` documents, starting with "
            "`version: 1`."
        )


def _profile_text(value: object) -> str | None:
    """A profile as the text the role writes, whichever way the entry spells it.

    The form writes a block scalar. A hand written entry may give a mapping,
    which the role renders through Jinja, and it is shown here as YAML.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return yaml.safe_dump(value, sort_keys=False, default_flow_style=False)
