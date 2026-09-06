# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The containers, read from the inventory and from what the machines publish.

A container here is a quadlet, and everything about it is an ordinary part of
the design: it is declared by variables the upstream roles read, deployed by an
ordinary run of the prerequisites playbook, and reported by the exporters every
node already runs. Nothing in this module reaches a machine except through a
run.

Starting and stopping are the runtime plane, and which button a container
carries is decided by who owns it. Pacemaker holds a resource for it: `crm
resource start`, on a member, and the cluster chooses the node. Nothing holds
one: `systemd`, on the machine named in the request, one machine at a time.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.inventory.editor import Scope
from app.inventory.service import ImportRefused, RefusedWrite
from app.runs.actions import Action
from app.runs.service import RunService
from app.services.containers import (
    ContainerService,
    ContainersView,
    InvalidContainer,
    UnknownContainer,
)

router = APIRouter(
    prefix="/containers",
    tags=["containers"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

admin = Depends(require_role(Role.ADMIN))
operator = Depends(require_role(Role.OPERATOR))


def _service(request: Request) -> ContainerService:
    return request.app.state.container_service


def _runs(request: Request) -> RunService:
    return request.app.state.run_service


class ContainerDeclaration(BaseModel):
    """A container to add, in the terms the upstream roles read."""

    name: str = Field(
        description=(
            "The quadlet's own name. It becomes `<name>.container` under "
            "/etc/containers/systemd, the unit `<name>.service`, and in a "
            "cluster the Pacemaker resource."
        )
    )
    scope_kind: str = Field(
        default="group",
        description="`group` or `host`: where the upload entry is written",
    )
    scope_name: str = Field(
        description="The group or the machine the entry is written on"
    )
    src: str | None = Field(
        default=None,
        description=(
            "The quadlet file, as the inventory names it. Defaults to "
            "`../files/<name>.container`, which is where "
            "PUT /inventory/files/files/<name>.container puts it."
        ),
    )
    pacemaker: bool = Field(
        default=False,
        description=(
            "Hand the unit to Pacemaker, by appending a primitive to "
            "extra_crm_cmd_to_run on cluster_machines. Cluster only."
        ),
    )


class DeclarationResponse(BaseModel):
    name: str
    commit: str
    message: str
    playbook: str = Field(
        description="The run that uploads the file and reloads systemd"
    )
    cluster_playbook: str | None = Field(
        default=None,
        description="The run that loads the primitive into the CIB, when there is one",
    )


class ActionResponse(BaseModel):
    """The run that carries out the action, watched like any other."""

    run_id: str
    state: str
    container: str
    action: str
    target: str
    """The unit or the Pacemaker resource the play names."""
    host: str = ""
    """The machine it acts on, empty when Pacemaker chose."""
    managed: str


@router.get("", response_model=ContainersView)
def containers(request: Request) -> ContainersView:
    """Every container the inventory declares, with its unit on each machine.

    `units` is what each machine's `node_exporter` says about the unit podman's
    generator wrote, `resource` is Pacemaker's line when the cluster holds one,
    and `managed` says which of the two owns it. `undeclared` lists the systemd
    resources the cluster runs that no quadlet here explains.
    """
    return _service(request).containers()


@router.post("", status_code=201)
def declare(
    request: Request,
    payload: ContainerDeclaration,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> DeclarationResponse:
    """Declare one container, one commit, and answer with the runs to launch.

    Adding a container is two acts the page presents as one: the quadlet file
    is committed with the inventory through `/inventory/files`, and this writes
    the entry that uploads it. Nothing here reaches a machine.
    """
    service = _service(request)
    if payload.scope_kind not in ("group", "host"):
        raise ApiError(
            "invalid_container",
            f"{payload.scope_kind!r} is not a scope. A container is declared "
            "on a `group` of machines or on one `host`.",
            400,
        )
    source = payload.src or f"../files/{payload.name}.container"
    try:
        commit = service.declare(
            payload.name,
            Scope(payload.scope_kind, payload.scope_name),
            source,
            user.username,
            if_match,
            payload.pacemaker,
        )
    except InvalidContainer as error:
        raise ApiError("invalid_container", str(error), 400) from error
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
        playbook=service.upload_playbook(),
        cluster_playbook=service.cluster_playbook if payload.pacemaker else None,
    )


@router.post("/{name}/start", status_code=202)
def start(
    request: Request,
    name: str,
    host: str | None = None,
    user: User = operator,
) -> ActionResponse:
    """Start one container, as a run.

    Through Pacemaker where the cluster holds a resource for it, which is one
    act for the whole cluster and Pacemaker's decision where it lands. Through
    systemd otherwise, on the machine named in `host`, which is one machine of
    however many the inventory sends the quadlet to.
    """
    return _act(request, name, host, user, start=True)


@router.post("/{name}/stop", status_code=202)
def stop(
    request: Request,
    name: str,
    host: str | None = None,
    user: User = operator,
) -> ActionResponse:
    """Stop one container, as a run.

    In a cluster the resource's target role is set to Stopped, so Pacemaker
    leaves it down until it is started again. Through systemd it is a stop and
    not a disable: a quadlet with an `[Install]` section comes back at the next
    boot.
    """
    return _act(request, name, host, user, start=False)


def _act(
    request: Request,
    name: str,
    host: str | None,
    user: User,
    start: bool,
) -> ActionResponse:
    service = _service(request)
    try:
        quadlet = service.check_known(name)
    except UnknownContainer as error:
        raise ApiError("unknown_container", str(error), 404) from error

    if not quadlet.actionable:
        raise ApiError(
            "not_actionable",
            f"{name} is a {quadlet.kind} quadlet. It is pulled in by the "
            "container that uses it, so it is started and stopped with it "
            "rather than on its own.",
            400,
        )

    resource = service.resource_for(quadlet.unit)
    if resource is not None:
        action = Action.RESOURCE_START if start else Action.RESOURCE_STOP
        record = _runs(request).launch_action(action, resource.id, user.username)
        return ActionResponse(
            run_id=record.id,
            state=record.state.value,
            container=name,
            action=action.value,
            target=resource.id,
            managed="pacemaker",
        )

    machines = service.hosts_of(name)
    chosen = _machine(name, host, machines)
    action = Action.UNIT_START if start else Action.UNIT_STOP
    record = _runs(request).launch_action(
        action, quadlet.unit, user.username, host=chosen
    )
    return ActionResponse(
        run_id=record.id,
        state=record.state.value,
        container=name,
        action=action.value,
        target=quadlet.unit,
        host=chosen,
        managed="systemd",
    )


def _machine(name: str, host: str | None, machines: list[str]) -> str:
    """Which machine the unit act runs on, refused rather than guessed at.

    A quadlet declared on a group is a unit on every machine of it, and a start
    that picked one of the three on the caller's behalf would be a start of
    something else than what was asked for.
    """
    if host:
        if host not in machines:
            raise ApiError(
                "unknown_host",
                f"This inventory does not send {name} to {host}. It goes to "
                + (", ".join(machines) if machines else "no machine at all")
                + ".",
                404,
            )
        return host
    if len(machines) == 1:
        return machines[0]
    if not machines:
        raise ApiError(
            "no_machine",
            f"This inventory sends {name} to no machine, so there is no unit "
            "to act on.",
            409,
        )
    raise ApiError(
        "host_required",
        f"{name} is a unit on {', '.join(machines)}, and no Pacemaker resource "
        "holds it, so the machine to act on has to be named.",
        400,
    )
