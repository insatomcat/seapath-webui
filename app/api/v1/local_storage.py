# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The local volumes of a machine: what its disks have room for, and a new one.

Reading is a viewer's, like every reading. Declaring a volume is a commit to
the machine's `configure_local_storage_volumes`, which is desired state and so
an administrator's. What partitions the disk is a run of
`seapath_setup_local_storage` narrowed to that machine, launched through
`POST /runs` like any other: nothing in this module reaches a disk.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.inventory.service import RefusedWrite
from app.services.local_storage import (
    InvalidVolume,
    LocalStorage,
    LocalStorageService,
    LocalVolume,
)

router = APIRouter(
    prefix="/storage/local",
    tags=["storage"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

admin = Depends(require_role(Role.ADMIN))


def _service(request: Request) -> LocalStorageService:
    return request.app.state.local_storage_service


class DeclareResponse(BaseModel):
    commit: str | None = None
    message: str = ""


@router.get("", response_model=LocalStorage)
def local_storage(
    request: Request, host: str | None = Query(default=None)
) -> LocalStorage:
    """The disks, the free space after their last partition, and the groups.

    Asked of the machine now, over one SSH connection, as root: `parted` and
    `vgs` need it. `declared` is what the inventory already holds for it, each
    with whether the machine has it mounted.
    """
    try:
        return _service(request).read(host)
    except InvalidVolume as error:
        raise ApiError("unknown_host", str(error), 404) from error


@router.post("/{host}/volumes", response_model=DeclareResponse)
def declare(
    request: Request,
    host: str,
    payload: LocalVolume,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> DeclareResponse:
    """Append one volume to the machine's `configure_local_storage_volumes`.

    Checked the way the role checks it, so a value the role would refuse is
    refused here rather than a run later. The role checks again on the machine,
    against the disk as it is then.
    """
    try:
        commit = _service(request).declare(host, payload, user.username, if_match)
    except InvalidVolume as error:
        raise ApiError("invalid_volume", str(error), 400) from error
    except RefusedWrite as error:
        raise ApiError(
            "refused_write",
            str(error),
            409,
            {"divergences": [item.model_dump() for item in error.divergences]},
        ) from error
    return DeclareResponse(
        commit=commit.hash if commit else None,
        message=commit.message if commit else "Nothing changed.",
    )
