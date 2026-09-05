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
    enable: bool = True


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
    definition: dict[str, Any] = {
        "vm_disk": payload.vm_disk,
        "vm_template": payload.vm_template,
        "xml_path": payload.xml_path,
    }
    # Both switches default in the roles to what an ordinary deployment wants,
    # so only a guest that departs from that carries one. An entry spelling out
    # `force: false` and `enable: true` says nothing and reads as if it did.
    if payload.force:
        definition["force"] = True
    if not payload.enable:
        definition["enable"] = False

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
