# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Real time conformance, and the measurements that back it.

Read only, like the node view. Nothing here tunes a machine: the conformance
half reports what the tuning came out as, and the measurement half reads the
histogram a `cyclictest` run fetched. Launching that run is `POST /runs` like
any other playbook, which is what keeps one lock, one confirmation and one
history across everything that touches a machine.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.core.auth import Role
from app.core.security import require_role
from app.hosts.models import RealtimeReading
from app.services.realtime import Measurement, RealtimeConformance, RealtimeService

router = APIRouter(
    prefix="/realtime",
    tags=["realtime"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _service(request: Request) -> RealtimeService:
    return request.app.state.realtime_service


@router.get("", response_model=RealtimeConformance)
def conformance(request: Request) -> RealtimeConformance:
    """What this machine's real time tuning is, against what it was told.

    A check is either conformance, where the inventory declares a value and
    the two are compared, or advice, where nothing declares one. The
    difference is in the payload, because only the first kind has an action
    behind it: edit the inventory and converge.
    """
    return _service(request).conformance()


@router.get("/reading", response_model=RealtimeReading)
def reading(request: Request) -> RealtimeReading:
    """The raw values the checks are formed from, for an automation client."""
    return _service(request).conformance().reading


@router.get("/measurements", response_model=list[Measurement])
def measurements(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[Measurement]:
    """The cyclictest runs launched from this node, newest first.

    Each carries the inventory commit its machines were converged from, so a
    latency figure can be read against the isolation that produced it.
    """
    return _service(request).measurements(limit=limit)
