# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The Pacemaker cluster, as its coordinator reports it.

Reading is what this page is, and everything that **configures** a cluster is
elsewhere by design: joining a machine is an inventory edit and
`cluster_setup_ha`, and removing one is `cluster_remove_machine`.

Refreshing a resource is the exception, and it is the same exception D30 makes
for starting a guest. It changes no desired state: it deletes an operation
history Pacemaker keeps, so a failure that has been dealt with stops holding a
resource down. It reaches the machine the way every other act here does, as a
generated one task run over the SSH path a convergence uses, under the same
lock and in the same history. No `crm` runs inside this container, which is the
line that matters.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

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

# Refreshing is the runtime plane, so it is an operator's act, exactly as
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
