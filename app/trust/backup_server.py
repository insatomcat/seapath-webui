# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Installing the cluster members' backup keys on the backup server.

What `ssh-copy-id` does, for the keys of several machines at once: one
connection to the server with the password of the account the backups are
pushed to, typed once by an operator, and the public keys appended to that
account's `authorized_keys`. It is the one place this service writes to a
machine that is not a SEAPATH machine, and [D57](../../docs/decisions.md) is
why that is allowed and how far it goes.

Four rules keep it as narrow as that:

**Append, never rewrite.** A key already there is left alone and a missing one
is added at the end, after a newline if the file did not end with one. Nothing
is ever removed: the server's `authorized_keys` belongs to whoever runs it.

**The host key is the one the operator confirmed.** The connection trusts only
the lines the inventory holds in `backup_restore_remote_host_keys`, which an
operator compared against the server before committing them. A server that
answers with another key is refused before the password is sent.

**The password lives in memory for one connection.** It reaches `ssh` through
`SSH_ASKPASS`, in the environment of that one process, and it is never logged,
never written to disk and never part of an error message. Only
password authentication is attempted, once.

**The keys are checked before they are written.** Each is an ed25519 public key
this service read from a member, with the comment the role gives it, so nothing
a browser sent can become a line on the server.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10
TIMEOUT_SECONDS = 30.0

# What a key the role generated looks like, and nothing else. The comment is
# `backup_restore@<inventory_hostname>`.
PUBLIC_KEY = re.compile(
    r"^ssh-ed25519 [A-Za-z0-9+/]+={0,2} backup_restore@[A-Za-z0-9._-]+$"
)

# `[user@]host`, as `app/services/backup.py` already checked it.
_SERVER = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._:-]+$")

# Where the askpass helper finds the password. Only in the environment of the
# one `ssh` this module starts.
_SECRET = "SEAPATH_WEBUI_ASKPASS_SECRET"
_ASKPASS = f"#!/bin/sh\nprintf '%s\\n' \"${_SECRET}\"\n"


class InstallRefused(Exception):
    """The keys could not be installed, and the message says what ssh answered."""


@dataclass(frozen=True)
class InstallRequest:
    server: str
    """`[user@]host`, as the backups are pushed to it."""
    port: int
    known_hosts: tuple[str, ...]
    """The server's host keys as the inventory holds them, confirmed."""
    keys: tuple[str, ...]
    password: str = field(repr=False)


class KeyInstaller(Protocol):
    def install(self, request: InstallRequest) -> None: ...


def append_script(keys: tuple[str, ...]) -> str:
    """What runs on the server, as its account's own shell reads it.

    POSIX sh, because a backup server is whatever the site already had. The
    file and its directory are created private when they are missing, which is
    what `sshd` requires of them.
    """
    quoted = " ".join(shlex.quote(key) for key in keys)
    return (
        "umask 077; "
        "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys || exit 1; "
        "f=~/.ssh/authorized_keys; "
        'if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then echo >> "$f"; fi; '
        f"for k in {quoted}; do "
        'grep -qxF "$k" "$f" || printf \'%s\\n\' "$k" >> "$f" || exit 1; '
        "done"
    )


def install_command(request: InstallRequest, known_hosts_file: Path) -> list[str]:
    """The invocation, in one reviewable place, which a test asserts."""
    return [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=yes",
        # A password, once, and nothing else: no key of this container is
        # offered to a server that has no business seeing one.
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "PreferredAuthentications=keyboard-interactive,password",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        "-p",
        str(request.port),
        request.server,
        append_script(request.keys),
    ]


def check(request: InstallRequest) -> None:
    if not _SERVER.match(request.server):
        raise InstallRefused(f"{request.server} is not `[user@]host`.")
    if not request.known_hosts:
        raise InstallRefused(
            "No host key of the backup server has been confirmed, so there is "
            "no way to tell the server from something in between. Read and "
            "confirm its host key first."
        )
    bad = [key for key in request.keys if not PUBLIC_KEY.match(key)]
    if bad or not request.keys:
        raise InstallRefused(
            "Only the ed25519 keys the backup_restore role generated on the "
            "cluster members are installed, and "
            + ("none was read." if not request.keys else "one of these is not.")
        )


class SshKeyInstaller:
    """`ssh`, with the password handed over by an askpass helper."""

    def install(self, request: InstallRequest) -> None:
        check(request)
        with tempfile.TemporaryDirectory(prefix="backup-trust-") as scratch:
            directory = Path(scratch)
            known_hosts = directory / "known_hosts"
            known_hosts.write_text("\n".join(request.known_hosts) + "\n")
            askpass = directory / "askpass"
            askpass.write_text(_ASKPASS)
            askpass.chmod(0o700)
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": scratch,
                "SSH_ASKPASS": str(askpass),
                "SSH_ASKPASS_REQUIRE": "force",
                _SECRET: request.password,
            }
            try:
                completed = subprocess.run(  # noqa: S603 - fixed argv
                    install_command(request, known_hosts),
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    env=environment,
                )
            except subprocess.TimeoutExpired as error:
                raise InstallRefused(
                    f"{request.server} did not answer within "
                    f"{TIMEOUT_SECONDS:.0f} seconds."
                ) from error
        if completed.returncode != 0:
            # What ssh said, which never holds the password: it is not echoed
            # and it is not part of any message ssh prints.
            reason = (completed.stderr or completed.stdout).strip()
            if "IDENTIFICATION HAS CHANGED" in reason or (
                "Host key verification failed" in reason
            ):
                raise InstallRefused(
                    f"{request.server} answered with a host key other than the "
                    "one confirmed in the inventory, so the password was not "
                    "sent. Either the server was reinstalled, and its key has "
                    "to be read and confirmed again, or something is answering "
                    "in its place."
                )
            raise InstallRefused(reason or f"ssh exited {completed.returncode}.")
        logger.info(
            "Installed %d backup key(s) on %s", len(request.keys), request.server
        )


@dataclass
class FakeKeyInstaller:
    """Records what it was asked, password included, for the suite to check."""

    requests: list[InstallRequest] = field(default_factory=list)
    refusal: str | None = None

    def install(self, request: InstallRequest) -> None:
        check(request)
        self.requests.append(request)
        if self.refusal is not None:
            raise InstallRefused(self.refusal)


__all__ = [
    "PUBLIC_KEY",
    "FakeKeyInstaller",
    "InstallRefused",
    "InstallRequest",
    "KeyInstaller",
    "SshKeyInstaller",
    "append_script",
    "install_command",
]
