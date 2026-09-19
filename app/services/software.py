# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The software updates of the machines, as the Updates page shows them.

Everything here is a run or a reading of one. What a machine would install is
the answer of the last check run, kept in that run's results directory; which
machines were updated, and when, is the history of the update runs. Nothing in
this module reaches a machine, and nothing is written to the inventory: an
upgrade is an act made once, like a backup (D53), and the run record is its
trace.

Two rules are held here rather than left to the page.

**This machine is updated alone.** The playbook reboots each machine, and the
controller is this service, so a run holding this machine ends with its
reboot, and a machine after it would never be reached. The machine finishes
its own update once its new system is up. `RunService` refuses this machine
beside others whatever launched the run, and refuses it outright when the
installed playbook still finishes on the controller.

**A reading older than an update says so.** A check that ran before a machine
was updated describes a machine that is not there any more, and a page that
listed its packages as pending would send an operator to update it twice.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.errors import ApiError
from app.inventory.resolve import ROOT
from app.inventory.service import InventoryService
from app.runs import scope as scoping
from app.runs import software as plays
from app.runs.models import RunRecord, RunState
from app.runs.scope import RunScope
from app.runs.service import PlaybookAvailability, RunService

logger = logging.getLogger(__name__)

# How far back the history is read. A check or an update is a handful of runs
# a month, and the list of runs is read newest first.
_HISTORY = 300

_ACTIVE = (RunState.PENDING, RunState.RUNNING)


class SoftwareRun(BaseModel):
    """A run as the page names it: which one, how it ended, who and when."""

    id: str
    state: RunState
    launched_by: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


class MachineSoftware(BaseModel):
    host: str
    this_node: bool = Field(
        default=False, description="Whether this is the machine serving the page"
    )
    reading: plays.MachineReading | None = Field(
        default=None,
        description="What the last check found, absent when it brought nothing back",
    )
    stale: bool = Field(
        default=False,
        description="The machine was updated after the check that is shown",
    )
    last_update: SoftwareRun | None = None


class SoftwareView(BaseModel):
    """What `GET /software` answers."""

    machines: list[MachineSoftware] = Field(default_factory=list)
    this_host: str | None = None
    check: SoftwareRun | None = Field(
        default=None, description="The newest check run, going or finished"
    )
    checked: SoftwareRun | None = Field(
        default=None, description="The check the readings come from"
    )
    updating: SoftwareRun | None = Field(
        default=None, description="An update run still going"
    )
    update: PlaybookAvailability | None = None
    one_at_a_time: bool = Field(
        default=True,
        description=(
            "Whether the installed playbook takes several machines one by one. "
            "When it does not, an update run names one machine."
        ),
    )
    updates_itself: bool = Field(
        default=False,
        description=(
            "Whether the installed playbook lets the machine serving this page "
            "be updated from it, alone. When it does not, that machine is "
            "updated from another one."
        ),
    )
    reboots_for_kernel: bool = Field(
        default=False,
        description=(
            "Whether the installed playbook reboots a machine only when it gets "
            "a new kernel. When it does not, every machine updated reboots."
        ),
    )
    note: str | None = None


