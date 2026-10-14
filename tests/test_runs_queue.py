# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The queue: runs accepted while another is going, played one after the other.

Three properties carry it. A queued run plays the inventory it was confirmed
against, frozen when it was queued. The runs behind one that failed still start,
since each was confirmed on its own. And the queue dies with the process, so a
restart never starts a run at boot with nobody watching. See D76.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.runs import fake
from app.runs.adapter import (
    CancelCheck,
    EventHandler,
    OutputHandler,
    RunOutcome,
    RunRequest,
)
from app.runs.install import LOCK_DESCRIPTION, LOCK_ID
from app.runs.models import RunRecord, RunState
from tests.test_runs import (  # noqa: F401
    build,
    inventory,
    store,
    trust,
    wait_for,
)
from tests.test_runs import inventory as inventory_fixture  # noqa: F401
from tests.test_runs_seed import PASSWORD, committed, declared  # noqa: F401


class GatedAdapter(fake.FakeRunAdapter):
    """A fake run that goes on until the test lets it end.

    Each run waits on a gate of its own, opened in the order the test chooses,
    so a run is provably going while the next is queued behind it.
    """

    def __init__(self, failing: set[int] | None = None) -> None:
        super().__init__()
        self.gates: list[threading.Event] = []
        self.started = threading.Semaphore(0)
        self.failing = failing or set()
        self.inventories: list[str] = []

    def execute(
        self,
        request: RunRequest,
        on_event: EventHandler,
        on_output: OutputHandler,
        should_cancel: CancelCheck,
    ) -> RunOutcome:
        index = len(self.gates)
        gate = threading.Event()
        self.gates.append(gate)
        self.inventories.append(request.inventory_file.read_text())
        self.started.release()
        gate.wait(5)
        if index in self.failing:
            self.events = fake.failed_run()
            self.return_code = 2
        else:
            self.events = fake.successful_run()
            self.return_code = 0
        return super().execute(request, on_event, on_output, should_cancel)

    def wait_started(self, count: int) -> None:
        for _ in range(count):
            assert self.started.acquire(timeout=5), "A run never started"

    def release(self, index: int) -> None:
        deadline = time.time() + 5
        while len(self.gates) <= index:
            assert time.time() < deadline, f"Run {index} never started"
            time.sleep(0.01)
        self.gates[index].set()


def test_a_run_queued_while_nothing_is_going_starts_at_once(
    store, inventory, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, inventory, trust, adapter, tmp_path)

    record = service.launch("seapath_setup_main", "alice", queue=True)

    assert record.state is RunState.RUNNING
    assert service.queued() == []
    adapter.release(0)
    assert wait_for(service, record.id).state is RunState.SUCCESS


def test_queued_runs_wait_and_start_in_the_order_they_were_asked_for(
    store, inventory, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, inventory, trust, adapter, tmp_path)

    first = service.launch("seapath_setup_main", "alice", queue=True)
    adapter.wait_started(1)
    second = service.launch("seapath_setup_hardening", "alice", queue=True)
    third = service.launch("seapath_setup_main", "bob", queue=True, check=True)

    assert second.state is RunState.QUEUED
    assert third.state is RunState.QUEUED
    assert [record.id for record in service.queued()] == [second.id, third.id]
    # On disk too, so the history shows them waiting.
    assert store.load(second.id).state is RunState.QUEUED
    # Nothing of theirs has reached a machine.
    assert len(adapter.gates) == 1
    assert service.going().id == first.id

    adapter.release(0)
    adapter.wait_started(1)
    assert service.going().id == second.id
    assert [record.id for record in service.queued()] == [third.id]
    adapter.release(1)
    adapter.release(2)

    for record in (first, second, third):
        assert wait_for(service, record.id).state is RunState.SUCCESS
    assert [request.playbook for request in adapter.requests] == [
        "seapath.ansible.seapath_setup_main",
        "seapath.ansible.seapath_setup_hardening",
        "seapath.ansible.seapath_setup_main",
    ]
    assert adapter.requests[2].check is True
    assert service.queued() == []


