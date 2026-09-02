# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The console adapter: one `ssh` in a pseudo terminal, and nothing else.

This is a third host adapter beside the two AGENTS.md describes, and it is
deliberately the thinnest of them. It runs the `ssh` client the image already
carries, with the key the trust provisioned and the `known_hosts` the startup
wrote, against the `ansible` account. It renders no file, it holds no state,
and what it can reach is exactly what a run can reach: an operator typing here
has the same access the configuration plane already has, no more.

The window size is handled by hand because the child is given a pseudo terminal
without making it a controlling one. `TIOCSWINSZ` on the master changes the
size, and the `SIGWINCH` that would normally follow is sent explicitly, which
`ssh` turns into a window change message for the remote shell. The alternative,
a `preexec_fn` calling `setsid` and `TIOCSCTTY`, forks a process that has
threads and holds locks, which is a worse trade for the same result.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import struct
import termios
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

_READ_BYTES = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 10
_TERMINATE_GRACE_SECONDS = 3

# The child's environment, written out rather than inherited. The service runs
# with whatever the container gave it, and a shell is not the place to find out
# which of those variables mattered.
_TERM = "xterm-256color"


@dataclass(frozen=True)
class ConsoleRequest:
    """Where the console goes, and how wide the terminal is when it opens."""

    address: str
    user: str
    private_key_file: Path
    known_hosts_file: Path
    columns: int = 80
    lines: int = 24


class ConsoleProcess(Protocol):
    async def read(self) -> bytes:
        """The next chunk of output, or `b""` once the session is over."""

    def write(self, data: bytes) -> None: ...

    def resize(self, columns: int, lines: int) -> None: ...

    async def close(self) -> int | None:
        """End the session and report the exit code, if there was one."""


class ConsoleAdapter(Protocol):
    async def open(self, request: ConsoleRequest) -> ConsoleProcess: ...


def ssh_command(request: ConsoleRequest) -> list[str]:
    """The invocation, in one reviewable place.

    `-F /dev/null` because the ssh client configuration on this image is the
    one the runs write for `ansible.posix.synchronize`, and a console that
    inherited it would connect differently depending on what the last run
    needed. Everything this connection is is on this command line.

    `BatchMode=yes` is not about automation here: without it a refused key ends
    in a password prompt inside the terminal, on an account whose password
    authentication the hardening role has disabled, so the operator would type
    into a prompt that cannot succeed.
    """
    return [
        "ssh",
        "-tt",
        "-F",
        "/dev/null",
        "-o",
        f"UserKnownHostsFile={request.known_hosts_file}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        # A console must not ride the multiplexed connection a run holds open,
        # and must not leave one behind for a run to find.
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        f"ConnectTimeout={_CONNECT_TIMEOUT_SECONDS}",
        "-i",
        str(request.private_key_file),
        "-l",
        request.user,
        request.address,
    ]


class SshConsoleAdapter:
    """Spawns the real client. The fake in `app.console.fake` replaces it."""

    async def open(self, request: ConsoleRequest) -> ConsoleProcess:
        master, replica = os.openpty()
        try:
            _set_window_size(replica, request.columns, request.lines)
            process = await asyncio.create_subprocess_exec(
                *ssh_command(request),
                stdin=replica,
                stdout=replica,
                stderr=replica,
                start_new_session=True,
                env={
                    "TERM": _TERM,
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/root"),
                    "LANG": "C.UTF-8",
                },
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(replica)
        return PtyConsoleProcess(process, master)


class PtyConsoleProcess:
    """The master side of the pseudo terminal, as an async byte stream."""

    def __init__(self, process: asyncio.subprocess.Process, master: int) -> None:
        self._process = process
        self._master: int | None = master
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._ended = False
        os.set_blocking(master, False)
        self._loop.add_reader(master, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master, _READ_BYTES)
        except BlockingIOError:
            return
        except OSError:
            # EIO is how a pseudo terminal reports that the far end is gone,
            # which is what the shell exiting looks like from here.
            data = b""
        if not data:
            self._detach()
        self._chunks.put_nowait(data)

    async def read(self) -> bytes:
        if self._ended and self._chunks.empty():
            return b""
        data = await self._chunks.get()
        if not data:
            self._ended = True
        return data

    def write(self, data: bytes) -> None:
        if self._master is None:
            return
        try:
            os.write(self._master, data)
        except (BlockingIOError, OSError):
            # Keystrokes, not a stream: a terminal input buffer that is full
            # means the session is already gone or wedged, and the read side
            # is what reports that.
            logger.debug("Dropped console input, the terminal did not accept it")

    def resize(self, columns: int, lines: int) -> None:
        if self._master is None:
            return
        _set_window_size(self._master, columns, lines)
        with contextlib.suppress(ProcessLookupError):
            self._process.send_signal(signal.SIGWINCH)

    async def close(self) -> int | None:
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            try:
                await asyncio.wait_for(
                    self._process.wait(), timeout=_TERMINATE_GRACE_SECONDS
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
                await self._process.wait()
        self._detach()
        return self._process.returncode

    def _detach(self) -> None:
        if self._master is None:
            return
        self._loop.remove_reader(self._master)
        os.close(self._master)
        self._master = None


def _set_window_size(fd: int, columns: int, lines: int) -> None:
    size = struct.pack("HHHH", lines, columns, 0, 0)
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
