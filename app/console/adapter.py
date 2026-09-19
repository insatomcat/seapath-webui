# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The console adapter: one `ssh` in a pseudo terminal, and nothing else.

This is a third host adapter beside the two AGENTS.md describes, and it is
deliberately the thinnest of them. It runs the `ssh` client the image already
carries, with the key the trust provisioned and the `known_hosts` the startup
wrote, against the `ansible` account of a machine or a guest the inventory
declares. It renders no file, it holds no state, and what it can reach is
exactly what a run can reach: an operator typing here has the same access the
configuration plane already has, no more.

The window size is handled by hand because the child is given a pseudo terminal
without making it a controlling one. `TIOCSWINSZ` on the master changes the
size, and the `SIGWINCH` that would normally follow is sent explicitly, which
`ssh` turns into a window change message for the remote shell. The alternative,
a `preexec_fn` calling `setsid` and `TIOCSCTTY`, forks a process that has
threads and holds locks, which is a worse trade for the same result.

A guest's graphic console is the same ssh with no terminal at all: the bytes of
the VNC protocol cross it on stdin and stdout, and a pseudo terminal in the way
would translate line endings and echo them back. See D62.
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
_DIAGNOSTIC_BYTES = 4 * 1024

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
    # Offered after `private_key_file`, in the order a run offers them: the
    # site key, to a machine that is not this one.
    extra_key_files: tuple[Path, ...] = ()
    # What the far end runs in the terminal instead of the login shell, as one
    # string the remote shell parses. Empty for a shell, and set by the service
    # alone for a guest's serial console: nothing a browser sends reaches it.
    command: str = ""
    columns: int = 80
    lines: int = 24
    # False for a byte stream: the far end gets pipes, and nothing between the
    # two ends reads what crosses them.
    terminal: bool = True


class ConsoleProcess(Protocol):
    async def read(self) -> bytes:
        """The next chunk of output, or `b""` once the session is over."""

    def write(self, data: bytes) -> None: ...

    def resize(self, columns: int, lines: int) -> None: ...

    async def close(self) -> int | None:
        """End the session and report the exit code, if there was one."""


class StreamProcess(Protocol):
    async def read(self) -> bytes:
        """The next chunk of the stream, or `b""` once it is over."""

    async def write(self, data: bytes) -> None: ...

    def diagnostic(self) -> str:
        """What the far end wrote on its error output, the last of it."""

    async def close(self) -> int | None:
        """End the stream and report the exit code, if there was one."""


class ConsoleAdapter(Protocol):
    async def open(self, request: ConsoleRequest) -> ConsoleProcess: ...

    async def open_stream(self, request: ConsoleRequest) -> StreamProcess: ...


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
    identities = [
        argument
        for key_file in (request.private_key_file, *request.extra_key_files)
        for argument in ("-i", str(key_file))
    ]
    return [
        "ssh",
        "-tt" if request.terminal else "-T",
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
        *identities,
        "-l",
        request.user,
        request.address,
        *([request.command] if request.command else []),
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

    async def open_stream(self, request: ConsoleRequest) -> StreamProcess:
        process = await asyncio.create_subprocess_exec(
            *ssh_command(request),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", "/root"),
                "LANG": "C.UTF-8",
            },
        )
        return PipeStreamProcess(process)


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


class PipeStreamProcess:
    """The ssh's stdin and stdout as a byte stream, and its stderr kept aside.

    The error output is read as it comes, so a chatty far end cannot fill the
    pipe and stall the stream, and only its tail is kept: it is what says why a
    stream ended before it started, `virsh` naming a domain with no display.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._errors = bytearray()
        self._drain = asyncio.create_task(self._read_errors())

    async def _read_errors(self) -> None:
        assert self._process.stderr is not None
        while chunk := await self._process.stderr.read(_READ_BYTES):
            self._errors += chunk
            del self._errors[:-_DIAGNOSTIC_BYTES]

    async def read(self) -> bytes:
        assert self._process.stdout is not None
        return await self._process.stdout.read(_READ_BYTES)

    async def write(self, data: bytes) -> None:
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(data)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The read side is what reports that the far end is gone.
            logger.debug("Dropped stream input, the ssh is gone")

    def diagnostic(self) -> str:
        return self._errors.decode(errors="replace").strip()

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
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._drain, timeout=_TERMINATE_GRACE_SECONDS)
        return self._process.returncode


def _set_window_size(fd: int, columns: int, lines: int) -> None:
    size = struct.pack("HHHH", lines, columns, 0, 0)
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
