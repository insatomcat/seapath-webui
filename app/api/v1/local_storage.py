# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The local volumes of a machine: what its disks have room for, and a new one.

Reading is a viewer's, like every reading. Creating a volume partitions a disk,
which is an administrator's: a run of `configure_local_storage` on that machine
alone, given the one volume, and nothing written to the inventory (D58). The
entries the inventory may still declare can be removed, which is a commit.
Nothing in this module reaches a disk.
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


class CreateResponse(BaseModel):
    """The run that creates the volume, watched like any other."""

    run_id: str
    state: str


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


@router.post("/{host}/volumes", response_model=CreateResponse)
def create(
    request: Request,
    host: str,
    payload: LocalVolume,
    user: User = admin,
) -> CreateResponse:
    """Create one volume on that machine: a run, and nothing written.

    Checked the way the role checks it, so a value the role would refuse is
    refused here rather than a run later. The run applies
    `configure_local_storage` to that machine alone, with this volume as a
    play variable; the role checks again on the machine, against the disk as
    it is then.
    """
    try:
        record = _service(request).create(host, payload, user.username)
    except InvalidVolume as error:
        raise ApiError("invalid_volume", str(error), 400) from error
    return CreateResponse(run_id=record.id, state=record.state.value)


@router.delete("/{host}/volumes/{name}", response_model=DeclareResponse)
def forget(
    request: Request,
    host: str,
    name: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> DeclareResponse:
    """Remove one volume from the machine's `configure_local_storage_volumes`.

    The inventory only. The role never removes a partition, a file system or
    a mount, so what an earlier run created stays as it is; later runs stop
    applying the entry, which is how one the role refuses stops failing every
    run after it.
    """
    try:
        commit = _service(request).forget(host, name, user.username, if_match)
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
