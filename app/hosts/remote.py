# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""One command on one machine, its output, and nothing else.

A read that cannot be made from this container because the thing being read is
not reachable from here. There is one such reading, and it is what the backup
server holds: the SSH trust that reaches that server is root's own key on each
cluster member, put there by the site, and this container neither holds it nor
should.

This is the read only half of what `app/console/adapter.py` already does. The
console opens `ssh` in a pseudo terminal and streams bytes both ways; this runs
the same `ssh`, with the same options, captures what came back and stops. Both
reach the `ansible` account of a machine the inventory declares, with the key
the trust provisioned and the `known_hosts` the startup wrote, which is exactly
what a run reaches and no more. [D54](../../docs/decisions.md) has the bounds.

Two rules keep it as narrow as it looks, and they are the console's own:

**The command is built here, never by a caller.** What a browser sends reaches
a value inside the command, checked before it gets there, and never the command
itself.

**`BatchMode=yes` and `ConnectTimeout` are not optional.** Without the first, a
refused key ends in a password prompt that this process will never answer, and
the read sits there until something kills it. Without the second, an
unreachable machine costs whatever the operating system's TCP timeout happens
to be. The first version of the backup listing had neither, on the second hop,
and an operator watched a page say nothing while the cluster's run lock was
held. See D54.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# How long the connection to the machine is given. The same value the console
# uses, for the same reason: a machine either answers its SSH port promptly or
# is not there.
CONNECT_TIMEOUT_SECONDS = 10

# How long the whole command is given, connection included. A directory listing
# on a backup server is fast; a backup server that has gone away is what this
# bounds.
TIMEOUT_SECONDS = 30.0


class RemoteRefused(Exception):
    """The command could not be run, and the message says what ssh answered."""


@dataclass(frozen=True)
class RemoteRequest:
    """Where the command goes, and what it is."""

    address: str
    user: str
    command: str
    """Run by the far end's shell, built by this service and never by a caller."""
    private_key_file: Path
    known_hosts_file: Path
    extra_key_files: tuple[Path, ...] = ()
    timeout: float = TIMEOUT_SECONDS


class RemoteRunner(Protocol):
    """Runs one command on one machine and hands back its standard output.

    Injected like every other adapter, so the suite reaches no machine.
    """

    def run(self, request: RemoteRequest) -> str: ...


def ssh_command(request: RemoteRequest) -> list[str]:
    """The invocation, in one reviewable place.

    The console's option set without `-tt`: there is no terminal to allocate
    for a command whose output is being parsed, and allocating one would put
    the far end's echo and CRLF line endings into it.
    """
    identities = [
        argument
        for key_file in (request.private_key_file, *request.extra_key_files)
        for argument in ("-i", str(key_file))
    ]
    return [
        "ssh",
        # The ssh client configuration on this image is the one the runs write
        # for `ansible.posix.synchronize`, and a read that inherited it would
        # connect differently depending on what the last run needed.
        "-F",
        "/dev/null",
        "-o",
        f"UserKnownHostsFile={request.known_hosts_file}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        # Never a prompt. This process has no terminal and no way to answer
        # one, so a refused key has to be an error rather than a wait.
        "-o",
        "BatchMode=yes",
        # A read must not ride the multiplexed connection a run holds open, and
        # must not leave one behind for a run to find.
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        *identities,
        "-l",
        request.user,
        request.address,
        request.command,
    ]


class SshRemoteRunner:
    """`ssh`, the client the image already carries for the configuration plane."""

    def run(self, request: RemoteRequest) -> str:
        argv = ssh_command(request)
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, never a shell
                argv,
                capture_output=True,
                text=True,
                timeout=request.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RemoteRefused(
                f"{request.address} did not answer within {request.timeout:.0f} "
                "seconds."
            ) from error
        except OSError as error:  # pragma: no cover - defensive
            raise RemoteRefused(str(error)) from error

        if completed.returncode != 0:
            # What ssh or the far end said, because a host key that was never
            # accepted, an account that refuses the key and a command that
            # failed are three different repairs.
            reason = (completed.stderr or completed.stdout).strip()
            raise RemoteRefused(reason or f"ssh exited {completed.returncode}.")
        return completed.stdout


class FakeRemoteRunner:
    """Answers from a script, and records what it was asked."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.requests: list[RemoteRequest] = []
        self.refusal: str | None = None

    def run(self, request: RemoteRequest) -> str:
        self.requests.append(request)
        if self.refusal is not None:
            raise RemoteRefused(self.refusal)
        for fragment, answer in self.answers.items():
            if fragment in request.command:
                return answer
        return ""


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "TIMEOUT_SECONDS",
    "FakeRemoteRunner",
    "RemoteRefused",
    "RemoteRequest",
    "RemoteRunner",
    "SshRemoteRunner",
    "ssh_command",
]
