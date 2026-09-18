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
    BackupCatalogue,
    BackupService,
    BackupView,
    Estimate,
    InvalidBackupSetting,
    StagingReading,
)
from app.services.backup_trust import (
    BackupConnection,
    BackupTrustService,
    ServerHostKey,
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


def _trust(request: Request) -> BackupTrustService:
    return request.app.state.backup_trust_service


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


class HostKeysRequest(BaseModel):
    host_keys: list[str] = Field(
        description="The server's host keys, as known_hosts lines, confirmed"
    )


class InstallKeysRequest(BaseModel):
    password: str = Field(
        description="The password of the account the backups are pushed to. "
        "Used for one connection and never stored or logged."
    )


class RunResponse(BaseModel):
    """The run that carries out the act, watched like any other."""

    run_id: str
    state: str
    action: str
    guest: str = ""


@router.get("", response_model=BackupView, dependencies=[reads.reading])
def backup(request: Request) -> BackupView:
    """Where the backups go, what a full one would weigh, and what is there.

    `settings` is the seven variables as the cluster members resolve them, and
    what this node's own conf file holds beside each. Read off the disk, so it
    answers at once: the estimate and the catalogue are their own endpoints,
    because each costs a wait an operator has to ask for.
    """
    return _service(request).view()


@router.get("/estimate", response_model=Estimate)
def estimate(request: Request) -> Estimate:
    """What a full backup would weigh, per guest, from `rbd du`.

    Its own endpoint because it is its own cost. With no fast-diff map
    `rbd du` walks every object of an image, so it is never on the path of
    the page being drawn: `GET /backup` answers at once and this is asked
    when an operator presses the button. It runs on the member the backups
    run on, and only on the images the filters select.

    A Ceph that does not answer in time is reported in `error` rather than
    failing the request, because the reading says nothing about whether a
    backup can be taken.
    """
    return _service(request).estimate()


@router.get("/catalogue", response_model=BackupCatalogue)
def catalogue(request: Request) -> BackupCatalogue:
    """What the backup server holds, asked now over one SSH connection.

    Its own endpoint because it is its own cost: two hops and a directory
    listing, a second or two on a server that answers and ten on one that has
    gone away. `GET /backup` answers off the disk and never waits for this.

    A read, so it takes no run and no lock. It used to be a run, which held the
    cluster's lock while an operator browsed and could not show an answer until
    the run had ended. See D54.
    """
    return _service(request).catalogue()


@router.get("/staging", response_model=StagingReading)
def staging(request: Request) -> StagingReading:
    """Whether the two staging directories exist, and the room they have.

    Asked of the member the backups run on, over one SSH connection, because
    that is where `backup_full.sh` writes a qcow2 of every guest before it
    sends anything. A directory that is not there yet comes with the file
    system it would be created on.
    """
    return _service(request).staging()


@router.get("/connection", response_model=BackupConnection)
def connection(request: Request) -> BackupConnection:
    """Whether each cluster member can push to the backup server.

    Every member asked at once, over one SSH connection each: the public half
    of its backup key, and a connection to the server made the way a backup
    makes it, with `BatchMode`. `host_keys` is what the inventory holds of the
    server's own keys, confirmed by an operator.
    """
    return _trust(request).read()


@router.post("/connection/scan", response_model=list[ServerHostKey])
def scan(request: Request, user: User = admin) -> list[ServerHostKey]:
    """The backup server's host keys, read over the network, to be compared.

    Nothing is written. What comes back is shown with its fingerprint so an
    operator can compare it against the server before confirming it.
    """
    return _trust(request).scan()


@router.put("/connection/key", response_model=SettingsResponse)
def prepare(
    request: Request,
    payload: HostKeysRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> SettingsResponse:
    """Commit the confirmed host keys, a dedicated key and a shell using it.

    One commit on `cluster_machines`. A run of `seapath_setup_backup_restore`
    then generates the key on every member and trusts the host key there.
    """
    try:
        commit = _trust(request).prepare(payload.host_keys, user.username, if_match)
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


@router.post("/connection/install", response_model=BackupConnection)
def install(
    request: Request, payload: InstallKeysRequest, user: User = admin
) -> BackupConnection:
    """Append every member's backup key to the server's `authorized_keys`.

    `ssh-copy-id`, for all the members at once: one connection with the
    password typed here, which is used for that connection alone. The server's
    host key must have been confirmed first, and the connection refuses a
    server that answers with another one before the password is sent. Every
    member is then asked again. See D57.
    """
    return _trust(request).install(payload.password, user.username)


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
