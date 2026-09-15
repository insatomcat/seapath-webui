# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What the console is allowed to be, decided in one place.

The adapter knows how to open a terminal. This decides whether it may be
opened at all, and where: the console can be turned off, it is capped so a
forgotten tab cannot exhaust an sshd, and it refuses to try when the trust it
would use has not been provisioned, because "Permission denied (publickey)"
inside a terminal is a worse answer than saying so before opening one.

The account is the `ansible` one, with the keys this node already holds. That
is a deliberate consequence rather than an oversight: this service has no other
credential, and inventing one, a per operator key or a password path, would be
a second trust story to secure, replicate and revoke. What it means in practice
is that the console gives what the configuration plane already gives, which is
root through `sudo`, so `console_min_role` exists for a site that wants the
button to require more than reading.

Where a console goes is a name, never an address. The browser asks for an
entry of the inventory, and the address is the `ansible_host` a run would
connect to. A console that accepted an address would be an ssh relay to
anything the administration network routes, carrying the site key; one that
accepts a name reaches exactly the machines and guests a run reaches. See D51.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from app.console.adapter import ConsoleAdapter, ConsoleProcess, ConsoleRequest
from app.core.auth import Role
from app.inventory.service import InventoryState
from app.trust import known_hosts

logger = logging.getLogger(__name__)

_MIN_COLUMNS, _MAX_COLUMNS = 20, 500
_MIN_LINES, _MAX_LINES = 5, 200


