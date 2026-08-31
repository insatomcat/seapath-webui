# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The real adapter against a real Ansible, with no machine involved.

Everything else in the suite runs against a fake event stream, which proves
this service reads events correctly but not that they are the events
`ansible-runner` actually emits. This file closes that gap: a trivial playbook
against `localhost` over the local connection, no SSH, no SEAPATH, no cluster.

It is skipped where `ansible-runner` is not installed, so the promise that the
suite runs on any laptop still holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.runs.adapter import AnsibleRunnerAdapter, RunRequest
from app.runs.models import RunProgress
from app.runs.progress import apply_event, summarise

pytest.importorskip("ansible_runner")

_PLAYBOOK = """\
---
- name: A play that changes nothing
  hosts: all
  gather_facts: false
  tasks:
    - name: Report something
      ansible.builtin.debug:
        msg: converged
    - name: Report a change
      ansible.builtin.set_fact:
        marker: true
      changed_when: true
"""

_INVENTORY = """\
all:
  hosts:
    testhost:
      ansible_connection: local
      ansible_python_interpreter: "{{ ansible_playbook_python }}"
"""


@pytest.fixture
def request_for(tmp_path: Path) -> RunRequest:
    directory = tmp_path / "run"
    (directory / "project").mkdir(parents=True)
    (directory / "project" / "trivial.yaml").write_text(_PLAYBOOK)
    inventory = directory / "inventory.yaml"
    inventory.write_text(_INVENTORY)
    return RunRequest(
        run_id="integration",
        playbook="trivial.yaml",
        inventory_file=inventory,
        private_data_dir=directory,
        collections_path=tmp_path / "collections",
        private_key_file=tmp_path / "key",
        known_hosts_file=tmp_path / "known_hosts",
    )


def test_the_event_stream_is_the_shape_this_service_reads(
    request_for: RunRequest,
) -> None:
    events: list[dict] = []
    outcome = AnsibleRunnerAdapter().execute(
        request_for,
        on_event=events.append,
        on_output=lambda _: None,
        should_cancel=lambda: False,
    )

    assert outcome.return_code == 0

    run_progress = RunProgress()
    for event in events:
        apply_event(run_progress, event)

    # The assertions that matter: the recap arrived, and the per host counts
    # this service derives from real events are the real ones.
    assert run_progress.final_status_seen is True
    assert run_progress.tasks_started >= 2
    host = run_progress.hosts["testhost"]
    assert host.ok >= 1
    assert host.changed >= 1
    assert host.failed == 0


def test_the_summaries_a_browser_receives_carry_no_task_payload(
    request_for: RunRequest,
) -> None:
    events: list[dict] = []
    AnsibleRunnerAdapter().execute(
        request_for,
        on_event=events.append,
        on_output=lambda _: None,
        should_cancel=lambda: False,
    )

    summaries = [summary for summary in map(summarise, events) if summary]
    keys = {key for summary in summaries for key in summary}

    assert {"play", "task", "result", "stats"} & {s["kind"] for s in summaries}
    # No `res`, no `stdout`, no module arguments. The raw stream is megabytes
    # nobody reads and a place for a secret to reach a browser. `seconds` is
    # the one number kept out of a host result, because the alternative is a
    # callback plugin printing the same thing into stdout.
    assert keys <= {
        "kind",
        "play",
        "task",
        "host",
        "outcome",
        "message",
        "seconds",
        "output",
        "stats",
    }
    # And it is a real reading, from a real playbook, rather than a field this
    # service invents.
    assert any(
        isinstance(summary.get("seconds"), int | float)
        for summary in summaries
        if summary["kind"] == "result"
    )


def test_a_failing_task_is_reported_with_its_reason(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    (directory / "project").mkdir(parents=True)
    (directory / "project" / "failing.yaml").write_text(
        "---\n"
        "- name: A play that fails\n"
        "  hosts: all\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - name: Fail on purpose\n"
        "      ansible.builtin.fail:\n"
        "        msg: eno1 does not exist\n"
    )
    inventory = directory / "inventory.yaml"
    inventory.write_text(_INVENTORY)

    events: list[dict] = []
    outcome = AnsibleRunnerAdapter().execute(
        RunRequest(
            run_id="integration",
            playbook="failing.yaml",
            inventory_file=inventory,
            private_data_dir=directory,
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
        ),
        on_event=events.append,
        on_output=lambda _: None,
        should_cancel=lambda: False,
    )

    assert outcome.return_code != 0
    failures = [
        summary
        for summary in map(summarise, events)
        if summary and summary.get("outcome") == "failed"
    ]

    assert failures
    assert failures[0]["message"] == "eno1 does not exist"
    assert failures[0]["host"] == "testhost"
