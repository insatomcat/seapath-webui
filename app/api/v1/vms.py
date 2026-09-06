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

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.inventory.service import ImportRefused, RefusedWrite
from app.runs.actions import Action
from app.runs.service import RunService
from app.services.metadata import (
    InvalidMetadata,
    MetadataService,
    MetadataView,
    RbdUnavailable,
)
from app.services.vms import GuestsView, InvalidGuest, UnknownGuest, VmService

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
    vm_disk: str | None = Field(
        default=None, description="The disk image a creation starts from"
    )
    vm_template: str | None = Field(
        default=None, description="A Jinja2 libvirt XML, rendered per guest"
    )
    xml_path: str | None = Field(
        default=None, description="A libvirt XML taken as it is"
    )
    force: bool = False

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
    commit: str
    message: str
    playbook: str = Field(
        description="The catalogue entry that deploys the group in this mode"
    )


@router.get("", response_model=GuestsView)
def guests(request: Request) -> GuestsView:
    """Every guest the inventory declares, with its files and its resource.

    `files` answers whether a deployment would find the disk image and the XML
    each guest names, which is the question worth asking before the run rather
    than during it. `resource` is Pacemaker's line for the guest, absent on a
    standalone machine and whenever nothing publishes one, with `runtime_note`
    saying which of the two it is.
    """
    return _service(request).guests()


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
    definition = _definition(payload)

    try:
        commit = service.declare(payload.name, definition, user.username, if_match)
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

    return DeclarationResponse(
        name=payload.name,
        commit=commit.hash,
        message=commit.message,
        playbook=service.deploy_playbook(),
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

    record = _runs(request).launch_action(action, name, user.username)
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


def _definition(payload: GuestDeclaration) -> dict[str, Any]:
    """The entry to write, in the order it reads well in the file.

    Only what departs from the roles' own defaults. An entry spelling out
    `force: false` and `enable: true` on every guest says nothing and reads as
    if it did, and the file is somebody's audit trail.
    """
    definition: dict[str, Any] = {
        "vm_disk": payload.vm_disk,
        "vm_template": payload.vm_template,
        "xml_path": payload.xml_path,
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
