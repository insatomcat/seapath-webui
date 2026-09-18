# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The connection from the cluster members to the backup server.

The backups are pushed by root on a cluster member, over SSH, with no password,
so every member needs a key the backup server accepts and the server's host key
in root's `known_hosts`. Until now that was the site's to arrange by hand, on
each member, and a member without it failed its first backup on a prompt nobody
answered. This module assists it, in three steps, each on the side of the line
it belongs to:

1. **The server's host key is read and confirmed.** `ssh-keyscan` from this
   container, the fingerprints shown to an operator who compares them against
   the server. What they confirm is committed as
   `backup_restore_remote_host_keys`, beside `backup_restore_ssh_key` naming a
   key dedicated to the backups and a `remote_shell` that uses it.
2. **The role does the machines' half.** A run of
   `seapath_setup_backup_restore` generates the key on every member, once, and
   adds the confirmed host key to root's `known_hosts`. Nothing here writes to a
   cluster member.
3. **The keys reach the server.** The public half of each member's key is read
   over the one SSH connection the listing uses, and installed on the server
   with the password of the account the backups are pushed to, typed once. That
   is `app/trust/backup_server.py`, and [D57](../../docs/decisions.md) is why
   this service may do it.

Then every member is asked to reach the server the way a backup will, with
`BatchMode`, and the page says which can.
"""

from __future__ import annotations

import logging
import shlex
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.core.errors import ApiError
from app.core.logging import audit_event
from app.hosts.remote import RemoteRefused, RemoteRequest, RemoteRunner
from app.inventory.editor import Scope
from app.inventory.repository import Commit
from app.inventory.resolve import groups, members, resolve
from app.inventory.service import ImportRefused, InventoryService
from app.runs.backup import BackupTarget
from app.runs.service import RunPaths
from app.services.backup import GROUP, BackupService, InvalidBackupSetting
from app.trust import keyscan
from app.trust.backup_server import (
    PUBLIC_KEY,
    InstallRefused,
    InstallRequest,
    KeyInstaller,
)

logger = logging.getLogger(__name__)

# Where the role generates the key the backups are pushed with, on every member.
KEY_PATH = "/root/.ssh/backup_restore_ed25519"

_KEY = "backup_restore_ssh_key"
_HOST_KEYS = "backup_restore_remote_host_keys"
_SHELL = "backup_restore_remote_shell"

# What the member is given on top of the site's own `remote_shell` when it is
# asked to reach the server: the listing's own two options, for the listing's
# reason. A prompt on a connection with no terminal waits for good.
_PROBE_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


class MemberConnection(BaseModel):
    """One cluster member, and whether a backup pushed from it would connect."""

    host: str
    key: str | None = None
    """The public half of its backup key, as the role generated it."""
    reaches: bool | None = None
    """`None` when the member itself could not be asked."""
    message: str = ""


class ServerHostKey(BaseModel):
    key_type: str
    fingerprint: str
    line: str
    """The `known_hosts` line, under the name the members connect to."""


class BackupConnection(BaseModel):
    """The trust between the cluster members and the backup server."""

    server: str = ""
    port: int = 22
    key_path: str = ""
    """The key the inventory names for the backups, or empty."""
    host_keys: list[ServerHostKey] = Field(default_factory=list)
    """The server's host keys the inventory holds, confirmed by an operator."""
    uses_key: bool = False
    """`remote_shell` names that key, so the backups are pushed with it."""
    members: list[MemberConnection] = Field(default_factory=list)
    read_at: str | None = None
    note: str = ""


