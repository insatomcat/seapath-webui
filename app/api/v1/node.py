# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The node view. Read only, in the strong sense: nothing here changes a host.

Every endpoint is open to the viewer role, which is the whole point of having
one. Configuration lives elsewhere, and from M1 it is reached by editing the
inventory and running a playbook.

What is here is what this machine *is*, which is what the inventory form needs.
What it is *doing* comes from prometheus-node-exporter, which every node runs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.auth import Role
from app.core.security import require_role
from app.hosts.models import CpuReading, DisksReading, NetworkReading
from app.services.node import NodeService, NodeSummary
from app.services.update import ServiceUpdate, UpdateService

router = APIRouter(
    prefix="/node",
    tags=["node"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _service(request: Request) -> NodeService:
    return request.app.state.node_service


def _update(request: Request) -> UpdateService:
    return request.app.state.update_service


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


@router.get("/cpu", response_model=CpuReading)
def cpu(request: Request) -> CpuReading:
    return _service(request).cpu()


@router.get("/network", response_model=NetworkReading)
def network(request: Request) -> NetworkReading:
    return _service(request).network()


@router.get("/disks", response_model=DisksReading)
def disks(request: Request) -> DisksReading:
    return _service(request).disks()
