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

from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from app.api.v1 import reads
from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.inventory import delivery
from app.inventory.editor import Scope
from app.inventory.repository import StaleWrite
from app.inventory.service import ImportRefused, RefusedWrite
from app.runs.actions import Action
from app.runs.catalogue import RECREATE_VARIABLE, RESTART_VARIABLE
from app.runs.service import RunService
from app.services.containers import (
    WORKLOAD_PLAYBOOK,
    ContainerService,
    ContainersView,
    InvalidContainer,
    QuadletFiles,
    UnknownContainer,
)
from app.services.deliveries import (
    DeliveryService,
    Installed,
    Placement,
    RefusedPlacement,
    RefusedValues,
    StagedDelivery,
    UnknownDelivery,
    UnknownValues,
    WorkloadValues,
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


def _deliveries(request: Request) -> DeliveryService:
    return request.app.state.delivery_service


class SiteValues(BaseModel):
    """The site values of a workload, by key, as a form sends them."""

    values: dict[str, Any] = Field(default_factory=dict)
    apply: bool = Field(
        default=False,
        description=(
            "Once committed, launch deploy_containers_cluster and restart the "
            "workload at its end, so that the change applies now"
        ),
    )


class DeliveryInstallation(SiteValues):
    """What installing a delivery asks the operator for."""

    placement: Placement | None = Field(
        default=None,
        description=(
            "Where the workload runs. Omitted, it keeps the placement it has; "
            "sent with both fields empty, the cluster chooses"
        ),
    )
    examples: list[str] | None = Field(
        default=None,
        description=(
            "The examples to copy where the site has no configuration file of "
            "its own. Omitted, every one is copied; one left out stays missing, "
            "and the run stops until the site adds its file"
        ),
    )
    recreate: bool = Field(
        default=False,
        description=(
            "Launch the run that starts the workload again from nothing: its "
            "resource is deleted and its RBD image put aside, then both are "
            "created as on a first deployment. The configuration of the site is "
            "kept; the workload starts without the state it wrote"
        ),
    )


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
            "Let Pacemaker run it, as a workload of cluster_containers that "
            "deploy_containers_cluster deploys on every hypervisor of the "
            "cluster. The scope is then not used. Cluster only."
        ),
    )


class DeclarationResponse(BaseModel):
    name: str
    commit: str
    message: str
    playbook: str = Field(
        description=(
            "The run that puts it on the machines: the one that uploads the "
            "file and reloads systemd, or deploy_containers_cluster for a "
            "container Pacemaker runs"
        )
    )


class Removal(BaseModel):
    """What removing a workload asks the operator for."""

    remove_rbd: bool = Field(
        default=False,
        description=(
            "Delete the workload's RBD image too, with its snapshots and the "
            "images put aside: the state it wrote is gone. Left false, the "
            "image stays in the pool, and the backups go on exporting it"
        ),
    )


class RemovalResponse(BaseModel):
    """The commit marking the workload absent, and the run applying it."""

    name: str
    commit: str
    message: str
    run_id: str


class RedeclarationResponse(BaseModel):
    """The commit declaring the workload again, and the run deploying it."""

    name: str
    commit: str
    message: str
    run_id: str


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


@router.get("", response_model=ContainersView, dependencies=[reads.reading])
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
        playbook=(
            WORKLOAD_PLAYBOOK if payload.pacemaker else service.upload_playbook()
        ),
    )


# Deliveries: a workload as a supplier hands it over. Declared before the
# routes taking a container name, which would otherwise read `deliveries` as one.


@router.post("/deliveries", status_code=201)
async def stage_delivery(request: Request, user: User = admin) -> StagedDelivery:
    """Receive a delivery archive, unpack it and check it.

    The body is the archive itself, as for an artefact. The answer is what it
    holds and the site values it asks for, or every reason it cannot be
    installed in `findings`. Nothing is written to the inventory yet.
    """
    return await _deliveries(request).stage(request.stream())


@router.get("/deliveries/{staged}", response_model=StagedDelivery)
def staged_delivery(request: Request, staged: str) -> StagedDelivery:
    try:
        return _deliveries(request).staged(staged)
    except UnknownDelivery as error:
        raise ApiError("unknown_delivery", str(error), 404) from error


@router.delete("/deliveries/{staged}", status_code=204)
def discard_delivery(request: Request, staged: str, user: User = admin) -> Response:
    try:
        _deliveries(request).discard(staged)
    except UnknownDelivery as error:
        raise ApiError("unknown_delivery", str(error), 404) from error
    return Response(status_code=204)