class BackupTrustService:
    def __init__(
        self,
        inventory: InventoryService,
        backup: BackupService,
        remote: RemoteRunner,
        keys: RunPaths,
        ansible_user: str,
        installer: KeyInstaller,
        scanner=keyscan.scan,
    ) -> None:
        self._inventory = inventory
        self._backup = backup
        self._remote = remote
        self._keys = keys
        self._ansible_user = ansible_user
        self._installer = installer
        # Public so the suite can answer for a server it cannot reach.
        self.scanner = scanner

    # Reading

    def read(self) -> BackupConnection:
        """Every member asked at once: its key, and whether it reaches the server."""
        target = self._backup.target()
        view = self._declared(target)
        if not target.remote_serv:
            view.note = "This inventory names no backup server yet."
            return view
        addresses = self._members()
        if not addresses:
            view.note = "This inventory has no cluster member to ask."
            return view
        with ThreadPoolExecutor(max_workers=len(addresses)) as pool:
            view.members = list(
                pool.map(
                    lambda item: self._ask(item[0], item[1], target, view.key_path),
                    addresses,
                )
            )
        view.read_at = datetime.now(tz=UTC).isoformat()
        return view

    def scan(self) -> list[ServerHostKey]:
        """The backup server's host keys, read over the network from here.

        What an operator is shown to compare against the server. Nothing is
        written: `ssh-keyscan` learns a key over the network, from whoever
        answers, and only the operator's comparison makes it trust.
        """
        target = self._backup.target()
        if not target.remote_serv:
            raise ApiError(
                "backup_not_configured", "This inventory names no backup server.", 409
            )
        host = _host(target.remote_serv)
        port = _port(target)
        try:
            found = self.scanner([host], port)
        except keyscan.ScanFailed as error:
            raise ApiError("scan_failed", str(error), 502) from error
        name = host if port == 22 else f"[{host}]:{port}"
        return [
            ServerHostKey(
                key_type=item.key_type,
                fingerprint=item.fingerprint,
                line=f"{name} {item.key}",
            )
            for item in found
        ]

    # Writing

    def prepare(
        self, host_keys: list[str], author: str, expected_head: str | None = None
    ) -> Commit | None:
        """Commit the confirmed host keys, the dedicated key and a shell using it.

        One commit on `cluster_machines`, where the seven settings already are.
        `remote_shell` keeps every option the site gave it and gains `-i` with
        the dedicated key, because a site that wrote `ssh` explicitly would
        otherwise keep pushing with root's default key.
        """
        target = self._backup.target()
        if not target.remote_serv:
            raise InvalidBackupSetting("This inventory names no backup server yet.")
        host = _host(target.remote_serv)
        port = _port(target)
        name = host if port == 22 else f"[{host}]:{port}"
        lines = []
        for line in host_keys:
            fields = line.split()
            if (
                len(fields) != 3
                or fields[0] != name
                or not keyscan.is_host_key(" ".join(fields[1:]))
            ):
                raise InvalidBackupSetting(
                    f"{line!r} is not a host key of {name} as known_hosts holds "
                    "one. Read the server's keys again and confirm those."
                )
            lines.append(" ".join(fields))
        if not lines:
            raise InvalidBackupSetting(
                "Confirm at least one host key of the backup server: the members "
                "refuse to push to a server they cannot recognise."
            )

        variables = {
            _KEY: KEY_PATH,
            _HOST_KEYS: lines,
            _SHELL: with_identity(target.remote_shell, KEY_PATH),
        }
        document = self._inventory.raw()
        hosts = sorted(members(groups(document), GROUP))
        try:
            return self._inventory.write_variables(
                writes=[(Scope("group", GROUP), variables)],
                intended={host: dict(variables) for host in hosts},
                message=(
                    f"webui: push the backups to {target.remote_serv} with a "
                    "key of their own"
                ),
                author=author,
                expected_head=expected_head,
            )
        except ImportRefused as error:
            raise InvalidBackupSetting(str(error)) from error

    def install(self, password: str, author: str) -> BackupConnection:
        """Put every member's public key on the server, then ask them again.

        The keys are the ones read from the members just now, and only those:
        a member without its key yet is named, and the role run that generates
        it is what the page offers first.
        """
        before = self.read()
        if not before.host_keys:
            raise ApiError(
                "host_key_unconfirmed",
                "The backup server's host key has not been confirmed, so there "
                "is no way to tell it from something answering in its place. "
                "Read and confirm it first.",
                409,
            )
        keys = tuple(
            member.key
            for member in before.members
            if member.key and PUBLIC_KEY.match(member.key)
        )
        missing = [member.host for member in before.members if not member.key]
        if not keys:
            raise ApiError(
                "no_backup_key",
                "No cluster member has its backup key yet. Apply the backup "
                "configuration first: the role generates one on every member.",
                409,
            )
        try:
            self._installer.install(
                InstallRequest(
                    server=before.server,
                    port=before.port,
                    known_hosts=tuple(item.line for item in before.host_keys),
                    keys=keys,
                    password=password,
                )
            )
        except InstallRefused as error:
            raise ApiError("install_refused", str(error), 502) from error
        audit_event(
            "backup.keys_installed",
            server=before.server,
            keys=len(keys),
            by=author,
        )
        after = self.read()
        if missing:
            after.note = (
                "Installed the keys of every member that has one. "
                + ", ".join(missing)
                + " had none yet: apply the backup configuration, then install "
                "again."
            )
        return after

    # Internals

    def _declared(self, target: BackupTarget) -> BackupConnection:
        document = self._inventory.raw()
        hosts = sorted(members(groups(document), GROUP)) if document.strip() else []
        resolved = resolve(document).get(hosts[0], {}) if hosts else {}
        key_path = str(resolved.get(_KEY) or "")
        host_keys = []
        for line in resolved.get(_HOST_KEYS) or []:
            fields = str(line).split()
            if len(fields) >= 3:
                host_keys.append(
                    ServerHostKey(
                        key_type=fields[1],
                        fingerprint=keyscan.fingerprint_of(" ".join(fields[1:3])),
                        line=" ".join(fields[:3]),
                    )
                )
        return BackupConnection(
            server=target.remote_serv,
            port=_port(target),
            key_path=key_path,
            host_keys=host_keys,
            uses_key=bool(key_path) and key_path in target.shell_argv,
        )

    def _members(self) -> list[tuple[str, str]]:
        state = self._inventory.state()
        if state.inventory is None:
            return []
        found = []
        for name in state.inventory.cluster_members:
            node = state.inventory.hosts.get(name)
            address = getattr(node, "ansible_host", None) if node else None
            if address:
                found.append((name, str(address)))
        return found

    def _ask(
        self, host: str, address: str, target: BackupTarget, key_path: str
    ) -> MemberConnection:
        try:
            answer = self._remote.run(
                RemoteRequest(
                    address=address,
                    user=self._ansible_user,
                    command=probe_command(target, key_path),
                    private_key_file=self._keys.private_key_file,
                    known_hosts_file=self._keys.known_hosts_file,
                    extra_key_files=self._keys.extra_key_files(),
                )
            )
        except RemoteRefused as error:
            return MemberConnection(
                host=host, message=f"{host} could not be asked: {error}"
            )
        member = MemberConnection(host=host)
        for line in answer.splitlines():
            if line.startswith("pub "):
                key = line[len("pub ") :].strip()
                member.key = key if PUBLIC_KEY.match(key) else None
            elif line.startswith("reach ok"):
                member.reaches = True
            elif line.startswith("reach failed"):
                member.reaches = False
                member.message = line[len("reach failed") :].strip()
        return member


def probe_command(target: BackupTarget, key_path: str) -> str:
    """What each member is asked: its public key, and a connection like a backup's.

    Root, because the key and the trust are root's. The connection runs `true`
    on the server with the site's own `remote_shell` and the listing's two
    options, so it succeeds exactly when a backup's `rsync -e` would connect.
    """
    shell = target.shell_argv
    connect = shlex.join(
        [shell[0], *_PROBE_OPTIONS, *shell[1:], target.remote_serv, "true"]
    )
    parts = []
    if key_path:
        public = shlex.quote(key_path + ".pub")
        parts.append(
            f"if [ -r {public} ]; then printf 'pub %s\\n' \"$(cat {public})\"; fi"
        )
    parts.append(
        f"if e=$({connect} 2>&1 >/dev/null); then echo 'reach ok'; "
        "else printf 'reach failed %s\\n' \"$(printf %s \"$e\" | tr '\\n' ' ')\"; fi"
    )
    return "sudo -n /bin/sh -c " + shlex.quote("; ".join(parts))


def with_identity(shell: str, key_path: str) -> str:
    """The site's `remote_shell`, with `-i <key>` unless it names one already."""
    words = shlex.split(shell or "ssh")
    if "-i" in words or any(word.startswith("-i") and len(word) > 2 for word in words):
        return shlex.join(words)
    return shlex.join([words[0], "-i", key_path, *words[1:]])


def _host(server: str) -> str:
    return server.rpartition("@")[2]


def _port(target: BackupTarget) -> int:
    """The port `remote_shell` gives ssh, which is where the server listens."""
    words = target.shell_argv
    for index, word in enumerate(words):
        value = None
        if word == "-p" and index + 1 < len(words):
            value = words[index + 1]
        elif word.startswith("-p") and len(word) > 2:
            value = word[2:]
        elif word == "-o" and index + 1 < len(words):
            option = words[index + 1]
            if option.lower().startswith("port="):
                value = option.split("=", 1)[1]
        if value and value.isdigit():
            return int(value)
    return 22


__all__ = [
    "KEY_PATH",
    "BackupConnection",
    "BackupTrustService",
    "MemberConnection",
    "ServerHostKey",
    "probe_command",
    "with_identity",
]
