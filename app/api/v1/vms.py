# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The guests, read from the inventory and from Pacemaker.

Read only, and for now that is the whole of it. A guest is defined by an entry
in the `VMs` group, which is committed through `/inventory` like every other
part of the desired state, and deployed by a run of `deploy_vms_cluster` or
`deploy_vms_standalone` through `/runs`. Neither of those gets a second door
here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from app.api.v1 import reads
from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.inventory import cloudinit
from app.inventory.model import Mode
from app.inventory.service import GuestExists, ImportRefused, RefusedWrite
from app.runs.actions import Action
from app.runs.service import RunService
from app.services.domain_xml import (
    DomainXml,
    DomainXmlService,
    InvalidDomain,
    NoDomain,
)
from app.services.metadata import (
    InvalidMetadata,
    MetadataService,
    MetadataView,
    RbdUnavailable,
)
from app.services.ping import InvalidAddress, PingAnswer, Pinger
from app.services.ping import target as ping_target
from app.services.vms import (
    DisplaysView,
    GuestsView,
    InvalidGuest,
    NoSources,
    NotDisabled,
    SourceFile,
    UnknownGuest,
    VmService,
)
from app.trust import known_hosts
from app.trust.service import GuestTrust, TrustService

router = APIRouter(
    prefix="/vms",
    tags=["vms"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

admin = Depends(require_role(Role.ADMIN))
operator = Depends(require_role(Role.OPERATOR))


def _service(request: Request) -> VmService:
    return request.app.state.vm_service


def _runs(request: Request) -> RunService:
    return request.app.state.run_service


def _metadata(request: Request) -> MetadataService:
    return request.app.state.metadata_service


class GuestDeclaration(BaseModel):
    """A VM to add, in the terms the deployment roles read."""

    name: str = Field(
        description="The libvirt domain name, which is also the inventory key"
    )
    deployment: str | None = Field(
        default=None,
        description=(
            "`cluster` or `standalone`: which playbook creates it, written as "
            "the group the guest goes into. Absent leaves it in `VMs` itself, "
            "which is the file with one deployment to send it to."
        ),
    )
    vm_disk: str | None = Field(
        default=None, description="The disk image a creation starts from"
    )
    vm_template: str | None = Field(
        default=None, description="A Jinja2 libvirt XML, rendered per guest"
    )
    xml_path: str | None = Field(
        default=None, description="A libvirt XML taken as it is"
    )
    network: cloudinit.GuestNetwork | None = Field(
        default=None,
        description=(
            "The guest's network, which becomes three variables on the entry: "
            "the interface `guest.xml.j2` renders, the cloud-init seed the "
            "guest applies on its first boot, and the address a play reaches "
            "it at. A guest whose image carries its own configuration needs "
            "none of it. More than one interface is written on the Inventory "
            "page, where a site's own variables live"
        ),
    )
    force: bool = False
    replace: bool = Field(
        default=False,
        description=(
            "Write the entry over a guest of that name the file already "
            "declares, in the group it sits in. The guest itself is left as it "
            "is: the roles skip a guest the hypervisor already has, unless its "
            "entry carries `force`"
        ),
    )

    # What `cluster_vm create` is given, and therefore what is written into the
    # image's metadata once and for good. Changing one afterwards is the
    # metadata window and an outage, which is why they are asked here.
    enable: bool = Field(
        default=True, description="Add it to the cluster once it is created"
    )
    nostart: bool = Field(
        default=False, description="Cluster: define it and leave it stopped"
    )
    autostart: bool = Field(
        default=True, description="Standalone: start it when the hypervisor boots"
    )
    disk_extract: bool = Field(
        default=False, description="Standalone: the image is a gzipped raw disk"
    )
    live_migration: bool = False
    migrate_to_timeout: int | None = Field(
        default=None, ge=0, description="Seconds a live migration may take"
    )
    migration_downtime: int | None = Field(
        default=None, ge=0, description="Milliseconds the guest may be paused"
    )
    priority: int | None = Field(
        default=None, ge=0, description="Pacemaker resource priority"
    )
    preferred_host: str | None = Field(
        default=None, description="Run it here when possible"
    )
    pinned_host: str | None = Field(default=None, description="Run it here or nowhere")
    disk_bus: str | None = None
    colocated_vms: list[str] = Field(default_factory=list)
    strong_colocation: bool = False
    vm_pinning_profile: str | None = Field(
        default=None, description="The seapath-alloc profile, as YAML"
    )


class DeclarationResponse(BaseModel):
    name: str
    commit: str | None = Field(
        default=None,
        description=(
            "The commit that declared the guest. Absent where the entry "
            "already said exactly this, which is what a second attempt "
            "produces after a declaration that went through and a deployment "
            "that did not: the file is unchanged, so there is nothing to record"
        ),
    )
    message: str | None = None
    playbook: str = Field(
        description="The catalogue entry that deploys the group in this mode"
    )
    trusted_keys: list[str] = Field(
        default_factory=list,
        description=(
            "The fingerprints of the keys the seed installs in the guest: this "
            "node's own, then the site key where one is installed, which is "
            "what lets a run from another node reach the guest too. What an "
            "operator compares against the guest's `authorized_keys` when a "
            "run into it is refused"
        ),
    )
    mac_address: str | None = Field(
        default=None,
        description=(
            "The MAC of the interface the entry declares, reported because "
            "this service generates it when a bridge is named and the "
            "declaration leaves it out. It is what the guest's domain will "
            "carry and what its seed matches on"
        ),
    )


@router.get("", response_model=GuestsView, dependencies=[reads.reading])
def guests(request: Request) -> GuestsView:
    """Every guest the inventory declares, with its files and its resource.

    `files` answers whether a deployment would find the disk image and the XML
    each guest names, which is the question worth asking before the run rather
    than during it. `resource` is Pacemaker's line for the guest, absent on a
    standalone machine and whenever nothing publishes one, with `runtime_note`
    saying which of the two it is.
    """
    return _service(request).guests()


@router.get("/displays", response_model=DisplaysView)
def displays(request: Request) -> DisplaysView:
    """Which guests have a VNC display, for the graphic console button.

    A cluster guest's from the domain XML `vm_manager` keeps in its RBD image
    metadata, one `rbd` per guest. A standalone guest's from `virsh dumpxml`
    on the machine its libvirt exporter reports it on, one `ssh` per machine.
    On a request of its own, so the table is drawn without waiting for it.
    See D62.
    """
    service = _service(request)
    view = service.displays()
    located = {
        guest.name: guest.domain.host
        for guest in service.guests().guests
        if guest.deployment != Mode.CLUSTER.value and guest.domain is not None
    }
    view.guests.update(_domains(request).displays(located))
    return view


@router.get("/ping", response_model=PingAnswer, dependencies=[admin])
def ping(request: Request, address: str) -> PingAnswer:
    """Whether something already answers at an address a new guest is given.

    Echo requests sent from this node, up to three, stopping at the first
    reply. `answered` is conclusive; `silent` means nothing this node reaches
    answered, which a host dropping ICMP also produces; `unknown` says why this
    node could not tell. `address` may carry its prefix length, as the form
    field does. Asked by the same role that declares the guest.
    """
    try:
        destination = ping_target(address)
    except InvalidAddress as error:
        raise ApiError("invalid_address", str(error), 400) from error
    pinger: Pinger = request.app.state.pinger
    return pinger.ping(destination)


@router.post("", status_code=201)
def declare(
    request: Request,
    payload: GuestDeclaration,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> DeclarationResponse:
    """Add one guest to the inventory, and answer with the run to launch next.

    Adding a VM is three acts that the page presents as one: the disk image
    goes to the artefacts, the libvirt XML is committed with the inventory, and
    this declares the guest that names them. Deploying it is the run named in
    the answer, launched through `/runs` like every other. Nothing here reaches
    a machine. See [D30](decisions.md#d30).
    """
    service = _service(request)
    # The MAC, resolved once so that the entry, the check and the answer all
    # carry the same one: generated where a bridge is named for a template to
    # render, read off the XML where the operator brought one.
    try:
        network = (
            service.complete_network(
                payload.network, payload.xml_path, payload.vm_template
            )
            if payload.network
            else None
        )
    except InvalidGuest as error:
        raise ApiError("invalid_guest", str(error), 400) from error
    trust = _trust(request, network)
    definition = _definition(payload, network, trust)
    try:
        deployment = Mode(payload.deployment) if payload.deployment else None
    except ValueError as error:
        raise ApiError(
            "invalid_guest",
            f"{payload.deployment!r} is not a deployment. `cluster` creates "
            "the guest with deploy_vms_cluster, `standalone` with "
            "deploy_vms_standalone.",
            400,
        ) from error

    try:
        commit = service.declare(
            payload.name,
            definition,
            user.username,
            if_match,
            deployment,
            replace=payload.replace,
            network=network,
        )
    except InvalidGuest as error:
        raise ApiError("invalid_guest", str(error), 400) from error
    except GuestExists as error:
        # A code of its own because it has a remedy the page offers: the same
        # declaration again, with `replace`.
        raise ApiError("guest_exists", str(error), 409) from error
    except RefusedWrite as error:
        raise ApiError(
            "refused_write",
            str(error),
            409,
            {"divergences": [d.model_dump() for d in error.divergences]},
        ) from error
    except ImportRefused as error:
        raise ApiError(
            "invalid_inventory",
            str(error),
            422,
            {"findings": [f.model_dump() for f in error.validation.findings]},
        ) from error

    address = definition.get("ansible_host")
    if address and definition.get("ansible_ssh_common_args"):
        # A guest that accepts its host key on first use accepts only a key
        # its address has none of yet. See `known_hosts.forget_address`.
        known_hosts.forget_address(request.app.state.settings.known_hosts_file, address)

    return DeclarationResponse(
        name=payload.name,
        commit=commit.hash if commit else None,
        message=commit.message if commit else None,
        playbook=service.deploy_playbook(payload.name),
        mac_address=network.mac_address if network else None,
        trusted_keys=trust.fingerprints if trust else [],
    )


class ActionResponse(BaseModel):
    """The run that carries out the action, watched like any other."""

    run_id: str
    state: str
    guest: str
    action: str


def _act(request: Request, name: str, action: Action, user: User) -> ActionResponse:
    service = _service(request)
    try:
        service.check_known(name)
    except UnknownGuest as error:
        raise ApiError("unknown_guest", str(error), 404) from error

    record = _runs(request).launch_action(
        action, name, user.username, service.deployment_of(name)
    )
    return ActionResponse(
        run_id=record.id,
        state=record.state.value,
        guest=name,
        action=action.value,
    )


@router.post("/{name}/start", status_code=202)
def start(request: Request, name: str, user: User = operator) -> ActionResponse:
    """Start one guest, as a run.

    The runtime plane, and it reaches the machine the way everything else
    does: one task calling the upstream module, over the SSH path a
    convergence uses, under the same lock. See [D30](decisions.md#d30).
    """
    return _act(request, name, Action.START, user)


@router.post("/{name}/stop", status_code=202)
def stop(request: Request, name: str, user: User = operator) -> ActionResponse:
    """Stop one guest, as a run.

    In a cluster the resource is disabled as well as stopped, so Pacemaker
    leaves it down until it is started again. On a standalone machine the
    guest is asked through ACPI, so one that ignores ACPI keeps running.
    """
    return _act(request, name, Action.STOP, user)


@router.post("/{name}/restart", status_code=202)
def restart(request: Request, name: str, user: User = operator) -> ActionResponse:
    """Shut a standalone guest down and start it again, as a run.

    What makes a new definition or pinning profile take effect: libvirt reads
    the first, and the seapath-alloc hook the second, when the guest starts
    from shut off. A cluster guest's equivalent is `reconfigure`, since its
    configuration is read when Pacemaker creates its resource.
    """
    _standalone(request, name)
    return _act(request, name, Action.RESTART, user)


def _in_cluster(request: Request, name: str) -> None:
    """A guest Pacemaker can hold, which is the only kind these two act on."""
    if not _service(request).in_cluster(name):
        raise ApiError(
            "not_in_cluster",
            f"{name} is deployed on a standalone machine, so there is no "
            "Pacemaker resource to remove or to create. Stopping it is what "
            "keeps it down there.",
            409,
        )


@router.post("/{name}/disable", status_code=202)
def disable(request: Request, name: str, user: User = operator) -> ActionResponse:
    """Take one guest out of the cluster, as a run.

    `cluster_vm disable`: the guest is stopped and its Pacemaker resource
    removed. The disk image, its metadata and the inventory entry stay, so
    `POST /vms/{name}/enable` puts it back, and a deployment run leaves it
    alone because `deploy_vms_cluster` only creates a guest Ceph does not
    hold.
    """
    _known(request, name)
    _in_cluster(request, name)
    return _act(request, name, Action.DISABLE, user)


@router.post("/{name}/enable", status_code=202)
def enable(request: Request, name: str, user: User = operator) -> ActionResponse:
    """Put a disabled guest back in the cluster, as a run.

    `cluster_vm enable`, which builds the Pacemaker resource from the metadata
    on the guest's image and lets Pacemaker start it where it chooses.
    """
    _known(request, name)
    _in_cluster(request, name)
    return _act(request, name, Action.ENABLE, user)


class DeletionResponse(ActionResponse):
    """The commit that took the entry out, and the run that deletes the disk."""

    commit: str
    message: str


@router.post("/{name}/delete", status_code=202)
def delete(
    request: Request,
    name: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> DeletionResponse:
    """Delete a disabled guest for good: its entry, then its disk.

    Two acts in the order that fails safe. The entry is committed out of the
    inventory first, so no deployment run can create the guest again; then
    `cluster_vm remove` deletes its RBD group and images, as a run. A run that
    fails leaves an image nothing declares, which the history of the inventory
    brings back by reverting the commit. A run that cannot start at all, the
    lock held by a convergence for one, reverts the commit here and answers
    with the reason.

    Only a guest `disable` left, which is what `cluster_vm status` calls
    Disabled: `409 not_disabled` otherwise. `admin`, because it writes the
    inventory and destroys data.
    """
    service = _service(request)
    _known(request, name)
    _in_cluster(request, name)
    try:
        commit = service.undeclare(name, user.username, if_match)
    except UnknownGuest as error:
        raise ApiError("unknown_guest", str(error), 404) from error
    except NotDisabled as error:
        raise ApiError("not_disabled", str(error), 409) from error
    except InvalidGuest as error:
        raise ApiError("invalid_guest", str(error), 409) from error
    except RefusedWrite as error:
        raise ApiError(
            "refused_write",
            str(error),
            409,
            {"divergences": [d.model_dump() for d in error.divergences]},
        ) from error
    except ImportRefused as error:
        raise ApiError(
            "invalid_inventory",
            str(error),
            422,
            {"findings": [f.model_dump() for f in error.validation.findings]},
        ) from error

    try:
        record = _runs(request).launch_action(
            Action.DELETE, name, user.username, Mode.CLUSTER
        )
    except ApiError:
        # Declared again rather than left half deleted: an entry with its disk
        # is where the operator started, and they can try once the run that
        # held the lock is over.
        request.app.state.inventory_service.revert(commit.hash, user.username)
        raise
    return DeletionResponse(
        run_id=record.id,
        state=record.state.value,
        guest=name,
        action=Action.DELETE.value,
        commit=commit.hash,
        message=commit.message,
    )


class DeletedSources(BaseModel):
    """The files a guest was created from, deleted from this node."""

    name: str
    files: list[SourceFile] = Field(default_factory=list)
    commits: list[str] = Field(
        default_factory=list,
        description="One per file removed from the versioned folder",
    )


@router.post("/{name}/delete-sources")
def delete_sources(request: Request, name: str, user: User = admin) -> DeletedSources:
    """Delete the image and the XML a created guest was made from.

    Offered once the deployment run that created the guest has taken its
    creation lines out of the entry, which names the files in its commit. Only
    the files this node still holds and no entry of the inventory names: the
    image from the artefacts, the XML from the versioned folder as a commit
    (`files: remove <path>`). A file SEAPATH's collection ships is never one of
    them. `409 no_sources` when nothing is left to delete. `admin`, because it
    destroys a file another declaration could have used.
    """
    service = _service(request)
    try:
        files, commits = service.delete_sources(name, user.username)
    except UnknownGuest as error:
        raise ApiError("unknown_guest", str(error), 404) from error
    except NoSources as error:
        raise ApiError("no_sources", str(error), 409) from error
    return DeletedSources(
        name=name, files=files, commits=[commit.hash for commit in commits]
    )


def _trust(
    request: Request, network: cloudinit.GuestNetwork | None
) -> GuestTrust | None:
    """This node's account and key line, where the declaration asks to install them.

    Read only then: the key pair is created on first use, and a declaration
    that asked for no trust has no reason to touch the trust material.
    """
    if network is None or not network.trust_this_node:
        return None
    trust: TrustService = request.app.state.trust_service
    return trust.guest_trust(request.app.state.node_hostname)


def _definition(
    payload: GuestDeclaration,
    network: cloudinit.GuestNetwork | None = None,
    trust: GuestTrust | None = None,
) -> dict[str, Any]:
    """The entry to write, in the order it reads well in the file.

    Only what departs from the roles' own defaults. An entry spelling out
    `force: false` and `enable: true` on every guest says nothing and reads as
    if it did, and the file is somebody's audit trail.

    The network sits directly under the files, because what an operator looks
    for in a guest's entry is where its disk is and where the guest is.
    """
    definition: dict[str, Any] = {
        "vm_disk": payload.vm_disk,
        "vm_template": payload.vm_template,
        "xml_path": payload.xml_path,
        **(
            cloudinit.variables(
                payload.name,
                network,
                account=trust.account if trust else None,
                key_lines=trust.key_lines if trust else None,
            )
            if network
            else {}
        ),
        "preferred_host": payload.preferred_host,
        "pinned_host": payload.pinned_host,
        "priority": payload.priority,
        "migrate_to_timeout": payload.migrate_to_timeout,
        "migration_downtime": payload.migration_downtime,
        "disk_bus": payload.disk_bus,
        "colocated_vms": payload.colocated_vms,
        "vm_pinning_profile": payload.vm_pinning_profile,
    }
    for name, value, default in (
        ("force", payload.force, False),
        ("enable", payload.enable, True),
        ("nostart", payload.nostart, False),
        ("autostart", payload.autostart, True),
        ("disk_extract", payload.disk_extract, False),
        ("live_migration", payload.live_migration, False),
        ("strong_colocation", payload.strong_colocation, False),
    ):
        if value != default:
            definition[name] = value
    return definition


class MetadataWrite(BaseModel):
    """One key to set or to remove on a guest's RBD image."""

    key: str = Field(description="An RBD metadata key, letters, digits, _ . -")
    value: str | None = Field(
        default=None,
        description="The value to write. Absent or null removes the key.",
    )


def _known(request: Request, name: str) -> None:
    try:
        _service(request).check_known(name)
    except UnknownGuest as error:
        raise ApiError("unknown_guest", str(error), 404) from error


def _has_an_image(request: Request, name: str) -> None:
    """A guest whose metadata lives somewhere this can read.

    The metadata is on an RBD image, and a guest `deploy_vms_standalone`
    creates has a qcow2 in the local libvirt pool instead. Saying so beats an
    `rbd` call that fails on a name Ceph has never heard of.
    """
    if _service(request).deployment_of(name) is not Mode.CLUSTER:
        raise ApiError(
            "no_image",
            f"{name} is deployed on a standalone machine, so its disk is a "
            "file in the libvirt pool and there is no RBD image to carry "
            "metadata. What Pacemaker reads off one is what this window "
            "edits, and a standalone guest has no Pacemaker either.",
            409,
        )


def _unavailable(error: RbdUnavailable) -> ApiError:
    """Ceph could not be asked, said as what an operator can act on.

    503 rather than 500: the request was legitimate and the store it needs is
    the thing that did not answer. A standalone machine has no Ceph at all, and
    that is the ordinary reason.
    """
    return ApiError(
        "ceph_unavailable",
        f"The guest's image could not be read: {error}",
        503,
    )


@router.get("/{name}/metadata", response_model=MetadataView)
def metadata(request: Request, name: str) -> MetadataView:
    """Everything the guest's RBD image carries.

    Read from Ceph as this request is served, with `rbd image-meta list`.
    `changes` is empty here: it is what a write moved, and this moves nothing.
    """
    _known(request, name)
    _has_an_image(request, name)
    try:
        return _metadata(request).read(name)
    except RbdUnavailable as error:
        raise _unavailable(error) from error


@router.put("/{name}/metadata", response_model=MetadataView)
def write_metadata(
    request: Request, name: str, payload: MetadataWrite, user: User = admin
) -> MetadataView:
    """Add, change or remove one metadata key.

    A `value` writes it, whether the key was there or not. No `value` removes
    it. The image is read before and after, so `changes` is what actually moved
    rather than what the caller asked for.

    The guest keeps running and keeps the configuration it started with:
    Pacemaker reads these keys when it creates the resource. Applying the
    change is `POST /vms/{name}/reconfigure`, and that is an outage.
    """
    _known(request, name)
    _has_an_image(request, name)
    try:
        return _metadata(request).write(name, payload.key, payload.value, user.username)
    except InvalidMetadata as error:
        raise ApiError("invalid_metadata", str(error), 400) from error
    except RbdUnavailable as error:
        raise _unavailable(error) from error


@router.post("/{name}/reconfigure", status_code=202)
def reconfigure(request: Request, name: str, user: User = operator) -> ActionResponse:
    """Make a metadata change take effect, which stops and restarts the guest.

    `disable` then `enable` through `cluster_vm`, as a run. `enable_vm` reads
    the `_` metadata keys only when the guest is not already a Pacemaker
    resource, so there is no way to apply one of them without the guest going
    down and coming back. The confirmation says so before it happens.
    """
    return _act(request, name, Action.RECONFIGURE, user)


def _standalone(request: Request, name: str) -> None:
    _known(request, name)
    if _service(request).deployment_of(name) is Mode.CLUSTER:
        raise ApiError(
            "not_standalone",
            f"{name} is a cluster guest. Pacemaker holds it, and what applies "
            "its configuration is reconfigure, from the Metadata window.",
            409,
        )


def _domains(request: Request) -> DomainXmlService:
    return request.app.state.domain_xml_service


class DomainWrite(BaseModel):
    """A guest's libvirt domain, as `virsh edit` would leave it."""

    xml: str = Field(description="The whole `<domain>`, as `virsh dumpxml` gives it")
    restart: bool = Field(
        default=False,
        description=(
            "End the run with the guest shut down and started, which is what "
            "applies the definition. Otherwise it applies at the next start "
            "from shut off"
        ),
    )


class DefineResponse(BaseModel):
    """The run that defines the domain, or none when nothing moved."""

    guest: str
    changed: bool
    run_id: str | None = None
    state: str | None = None


@router.get("/{name}/xml", response_model=DomainXml)
def domain_xml(request: Request, name: str, user: User = admin) -> DomainXml:
    """A standalone guest's persistent definition, as its machine holds it.

    `virsh dumpxml --inactive` over the SSH path a run takes, read as this
    request is served. The inactive definition is what `virsh edit` edits: the
    running domain carries what libvirt added when it started it. A cluster
    guest's domain lives in the metadata of its image instead.
    """
    _standalone(request, name)
    try:
        return _domains(request).read(name)
    except NoDomain as error:
        raise ApiError("no_domain", str(error), 409) from error


@router.put("/{name}/xml", response_model=DefineResponse)
def define_domain(
    request: Request, name: str, payload: DomainWrite, user: User = admin
) -> DefineResponse:
    """Define a standalone guest again from an edited XML, as a run.

    The domain is read again and the XML checked against it: the same
    `<name>`, and the `<uuid>` libvirt holds it under. The run is one task of
    `community.libvirt.virt`, `command: define`, which is what
    `deploy_vms_standalone` creates the domain with, and the XML is written
    into the run's own tree. With `restart`, the same run then shuts the guest
    down and starts it, which applies the definition; without it, the guest
    takes it at its next start from shut off.

    The inventory is not changed. A guest created again from its entry gets
    the domain its template renders. See D66.
    """
    _standalone(request, name)
    try:
        record = _domains(request).define(
            name, payload.xml, user.username, payload.restart
        )
    except InvalidDomain as error:
        raise ApiError("invalid_domain", str(error), 400) from error
    except NoDomain as error:
        raise ApiError("no_domain", str(error), 409) from error
    if record is None:
        return DefineResponse(guest=name, changed=False)
    return DefineResponse(
        guest=name, changed=True, run_id=record.id, state=record.state.value
    )


class ProfileWrite(BaseModel):
    """A standalone guest's seapath-alloc profile."""

    profile: str | None = Field(
        default=None,
        description=(
            "The profile as YAML, a mapping starting with `version: 1`. Empty "
            "or absent takes `vm_pinning_profile` out of the entry, and the "
            "run removes the file"
        ),
    )
    restart: bool = Field(
        default=False,
        description=(
            "End the run with the guest shut down and started, which is when "
            "the seapath-alloc hook reads the profile"
        ),
    )


class ProfileResponse(BaseModel):
    guest: str
    commit: str | None = None
    message: str | None = None
    run_id: str | None = Field(
        default=None, description="The run writing it to the machine"
    )
    state: str | None = None


@router.put("/{name}/pinning-profile", response_model=ProfileResponse)
def write_profile(
    request: Request,
    name: str,
    payload: ProfileWrite,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> ProfileResponse:
    """Write a standalone guest's `vm_pinning_profile`, and put it on the machine.

    One commit on the guest's entry, then one run on the machine holding the
    guest: the two tasks `deploy_vms_standalone` writes
    `/etc/seapath/alloc.d/<guest>.yaml` with, reading the value from the
    committed inventory, and with `restart` the guest shut down and started,
    since the seapath-alloc hook reads the file when it starts. No commit and
    no run when the entry already said exactly this. A cluster guest answers
    `400 invalid_guest`: its profile is `_seapath_alloc` in the metadata of
    its image. See D66.
    """
    service = _service(request)
    domains = _domains(request)
    try:
        service.check_known(name)
    except UnknownGuest as error:
        raise ApiError("unknown_guest", str(error), 404) from error
    if service.deployment_of(name) is not Mode.CLUSTER and not domains.machine(name):
        # Refused before the commit, so the entry never says something no run
        # could put on a machine.
        raise ApiError(
            "no_domain",
            f"No machine can be named for {name}: no libvirt exporter reports "
            "it and the inventory has more than one standalone machine.",
            409,
        )
    try:
        commit = service.set_pinning_profile(
            name, payload.profile, user.username, if_match
        )
    except UnknownGuest as error:
        raise ApiError("unknown_guest", str(error), 404) from error
    except InvalidGuest as error:
        raise ApiError("invalid_guest", str(error), 400) from error
    except RefusedWrite as error:
        raise ApiError(
            "refused_write",
            str(error),
            409,
            {"divergences": [d.model_dump() for d in error.divergences]},
        ) from error
    except ImportRefused as error:
        raise ApiError(
            "invalid_inventory",
            str(error),
            422,
            {"findings": [f.model_dump() for f in error.validation.findings]},
        ) from error
    if commit is None:
        return ProfileResponse(guest=name)
    record = domains.write_profile(name, user.username, payload.restart)
    return ProfileResponse(
        guest=name,
        commit=commit.hash,
        message=commit.message,
        run_id=record.id,
        state=record.state.value,
    )