@router.post("/deliveries/{staged}/install", status_code=201)
def install_delivery(
    request: Request,
    staged: str,
    payload: DeliveryInstallation,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> Installed:
    """Install a staged delivery with the site values, as one commit.

    The quadlets and examples go to the versioned folder, the site's
    configuration files to its `site/` when it has none yet, the image archives
    to the artefacts, and the workload to `cluster_containers`. The answer
    names the run that puts it on the machines, `deploy_containers_cluster`,
    and carries it when `apply` or `recreate` asked for it to be launched.
    """
    variable = (
        RECREATE_VARIABLE
        if payload.recreate
        else RESTART_VARIABLE if payload.apply else None
    )
    if variable:
        _refuse_ignored(request, variable)
    try:
        installed = _deliveries(request).install(
            staged,
            payload.values,
            user.username,
            if_match,
            payload.placement,
            payload.examples,
        )
    except UnknownDelivery as error:
        raise ApiError("unknown_delivery", str(error), 404) from error
    except RefusedPlacement as error:
        raise ApiError("invalid_placement", str(error), 400) from error
    except RefusedValues as error:
        raise ApiError(
            "invalid_values", str(error), 400, {"refused": error.refused}
        ) from error
    except delivery.InvalidDelivery as error:
        raise ApiError(
            "invalid_delivery", str(error), 400, {"findings": error.findings}
        ) from error
    except StaleWrite as error:
        raise ApiError("stale_write", str(error), 409) from error
    except (InvalidContainer, RefusedWrite) as error:
        raise ApiError("invalid_container", str(error), 400) from error
    except ImportRefused as error:
        raise ApiError(
            "invalid_inventory",
            str(error),
            422,
            {"findings": [f.model_dump() for f in error.validation.findings]},
        ) from error
    if not variable:
        return installed
    return _launch(request, user, installed, {variable: installed.name})


def _refuse_ignored(request: Request, variable: str) -> None:
    """Before the commit: a collection whose role would ignore the variable
    would turn "apply now" into a commit and a run that applies nothing."""
    refused = _runs(request).undeclared(WORKLOAD_PLAYBOOK, [variable])
    if not refused:
        return
    if variable == RESTART_VARIABLE:
        refused += (
            " Until then, commit without applying, run "
            f"{WORKLOAD_PLAYBOOK} and restart the workload on this page."
        )
    raise ApiError("precondition_failed", refused, 409, {"unmet": [refused]})


def _launch(
    request: Request, user: User, installed: Installed, variables: dict[str, str]
) -> Installed:
    """One gesture, one run: the window follows it. The commit is made either
    way, so a run the service refuses says so beside the commit it follows."""
    try:
        record = _runs(request).launch(
            WORKLOAD_PLAYBOOK, user.username, variables=variables
        )
    except ApiError as error:
        raise ApiError(
            error.code,
            (
                f"{installed.name} is committed"
                + (f" as {installed.commit[:12]}" if installed.commit else "")
                + f", and the run applying it was not launched: {error.message}"
            ),
            error.status_code,
            error.detail,
        ) from error
    installed.run_id = record.id
    return installed


@router.get("/{name}/values", response_model=WorkloadValues)
def workload_values(request: Request, name: str) -> WorkloadValues:
    """The site values of a workload installed from a delivery, and their form."""
    try:
        return _deliveries(request).values(name)
    except UnknownValues as error:
        raise ApiError("no_values", str(error), 404) from error


@router.put("/{name}/values")
def set_workload_values(
    request: Request,
    name: str,
    payload: SiteValues,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> Installed:
    """Change the site values of a workload, as one commit.

    They reach the machines on the next run of `deploy_containers_cluster`, and
    the running workload once it is restarted: an environment variable is
    read when a process starts. `apply` launches that run, which restarts the
    workload at its end.
    """
    if payload.apply:
        _refuse_ignored(request, RESTART_VARIABLE)
    try:
        installed = _deliveries(request).set_values(
            name, payload.values, user.username, if_match
        )
    except UnknownValues as error:
        raise ApiError("no_values", str(error), 404) from error
    except RefusedValues as error:
        raise ApiError(
            "invalid_values", str(error), 400, {"refused": error.refused}
        ) from error
    except StaleWrite as error:
        raise ApiError("stale_write", str(error), 409) from error
    except (InvalidContainer, RefusedWrite) as error:
        raise ApiError("invalid_container", str(error), 400) from error
    if not payload.apply:
        return installed
    return _launch(request, user, installed, {RESTART_VARIABLE: name})


@router.post("/{name}/remove", status_code=202)
def remove(
    request: Request,
    name: str,
    payload: Removal,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> RemovalResponse:
    """Remove a workload of `cluster_containers` from the cluster.

    Two acts, in the order a deletion of a guest takes them. The entry is
    marked `state: absent` as one commit, then `deploy_containers_cluster`
    runs: the role stops the resource and deletes it with its constraints,
    and takes the quadlets, the configuration, the image archives and the
    images off every node, and the RBD image with `remove_rbd`. Once that run
    has succeeded, the entry and the files it named leave the inventory as a
    commit of their own, by the same operator. A run that cannot start at all
    reverts the first commit here and answers with the reason.

    A container `upload_extra_files` uploads has no role to take it off the
    machines and is refused: `404 unknown_container`. `admin`, because it
    writes the inventory and may destroy data.
    """
    try:
        commit = _service(request).remove(
            name, payload.remove_rbd, user.username, if_match
        )
    except UnknownContainer as error:
        raise ApiError("unknown_container", str(error), 404) from error
    except InvalidContainer as error:
        raise ApiError("invalid_container", str(error), 409) from error
    except StaleWrite as error:
        raise ApiError("stale_write", str(error), 409) from error
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
        record = _runs(request).launch(WORKLOAD_PLAYBOOK, user.username)
    except ApiError:
        # Declared again rather than left marked: a workload that runs is
        # where the operator started, and they can try once the run holding
        # the lock is over.
        request.app.state.inventory_service.revert(commit.hash, user.username)
        raise
    return RemovalResponse(
        name=name, commit=commit.hash, message=commit.message, run_id=record.id
    )


@router.post("/{name}/redeclare", status_code=202)
def redeclare(
    request: Request,
    name: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> RedeclarationResponse:
    """Declare again a workload a removal took out of the inventory.

    The entry its removal marked absent, without `state` and `remove_rbd`, and
    the files the same removal took out of the folder, read from the history
    as one commit, then a run of `deploy_containers_cluster`. The role keeps
    the RBD image it finds in the pool, so a workload whose state was restored
    from a backup first starts on that state. A run that cannot start reverts
    the commit here and answers with the reason.

    `404 unknown_container` for a name no removal took out, or one declared
    again since. `409 invalid_container` when its image archives left the
    artefacts with it, or a guest has the name now. `admin`, because it
    writes the inventory.
    """
    try:
        commit = _service(request).redeclare(name, user.username, if_match)
    except UnknownContainer as error:
        raise ApiError("unknown_container", str(error), 404) from error
    except InvalidContainer as error:
        raise ApiError("invalid_container", str(error), 409) from error
    except StaleWrite as error:
        raise ApiError("stale_write", str(error), 409) from error
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
        record = _runs(request).launch(WORKLOAD_PLAYBOOK, user.username)
    except ApiError:
        # Left undeclared rather than declared with nothing deploying it: the
        # page offers the same gesture again once the run holding the lock is
        # over.
        request.app.state.inventory_service.revert(commit.hash, user.username)
        raise
    return RedeclarationResponse(
        name=name, commit=commit.hash, message=commit.message, run_id=record.id
    )


@router.get("/{name}/files", response_model=QuadletFiles)
def quadlet_files(request: Request, name: str) -> QuadletFiles:
    """Every file one container is made of, as the inventory holds them.

    The quadlet the Quadlet column names comes first, then the ones it is
    joined to: the pod, the containers and the networks of a workload and the
    files its RBD image carries, or the `.network`, `.volume` and `.pod` files
    an uploaded quadlet names. Each is read through the reference a run would
    follow, and a file that cannot be shown carries its own `error` rather
    than failing the others.

    A path pointing outside the folders a run overlays is refused: this answers
    for the inventory, not for the filesystem of the machine it happens to be
    running on.
    """
    try:
        return _service(request).quadlet_files(name)
    except UnknownContainer as error:
        raise ApiError("unknown_container", str(error), 404) from error


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


@router.post("/{name}/restart", status_code=202)
def restart(request: Request, name: str, user: User = operator) -> ActionResponse:
    """Restart a container Pacemaker holds, as a run.

    `crm resource restart`, which is how a changed quadlet, image or site value
    reaches a workload that is running. A container systemd owns is stopped and
    started on its machine instead.
    """
    service = _service(request)
    try:
        quadlet = service.check_known(name)
    except UnknownContainer as error:
        raise ApiError("unknown_container", str(error), 404) from error
    resource = service.resource_for(quadlet.unit)
    if resource is None:
        raise ApiError(
            "not_a_resource",
            f"No Pacemaker resource holds {name}, so there is nothing to restart "
            "as a whole: stop it and start it again on its machine.",
            409,
        )
    action = Action.RESOURCE_RESTART
    record = _runs(request).launch_action(action, resource.id, user.username)
    return ActionResponse(
        run_id=record.id,
        state=record.state.value,
        container=name,
        action=action.value,
        target=resource.id,
        managed="pacemaker",
    )


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
