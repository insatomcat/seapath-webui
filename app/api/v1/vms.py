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
from app.runs.actions import KEY, MAX_VALUE_BYTES, Action, MetadataOp, image_of
from app.runs.service import RunService
from app.services import metadata as metadata_results
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


class MetadataRun(BaseModel):
    """The run that reads or writes, watched like any other."""

    run_id: str
    state: str
    guest: str
    operation: str


class MetadataView(BaseModel):
    """What the last metadata run for this guest brought back."""

    guest: str
    image: str = Field(description="The RBD image the metadata lives on")
    run_id: str | None = None
    finished_at: str | None = None
    state: str | None = None
    entries: dict[str, str] = Field(default_factory=dict)
    changes: list[metadata_results.MetadataChange] = Field(default_factory=list)
    note: str = ""


def _metadata_run(
    request: Request,
    name: str,
    op: MetadataOp,
    user: User,
    key: str = "",
    value: str = "",
) -> MetadataRun:
    service = _service(request)
    try:
        service.check_known(name)
    except UnknownGuest as error:
        raise ApiError("unknown_guest", str(error), 404) from error

    record = _runs(request).launch_metadata(op, name, user.username, key, value)
    return MetadataRun(
        run_id=record.id,
        state=record.state.value,
        guest=name,
        operation=op.value,
    )


def _checked_key(key: str) -> str:
    if not KEY.match(key):
        raise ApiError(
            "invalid_metadata_key",
            (
                f"{key!r} is not an RBD metadata key this service writes. "
                "Letters, digits, underscore, dot and dash, up to 128 of them."
            ),
            400,
        )
    return key


@router.get("/{name}/metadata", response_model=MetadataView)
def metadata(request: Request, name: str) -> MetadataView:
    """The metadata the last read or write brought back, and when.

    A value rather than a live reading: the metadata lives on an RBD image and
    getting it costs a run, so the page shows what it last saw and offers to
    look again. `POST /vms/{name}/metadata/read` is that.
    """
    service = _service(request)
    try:
        service.check_known(name)
    except UnknownGuest as error:
        raise ApiError("unknown_guest", str(error), 404) from error
    record, result = _runs(request).latest_metadata(name)
    if record is None:
        return MetadataView(
            guest=name,
            image=image_of(name),
            note=(
                "Nobody has read this guest's metadata from this node yet. "
                "Reading it is a run against the machine holding the image."
            ),
        )
    return MetadataView(
        guest=name,
        image=image_of(name),
        run_id=record.id,
        finished_at=record.finished_at.isoformat() if record.finished_at else None,
        state=record.state.value,
        entries=result.entries if result else {},
        changes=result.changes if result else [],
        note=(
            ""
            if result
            else "The last run brought nothing back. Its log says what happened."
        ),
    )


@router.post("/{name}/metadata/read", status_code=202)
def read_metadata(request: Request, name: str, user: User = operator) -> MetadataRun:
    """Read the guest's RBD image metadata, as a run that changes nothing.

    It takes no lock: the lock exists so two operators do not converge the same
    machines at once, and this converges nothing. Refusing to show what a guest
    is configured with while a convergence is going would be a page hiding the
    answer at the moment somebody wants it.
    """
    return _metadata_run(request, name, MetadataOp.READ, user)


@router.put("/{name}/metadata", status_code=202)
def write_metadata(
    request: Request, name: str, payload: MetadataWrite, user: User = admin
) -> MetadataRun:
    """Add, change or remove one metadata key.

    A `value` writes it, whether the key was there or not. No `value` removes
    it. Either way the run reads the image before and after, so the answer to
    "did this change anything" comes from the image.

    The guest keeps running and keeps the configuration it started with:
    Pacemaker reads these keys when the resource is created. Applying the
    change is `POST /vms/{name}/reconfigure`, and that is an outage.
    """
    key = _checked_key(payload.key)
    if payload.value is None:
        return _metadata_run(request, name, MetadataOp.REMOVE, user, key)
    if len(payload.value.encode()) > MAX_VALUE_BYTES:
        raise ApiError(
            "invalid_metadata_value",
            f"A metadata value is at most {MAX_VALUE_BYTES} bytes.",
            400,
        )
    return _metadata_run(request, name, MetadataOp.SET, user, key, payload.value)


@router.post("/{name}/reconfigure", status_code=202)
def reconfigure(request: Request, name: str, user: User = admin) -> ActionResponse:
    """Make a metadata change take effect, which stops and restarts the guest.

    `disable` then `enable` through `cluster_vm`. `enable_vm` reads the `_`
    metadata keys only when the guest is not already a Pacemaker resource, so
    there is no way to apply one of them without the guest going down and
    coming back. The confirmation says so before it happens.
    """
    return _act(request, name, Action.RECONFIGURE, user)
