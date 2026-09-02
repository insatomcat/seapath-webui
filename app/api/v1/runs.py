# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Launching playbooks, and watching them.

Launching a run is an administrator's act. It converges a live substation
hypervisor, it restarts whatever the roles decide to restart, and some entries
reboot the machine at the end.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.runs.models import RunRecord
from app.runs.service import PlaybookAvailability, RunService

router = APIRouter(tags=["runs"])

viewer = Depends(require_role(Role.VIEWER))
operator = Depends(require_role(Role.OPERATOR))
admin = Depends(require_role(Role.ADMIN))

# How often the event stream looks for new lines. The file is appended to by
# the run thread, so this is a tail, not a poll of anything expensive.
_STREAM_INTERVAL = 0.4


def _service(request: Request) -> RunService:
    return request.app.state.run_service


class LaunchRequest(BaseModel):
    playbook: str = Field(description="An id from GET /playbooks")
    check: bool = False
    variables: dict[str, Any] = Field(default_factory=dict)


class LaunchResponse(BaseModel):
    run_id: str
    state: str
    preview: str = Field(
        description="The check mode quality of this playbook: full, partial or none"
    )


@router.get("/playbooks")
def playbooks(request: Request, user: User = viewer) -> list[PlaybookAvailability]:
    """The catalogue, with why each entry is or is not offered right now."""
    return _service(request).playbooks()


@router.post("/runs", status_code=202)
def launch(
    request: Request, payload: LaunchRequest, user: User = admin
) -> LaunchResponse:
    service = _service(request)
    record = service.launch(
        playbook_id=payload.playbook,
        launched_by=user.username,
        variables=payload.variables,
        check=payload.check,
    )
    entry = next(item for item in service.entries() if item.id == payload.playbook)
    # Carried back so the UI can refuse to present a partial check as a
    # guarantee.
    return LaunchResponse(
        run_id=record.id, state=record.state.value, preview=entry.preview.value
    )


@router.get("/runs")
def runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    user: User = viewer,
) -> list[RunRecord]:
    return _service(request).list(limit)


@router.get("/runs/{run_id}")
def run(request: Request, run_id: str, user: User = viewer) -> RunRecord:
    record = _service(request).get(run_id)
    if record is None:
        raise ApiError("unknown_run", f"There is no run {run_id}.", 404)
    return record


@router.get("/runs/{run_id}/log")
def log(request: Request, run_id: str, user: User = viewer) -> Response:
    if _service(request).get(run_id) is None:
        raise ApiError("unknown_run", f"There is no run {run_id}.", 404)
    return Response(content=_service(request).log(run_id), media_type="text/plain")


@router.post("/runs/{run_id}/cancel")
def cancel(request: Request, run_id: str, user: User = operator) -> RunRecord:
    """Best effort, and honest that a cancelled convergence leaves a partial state."""
    return _service(request).cancel(run_id)


@router.get("/runs/{run_id}/events")
async def events(
    request: Request,
    run_id: str,
    offset: int = Query(default=0, ge=0),
    user: User = viewer,
) -> StreamingResponse:
    """Server sent events, one per Ansible task event.

    Resumable by index: a browser that reconnects after the machine rebooted
    asks for what it has not seen, rather than replaying a whole convergence.
    """
    service = _service(request)
    if service.get(run_id) is None:
        raise ApiError("unknown_run", f"There is no run {run_id}.", 404)

    async def stream():
        index = offset
        while True:
            for event in service.events(run_id, index):
                yield f"id: {index}\ndata: {json.dumps(event)}\n\n"
                index += 1

            record = service.get(run_id)
            if record is not None and record.finished:
                # The last thing a client receives is the verdict, so a browser
                # that missed the state change still learns how it ended.
                yield ("event: state\n" f"data: {json.dumps(_final(record))}\n\n")
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(_STREAM_INTERVAL)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _final(record: RunRecord) -> dict[str, Any]:
    return {
        "state": record.state.value,
        "message": record.message,
        "return_code": record.return_code,
        "hosts": {
            host: progress.model_dump()
            for host, progress in record.progress.hosts.items()
        },
    }
