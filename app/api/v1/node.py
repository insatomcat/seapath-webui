# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The node view. Read only, in the strong sense: nothing here changes a host.

Every reading is open to the viewer role, which is the whole point of having
one. Configuration lives elsewhere, and from M1 it is reached by editing the
inventory and running a playbook.

One endpoint here writes, and what it writes is the inventory: pinning the
version of this service that the machines should run. It is an administrator's
act, it produces a commit like every other change to the desired state, and it
changes no machine on its own. Applying it is a run of
`seapath_setup_deploy_seapath_webui`, launched and confirmed like any other.

What is here is what this machine *is*, which is what the inventory form needs.
What it is *doing* comes from prometheus-node-exporter, which every node runs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.hosts.models import CpuReading, DisksReading, NetworkReading
from app.inventory.repository import RepositoryError, StaleWrite
from app.inventory.service import RefusedWrite
from app.services.node import NodeService, NodeSummary
from app.services.update import (
    AvailableRelease,
    Pinned,
    PinRefused,
    ServiceUpdate,
    UpdateService,
)

# Pinning the version of this service is an administrator's act, the way
# every other write to the desired state is.
admin = Depends(require_role(Role.ADMIN))

router = APIRouter(
    prefix="/node",
    tags=["node"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _service(request: Request) -> NodeService:
    return request.app.state.node_service


def _update(request: Request) -> UpdateService:
    return request.app.state.update_service


class PinRequest(BaseModel):
    """The version to write into the inventory, chosen by an administrator."""

    version: str = Field(
        description="A tag of the image repository the machines already name"
    )


@router.get("", response_model=NodeSummary)
def node(request: Request) -> NodeSummary:
    return _service(request).summary()


@router.get("/update", response_model=ServiceUpdate)
def update(request: Request) -> ServiceUpdate:
    """Which version of this service the inventory asks for, and which answers.

    Read only, like everything here. Replacing this service is an Ansible run
    like any other: the reference is a variable, and applying it is what makes
    it real. See D23.
    """
    return _update(request).state()


@router.get("/update/latest", response_model=AvailableRelease)
def latest(request: Request) -> AvailableRelease:
    """The highest version the registry holds for the image this node names.

    One HTTPS GET to the registry the inventory already points the machines at,
    and nothing else. A node with no route to a registry gets a sentence saying
    so, which is a supported answer here: a substation hypervisor is not
    expected to reach the internet.
    """
    return _update(request).available()


@router.post("/update", response_model=Pinned)
def pin(
    request: Request,
    payload: PinRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> Pinned:
    """Write this version as the image of every machine that names one.

    A commit in the inventory, carrying the authenticated user like every
    other. It changes no machine: the answer names the playbook that does, and
    an operator confirms that run with the disruption spelled out, the way they
    confirm any other convergence of a live hypervisor.
    """
    try:
        return _update(request).pin(payload.version, user.username, if_match)
    except PinRefused as error:
        raise ApiError(error.code, str(error), error.status) from error
    except RefusedWrite as error:
        raise ApiError(
            "refused_write",
            str(error),
            409,
            {"divergences": [d.model_dump() for d in error.divergences]},
        ) from error
    except StaleWrite as error:
        # Refusing beats merging, here as everywhere else a write lands.
        raise ApiError("stale_write", str(error), 409) from error
    except RepositoryError as error:
        raise ApiError("repository_error", str(error), 409) from error


@router.get("/cpu", response_model=CpuReading)
def cpu(request: Request) -> CpuReading:
    return _service(request).cpu()


@router.get("/network", response_model=NetworkReading)
def network(request: Request) -> NetworkReading:
    return _service(request).network()


@router.get("/disks", response_model=DisksReading)
def disks(request: Request) -> DisksReading:
    return _service(request).disks()
