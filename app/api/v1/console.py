# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The console websocket, and the description the page reads before opening it.

Two things here are not like the rest of the API and both are about the fact
that a websocket handshake is not a fetch. It carries the session cookie
whatever origin opened it, and no CSRF middleware sees it, so the `Origin`
header is checked here before the socket is accepted. And a failure cannot be
an error envelope with a status code, so a refusal is a close code with a
message the terminal prints.

The wire is deliberately small. The browser sends JSON text frames, `input` and
`resize`. The node sends binary frames, which are the bytes of the terminal
exactly as they came off the pseudo terminal, and JSON text frames for the
events around them. Binary rather than JSON escaped text because a terminal
stream is not text until an emulator has decoded it, and a UTF-8 sequence split
across two reads must stay split rather than become a replacement character.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request, WebSocket

from app.console.adapter import ConsoleProcess
from app.console.service import (
    ConsoleInfo,
    ConsoleService,
    ConsoleUnavailable,
    clamp_window,
)
from app.core.auth import Role
from app.core.security import current_session, require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/node/console", tags=["console"])

_DEFAULT_COLUMNS = 80
_DEFAULT_LINES = 24

# Close codes. 1000 and 1008 are the standard ones; the 4xxx range is private
# and mirrors the HTTP status the same refusal would have carried.
_NORMAL = 1000
_POLICY = 1008
_UNAUTHENTICATED = 4401
_FORBIDDEN = 4403
_TIMED_OUT = 4408
_UNAVAILABLE = 4409
_FAILED = 4500


@dataclass(frozen=True)
class _Ending:
    code: int
    reason: str


def _service(connection: Request | WebSocket) -> ConsoleService:
    return connection.app.state.console_service


@router.get(
    "", response_model=ConsoleInfo, dependencies=[Depends(require_role(Role.VIEWER))]
)
def console(request: Request) -> ConsoleInfo:
    return _service(request).info()


@router.websocket("/ws")
async def console_stream(websocket: WebSocket) -> None:
    service = _service(websocket)

    # Before accepting, because a socket opened from another origin has no
    # business being answered at all. A websocket is not subject to the same
    # origin policy and rides the session cookie, so this is the check the
    # CSRF middleware performs for every other unsafe request.
    if not _same_origin(websocket):
        await websocket.close(code=_POLICY)
        return

    await websocket.accept()

    session = current_session(websocket)
    if session is None:
        await _refuse(
            websocket,
            _UNAUTHENTICATED,
            "authentication_required",
            "This session has expired. Sign in again.",
        )
        return
    if not session.user.role.can(service.required_role):
        await _refuse(
            websocket,
            _FORBIDDEN,
            "permission_denied",
            f"A console requires the {service.required_role.value} role.",
        )
        return

    columns, lines = clamp_window(
        _window(websocket, "columns", _DEFAULT_COLUMNS),
        _window(websocket, "lines", _DEFAULT_LINES),
    )
    try:
        process = await service.open(session.username, columns, lines)
    except ConsoleUnavailable as failure:
        await _refuse(websocket, _UNAVAILABLE, failure.code, failure.message)
        return
    except OSError as failure:
        logger.error("Could not open a console: %s", failure)
        await _refuse(
            websocket,
            _FAILED,
            "console_failed",
            f"The ssh client could not be started: {failure}",
        )
        return

    # In a `finally` because the terminal has to be released however this ends,
    # a browser that vanished and a service shutting down included. A session
    # that is never released holds an ssh open and counts against the limit
    # until this node is restarted.
    ending = _Ending(_FAILED, "the console stream failed")
    try:
        await websocket.send_json({"type": "ready", "target": service.target})
        ending = await _pump(websocket, process, service.idle_timeout_seconds)
    finally:
        await service.close(process, session.username)
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=ending.code, reason=ending.reason)


async def _pump(
    websocket: WebSocket, process: ConsoleProcess, idle_timeout: int
) -> _Ending:
    """Both directions at once, and whichever ends first ends the session."""
    tasks = {
        asyncio.create_task(_to_browser(websocket, process)),
        asyncio.create_task(_to_terminal(websocket, process, idle_timeout)),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    ending = _Ending(_NORMAL, "session ended")
    for task in done:
        try:
            ending = task.result()
        except Exception as failure:  # pragma: no cover - defensive
            logger.warning("Console stream failed: %s", failure)
            ending = _Ending(_FAILED, "the console stream failed")
    return ending


async def _to_browser(websocket: WebSocket, process: ConsoleProcess) -> _Ending:
    while True:
        data = await process.read()
        if not data:
            return _Ending(_NORMAL, "the shell exited")
        await websocket.send_bytes(data)


async def _to_terminal(
    websocket: WebSocket, process: ConsoleProcess, idle_timeout: int
) -> _Ending:
    # The timeout counts keystrokes, not output: what it is there to close is
    # a console left open on a screen nobody is in front of, and a session
    # printing a log to an empty room is exactly that.
    timeout = idle_timeout or None
    while True:
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
        except TimeoutError:
            return _Ending(
                _TIMED_OUT,
                f"closed after {idle_timeout} seconds without a keystroke",
            )
        if message["type"] == "websocket.disconnect":
            return _Ending(_NORMAL, "the browser went away")

        text = message.get("text")
        if text is None:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue

        kind = payload.get("type")
        if kind == "input":
            data = payload.get("data")
            if isinstance(data, str):
                process.write(data.encode())
        elif kind == "resize":
            columns, lines = clamp_window(
                _as_int(payload.get("columns"), _DEFAULT_COLUMNS),
                _as_int(payload.get("lines"), _DEFAULT_LINES),
            )
            process.resize(columns, lines)


async def _refuse(
    websocket: WebSocket, code: int, error_code: str, message: str
) -> None:
    with contextlib.suppress(RuntimeError):
        await websocket.send_json(
            {"type": "error", "code": error_code, "message": message}
        )
        await websocket.close(code=code, reason=message[:120])


def _same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        # Not a browser. Nothing forged the request, since only a browser
        # attaches a cookie nobody asked it to attach.
        return True
    host = websocket.headers.get("host", "")
    return origin in (f"https://{host}", f"http://{host}")


def _window(websocket: WebSocket, name: str, fallback: int) -> int:
    return _as_int(websocket.query_params.get(name), fallback)


def _as_int(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
