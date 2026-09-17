# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The backups: where they go, what they would weigh, what is there, and four acts.

Every act here is a run of the scripts the `backup_restore` role installed, and
nothing in this module reaches a machine except through one. Where the backups
go is written in the inventory, which is a commit, and the estimate is a
reading of Ceph over the client D31 established.

The roles follow the split the rest of this service uses. Changing where a
site's backups go is desired state, so it is an administrator's. Taking a
backup and reading the server are the operator's, the way starting a guest is.
Restoring is an administrator's, because it destroys a running guest and
replaces it with what a directory on another machine happens to hold, which is
the most destructive single act this service offers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from app.api.v1 import reads
from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.inventory.service import RefusedWrite
from app.runs.backup import BackupAction
from app.services.backup import (
    BackupService,
    BackupView,
    Estimate,
    InvalidBackupSetting,
)

router = APIRouter(
    prefix="/backup",
    tags=["backup"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

admin = Depends(require_role(Role.ADMIN))
operator = Depends(require_role(Role.OPERATOR))


def _service(request: Request) -> BackupService:
    return request.app.state.backup_service


class BackupSettings(BaseModel):
    """Where a site's backups go, in the terms the scripts take.

    The keys are the conf file's own, with or without the `backup_restore_`
    prefix the inventory writes them under, so a caller can send either.
    """

    remote_serv: str = Field(default="", description="`[user@]host`")
    remote_dir: str = Field(default="", description="Ends with a slash")
    local_dir: str = Field(
        default="", description="Staging directory, ends with a slash"
    )
    local_tmp_dir: str = Field(
        default="", description="Restore staging directory, ends with a slash"
    )
    remote_shell: str = Field(default="ssh", description="`ssh`, with its options")
    include_vm: str = Field(default="", description="Extended regular expression")
    exclude_vm: str = Field(default="", description="Extended regular expression")


class SettingsResponse(BaseModel):
    commit: str | None = None
    message: str = ""


class RestoreRequest(BaseModel):
    guest: str
    full_date: str = Field(description="The full backup directory, twelve digits")
    date: str = Field(description="The date inside it to replay up to")


class RunResponse(BaseModel):
    """The run that carries out the act, watched like any other."""

    run_id: str
    state: str
    action: str
    guest: str = ""


@router.get("", response_model=BackupView, dependencies=[reads.reading])
def backup(request: Request) -> BackupView:
    """Where the backups go, what a full one would weigh, and what is there.

    `settings` is the seven variables as the cluster members resolve them,
    `estimate` is `rbd du` summed per guest with the two filters applied, and
    `catalogue` is what the last listing run found on the backup server. None
    of the three reaches a machine.
    """
    return _service(request).view()


@router.get("/estimate", response_model=Estimate)
def estimate(request: Request) -> Estimate:
    """What a full backup would weigh, per guest, from `rbd du`.

    Its own endpoint because it is its own cost. `rbd du` adds up the objects
    of every image in the pool, which on a real cluster is minutes, so it is
    never on the path of the page being drawn: `GET /backup` answers at once
    and this is asked when an operator presses the button.

    A Ceph that does not answer in time is reported in `error` rather than
    failing the request, because the reading says nothing about whether a
    backup can be taken.
    """
    return _service(request).estimate()


@router.put("/settings", response_model=SettingsResponse)
def settings(
    request: Request,
    payload: BackupSettings,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> SettingsResponse:
    """Write the seven variables on `cluster_machines`, as one commit.

    The two staging directories are checked hardest, and the refusal says why:
    `backup_full.sh` empties one of them with `rm -rf "${local_dir}"*`, so a
    value without its trailing slash removes every sibling whose name starts
    the same way.
    """
    try:
        commit = _service(request).save(payload.model_dump(), user.username, if_match)
    except InvalidBackupSetting as error:
        raise ApiError("invalid_backup_setting", str(error), 400) from error
    except RefusedWrite as error:
        raise ApiError(
            "refused_write",
            str(error),
            409,
            {"divergences": [item.model_dump() for item in error.divergences]},
        ) from error
    return SettingsResponse(
        commit=commit.hash if commit else None,
        message=commit.message if commit else "Nothing changed.",
    )


@router.post("/full", status_code=202)
def full(request: Request, user: User = operator) -> RunResponse:
    """Take a full backup of every guest the filters select.

    It purges the snapshots of every image it exports before taking the base
    snapshot this backup and the increments after it are made against, so the
    increments of the previous full backup stop being applicable. The
    confirmation on the page says so.
    """
    return _launch(request, BackupAction.FULL, user)


@router.post("/incremental", status_code=202)
def incremental(request: Request, user: User = operator) -> RunResponse:
    """Export what changed since each image's latest snapshot."""
    return _launch(request, BackupAction.INCREMENTAL, user)


@router.post("/listing", status_code=202)
def listing(request: Request, user: User = operator) -> RunResponse:
    """Ask the backup server what it holds, and bring the listing back.

    The only way this service can see the backup server: the SSH trust that
    reaches it belongs to the cluster members and is the one the backups are
    pushed with. The run reads and writes nothing, and what it brought back is
    in `catalogue` on the next `GET /backup`.
    """
    return _launch(request, BackupAction.LIST, user)


@router.post("/restore", status_code=202)
def restore(
    request: Request, payload: RestoreRequest, user: User = admin
) -> RunResponse:
    """Recreate one guest from a backup, over whatever is there now.

    The guest and both dates are checked against the listing this service last
    read, so a restore names something the server actually holds rather than
    failing on the machine after the confirmation.
    """
    return _launch(
        request,
        BackupAction.RESTORE,
        user,
        guest=payload.guest,
        full_date=payload.full_date,
        incremental_date=payload.date,
    )


def _launch(
    request: Request,
    action: BackupAction,
    user: User,
    guest: str = "",
    full_date: str = "",
    incremental_date: str = "",
) -> RunResponse:
    record = _service(request).launch(
        action,
        user.username,
        guest=guest,
        full_date=full_date,
        incremental_date=incremental_date,
    )
    return RunResponse(
        run_id=record.id,
        state=record.state.value,
        action=action.value,
        guest=guest,
    )
