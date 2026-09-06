# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The Pacemaker cluster, as its coordinator reports it.

Reading is most of what this page is, and everything that **configures** a
cluster is elsewhere by design: joining a machine is an inventory edit and
`cluster_setup_ha`, and removing one is `cluster_remove_machine`.

Three acts sit beside the reading, and they are the exception D30 makes for
starting a guest, extended once by D34. None of them changes a desired state.
Refreshing deletes an operation history Pacemaker keeps, so a failure that has
been dealt with stops holding a resource down. Moving writes the `cli-prefer`
location constraint, which is the object `preferred_host` already produces:
`vm_manager` implements that field by running this very command, so a
deliberate move adds no kind of rule the cluster did not carry. Standby is the
node scoped form of the same thing, and it is what an operator does before
rebooting a hypervisor.

Each of them reaches the machine the way every other act here does, as a
generated one task run over the SSH path a convergence uses, under the same
lock and in the same history. No `crm` runs inside this container, which is the
line that matters.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.cluster import ha
from app.cluster.ha import PacemakerCluster
from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.runs.actions import Action
from app.runs.service import RunService
from app.services.cluster import ClusterService

router = APIRouter(
    prefix="/cluster",
    tags=["cluster"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

# All of these are the runtime plane, so they are an operator's act, exactly as
# starting and stopping a guest are.
operator = Depends(require_role(Role.OPERATOR))


def _service(request: Request) -> ClusterService:
    return request.app.state.cluster_service


def _runs(request: Request) -> RunService:
    return request.app.state.run_service


@router.get("", response_model=PacemakerCluster)
def cluster(request: Request) -> PacemakerCluster:
    """Members, resources, quorum and failures, read from each node's exporter.

    Every machine the inventory declares is asked, in parallel, and the
    coordinator's answer is the one reported: each member describes the whole
    cluster, and they only disagree when something has gone wrong, which is
    when this page is open. `source` names the node that answered and `from_dc`
    says whether it was the coordinator.

    A machine that could not be reached appears in `reach` with the reason,
    and the members that did answer are still reported.
    """
    return _service(request).pacemaker()


class RefreshResponse(BaseModel):
    """The run that carries out the refresh, watched like any other."""

    run_id: str
    state: str
    resource: str = ""
    """The resource refreshed, empty when it was the whole cluster."""


def _launch(request: Request, action: Action, name: str, user: User) -> RefreshResponse:
    record = _runs(request).launch_action(action, name, user.username)
    return RefreshResponse(run_id=record.id, state=record.state.value, resource=name)


def _reporting(request: Request) -> set[str]:
    """The resources the cluster answered for, or a refusal saying it did not.

    Refreshing a cluster nothing answered for would launch a run against a
    machine that may not be in a cluster at all, and the failure would arrive
    three minutes later as an Ansible error rather than here as a sentence.
    """
    known = _service(request).resource_names()
    if not known:
        raise ApiError(
            "no_cluster",
            (
                "No cluster answered, so there is nothing to refresh. The "
                "Cluster page says which machines could not be reached."
            ),
            409,
        )
    return known


@router.post("/resources/refresh", status_code=202)
def refresh_all(request: Request, user: User = operator) -> RefreshResponse:
    """Clear every resource's operation history, as a run.

    `crm resource refresh` with no resource named, which is what it means: the
    whole cluster at once. It costs a probe per resource per node, so the page
    offers it beside the per resource button rather than instead of it, and
    says which of the two is the smaller act.
    """
    _reporting(request)
    return _launch(request, Action.REFRESH_ALL, "", user)


@router.post("/resources/{name}/refresh", status_code=202)
def refresh(request: Request, name: str, user: User = operator) -> RefreshResponse:
    """Clear one resource's operation history, as a run.

    `crm resource refresh <resource>` on a cluster member, which is what an
    operator would type on the machine and what `vm_manager` reaches Pacemaker
    with. It deletes the failures Pacemaker recorded and asks it to probe the
    resource again, so a resource held down by a fail count that reached its
    migration threshold can be placed once the cause is fixed.

    The name is checked against the resources the cluster reports, so this
    cannot be pointed at anything Pacemaker does not know about.
    """
    if name not in _reporting(request):
        raise ApiError(
            "unknown_resource",
            f"{name} is not a resource this cluster reports.",
            404,
        )
    return _launch(request, Action.REFRESH, name, user)


# Placement, which is D34. A move and its inverse, and the node scoped pair
# that empties a machine and fills it again.


class MoveRequest(BaseModel):
    """Where to send a resource."""

    node: str = Field(description="The cluster member Pacemaker is to run it on")


class PlacementResponse(BaseModel):
    """The run that changes a resource's placement, watched like any other."""

    run_id: str
    state: str
    resource: str
    node: str = ""
    """Where a move sends it. Empty for a return."""
    restored: str = ""
    """The placement the inventory declares, which a return writes back.

    Empty when the inventory declares none, and the resource is then left
    where Pacemaker's own rules put it.
    """


class NodeResponse(BaseModel):
    """The run that takes a machine out of the cluster's reach, or puts it back."""

    run_id: str
    state: str
    node: str
    standby: bool
    """What the run asks for, so the caller need not read the action back."""


def _reading(request: Request) -> PacemakerCluster:
    """One reading of the cluster, or a refusal saying nothing answered.

    Taken once and passed around, rather than asked again per check: each
    reading is an HTTP GET to every machine of the inventory, and three of them
    to answer one request would triple the cost of a button.
    """
    cluster = _service(request).pacemaker()
    if not cluster.available:
        raise ApiError(
            "no_cluster",
            cluster.error
            or (
                "No cluster answered, so there is no placement to change. The "
                "Cluster page says which machines could not be reached."
            ),
            409,
        )
    return cluster


def _placeable(cluster: PacemakerCluster, name: str) -> None:
    """The resource exists, and moving it is a thing Pacemaker will accept.

    Two refusals, both of them arriving here as a sentence rather than three
    minutes later as a `crm` exit code.
    """
    instances = [item for item in cluster.resources if item.id == name]
    if not instances:
        raise ApiError(
            "unknown_resource",
            f"{name} is not a resource this cluster reports.",
            404,
        )
    clone = next((item.clone for item in instances if item.clone), "")
    if clone:
        raise ApiError(
            "resource_is_cloned",
            f"{name} is an instance of the clone {clone}. A clone runs on "
            "every member at once and Pacemaker places its instances itself, "
            "so there is no one node to send this one to.",
            409,
        )
    pinned = ha.pin(cluster, name)
    if pinned is not None:
        raise ApiError(
            "resource_pinned",
            f"{name} is pinned to {pinned.node} by {pinned.id}, which is what "
            "`pinned_host` writes: the guest runs there or nowhere. A move "
            "would leave two mandatory rules pulling in opposite directions, "
            "and a return would not remove this one. Changing where a pinned "
            "guest lives is its inventory entry and a redeployment.",
            409,
        )


def _target(cluster: PacemakerCluster, node: str) -> None:
    """The node named is one this cluster can actually place a resource on."""
    found = next((item for item in cluster.nodes if item.name == node), None)
    if found is None or found.type == "ping":
        placeable = [item.name for item in cluster.nodes if item.type != "ping"]
        raise ApiError(
            "unknown_node",
            f"{node} is not a member this cluster places resources on. It "
            f"reports {', '.join(placeable) or 'no such member'}.",
            404,
        )
    if "standby" in found.flags:
        raise ApiError(
            "node_in_standby",
            f"{node} is in standby, so Pacemaker places nothing there and the "
            "resource would stay where it is while carrying a constraint "
            "saying otherwise. Bring the node online first.",
            409,
        )


def _member(cluster: PacemakerCluster, name: str, standby: bool) -> None:
    """The machine named is a member, and the act would change something."""
    found = next((item for item in cluster.nodes if item.name == name), None)
    if found is None or found.type != "member":
        members = [item.name for item in cluster.nodes if item.type == "member"]
        raise ApiError(
            "unknown_node",
            f"{name} is not a member of this cluster. It reports "
            f"{', '.join(members) or 'no member at all'}.",
            404,
        )
    # A `crm node standby` on a node already in standby writes nothing, and a
    # run that does nothing is noise in a history somebody audits.
    if ("standby" in found.flags) == standby:
        raise ApiError(
            "already_there",
            f"{name} is already {'in standby' if standby else 'online'}.",
            409,
        )


def _elsewhere(cluster: PacemakerCluster, name: str, node: str) -> None:
    """A move names a node the resource is not already running on.

    Pacemaker refuses to move a resource that is already active where it is
    being sent and exits non-zero, so without this the run fails on the machine
    with nothing said here.

    Refusing rather than working around it, because the request underneath is a
    different one. Asking to keep a guest where it already is is a statement
    about where it *belongs*, and where a guest belongs is `preferred_host` on
    its inventory entry. Making the CIB and the inventory agree by writing the
    CIB again would leave the inventory still not describing the cluster, which
    is the thing the VMs page marks the row for.
    """
    current = next(
        (item.node for item in cluster.resources if item.id == name and item.node),
        "",
    )
    if current and current == node:
        raise ApiError(
            "already_there",
            f"{name} is already running on {node}, and Pacemaker refuses to "
            "move a resource to the node it is already active on. Keeping it "
            f"there is a placement rather than a move: `preferred_host: {node}` "
            "on its inventory entry says so, and the VMs page then reports the "
            "guest as held where the inventory declares.",
            409,
        )


@router.post("/resources/{name}/move", status_code=202)
def move(
    request: Request, name: str, payload: MoveRequest, user: User = operator
) -> PlacementResponse:
    """Ask Pacemaker to run a resource on a named node, as a run.

    `crm resource move <resource> <node>` on a cluster member, which writes the
    `cli-prefer-<resource>` location constraint. That is the same object
    `preferred_host` produces, written by the same command: `vm_manager`
    implements the field by calling it, so this adds no kind of rule the
    cluster did not already carry and the Cluster page's constraint table shows
    it beside the others.

    It is an override and it says so. The constraint stays until the placement
    is returned or the resource is rebuilt from its metadata, and while it is
    there it wins over whatever the inventory declares. What it costs the guest
    is the guest's own `live_migration`: with it Pacemaker migrates the domain,
    without it the guest is stopped where it runs and started on the other
    node.

    Both names are checked against what the cluster reported, so neither
    reaches a command argument from a URL alone, and the node has to be one the
    resource is not already running on: Pacemaker refuses that move and holding
    a guest where it is is `preferred_host` in the inventory. See
    [D34](decisions.md#d34).
    """
    cluster = _reading(request)
    _placeable(cluster, name)
    _target(cluster, payload.node)
    _elsewhere(cluster, name, payload.node)
    record = _runs(request).launch_action(
        Action.MOVE, name, user.username, node=payload.node
    )
    return PlacementResponse(
        run_id=record.id,
        state=record.state.value,
        resource=name,
        node=payload.node,
    )


@router.post("/resources/{name}/clear", status_code=202)
def clear(request: Request, name: str, user: User = operator) -> PlacementResponse:
    """Give a resource's placement back to the cluster, as a run.

    `crm resource clear <resource>`, which removes the `cli-prefer` and
    `cli-ban` constraints crmsh writes, followed by the one command that puts
    back what the inventory declares. The second half is the part worth having:
    a bare clear also removes the constraint `preferred_host` had put there,
    and a guest would silently lose its declared placement until somebody
    rebuilt its Pacemaker resource.

    `restored` names what will be written back, empty for a resource the
    inventory says nothing about. Pacemaker may move the resource either way,
    at the cost the move had.
    """
    cluster = _reading(request)
    _placeable(cluster, name)
    declared = _service(request).declared_placement(name)
    # A declared placement the cluster cannot honour is not written back: the
    # inventory naming a machine this cluster does not report is a finding the
    # Inventory page owns, and a run that ends on a `crm` error helps nobody.
    if declared and not any(item.name == declared for item in cluster.nodes):
        declared = ""
    record = _runs(request).launch_action(
        Action.CLEAR, name, user.username, node=declared
    )
    return PlacementResponse(
        run_id=record.id,
        state=record.state.value,
        resource=name,
        restored=declared,
    )


@router.post("/nodes/{name}/standby", status_code=202)
def standby(request: Request, name: str, user: User = operator) -> NodeResponse:
    """Empty a machine, as a run.

    `crm node standby <node>`, which is what an operator types before rebooting
    a hypervisor. Pacemaker moves every resource off it and places nothing
    there until it is brought back online, so on a live cluster this moves
    every guest the node was running, each at its own cost: those whose image
    allows live migration migrate, the rest stop there and start elsewhere.

    It leaves no per resource constraint behind, which is what makes it the
    honest way to show a cluster placing its guests. Quorum is untouched: a
    node in standby is still a Corosync member and still votes.
    """
    cluster = _reading(request)
    _member(cluster, name, standby=True)
    record = _runs(request).launch_action(Action.STANDBY, name, user.username)
    return NodeResponse(
        run_id=record.id, state=record.state.value, node=name, standby=True
    )


@router.post("/nodes/{name}/online", status_code=202)
def online(request: Request, name: str, user: User = operator) -> NodeResponse:
    """End a machine's standby, as a run.

    `crm node online <node>`. What comes back to it is Pacemaker's decision:
    a resource with nothing holding it elsewhere may well stay where the
    standby sent it, which is the cluster behaving correctly rather than the
    command failing.
    """
    cluster = _reading(request)
    _member(cluster, name, standby=False)
    record = _runs(request).launch_action(Action.ONLINE, name, user.username)
    return NodeResponse(
        run_id=record.id, state=record.state.value, node=name, standby=False
    )
