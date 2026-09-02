# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A console that answers without a machine.

The whole test suite runs on a laptop with no SEAPATH node, and the console is
no exception: this is a shell shaped echo, enough to exercise the websocket
protocol, the resize path and the session accounting. It is also what
`SEAPATH_WEBUI_USE_FAKES` serves, so the terminal can be worked on in a
browser without an ssh anywhere.
"""

from __future__ import annotations

import asyncio

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


class FakeConsoleAdapter:
    def __init__(self) -> None:
        self.opened: list[ConsoleRequest] = []
        self.processes: list[FakeConsoleProcess] = []

    async def open(self, request: ConsoleRequest) -> FakeConsoleProcess:
        self.opened.append(request)
        process = FakeConsoleProcess(request)
        self.processes.append(process)
        return process
