# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What the console is allowed to be, decided in one place.

The adapter knows how to open a terminal. This decides whether it may be
opened at all: the console can be turned off, it is capped so a forgotten tab
cannot exhaust the node's sshd, and it refuses to try when the trust it would
use has not been provisioned, because "Permission denied (publickey)" inside a
terminal is a worse answer than saying so before opening one.

The account is the `ansible` one, with the key this node already holds. That is
a deliberate consequence rather than an oversight: this service has no other
credential, and inventing one, a per operator key or a password path, would be
a second trust story to secure, replicate and revoke. What it means in practice
is that the console gives what the configuration plane already gives, which is
root through `sudo`, so `console_min_role` exists for a site that wants the
button to require more than reading.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from app.console.adapter import ConsoleAdapter, ConsoleProcess, ConsoleRequest
from app.core.auth import Role

logger = logging.getLogger(__name__)

_MIN_COLUMNS, _MAX_COLUMNS = 20, 500
_MIN_LINES, _MAX_LINES = 5, 200


class ConsoleUnavailable(Exception):
    """The console cannot be opened, with a code the front end can branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConsoleInfo(BaseModel):
    """What `GET /node/console` answers, so the page knows what to offer."""

    enabled: bool
    user: str
    target: str
    required_role: Role
    idle_timeout_seconds: int
    max_sessions: int
    active_sessions: int


class ConsoleService:
    def __init__(
        self,
        adapter: ConsoleAdapter,
        *,
        target: str,
        user: str,
        private_key_file: Path,
        known_hosts_file: Path,
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
    def target(self) -> str:
        return f"{self._user}@{self._target}"

    def info(self) -> ConsoleInfo:
        return ConsoleInfo(
            enabled=self._enabled,
            user=self._user,
            target=self._target,
            required_role=self._required_role,
            idle_timeout_seconds=self._idle_timeout,
            max_sessions=self._max_sessions,
            active_sessions=self._active,
        )

    async def open(self, username: str, columns: int, lines: int) -> ConsoleProcess:
        if not self._enabled:
            raise ConsoleUnavailable(
                "console_disabled",
                "The console is turned off on this node.",
            )
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

        columns, lines = clamp_window(columns, lines)
        request = ConsoleRequest(
            address=self._target,
            user=self._user,
            private_key_file=self._private_key_file,
            known_hosts_file=self._known_hosts_file,
            columns=columns,
            lines=lines,
        )
        process = await self._adapter.open(request)
        self._active += 1
        # The audit line. `git log` records who changed the desired state, and
        # a run records who launched it; a shell records nothing at all, so the
        # only trace this node keeps of one is here and in the journal.
        logger.info(
            "Console opened by %s on %s@%s (%d open)",
            username,
            self._user,
            self._target,
            self._active,
        )
        return process

    async def close(self, process: ConsoleProcess, username: str) -> None:
        code = await process.close()
        self._active = max(0, self._active - 1)
        logger.info(
            "Console of %s closed, ssh exit %s (%d open)",
            username,
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
