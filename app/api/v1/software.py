# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The software updates of the machines: what is pending, and the update.

Reading is a viewer's. Checking is a run that installs nothing, so it is the
operator's, the way reading the backup server is. Updating installs packages
and reboots machines, one at a time, so it is an administrator's. Nothing in
this module reaches a machine except through a run.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.core.auth import Role, User
from app.core.security import require_role
from app.services.software import SoftwareService, SoftwareView

router = APIRouter(
    prefix="/software",
    tags=["software"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

admin = Depends(require_role(Role.ADMIN))
operator = Depends(require_role(Role.OPERATOR))


def _service(request: Request) -> SoftwareService:
    return request.app.state.software_service


class UpdateRequest(BaseModel):
    hosts: list[str] = Field(
        description="The machines to update, one at a time, in inventory order"
    )


class RunResponse(BaseModel):
    """The run that was launched, watched like any other."""

    run_id: str
    state: str


@router.get("", response_model=SoftwareView)
def view(request: Request) -> SoftwareView:
    """What the last check found on each machine, and the last update of each.

    Read from the run history and never from the machines: what a machine
    would install is known only as of the last check, which the answer names.
    """
    return _service(request).view()


@router.post("/check", status_code=202, response_model=RunResponse)
def check(request: Request, user: User = operator) -> RunResponse:
    """Refresh the package lists of every machine and simulate the upgrade."""
    record = _service(request).check(user.username)
    return RunResponse(run_id=record.id, state=record.state.value)


@router.post("/update", status_code=202, response_model=RunResponse)
def update(request: Request, payload: UpdateRequest, user: User = admin) -> RunResponse:
    """Run `seapath_update_debian` on the machines named, one at a time.

    Each machine is upgraded and rebooted; a cluster member is put in standby
    first. The machine serving this API is refused: it is updated from another
    member, since the run has to outlive its reboot.
    """
    record = _service(request).update(payload.hosts, user.username)
    return RunResponse(run_id=record.id, state=record.state.value)
