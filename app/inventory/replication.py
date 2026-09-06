# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Replication: this node's inventory, pushed to the machines it declares.

Every node owns its own repository and accepts writes to it. Bringing the
copies together is an operator's act, and [D32] records why it stays one: the
transport is the SSH connection a run already makes, so replication asks for no
new credential, no new port and no new listener.

What travels is one branch of one git repository. Git accepts a fast forward
only, so a machine carrying commits this one has never seen ends the push with
an error naming it, and its history is still there afterwards. That refusal is
the whole safety of the design, and it is why this pushes a branch rather than
copying a folder: an `rsync` of the same tree would overwrite those commits and
leave nothing behind, and `git log` is the configuration audit trail.

The invocation lives here, in one reviewable place, the way the console's does.
Two things about it are worth reading before changing anything:

- **`sudo` on the far side.** The repository is root owned, and the connection
  is the `ansible` account. The account already has passwordless sudo, which is
  what lets Ansible converge the machine, so this grants nothing new; it uses
  the escalation the trust already implies. The ISO grants it as `/bin/sh`,
  which is why the remote helper is spelled through `sh -c` rather than as a
  bare `sudo git-receive-pack`.
- **The far side needs git.** The inventory repository on a SEAPATH node is a
  git repository the host holds, so this is the same requirement the audit
  trail already carries. A machine without it says so in git's own words.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from app.inventory.model import Inventory
from app.inventory.repository import (
    DEFAULT_BRANCH,
    InventoryRepository,
    RepositoryError,
)

logger = logging.getLogger(__name__)

# Long enough for a slow administration link, short enough that a page listing
# three machines still renders when one of them is down.
_CONNECT_TIMEOUT_SECONDS = 10
_COMMAND_TIMEOUT_SECONDS = 30.0

# One connection per machine, and the reference cluster has three.
_MAX_PARALLEL = 8


class Status(str, Enum):
    """What one machine's copy is, or what the push to it did."""

    UP_TO_DATE = "up_to_date"
    BEHIND = "behind"
    DIVERGED = "diverged"
    UPDATED = "updated"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"


class Replica(BaseModel):
    """One machine of the inventory, and the state of its copy."""

    host: str
    address: str
    commit: str | None = None
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class Target:
    host: str
    address: str


class Transport(Protocol):
    """How this node reaches one machine's repository.

    Injected for the same reason the command runner and the metrics client
    are: the whole suite runs on a laptop with no cluster, and what this
    service may reach over the network stays a short list in one place. The
    deployment has exactly one implementation, `SshTransport`.
    """

    def url(self, target: Target) -> str:
        """Where that machine's repository is, as git addresses it."""

    def ssh_command(self) -> str | None:
        """What git runs to reach it, or None when no ssh is involved."""

    def upload_pack(self) -> str | None:
        """How `git-upload-pack` is started there, or None for the default."""

    def receive_pack(self) -> str | None:
        """How `git-receive-pack` is started there, or None for the default."""


def build_ssh_command(
    private_key_file: Path,
    known_hosts_file: Path,
    extra_key_files: tuple[Path, ...] = (),
) -> str:
    """The ssh git runs, with the keys the trust provisioned.

    `-F /dev/null` because the ssh client configuration on this image is the
    one the runs write for `ansible.posix.synchronize`, and a replication that
    inherited it would connect differently depending on what the last run
    needed. `BatchMode=yes` because a refused key must end as an error rather
    than as a password prompt nobody is there to answer.

    Git splits this string with a shell, so every path is quoted.
    """
    parts = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        f"UserKnownHostsFile={shlex.quote(str(known_hosts_file))}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        # A replication must not ride the multiplexed connection a run holds
        # open, and must not leave one behind for a run to find.
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        f"ConnectTimeout={_CONNECT_TIMEOUT_SECONDS}",
        "-i",
        shlex.quote(str(private_key_file)),
    ]
    for path in extra_key_files:
        parts += ["-i", shlex.quote(str(path))]
    return " ".join(parts)


def remote_helper(program: str) -> str:
    """`git-receive-pack` or `git-upload-pack`, run as root on the far side.

    Git appends the repository path to this string and hands the whole line to
    the peer's login shell, so `"$0"` is that path. The `sh -c` wrapper is what
    the ISO's sudo rule allows: `NOPASSWD:EXEC:SETENV: /bin/sh`, which is the
    same rule Ansible's `become` goes through.
    """
    return f"sudo -n /bin/sh -c 'exec {program} \"$0\"'"


class SshTransport:
    """The deployment's transport: the `ansible` account, over ssh."""

    def __init__(
        self,
        *,
        user: str,
        private_key_file: Path,
        known_hosts_file: Path,
        remote_path: Path,
        # Resolved at each act rather than held: an operator can add or remove
        # the site key between two replications, and this must offer what is
        # installed now. The same callable the runs are given.
        extra_key_files: Callable[[], tuple[Path, ...]] = tuple,
    ) -> None:
        self._user = user
        self._private_key_file = private_key_file
        self._known_hosts_file = known_hosts_file
        self._remote_path = remote_path
        self._extra_key_files = extra_key_files

    def url(self, target: Target) -> str:
        return f"{self._user}@{target.address}:{self._remote_path}"

    def ssh_command(self) -> str | None:
        return build_ssh_command(
            self._private_key_file,
            self._known_hosts_file,
            self._extra_key_files(),
        )

    def upload_pack(self) -> str | None:
        return remote_helper("git-upload-pack")

    def receive_pack(self) -> str | None:
        return remote_helper("git-receive-pack")


