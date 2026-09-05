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

from fastapi import APIRouter, Depends, Request

from app.core.auth import Role
from app.core.security import require_role
from app.services.vms import GuestsView, VmService

router = APIRouter(
    prefix="/vms",
    tags=["vms"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _service(request: Request) -> VmService:
    return request.app.state.vm_service


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
