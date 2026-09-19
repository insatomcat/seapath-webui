# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A console that answers without a machine.

The whole test suite runs on a laptop with no SEAPATH node, and the console is
no exception: this is a shell shaped echo, enough to exercise the websocket
protocol, the resize path and the session accounting. It is also what
`SEAPATH_WEBUI_USE_FAKES` serves, so the terminal can be worked on in a
browser without an ssh anywhere.

The graphic console gets a display in the same spirit: the smallest VNC server
noVNC accepts, drawing one coloured square that changes colour at each key and
click, so the panel's keys, pointer and scaling can be tried with no guest.
"""

from __future__ import annotations

import asyncio
import shlex
import struct

from app.console.adapter import ConsoleRequest

_BANNER = "This is a fake console. Nothing you type reaches a machine.\r\n"


class FakeConsoleProcess:
    def __init__(self, request: ConsoleRequest) -> None:
        self.request = request
        self.resizes: list[tuple[int, int]] = []
        self.closed = False
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._prompt = f"{request.user}@{request.address}:~$ ".encode()
        self._emit(_BANNER.encode() + self._prompt)

    def _emit(self, data: bytes) -> None:
        self._chunks.put_nowait(data)

    async def read(self) -> bytes:
        return await self._chunks.get()

    def write(self, data: bytes) -> None:
        # End of transmission, which is what closes a real shell too.
        if b"\x04" in data:
            self._emit(b"\r\nlogout\r\n")
            self._emit(b"")
            return
        echoed = data.replace(b"\r", b"\r\n")
        self._emit(echoed)
        if b"\r" in data:
            self._emit(self._prompt)

    def resize(self, columns: int, lines: int) -> None:
        self.resizes.append((columns, lines))

    async def close(self) -> int | None:
        self.closed = True
        self._emit(b"")
        return 0


# The screen, and the colours the square takes in turn.
_WIDTH, _HEIGHT = 1024, 768
_GROUND = (22, 32, 46)
_SQUARES = ((74, 158, 255), (80, 200, 120), (230, 160, 60), (220, 80, 90))

# What each client message is, by its type byte: its length, or where its
# variable part's count sits and how wide each element is. Only what noVNC
# sends to a server that advertises no extension.
_FIXED = {0: 20, 3: 10, 4: 8, 5: 6}
_SET_ENCODINGS, _CUT_TEXT = 2, 6
_KEY, _POINTER = 4, 5


class FakeDisplay:
    """A VNC server with no authentication and one rectangle to draw.

    Speaks RFB 3.8 as QEMU does when it asks for no password, keeps the pixel
    format noVNC chooses, and answers an update request with the whole screen
    in RRE, a background and one square. An incremental request waits until a
    key or a click changed the square, the way a real server waits for the
    screen to change.
    """

    def __init__(self, request: ConsoleRequest, guest: str = "") -> None:
        self.request = request
        self.guest = guest
        self.received = bytearray()
        self.closed = False
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._pending = bytearray()
        self._stage = "version"
        self._format = (32, True, 255, 255, 255, 16, 8, 0)
        self._colour = 0
        self._waiting = False
        self._emit(b"RFB 003.008\n")

    def _emit(self, data: bytes) -> None:
        self._chunks.put_nowait(data)

    async def read(self) -> bytes:
        return await self._chunks.get()

    async def write(self, data: bytes) -> None:
        self.received += data
        self._pending += data
        while self._step():
            pass

    def diagnostic(self) -> str:
        return ""

    async def close(self) -> int | None:
        self.closed = True
        self._emit(b"")
        return 0

    def _take(self, count: int) -> bytes | None:
        if len(self._pending) < count:
            return None
        taken = bytes(self._pending[:count])
        del self._pending[:count]
        return taken

    def _step(self) -> bool:
        """Consume one message, if a whole one is there."""
        if self._stage == "version":
            if self._take(12) is None:
                return False
            # One security type, None.
            self._emit(b"\x01\x01")
            self._stage = "security"
            return True
        if self._stage == "security":
            if self._take(1) is None:
                return False
            self._emit(struct.pack(">I", 0))
            self._stage = "init"
            return True
        if self._stage == "init":
            if self._take(1) is None:
                return False
            name = f"fake display of {self.guest or 'a guest'}".encode()
            self._emit(
                struct.pack(">HH", _WIDTH, _HEIGHT)
                + self._pixel_format()
                + struct.pack(">I", len(name))
                + name
            )
            self._stage = "normal"
            return True
        return self._message()

    def _message(self) -> bool:
        if not self._pending:
            return False
        kind = self._pending[0]
        if kind in _FIXED:
            body = self._take(_FIXED[kind])
            if body is None:
                return False
            self._handle(kind, body)
            return True
        if kind == _SET_ENCODINGS:
            if len(self._pending) < 4:
                return False
            count = struct.unpack(">H", self._pending[2:4])[0]
            return self._take(4 + 4 * count) is not None
        if kind == _CUT_TEXT:
            if len(self._pending) < 8:
                return False
            length = struct.unpack(">I", self._pending[4:8])[0]
            return self._take(8 + length) is not None
        # Nothing noVNC sends to this server: drop what is left rather than
        # guess at its framing.
        self._pending.clear()
        return False

    def _handle(self, kind: int, body: bytes) -> None:
        if kind == 0:
            bpp, _, big, true_colour = body[4:8]
            red, green, blue = struct.unpack(">HHH", body[8:14])
            shifts = tuple(body[14:17])
            self._format = (bpp, bool(big), red, green, blue, *shifts)
            if not true_colour:
                self._format = (32, False, 255, 255, 255, 16, 8, 0)
        elif kind == 3:
            if body[1] == 0:
                self._draw()
            else:
                self._waiting = True
        elif kind == _KEY and body[1]:
            self._change()
        elif kind == _POINTER and body[1]:
            self._change()

    def _change(self) -> None:
        self._colour = (self._colour + 1) % len(_SQUARES)
        if self._waiting:
            self._draw()

    def _draw(self) -> None:
        self._waiting = False
        side = 240
        x, y = (_WIDTH - side) // 2, (_HEIGHT - side) // 2
        rre = (
            struct.pack(">I", 1)
            + self._pixel(_GROUND)
            + self._pixel(_SQUARES[self._colour])
            + struct.pack(">HHHH", x, y, side, side)
        )
        self._emit(
            struct.pack(">BBH", 0, 0, 1)
            + struct.pack(">HHHHi", 0, 0, _WIDTH, _HEIGHT, 2)
            + rre
        )

    def _pixel_format(self) -> bytes:
        bpp, big, red, green, blue, rs, gs, bs = self._format
        return struct.pack(
            ">BBBBHHHBBBxxx", bpp, 24, int(big), 1, red, green, blue, rs, gs, bs
        )

    def _pixel(self, colour: tuple[int, int, int]) -> bytes:
        bpp, big, red, green, blue, rs, gs, bs = self._format
        value = (
            (colour[0] * red // 255) << rs
            | (colour[1] * green // 255) << gs
            | (colour[2] * blue // 255) << bs
        )
        width = bpp // 8
        return value.to_bytes(width, "big" if big else "little")


class FakeConsoleAdapter:
    def __init__(self) -> None:
        self.opened: list[ConsoleRequest] = []
        self.processes: list[FakeConsoleProcess] = []
        self.displays: list[FakeDisplay] = []

    async def open(self, request: ConsoleRequest) -> FakeConsoleProcess:
        self.opened.append(request)
        process = FakeConsoleProcess(request)
        self.processes.append(process)
        return process

    async def open_stream(self, request: ConsoleRequest) -> FakeDisplay:
        self.opened.append(request)
        # The guest is the last word of the command the service wrote, inside
        # the `sh -c` it hands to sudo.
        display = FakeDisplay(
            request, shlex.split(shlex.split(request.command)[-1])[-1]
        )
        self.displays.append(display)
        return display