class ReplicationService:
    """Who receives, what they hold, and what a push to them did."""

    def __init__(
        self,
        repository: InventoryRepository,
        transport: Transport,
        timeout: float = _COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._timeout = timeout

    def targets(
        self, inventory: Inventory | None, this_host: str | None
    ) -> list[Target]:
        """The machines a replication reaches.

        The machines the inventory declares, minus this one. The guests are
        absent by construction: they live in `guests`, and a member of the
        `VMs` group is a libvirt domain rather than a machine holding an
        inventory.
        """
        if inventory is None:
            return []
        return [
            Target(host=name, address=node.ansible_host)
            for name, node in inventory.hosts.items()
            if name != this_host and node.ansible_host
        ]

    def survey(
        self, inventory: Inventory | None, this_host: str | None
    ) -> list[Replica]:
        """Which commit each machine holds, asked of the machines themselves.

        Nothing about a replication is remembered, so no stored state can
        disagree with what the machines actually hold. This is the boundary the
        rest of the service draws everywhere else: ask the thing that owns the
        answer.
        """
        return self._fan_out(self._read, self.targets(inventory, this_host))

    def replicate(
        self, inventory: Inventory | None, this_host: str | None
    ) -> list[Replica]:
        """Push this node's branch to every machine, and report each one.

        A machine that is down is one line of the result. Nothing is rolled
        back on the machines that were reached: they hold a commit that is the
        desired state either way.
        """
        return self._fan_out(self._push, self.targets(inventory, this_host))

    def head(self) -> str | None:
        return self._repository.head()

    # Internals

    def _fan_out(
        self, act: Callable[[Target], Replica], targets: list[Target]
    ) -> list[Replica]:
        if not targets:
            return []
        # In parallel, because a page waits on the slowest machine and one that
        # is down costs the whole timeout.
        with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, len(targets))) as pool:
            return list(pool.map(act, targets))

    def _remote_head(self, target: Target) -> str | None:
        return self._repository.remote_head(
            self._transport.url(target),
            ssh_command=self._transport.ssh_command(),
            upload_pack=self._transport.upload_pack(),
            timeout=self._timeout,
        )

    def _read(self, target: Target) -> Replica:
        try:
            commit = self._remote_head(target)
        except RepositoryError as error:
            return self._unreachable(target, error)
        return Replica(
            host=target.host,
            address=target.address,
            commit=commit,
            status=self._compare(commit),
        )

    def _push(self, target: Target) -> Replica:
        try:
            commit = self._remote_head(target)
        except RepositoryError as error:
            return self._unreachable(target, error)

        if commit is not None and commit == self._repository.head():
            return Replica(
                host=target.host,
                address=target.address,
                commit=commit,
                status=Status.UP_TO_DATE,
            )

        try:
            self._repository.push(
                self._transport.url(target),
                ssh_command=self._transport.ssh_command(),
                receive_pack=self._transport.receive_pack(),
                timeout=self._timeout,
            )
        except RepositoryError as error:
            refusal = _refusal(target, str(error))
            if refusal is not None:
                return Replica(
                    host=target.host,
                    address=target.address,
                    commit=commit,
                    status=Status.REFUSED,
                    detail=refusal,
                )
            return self._unreachable(target, error)

        logger.info("Replicated the inventory to %s", target.host)
        return Replica(
            host=target.host,
            address=target.address,
            commit=self._repository.head(),
            status=Status.UPDATED,
        )

    def _compare(self, commit: str | None) -> Status:
        head = self._repository.head()
        if commit == head:
            return Status.UP_TO_DATE
        if commit is None or self._repository.contains(commit):
            return Status.BEHIND
        return Status.DIVERGED

    def _unreachable(self, target: Target, error: RepositoryError) -> Replica:
        return Replica(
            host=target.host,
            address=target.address,
            status=Status.UNREACHABLE,
            detail=_sentence(str(error)),
        )


def _refusal(target: Target, message: str) -> str | None:
    """What the machine refused, told from the machine not answering at all.

    A refusal is the far side saying no to a push it understood, and the one
    that matters has a sentence of its own: git protecting commits this node
    has never seen. Anything else it refuses is carried in its own words, which
    name the case better than a category would.
    """
    lowered = message.lower()
    if "non-fast-forward" in lowered or "fetch first" in lowered:
        return (
            f"{target.host} carries commits this node does not have. Open the "
            "UI on that machine and replicate from there, or revert what it "
            "holds. Nothing was overwritten."
        )
    if "rejected]" in lowered:
        return _sentence(message)
    return None


def _sentence(message: str) -> str:
    """Git's own words, on one line.

    A failure here is a machine that did not answer, an unknown host key, a
    missing repository or a missing git. Each of those git names better than
    anything invented here would, so the message is carried rather than
    replaced. `Warning: Permanently added` and the rest of the noise ssh writes
    on success has no place in it.
    """
    lines = [
        line.strip()
        for line in message.splitlines()
        if line.strip() and not line.strip().lower().startswith("warning:")
    ]
    return " ".join(lines)[:500]


__all__ = [
    "DEFAULT_BRANCH",
    "Replica",
    "ReplicationService",
    "SshTransport",
    "Status",
    "Target",
    "Transport",
    "build_ssh_command",
    "remote_helper",
]
