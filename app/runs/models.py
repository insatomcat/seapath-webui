# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What a run is, while it happens and afterwards."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    """Ended with no final status.

    Almost always because the playbook rebooted the machine it was running
    from, which `seapath_setup_hardening.yaml` does on every host by design.
    Presented as "relaunch", never as a failure: the playbooks are idempotent,
    and converging again is the recovery.
    """


class HostProgress(BaseModel):
    ok: int = 0
    changed: int = 0
    failed: int = 0
    skipped: int = 0
    unreachable: int = 0
    last_task: str | None = None

    @property
    def reached(self) -> bool:
        return bool(self.ok or self.changed or self.failed or self.skipped)


class RunProgress(BaseModel):
    play: str | None = None
    task: str | None = None
    tasks_started: int = 0
    # Seconds per task, the longest a host took. `ansible-runner` reports a
    # duration on every host result, so this costs nothing and answers the
    # question a commissioning run raises: which step took the four minutes.
    # The longest rather than the sum, because hosts run in parallel and the
    # sum would describe a run nobody waited through.
    durations: dict[str, float] = Field(default_factory=dict)
    hosts: dict[str, HostProgress] = Field(default_factory=dict)
    # True once Ansible emitted its recap, which is what tells a finished run
    # from one whose machine went away underneath it.
    final_status_seen: bool = False


class RunRecord(BaseModel):
    """The run as `GET /runs/{id}` answers it."""

    id: str
    playbook: str
    playbook_id: str
    state: RunState = RunState.PENDING
    check: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    launched_by: str
    # The pair that makes a deployment reproducible: which desired state, and
    # which version of the code that reads it.
    inventory_commit: str | None = None
    collection_version: str = "unknown"
    command: list[str] = Field(default_factory=list)
    return_code: int | None = None
    progress: RunProgress = Field(default_factory=RunProgress)
    message: str | None = None

    @property
    def finished(self) -> bool:
        return self.state in (
            RunState.SUCCESS,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.INTERRUPTED,
        )