class ConsoleUnavailable(Exception):
    """The console cannot be opened, with a code the front end can branch on."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class TargetKind(str, Enum):
    THIS_MACHINE = "this_machine"
    MACHINE = "machine"
    GUEST = "guest"


class ConsoleTarget(BaseModel):
    """One place a console can be opened on."""

    name: str
    kind: TargetKind
    address: str
    host_key_known: bool
    """Whether `known_hosts` holds a key for the address.

    The console checks host keys strictly and never learns one, so an address
    nobody accepted a key for is listed and refused until somebody does.
    """


class ConsoleInfo(BaseModel):
    """What `GET /node/console` answers, so the page knows what to offer."""

    enabled: bool
    user: str
    target: str
    required_role: Role
    idle_timeout_seconds: int
    max_sessions: int
    active_sessions: int
    targets: list[ConsoleTarget]


@dataclass(frozen=True)
class OpenedConsole:
    process: ConsoleProcess
    target: ConsoleTarget


class ConsoleService:
    def __init__(
        self,
        adapter: ConsoleAdapter,
        *,
        target: str,
        user: str,
        private_key_file: Path,
        known_hosts_file: Path,
        hostname: str = "",
        inventory: Callable[[], InventoryState] | None = None,
        extra_key_files: Callable[[], tuple[Path, ...]] = tuple,
        enabled: bool = True,
        required_role: Role = Role.VIEWER,
        max_sessions: int = 4,
        idle_timeout_seconds: int = 900,
    ) -> None:
        self._adapter = adapter
        self._target = target
        self._user = user
        self._private_key_file = private_key_file
        self._known_hosts_file = known_hosts_file
        self._hostname = hostname
        self._inventory = inventory
        self._extra_key_files = extra_key_files
        self._enabled = enabled
        self._required_role = required_role
        self._max_sessions = max_sessions
        self._idle_timeout = idle_timeout_seconds
        self._active = 0

    @property
    def required_role(self) -> Role:
        return self._required_role

    @property
    def idle_timeout_seconds(self) -> int:
        return self._idle_timeout

    @property
    def user(self) -> str:
        return self._user

    def info(self) -> ConsoleInfo:
        return ConsoleInfo(
            enabled=self._enabled,
            user=self._user,
            target=self._target,
            required_role=self._required_role,
            idle_timeout_seconds=self._idle_timeout,
            max_sessions=self._max_sessions,
            active_sessions=self._active,
            targets=self.targets(),
        )

    def targets(self) -> list[ConsoleTarget]:
        """This machine first, then the machines and the guests a run reaches.

        Read from the inventory at each call, so a guest declared a minute ago
        is offered and a machine taken out of the file is not. An entry with
        no `ansible_host` is left out rather than reached by its name: a run
        would ask the resolver, and a console that did the same would land
        wherever DNS says today, checked against a key recorded for an address.
        """
        state = self._inventory() if self._inventory is not None else None
        inventory = state.inventory if state is not None else None
        this_host = (state.this_host if state is not None else None) or self._hostname

        # The loopback, as before: the startup records its key from the host's
        # own filesystem at every start, so there is nothing to check here.
        targets = [
            ConsoleTarget(
                name=this_host,
                kind=TargetKind.THIS_MACHINE,
                address=self._target,
                host_key_known=True,
            )
        ]
        if inventory is None:
            return targets

        recorded = known_hosts.recorded_names(self._known_hosts_file)
        seen = {this_host}
        declared = [
            (name, TargetKind.MACHINE, node.ansible_host)
            for name, node in inventory.hosts.items()
        ] + [
            (name, TargetKind.GUEST, guest.ansible_host)
            for name, guest in inventory.guests.items()
        ]
        for name, kind, address in declared:
            address = (address or "").strip()
            # A templated address is resolved by Ansible and by nothing here.
            if name in seen or not address or "{{" in address:
                continue
            seen.add(name)
            targets.append(
                ConsoleTarget(
                    name=name,
                    kind=kind,
                    address=address,
                    host_key_known=address in recorded,
                )
            )
        return targets

    def resolve(self, name: str | None) -> ConsoleTarget:
        targets = self.targets()
        if not name:
            return targets[0]
        for target in targets:
            if target.name == name:
                return target
        raise ConsoleUnavailable(
            "unknown_host",
            f"{name} is not a machine or a guest of the inventory with an "
            "ansible_host, so no console can be opened on it.",
            status=404,
        )

    async def open(
        self, username: str, columns: int, lines: int, host: str | None = None
    ) -> OpenedConsole:
        if not self._enabled:
            raise ConsoleUnavailable(
                "console_disabled",
                "The console is turned off on this node.",
            )
        target = self.resolve(host)
        if self._active >= self._max_sessions:
            raise ConsoleUnavailable(
                "console_busy",
                f"There are already {self._active} consoles open on this node, "
                "which is the maximum. Close one and retry.",
            )
        if not self._private_key_file.exists():
            raise ConsoleUnavailable(
                "trust_missing",
                "This node has no key for the ansible account yet, so no "
                "console can be opened. The journal says why the self trust "
                "could not be provisioned.",
            )
        if not target.host_key_known:
            raise ConsoleUnavailable(
                "host_key_unknown",
                f"No host key is recorded for {target.name} at {target.address}. "
                "The console never accepts a key it has not seen. Accept it "
                "under Host keys on the Deployment page; a guest declared to "
                "accept its key on first use records it at the first run "
                "that reaches it.",
            )

        # This machine is reached over its self relation alone, which is the
        # one that carries `pty`. Any other gets the keys a run offers it, in
        # the same order.
        extra = (
            ()
            if target.kind is TargetKind.THIS_MACHINE
            else tuple(path for path in self._extra_key_files() if path.exists())
        )
        columns, lines = clamp_window(columns, lines)
        request = ConsoleRequest(
            address=target.address,
            user=self._user,
            private_key_file=self._private_key_file,
            known_hosts_file=self._known_hosts_file,
            extra_key_files=extra,
            columns=columns,
            lines=lines,
        )
        process = await self._adapter.open(request)
        self._active += 1
        # The audit line. `git log` records who changed the desired state, and
        # a run records who launched it; a shell records nothing at all, so the
        # only trace this node keeps of one is here and in the journal.
        logger.info(
            "Console opened by %s on %s, %s@%s (%d open)",
            username,
            target.name,
            self._user,
            target.address,
            self._active,
        )
        return OpenedConsole(process=process, target=target)

    async def close(self, opened: OpenedConsole, username: str) -> None:
        code = await opened.process.close()
        self._active = max(0, self._active - 1)
        logger.info(
            "Console of %s on %s closed, ssh exit %s (%d open)",
            username,
            opened.target.name,
            "unknown" if code is None else code,
            self._active,
        )


def clamp_window(columns: int, lines: int) -> tuple[int, int]:
    """A window size the far end can use.

    The size arrives from a browser and is handed to an ioctl, so it is bounded
    here rather than trusted. Both ends of the range are absurd on purpose:
    what is being refused is a zero, a negative or a number large enough to
    make a shell redraw the world.
    """
    return (
        _clamp(columns, _MIN_COLUMNS, _MAX_COLUMNS),
        _clamp(lines, _MIN_LINES, _MAX_LINES),
    )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))