class SoftwareService:
    def __init__(self, inventory: InventoryService, runs: RunService) -> None:
        self._inventory = inventory
        self._runs = runs

    def view(self) -> SoftwareView:
        state = self._inventory.state()
        machines = self._machines()
        history = self._runs.list(limit=_HISTORY)

        check = _newest(history, plays.CHECK)
        checked = _newest(history, plays.CHECK, finished=True)
        readings = (
            plays.read(self._runs.results(checked.id)) if checked is not None else {}
        )
        updates = [record for record in history if record.playbook_id == plays.UPDATE]

        rows = []
        for host in machines:
            last = next((record for record in updates if _reached(record, host)), None)
            rows.append(
                MachineSoftware(
                    host=host,
                    this_node=host == state.this_host,
                    reading=readings.get(host),
                    stale=_updated_since(last, checked),
                    last_update=_summary(last),
                )
            )

        availability = self._runs.playbooks({plays.UPDATE})
        return SoftwareView(
            machines=rows,
            this_host=state.this_host,
            check=_summary(check),
            checked=_summary(checked),
            updating=_summary(
                next((record for record in updates if record.state in _ACTIVE), None)
            ),
            update=availability[0] if availability else None,
            one_at_a_time=plays.one_at_a_time(self._runs.paths.collections_path),
            updates_itself=plays.updates_itself(self._runs.paths.collections_path),
            reboots_for_kernel=plays.reboots_for_kernel(
                self._runs.paths.collections_path
            ),
            note=None if machines else _NO_MACHINE,
        )

    def check(self, author: str) -> RunRecord:
        """Ask every machine what an upgrade would do, as a run."""
        return self._runs.launch_generated(
            plays.check_entry(), author, plays.check_play()
        )

    def update(self, hosts: list[str], author: str) -> RunRecord:
        """Update the machines named, one at a time, with the upstream playbook.

        The names are checked against the machines the inventory declares, so
        a run narrowed to a guest or a typo is refused here with a sentence
        rather than by Ansible with an empty play. This machine beside others
        is refused by the run service itself, for every caller.
        """
        chosen = sorted(set(hosts))
        if not chosen:
            raise ApiError(
                "no_machine",
                "Name at least one machine to update.",
                400,
            )
        machines = self._machines()
        unknown = [host for host in chosen if host not in machines]
        if unknown:
            raise ApiError(
                "unknown_machine",
                f"{', '.join(unknown)} is not a machine of this inventory. "
                + (f"It declares {', '.join(machines)}." if machines else ""),
                400,
                {"unknown": unknown, "machines": machines},
            )
        if len(chosen) > 1 and not plays.one_at_a_time(
            self._runs.paths.collections_path
        ):
            raise ApiError(
                "one_machine_at_a_time",
                "The SEAPATH collection this image ships updates every machine "
                "it is sent to at once, without moving their guests first. "
                "Update one machine per run, or install a collection whose "
                f"{plays.UPDATE} carries `serial: 1`.",
                409,
                {"hosts": chosen},
            )
        return self._runs.launch(plays.UPDATE, author, scope=RunScope(hosts=chosen))

    def _machines(self) -> list[str]:
        """Every host of the inventory that is not a guest, by name.

        Which is what the check plays with the default scope. Read from the
        file, like every scope, so an adopted inventory's machines are listed
        whatever groups they sit in.
        """
        try:
            document = self._inventory.raw()
        except OSError as error:
            logger.warning("Could not read the inventory: %s", error)
            return []
        return scoping.plan([ROOT], scoping.table(document)).hosts or []


_NO_MACHINE = (
    "This inventory declares no machine yet. The machines it declares are the "
    "ones checked and updated here."
)


def _newest(
    history: list[RunRecord], playbook_id: str, finished: bool = False
) -> RunRecord | None:
    for record in history:
        if record.playbook_id != playbook_id:
            continue
        if finished and not record.finished:
            continue
        return record
    return None


def _reached(record: RunRecord, host: str) -> bool:
    """Whether an update run touched the machine, or is going to.

    What a finished run reached is its progress, and not the machines it was
    sent to: one machine at a time, a failure stops the run before the next
    one, and a machine the run never reached was not updated by it.
    """
    if record.state in _ACTIVE:
        return host in (record.machines or [])
    return host in record.progress.hosts


def _updated_since(update: RunRecord | None, check: RunRecord | None) -> bool:
    """Whether an update reached the machine after the check was taken.

    Counted from the update's start: a run that failed halfway may already have
    installed packages, and a run cut by a reboot has no end at all.
    """
    if update is None or check is None:
        return False
    if update.started_at is None or check.started_at is None:
        return False
    return update.started_at > check.started_at


def _summary(record: RunRecord | None) -> SoftwareRun | None:
    if record is None:
        return None
    return SoftwareRun(
        id=record.id,
        state=record.state,
        launched_by=record.launched_by,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )
