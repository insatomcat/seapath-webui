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

Which machine the shell opens on is the `host` query parameter, a name the
inventory declares, and this machine when it is absent. The address is never
the browser's to give. `serial` names a guest instead, and the node picks the
machine and the command.

A guest's graphic console is a socket of its own, `/graphic?guest=<name>`, with
binary frames both ways: they are the VNC protocol between noVNC and the
guest's display, and this end reads nothing of them beyond telling the
operator's input from noVNC's own requests for the screen, for the idle
timeout. With no JSON on that wire, a refusal is the close code and its reason
alone. See D62.
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
    OpenedConsole,
    OpenedDisplay,
    clamp_window,
)
from app.core.auth import Role
from app.core.security import current_session, require_role
from app.core.sessions import Session

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
_NOT_FOUND = 4404
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


async def _admitted(
    websocket: WebSocket, service: ConsoleService, explained: bool = True
) -> Session | None:
    """The session a socket is opened for, once it is accepted and allowed.

    None when it was refused, and then it is already closed. `explained` is
    whether the refusal is also said in a JSON frame, which the terminal
    prints and noVNC would read as a broken server.
    """
    # Before accepting, because a socket opened from another origin has no
    # business being answered at all. A websocket is not subject to the same
    # origin policy and rides the session cookie, so this is the check the
    # CSRF middleware performs for every other unsafe request.
    if not _same_origin(websocket):
        await websocket.close(code=_POLICY)
        return None

    await websocket.accept()

    session = current_session(websocket)
    if session is None:
        await _refuse(
            websocket,
            _UNAUTHENTICATED,
            "authentication_required",
            "This session has expired. Sign in again.",
            explained,
        )
        return None
    if not session.user.role.can(service.required_role):
        await _refuse(
            websocket,
            _FORBIDDEN,
            "permission_denied",
            f"A console requires the {service.required_role.value} role.",
            explained,
        )
        return None
    return session


@router.websocket("/ws")
async def console_stream(websocket: WebSocket) -> None:
    service = _service(websocket)
    session = await _admitted(websocket, service)
    if session is None:
        return

    columns, lines = clamp_window(
        _window(websocket, "columns", _DEFAULT_COLUMNS),
        _window(websocket, "lines", _DEFAULT_LINES),
    )
    try:
        opened = await service.open(
            session.username,
            columns,
            lines,
            host=websocket.query_params.get("host"),
            serial=websocket.query_params.get("serial"),
        )
    except ConsoleUnavailable as failure:
        code = _NOT_FOUND if failure.status == 404 else _UNAVAILABLE
        await _refuse(websocket, code, failure.code, failure.message)
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
        await websocket.send_json(_ready(service, opened))
        ending = await _pump(websocket, opened.process, service.idle_timeout_seconds)
    finally:
        await service.close(opened, session.username)
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=ending.code, reason=ending.reason)


@router.websocket("/graphic")
async def graphic_stream(websocket: WebSocket) -> None:
    service = _service(websocket)
    session = await _admitted(websocket, service, explained=False)
    if session is None:
        return

    guest = websocket.query_params.get("guest") or ""
    try:
        opened = await service.open_graphic(session.username, guest)
    except ConsoleUnavailable as failure:
        code = _NOT_FOUND if failure.status == 404 else _UNAVAILABLE
        await _refuse(websocket, code, failure.code, failure.message, False)
        return
    except OSError as failure:
        logger.error("Could not open a graphic console: %s", failure)
        await _refuse(
            websocket,
            _FAILED,
            "console_failed",
            f"The ssh client could not be started: {failure}",
            False,
        )
        return

    # In a `finally` for the same reason as a terminal: an ssh left open holds
    # a session against the limit until this node restarts.
    ending = _Ending(_FAILED, "the graphic console stream failed")
    try:
        ending = await _relay(websocket, opened, service.idle_timeout_seconds)
    finally:
        await service.close_graphic(opened, session.username)
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=ending.code, reason=_reason(ending.reason))


async def _relay(
    websocket: WebSocket, opened: OpenedDisplay, idle_timeout: int
) -> _Ending:
    """Both directions of a display, and whichever ends first ends the session."""
    tasks = {
        asyncio.create_task(_display_to_browser(websocket, opened)),
        asyncio.create_task(_browser_to_display(websocket, opened, idle_timeout)),
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
            logger.warning("Graphic console stream failed: %s", failure)
            ending = _Ending(_FAILED, "the graphic console stream failed")
    return ending


async def _display_to_browser(websocket: WebSocket, opened: OpenedDisplay) -> _Ending:
    started = False
    while True:
        data = await opened.stream.read()
        if not data:
            break
        started = True
        await websocket.send_bytes(data)
    if started:
        return _Ending(_NORMAL, "the display closed")
    # Nothing ever came from the display, so the relay said why on its error
    # output: `virsh` finding no display, the domain not running here, sudo
    # refusing. That line is the only explanation the operator gets.
    await opened.stream.close()
    said = opened.stream.diagnostic().splitlines()
    return _Ending(
        _UNAVAILABLE,
        said[-1] if said else f"{opened.guest} has no display to open",
    )


async def _browser_to_display(
    websocket: WebSocket, opened: OpenedDisplay, idle_timeout: int
) -> _Ending:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + idle_timeout if idle_timeout else None
    while True:
        remaining = None if deadline is None else max(0.0, deadline - loop.time())
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
        except TimeoutError:
            return _Ending(
                _TIMED_OUT,
                f"closed after {idle_timeout} seconds without a key or a click",
            )
        if message["type"] == "websocket.disconnect":
            return _Ending(_NORMAL, "the browser went away")
        data = message.get("bytes")
        if not data:
            continue
        if deadline is not None and operator_input(data):
            deadline = loop.time() + idle_timeout
        await opened.stream.write(data)


# A FramebufferUpdateRequest: message type 3, ten bytes. noVNC sends one after
# every update it draws, with nobody at the keyboard, and flushes each on its
# own or with others of its kind.
_UPDATE_REQUEST = 3
_UPDATE_REQUEST_BYTES = 10


def operator_input(data: bytes) -> bool:
    """Whether a frame from noVNC carries more than requests for the screen.

    A frame made only of update requests is noVNC keeping the picture current.
    Anything else is a key, the pointer, or the handshake, and the idle timeout
    starts again. Nothing is parsed beyond that: a frame this misreads counts
    as input, which keeps a console open that could have closed, and nothing
    worse.
    """
    if len(data) % _UPDATE_REQUEST_BYTES:
        return True
    return any(
        data[offset] != _UPDATE_REQUEST
        for offset in range(0, len(data), _UPDATE_REQUEST_BYTES)
    )


def _ready(service: ConsoleService, opened: OpenedConsole) -> dict[str, str]:
    ready = {
        "type": "ready",
        "host": opened.target.name,
        "kind": opened.target.kind.value,
        "target": f"{service.user}@{opened.target.address}",
    }
    if opened.guest:
        ready["serial"] = opened.guest
    return ready


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
    websocket: WebSocket,
    code: int,
    error_code: str,
    message: str,
    explained: bool = True,
) -> None:
    with contextlib.suppress(RuntimeError):
        if explained:
            await websocket.send_json(
                {"type": "error", "code": error_code, "message": message}
            )
        await websocket.close(code=code, reason=_reason(message))


def _reason(message: str) -> str:
    """A close reason, which a websocket caps at 123 bytes of UTF-8."""
    return message.encode()[:123].decode(errors="ignore")


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
