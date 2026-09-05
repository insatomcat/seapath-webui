# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The Pacemaker cluster, as its coordinator reports it.

One endpoint, and it is a GET. Everything that changes a cluster is elsewhere
by design: joining a machine is an inventory edit and `cluster_setup_ha`,
removing one is `cluster_remove_machine`, and moving a resource is Pacemaker's
own decision. A POST here would be `crm` running inside this container, which
is the boundary the whole service is built around.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.cluster.ha import PacemakerCluster
from app.core.auth import Role
from app.core.security import require_role
from app.services.cluster import ClusterService

router = APIRouter(
    prefix="/cluster",
    tags=["cluster"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _service(request: Request) -> ClusterService:
    return request.app.state.cluster_service


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
