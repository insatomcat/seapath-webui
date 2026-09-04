# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The read only host adapter, and the command runner it is built on."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

from app.hosts.models import (
    CpuReading,
    DisksReading,
    NetworkReading,
    NodeIdentity,
    PtpClock,
    RealtimeReading,
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    """Runs a read only command against the host.

    Injected rather than called directly so that the tests can replay recorded
    output, and so that the set of commands the service is allowed to run stays
    a short, reviewable list in one place. That list is currently one entry
    long, `ip -j addr show`, which is the single thing this reading needs that
    sysfs does not carry.
    """

    def run(self, argv: list[str], timeout: float = 5.0) -> CommandResult: ...


class SubprocessRunner:
    def run(self, argv: list[str], timeout: float = 5.0) -> CommandResult:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, never a shell
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(127, "", f"{argv[0]}: not found in this image")
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"{argv[0]}: timed out after {timeout}s")
        except OSError as error:  # pragma: no cover - defensive
            return CommandResult(1, "", f"{argv[0]}: {error}")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class HostReader(Protocol):
    """What this machine is, and strictly nothing else.

    Hardware and identity, which is what the inventory form needs prefilled and
    what no exporter can answer. Live state is not in this protocol on purpose:
    it belongs to prometheus-node-exporter, which every node already runs.

    No method here changes anything. That is not a convention, it is the point:
    a machine is only ever changed by an Ansible run through the other adapter.
    """

    def node_identity(self) -> NodeIdentity: ...

    def cpu(self) -> CpuReading: ...

    def realtime(self) -> RealtimeReading: ...

    def network(self) -> NetworkReading: ...

    def ptp_clocks(self) -> list[PtpClock]: ...

    def disks(self) -> DisksReading: ...
