# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The inventory endpoints: the heart of the API.

Reading is open to viewers. Writing is an administrator's act, because a commit
here is a change to the desired state of a substation hypervisor, and the next
apply makes it real.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.inventory.discovery import Discovery
from app.inventory.grub import hash_password
from app.inventory.model import Inventory
from app.inventory.repository import Commit, RepositoryError, StaleWrite
from app.inventory.service import (
    InventoryService,
    InventoryState,
    ReadOnlyInventory,
)
from app.inventory.validation import ValidationResult

router = APIRouter(prefix="/inventory", tags=["inventory"])

# Reading the desired state is open to viewers. Changing it is an
# administrator's act: a commit here is a change to the desired state of a
# substation hypervisor, and the next apply makes it real.
viewer = Depends(require_role(Role.VIEWER))
admin = Depends(require_role(Role.ADMIN))


def _service(request: Request) -> InventoryService:
    return request.app.state.inventory_service


class CommitResponse(BaseModel):
    commit: str | None
    message: str | None = None
    validation: ValidationResult


class CandidateRequest(BaseModel):
    inventory: Inventory


class HostPatch(BaseModel):
    """Changed fields only, which is what a form submits.

    A GRUB password arrives here in clear exactly once, is hashed immediately,
    and only the hash is ever written. The inventory goes into git, and a
    password in git is a password in the audit trail forever.
    """

    changes: dict[str, Any]
    grub_password_plain: str | None = None


def _read_only(error: ReadOnlyInventory) -> ApiError:
    """A refusal that says exactly what it protected.

    409 rather than 403: the request was legitimate and the state of the
    resource is what refuses it.
    """
    return ApiError(
        "read_only_inventory",
        str(error),
        409,
        {"divergences": [d.model_dump() for d in error.divergences]},
    )


def _commit_response(
    commit: Commit | None, validation: ValidationResult
) -> CommitResponse:
    return CommitResponse(
        commit=commit.hash if commit else None,
        message=commit.message if commit else None,
        validation=validation,
    )


@router.get("")
def inventory(request: Request, user: User = viewer) -> InventoryState:
    return _service(request).state()


@router.get("/raw")
def raw(request: Request, user: User = viewer) -> Response:
    return Response(content=_service(request).raw(), media_type="text/yaml")


@router.get("/discovery")
def discovery(request: Request, user: User = viewer) -> Discovery:
    return _service(request).discovery()


@router.get("/history")
def history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    user: User = viewer,
) -> list[Commit]:
    return _service(request).history(limit)


@router.get("/diff")
def diff(
    request: Request,
    from_ref: str | None = Query(default=None, alias="from"),
    to_ref: str | None = Query(default=None, alias="to"),
    user: User = viewer,
) -> Response:
    try:
        return Response(
            content=_service(request).diff(from_ref, to_ref),
            media_type="text/x-diff",
        )
    except RepositoryError as error:
        raise ApiError("unknown_commit", str(error), 404) from error


@router.get("/export")
def export(request: Request, user: User = viewer) -> Response:
    return Response(
        content=_service(request).export(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="seapath-inventory.tar.gz"'
        },
    )


@router.post("/validate")
def validate(
    request: Request, payload: CandidateRequest, user: User = viewer
) -> ValidationResult:
    return _service(request).validate(payload.inventory)


@router.post("/preview")
def preview(
    request: Request, payload: CandidateRequest, user: User = viewer
) -> Response:
    """What committing this candidate would change, without committing it."""
    try:
        diff = _service(request).preview(payload.inventory)
    except ReadOnlyInventory as error:
        raise _read_only(error) from error
    return Response(
        content=diff,
        media_type="text/x-diff",
    )


@router.put("")
def replace(
    request: Request,
    payload: CandidateRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> CommitResponse:
    return _save(request, payload.inventory, user.username, if_match)


@router.patch("/hosts/{name}")
def patch_host(
    request: Request,
    name: str,
    payload: HostPatch,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> CommitResponse:
    """Change one machine's variables. The common path for the forms."""
    service = _service(request)
    state = service.state()
    if state.inventory is None:
        raise ApiError(
            "no_inventory",
            "There is no inventory yet on this node.",
            409,
        )
    if name not in state.inventory.hosts:
        raise ApiError("unknown_host", f"{name} is not in the inventory.", 404)

    changes = dict(payload.changes)
    if payload.grub_password_plain:
        # Hashed here and never stored, logged or echoed back.
        changes["grub_password"] = hash_password(payload.grub_password_plain)

    candidate = state.inventory.model_copy(deep=True)
    node = candidate.hosts[name]
    unknown = sorted(set(changes) - set(type(node).model_fields))
    if unknown:
        raise ApiError(
            "unknown_field",
            f"{', '.join(unknown)} is not a field of a machine's configuration.",
            400,
        )
    candidate.hosts[name] = node.model_copy(update=changes)

    return _save(request, candidate, user.username, if_match)


@router.post("/revert/{commit}")
def revert(request: Request, commit: str, user: User = admin) -> CommitResponse:
    """Create a revert commit. It is not applied: that is a separate act."""
    try:
        reverted = _service(request).revert(commit, user.username)
    except ReadOnlyInventory as error:
        raise _read_only(error) from error
    except RepositoryError as error:
        raise ApiError("revert_failed", str(error), 409) from error
    return _commit_response(reverted, ValidationResult())


def _save(
    request: Request,
    candidate: Inventory,
    author: str,
    if_match: str | None,
) -> CommitResponse:
    service = _service(request)
    try:
        commit, validation = service.save(candidate, author, expected_head=if_match)
    except ReadOnlyInventory as error:
        raise _read_only(error) from error
    except StaleWrite as error:
        # Refusing beats merging: a silently merged desired state is one nobody
        # reviewed.
        raise ApiError("stale_write", str(error), 409) from error

    if commit is None and not validation.valid:
        raise ApiError(
            "invalid_inventory",
            validation.errors()[0].message,
            422,
            {"findings": [finding.model_dump() for finding in validation.findings]},
        )
    return _commit_response(commit, validation)
