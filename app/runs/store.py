# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Run artefacts on disk.

Written as the run progresses and never buffered in memory, because a run will
die mid flight: `seapath_setup_hardening.yaml` reboots every host including the
one driving it, and the network roles can cut the connection under the process.
The design answer is not to prevent that but to make it harmless, and a trace
that survives the machine is most of it.

Each run is a directory holding the inventory it used, the exact command, the
event stream, the log and the status. That is also the whole persistence layer
of this service: there is no database anywhere.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from app.runs.models import RunRecord, RunState

logger = logging.getLogger(__name__)

_RECORD = "run.json"
_EVENTS = "events.jsonl"
_LOG = "stdout.log"
_INVENTORY = "inventory.yaml"
_SITE = "site"
_LOCK = ".lock"


class RunLocked(Exception):
    """Another run holds the lock.

    One run at a time, so two operators on two browsers cannot converge the
    same machines concurrently. The message names the run that is already
    going, because "try again later" is not an answer.
    """


class RunStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def directory(self, run_id: str) -> Path:
        return self._root / run_id

    # Records

    def create(self, record: RunRecord) -> Path:
        directory = self.directory(record.id)
        directory.mkdir(parents=True, exist_ok=True)
        # The inventory folder the run actually used is copied in by
        # `app.runs.staging`, frozen there so the repository can move on
        # without making the trace a lie. The whole folder, since the files an
        # inventory names are as much a part of what a run pushed as the
        # variables that name them.
        (directory / _EVENTS).touch()
        (directory / _LOG).touch()
        self.save(record)
        return directory

    def save(self, record: RunRecord) -> None:
        path = self.directory(record.id) / _RECORD
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(record.model_dump_json(indent=2))
        os.replace(temporary, path)

    def load(self, run_id: str) -> RunRecord | None:
        try:
            return RunRecord.model_validate_json(
                (self.directory(run_id) / _RECORD).read_text()
            )
        except (OSError, ValueError):
            return None

    def list(self, limit: int = 50) -> list[RunRecord]:
        if not self._root.is_dir():
            return []
        records = []
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            record = self.load(entry.name)
            if record is not None:
                records.append(record)
        records.sort(key=lambda record: record.id, reverse=True)
        return records[:limit]

    def inventory_of(self, run_id: str) -> str:
        directory = self.directory(run_id)
        # The staged folder first, then the flat copy runs recorded before the
        # inventory became a folder. A node upgraded in place holds both.
        for path in (directory / _SITE / _INVENTORY, directory / _INVENTORY):
            try:
                return path.read_text()
            except OSError:
                continue
        return ""

    # Streams

    def append_event(self, run_id: str, event: dict) -> None:
        with (self.directory(run_id) / _EVENTS).open("a") as handle:
            handle.write(json.dumps(event, default=str) + "\n")
            handle.flush()
            # The trace has to survive the machine going away, and a buffered
            # line does not.
            os.fsync(handle.fileno())

    def append_log(self, run_id: str, text: str) -> None:
        with (self.directory(run_id) / _LOG).open("a") as handle:
            handle.write(text)

    def events(self, run_id: str, offset: int = 0) -> Iterator[dict]:
        """Events from a given index, for a client that reconnected."""
        path = self.directory(run_id) / _EVENTS
        try:
            with path.open() as handle:
                for index, line in enumerate(handle):
                    if index < offset or not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # A line half written when the machine went down.
                        continue
        except OSError:
            return

    def log(self, run_id: str) -> str:
        try:
            return (self.directory(run_id) / _LOG).read_text()
        except OSError:
            return ""

    # The run lock

    def acquire(self, run_id: str, description: str | None = None) -> None:
        """Take the one lock, or say what is holding it.

        `description` is what the refusal calls the holder. A run is named by
        its id, and installing a collection takes this same lock, because
        replacing the tree a run is reading from is the one way to break a
        convergence that is already going.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / _LOCK
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise RunLocked(
                f"{self.lock_description()} is already going. Two operators "
                "must not converge the same machines at once."
            ) from None
        with os.fdopen(descriptor, "w") as handle:
            handle.write(f"{run_id}\n{description or f'Run {run_id}'}\n")

    def release(self, run_id: str) -> None:
        if self._lock_holder() in (run_id, None):
            (self._root / _LOCK).unlink(missing_ok=True)

    def locked(self) -> bool:
        return (self._root / _LOCK).exists()

    def lock_description(self) -> str:
        """What the lock holder calls itself, for a refusal to name it."""
        lines = self._lock_lines()
        if len(lines) > 1 and lines[1]:
            return lines[1]
        return f"Run {lines[0]}" if lines else "Another operation"

    def _lock_holder(self) -> str | None:
        lines = self._lock_lines()
        return lines[0] if lines and lines[0] else None

    def _lock_lines(self) -> list[str]:
        try:
            return (self._root / _LOCK).read_text().splitlines()
        except OSError:
            return []

    # Recovery

    def reconcile(self) -> list[RunRecord]:
        """Close out runs that the service was in the middle of when it died.

        Called at every start. A record left saying `running` is a run whose
        process is gone: the machine rebooted, or the container restarted.
        Marking it `interrupted` is both true and actionable, where leaving it
        `running` forever would hold the lock and block every future run.
        """
        recovered = []
        for record in self.list(limit=1000):
            if record.state not in (RunState.PENDING, RunState.RUNNING):
                continue
            record.state = RunState.INTERRUPTED
            record.finished_at = datetime.now(tz=UTC)
            record.message = (
                "The service restarted while this run was going. The run is "
                "relaunchable: the playbooks are idempotent, so converging "
                "again is the recovery."
            )
            self.save(record)
            recovered.append(record)
            logger.warning("Marked run %s as interrupted after a restart", record.id)
        # Nothing can legitimately hold the lock at a start: a run does not
        # survive the process, and neither does a collection being installed.
        # A lock left behind by a service that died is a node that refuses
        # every run until someone deletes a file, which is a worse failure
        # than the concurrency it guards against.
        (self._root / _LOCK).unlink(missing_ok=True)
        return recovered
