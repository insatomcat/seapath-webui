# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Launching runs, and being honest about how they end."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.errors import ApiError
from app.core.logging import audit_event
from app.inventory.service import InventoryService
from app.runs import catalogue, progress
from app.runs.adapter import RunAdapter, RunRequest
from app.runs.catalogue import PlaybookEntry, Precondition
from app.runs.models import RunProgress, RunRecord, RunState
from app.runs.store import RunLocked, RunStore
from app.trust import known_hosts
from app.trust.service import TrustService

logger = logging.getLogger(__name__)


class PlaybookAvailability(BaseModel):
    """A catalogue entry, and whether this node can run it right now."""

    entry: PlaybookEntry
    available: bool
    unmet: list[str] = []


class RunPaths(BaseModel):
    collections_path: Path
    private_key_file: Path
    known_hosts_file: Path
    # Resolved at launch rather than held: an operator can add or remove the
    # site key between two runs, and a run must use what is installed now.
    extra_key_files: Callable[[], tuple[Path, ...]] = tuple


class RunService:
    def __init__(
        self,
        store: RunStore,
        adapter: RunAdapter,
        inventory: InventoryService,
        trust: TrustService,
        paths: RunPaths,
        hostname: str,
        collection_version: str = "unknown",
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._inventory = inventory
        self._trust = trust
        self._paths = paths
        self._hostname = hostname
        self._collection_version = collection_version
        self._cancelled: set[str] = set()

    # Catalogue

    def playbooks(self) -> list[PlaybookAvailability]:
        unmet_by_condition = self._unmet_preconditions()
        missing = self._missing_playbooks()
        return [
            PlaybookAvailability(
                entry=entry,
                available=not self._blocking(entry, unmet_by_condition, missing),
                unmet=self._blocking(entry, unmet_by_condition, missing),
            )
            for entry in catalogue.CATALOGUE
        ]

    def _blocking(
        self,
        entry: PlaybookEntry,
        unmet: dict[Precondition, str],
        missing: set[str],
    ) -> list[str]:
        reasons = [unmet[c] for c in entry.requires if c in unmet]
        if entry.id in missing:
            reasons.insert(
                0,
                (
                    f"{entry.playbook} is not in the SEAPATH collection this "
                    f"image ships (version {self._collection_version}). The "
                    "catalogue and the collection are released separately."
                ),
            )
        return reasons

    def _unreachable(self, state) -> list[str]:
        """The machines in the inventory this node has no way of reaching.

        Reachability here is about credentials rather than about the network:
        whether a key would be offered and whether the host key is known. The
        network answer belongs to the run, which says it host by host.
        """
        if state.inventory is None:
            return []
        others = [name for name in state.inventory.hosts if name != state.this_host]
        if not others:
            return []
        if not self._paths.extra_key_files():
            return others

        known = known_hosts.read_peers(self._paths.known_hosts_file)
        return [
            name
            for name in others
            if state.inventory.hosts[name].ansible_host not in known
        ]

    def _missing_playbooks(self) -> set[str]:
        return catalogue.missing_from(self._paths.collections_path)

    def _unmet_preconditions(self) -> dict[Precondition, str]:
        unmet: dict[Precondition, str] = {}
        state = self._inventory.state()

        if not state.seeded or state.inventory is None:
            unmet[Precondition.INVENTORY_VALID] = (
                "There is no inventory yet. Fill in the machine's form first."
            )
        elif not state.validation.valid:
            failing = ", ".join(f.rule for f in state.validation.errors())
            unmet[Precondition.INVENTORY_VALID] = (
                f"The inventory does not validate: {failing}."
            )

        relations = self._trust.relations(self._hostname)
        if not relations or not relations[0].installed:
            unmet[Precondition.SELF_TRUST] = (
                "This node has no SSH trust with itself, so it cannot converge "
                "even its own configuration."
            )

        unreachable = self._unreachable(state)
        if unreachable:
            unmet[Precondition.PEER_REACHABLE] = (
                f"{', '.join(unreachable)} cannot be reached from this node. A "
                "run plays every machine the inventory declares, so it would "
                "die on those. Upload the site key, and accept their host "
                "keys, in Reaching the other machines."
            )

        mode = state.inventory.mode.value if state.inventory else None
        if mode != "cluster":
            unmet[Precondition.CLUSTER] = (
                "This machine is not part of a cluster. Add a node first."
            )
        if mode != "standalone":
            unmet[Precondition.STANDALONE] = "This machine is not standalone."

        return unmet

    # Runs

    def list(self, limit: int = 50) -> list[RunRecord]:
        return self._store.list(limit)

    def get(self, run_id: str) -> RunRecord | None:
        return self._store.load(run_id)

    def events(self, run_id: str, offset: int = 0):
        return self._store.events(run_id, offset)

    def log(self, run_id: str) -> str:
        return self._store.log(run_id)

    def reconcile(self) -> list[RunRecord]:
        return self._store.reconcile()

    def launch(
        self,
        playbook_id: str,
        launched_by: str,
        variables: dict[str, Any] | None = None,
        check: bool = False,
    ) -> RunRecord:
        entry = catalogue.get(playbook_id)
        if entry is None:
            raise ApiError(
                "unknown_playbook",
                f"{playbook_id} is not in the catalogue.",
                404,
                {"available": sorted(catalogue.BY_ID)},
            )

        blocking = self._blocking(
            entry, self._unmet_preconditions(), self._missing_playbooks()
        )
        if blocking:
            # Named, never a bare 400: the operator has to know which condition
            # to satisfy.
            raise ApiError(
                "precondition_failed",
                blocking[0],
                409,
                {"unmet": blocking},
            )

        if check and not entry.previewable:
            raise ApiError(
                "not_previewable",
                (
                    f"{entry.title} is driven by commands rather than by file "
                    "templates, so check mode would report nothing meaningful."
                ),
                409,
            )

        extra_vars = self._accepted_variables(entry, variables or {})

        state = self._inventory.state()
        run_id = _new_run_id()
        record = RunRecord(
            id=run_id,
            playbook=entry.playbook,
            playbook_id=entry.id,
            check=check,
            launched_by=launched_by,
            inventory_commit=state.commit,
            collection_version=self._collection_version,
        )

        # The lock before the directory: two operators must not converge the
        # same machines concurrently, and the loser must be told which run is
        # already going.
        try:
            self._store.acquire(run_id)
        except RunLocked as error:
            raise ApiError("run_in_progress", str(error), 409) from error

        try:
            directory = self._store.create(record, self._inventory.raw())
        except Exception:
            self._store.release(run_id)
            raise

        record.state = RunState.RUNNING
        record.started_at = datetime.now(tz=UTC)
        self._store.save(record)
        audit_event(
            "run.launched",
            run=run_id,
            playbook=entry.id,
            user=launched_by,
            check=check,
            commit=state.commit,
        )

        thread = threading.Thread(
            target=self._execute,
            args=(record, entry, directory, extra_vars),
            name=f"run-{run_id}",
            daemon=True,
        )
        thread.start()
        return record

    def cancel(self, run_id: str) -> RunRecord:
        record = self._store.load(run_id)
        if record is None:
            raise ApiError("unknown_run", f"There is no run {run_id}.", 404)
        if record.finished:
            raise ApiError(
                "run_finished", f"Run {run_id} is already {record.state.value}.", 409
            )
        self._cancelled.add(run_id)
        audit_event("run.cancel_requested", run=run_id)
        return record

    def _accepted_variables(
        self, entry: PlaybookEntry, supplied: dict[str, Any]
    ) -> dict[str, Any]:
        """Only what the catalogue entry declares.

        Anything else is refused, because a free form extra vars field is a tag
        selector wearing a different hat.
        """
        declared = {spec.name: spec for spec in entry.variables}
        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise ApiError(
                "unknown_variable",
                (
                    f"{entry.title} accepts "
                    + (", ".join(sorted(declared)) or "no variables")
                    + f", not {', '.join(unknown)}."
                ),
                400,
                {"accepted": sorted(declared)},
            )
        missing = [
            name
            for name, spec in declared.items()
            if spec.required and name not in supplied
        ]
        if missing:
            raise ApiError(
                "missing_variable",
                f"{entry.title} requires {', '.join(missing)}.",
                400,
            )
        return dict(supplied)

    def _execute(
        self,
        record: RunRecord,
        entry: PlaybookEntry,
        directory: Path,
        extra_vars: dict[str, Any],
    ) -> None:
        run_progress = RunProgress()

        def on_event(event: dict) -> None:
            progress.apply_event(run_progress, event)
            summary = progress.summarise(event)
            if summary is not None:
                self._store.append_event(record.id, summary)
            record.progress = run_progress
            self._store.save(record)

        try:
            outcome = self._adapter.execute(
                RunRequest(
                    run_id=record.id,
                    playbook=entry.playbook,
                    inventory_file=directory / "inventory.yaml",
                    private_data_dir=directory,
                    collections_path=self._paths.collections_path,
                    private_key_file=self._paths.private_key_file,
                    known_hosts_file=self._paths.known_hosts_file,
                    extra_key_files=self._paths.extra_key_files(),
                    extra_vars=extra_vars,
                    check=record.check,
                ),
                on_event=on_event,
                on_output=lambda text: self._store.append_log(record.id, text),
                should_cancel=lambda: record.id in self._cancelled,
            )
            record.command = outcome.command
            record.return_code = outcome.return_code
            record.state = self._final_state(outcome, run_progress)
            record.message = self._final_message(record, outcome)
        except Exception as error:  # pragma: no cover - defensive
            logger.exception("Run %s raised", record.id)
            record.state = RunState.FAILED
            record.message = str(error)
        finally:
            record.finished_at = datetime.now(tz=UTC)
            record.progress = run_progress
            self._store.save(record)
            self._store.release(record.id)
            self._cancelled.discard(record.id)
            audit_event("run.finished", run=record.id, state=record.state.value)

    @staticmethod
    def _final_state(outcome, run_progress: RunProgress) -> RunState:
        if outcome.cancelled:
            return RunState.CANCELLED
        if outcome.error:
            return RunState.FAILED
        # The rule that matters. A run that ends without Ansible's recap did
        # not finish, it stopped existing, and that is almost always because
        # the playbook rebooted the machine it was running from. Calling it a
        # failure would send an operator looking for a fault that is not there.
        if not run_progress.final_status_seen:
            return RunState.INTERRUPTED
        return RunState.SUCCESS if outcome.return_code == 0 else RunState.FAILED

    @staticmethod
    def _final_message(record: RunRecord, outcome) -> str | None:
        if outcome.error:
            return outcome.error
        if record.state is RunState.INTERRUPTED:
            reached = [
                host for host, state in record.progress.hosts.items() if state.reached
            ]
            return (
                "The run ended without a final status, which usually means the "
                "playbook rebooted the machine it was running from. Relaunching "
                "is safe: the playbooks are idempotent. Hosts reached: "
                + (", ".join(sorted(reached)) or "none")
                + "."
            )
        if record.state is RunState.CANCELLED:
            return (
                "Cancelled. A convergence stopped part way leaves the machine "
                "between two states, so relaunch or check it before relying "
                "on it."
            )
        if record.state is RunState.FAILED:
            return (
                "A host failed and any_errors_fatal stopped everything. The "
                "per host results below name which ones were reached."
            )
        return None


def _new_run_id() -> str:
    # Sortable, readable, and unique on a node: the store lists runs by
    # sorting on it, and an operator reads it in a directory listing.
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S.%f")
