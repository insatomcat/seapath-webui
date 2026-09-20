# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The cluster's journal, read now, over the SSH path a run takes.

One reading and the list of what it can be asked about. [D63](../../docs/decisions.md)
records why this exists at all, since [SPEC.md](../../SPEC.md) put the journal
with monitoring: an exporter answers what a machine is doing, and no exporter
answers what it printed while a migration was failing.

Every refusal here is a `409` naming the condition, which is what the API does
with a precondition rather than a bare `400`. The one that will be met most is
the rule that makes this affordable: a query carries a match on a field. The
numbers behind it are in `app/logs/journal.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import Role
from app.core.errors import ApiError, PermissionDenied
from app.core.security import require_role, require_user
from app.logs import journal
from app.logs.journal import Refused
from app.services.logs import Reading, Scope

router = APIRouter(
    prefix="/logs",
    tags=["logs"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

#: The window a caller that named none gets. Long enough to hold the act an
#: operator has just watched fail, short enough that the answer is a page.
DEFAULT_WINDOW = timedelta(minutes=15)


def _service(request: Request):
    return request.app.state.log_service


def _authorised(request: Request) -> None:
    """The configurable half of the role, checked where the console checks its own."""
    service = _service(request)
    user = require_user(request)
    if not user.role.can(service.required_role):
        raise PermissionDenied(
            f"Reading the journal requires the {service.required_role.value} role.",
            {"required": service.required_role.value, "role": user.role.value},
        )


def _refused(error: Refused) -> ApiError:
    return ApiError("precondition_failed", str(error), status_code=409)


class Machine(BaseModel):
    """A machine this reading can be asked about."""

    host: str
    address: str


class Sources(BaseModel):
    """What the page builds its controls from, in one answer.

    The scopes rather than a free field, and the machines the inventory
    declares rather than an address a browser gets to choose.
    """

    scopes: list[Scope] = Field(default_factory=list)
    machines: list[Machine] = Field(default_factory=list)
    default_window_minutes: int = int(DEFAULT_WINDOW.total_seconds() // 60)
    max_lines: int = journal.MAX_LINES
    default_lines: int = journal.DEFAULT_LINES


@router.get("/sources", response_model=Sources)
def sources(request: Request) -> Sources:
    """The scopes that can be read, and the machines that would be asked."""
    _authorised(request)
    service = _service(request)
    return Sources(
        scopes=list(service.scopes()),
        machines=[
            Machine(host=target.host, address=target.address)
            for target in service.machines()
        ],
    )


@router.get("", response_model=Reading)
def logs(
    request: Request,
    scope: Annotated[
        str | None,
        Query(
            description="A named scope from /logs/sources, which supplies the matches"
        ),
    ] = None,
    unit: Annotated[
        list[str] | None, Query(description="Systemd units, matched as OR")
    ] = None,
    identifier: Annotated[
        list[str] | None, Query(description="Syslog identifiers, matched as OR")
    ] = None,
    priority: Annotated[
        int | None,
        Query(
            ge=journal.MIN_PRIORITY,
            le=journal.MAX_PRIORITY,
            description="This priority and everything more urgent, 0 to 7",
        ),
    ] = None,
    grep: Annotated[
        str | None,
        Query(
            description="A pattern, applied to what the matches above already selected"
        ),
    ] = None,
    since: Annotated[
        datetime | None,
        Query(description="RFC 3339. The last fifteen minutes when absent"),
    ] = None,
    until: Annotated[datetime | None, Query(description="RFC 3339")] = None,
    lines: Annotated[
        int,
        Query(
            ge=1,
            le=journal.MAX_LINES,
            description="The most entries the merged answer holds",
        ),
    ] = journal.DEFAULT_LINES,
    host: Annotated[
        list[str] | None,
        Query(description="Machine names, every one of them when absent"),
    ] = None,
) -> Reading:
    """Every machine's journal over one window, merged, oldest first.

    The machines are asked in parallel and one that does not answer is a row in
    `machines` with the reason, never a failure of the whole reading: the two
    that answered are usually what the operator came for, and which one is
    silent is itself a finding.

    A query with no match on a field is refused. A regex over the text of every
    entry of a hypervisor's journal costs seconds of its CPU, and this service
    runs beside real time guests on a substation hypervisor. Name a scope, a
    unit, an identifier or a priority, and put the pattern on top of it.
    """
    _authorised(request)
    service = _service(request)
    try:
        query = _query(
            service,
            scope,
            unit or [],
            identifier or [],
            priority,
            grep,
            since,
            until,
            lines,
        )
        return service.read(query, hosts=host or None)
    except Refused as error:
        raise _refused(error) from error


def _query(
    service,
    scope: str | None,
    unit: list[str],
    identifier: list[str],
    priority: int | None,
    grep: str | None,
    since: datetime | None,
    until: datetime | None,
    lines: int,
) -> journal.Query:
    """The endpoint's parameters as a query, with the scope folded in.

    A scope contributes its matches to whatever the caller also sent rather
    than replacing them, so `scope=guests&host=node2&grep=migrat` is the shape
    the page builds and `unit=...` alone is the shape an automation client
    writes.

    Naming units beside a scope built on identifiers, or the other way round,
    asks for the lines that are both: `journalctl` matches the values of one
    field as OR and the fields against each other as AND. The page therefore
    sends its own units instead of a scope rather than beside one.
    """
    units = list(unit)
    identifiers = list(identifier)
    if scope is not None:
        named: Scope = service.scope(scope)
        units += [name for name in named.units if name not in units]
        identifiers += [name for name in named.identifiers if name not in identifiers]
        if priority is None:
            priority = named.priority
    return journal.Query(
        since=_since(since),
        until=_utc(until),
        units=tuple(units),
        identifiers=tuple(identifiers),
        priority=priority,
        grep=grep,
        lines=lines,
    )


def _since(since: datetime | None) -> datetime:
    if since is None:
        return datetime.now(UTC) - DEFAULT_WINDOW
    return _utc(since)


def _utc(moment: datetime | None) -> datetime | None:
    """A timestamp in UTC, whether or not the caller gave a zone.

    A naive timestamp is read as UTC rather than as this container's local
    time. The API is specified in UTC throughout, the container has no timezone
    worth trusting, and reading it as local would move a window by hours
    without saying so.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