def test_a_run_that_fails_does_not_hold_back_the_ones_behind_it(
    store, inventory, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter(failing={0})
    service = build(store, inventory, trust, adapter, tmp_path)

    first = service.launch("seapath_setup_main", "alice", queue=True)
    adapter.wait_started(1)
    second = service.launch("seapath_setup_main", "alice", queue=True)
    adapter.release(0)
    adapter.release(1)

    assert wait_for(service, first.id).state is RunState.FAILED
    assert wait_for(service, second.id).state is RunState.SUCCESS


def test_a_queued_run_plays_the_inventory_it_was_queued_with(
    store, inventory, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, inventory, trust, adapter, tmp_path)
    service.launch("seapath_setup_main", "alice", queue=True)
    adapter.wait_started(1)
    confirmed = inventory.state().commit

    queued = service.launch("seapath_setup_main", "alice", queue=True)
    # The repository moves on while the run waits.
    inventory.save_file("files/later.txt", b"written after the queueing", "bob")
    assert inventory.state().commit != confirmed

    adapter.release(0)
    adapter.release(1)
    record = wait_for(service, queued.id)

    assert record.inventory_commit == confirmed
    assert "files/later.txt" not in [staged.path for staged in record.files]
    assert not (
        adapter.requests[1].inventory_file.parent / "files" / "later.txt"
    ).exists()


def test_a_queued_run_taken_out_never_starts_and_the_next_one_does(
    store, inventory, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, inventory, trust, adapter, tmp_path)
    first = service.launch("seapath_setup_main", "alice", queue=True)
    adapter.wait_started(1)
    dropped = service.launch("seapath_setup_hardening", "alice", queue=True)
    kept = service.launch("seapath_setup_main", "alice", queue=True)

    cancelled = service.cancel(dropped.id)

    assert cancelled.state is RunState.CANCELLED
    assert "reached no machine" in cancelled.message
    assert store.load(dropped.id).state is RunState.CANCELLED
    assert [record.id for record in service.queued()] == [kept.id]
    # The run that is going is not the one cancelled.
    assert store.load(first.id).state is RunState.RUNNING

    adapter.release(0)
    adapter.release(1)
    assert wait_for(service, kept.id).state is RunState.SUCCESS
    assert [request.playbook for request in adapter.requests] == [
        "seapath.ansible.seapath_setup_main",
        "seapath.ansible.seapath_setup_main",
    ]


def test_a_launch_that_does_not_ask_to_wait_is_refused_ahead_of_the_queue(
    store, inventory, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, inventory, trust, adapter, tmp_path)
    first = service.launch("seapath_setup_main", "alice", queue=True)
    adapter.wait_started(1)
    queued = service.launch("seapath_setup_main", "alice", queue=True)

    with pytest.raises(ApiError) as refused:
        service.launch("seapath_setup_main", "bob")

    assert refused.value.code == "run_in_progress"
    assert refused.value.status_code == 409
    assert "1 run is queued" in refused.value.message
    adapter.release(0)
    adapter.release(1)
    wait_for(service, first.id)
    wait_for(service, queued.id)


def test_a_run_that_would_be_refused_is_refused_when_it_is_queued(
    store, inventory, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, inventory, trust, adapter, tmp_path)
    first = service.launch("seapath_setup_main", "alice", queue=True)
    adapter.wait_started(1)

    with pytest.raises(ApiError) as refused:
        service.launch(
            "seapath_setup_main",
            "alice",
            variables={"not_a_declared_variable": True},
            queue=True,
        )

    assert refused.value.status_code == 400
    assert service.queued() == []
    adapter.release(0)
    wait_for(service, first.id)


def test_a_queued_run_waits_for_a_collection_installation(
    store, inventory, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, inventory, trust, adapter, tmp_path)
    store.acquire(LOCK_ID, LOCK_DESCRIPTION)

    record = service.launch("seapath_setup_main", "alice", queue=True)
    assert record.state is RunState.QUEUED
    time.sleep(0.1)
    assert adapter.requests == []

    # The installation says nothing when it lets go, and the queue notices.
    store.release(LOCK_ID)
    adapter.wait_started(1)
    adapter.release(0)
    assert wait_for(service, record.id).state is RunState.SUCCESS


def test_a_restart_drops_the_queue_rather_than_starting_it(store) -> None:  # noqa: F811
    store.create(
        RunRecord(
            id="20261014T100000",
            playbook="seapath.ansible.seapath_setup_main",
            playbook_id="seapath_setup_main",
            state=RunState.QUEUED,
            launched_by="alice",
        )
    )

    recovered = store.reconcile()

    record = store.load("20261014T100000")
    assert [item.id for item in recovered] == ["20261014T100000"]
    assert record.state is RunState.CANCELLED
    assert record.started_at is None
    assert record.finished_at is not None
    assert "queue does not survive a restart" in record.message


def test_a_queued_run_leaves_no_password_hash_on_disk_while_it_waits(
    store, declared, trust, tmp_path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, declared, trust, adapter, tmp_path)
    first = service.launch("deploy_vms_standalone", "alice", queue=True)
    adapter.wait_started(1)

    queued = service.launch(
        "deploy_vms_standalone",
        "alice",
        root_passwords={"guest1": PASSWORD},
        queue=True,
    )

    waiting = "".join(
        path.read_text(errors="ignore")
        for path in store.directory(queued.id).rglob("*")
        if path.is_file()
    )
    assert "$6$" not in waiting
    assert PASSWORD not in waiting

    adapter.release(0)
    adapter.release(1)
    wait_for(service, queued.id)
    # The play read the hash, and the copy was wiped afterwards.
    assert "$6$rounds=" in adapter.inventories[1]
    assert "$6$" not in adapter.requests[1].inventory_file.read_text()
    assert wait_for(service, first.id).state is RunState.SUCCESS
    assert "hashed_passwd" not in committed(tmp_path)


def test_nothing_is_left_queued_once_the_queue_has_drained(
    store, inventory, trust, tmp_path: Path  # noqa: F811
) -> None:
    adapter = GatedAdapter()
    service = build(store, inventory, trust, adapter, tmp_path)
    first = service.launch("seapath_setup_main", "alice", queue=True)
    adapter.wait_started(1)
    second = service.launch("seapath_setup_main", "alice", queue=True)
    adapter.release(0)
    adapter.release(1)
    wait_for(service, first.id)
    wait_for(service, second.id)

    assert service.going() is None
    # A launch after the queue drained starts at once, as on an idle node.
    third = service.launch("seapath_setup_main", "alice")
    assert third.state is RunState.RUNNING
    adapter.release(2)
    wait_for(service, third.id)
