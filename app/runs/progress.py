# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Turning the Ansible event stream into something an operator can read.

`ansible-runner` emits a JSON event per task and per host, which is what makes
a progress view possible instead of a wall of text. This module owns the
mapping, and one rule inside it carries most of the value: the recap event is
what tells a finished run from one whose machine went away underneath it.
"""

from __future__ import annotations

from typing import Any

from app.runs.models import HostProgress, RunProgress

# The events worth showing. Everything else in the stream is bookkeeping.
_HOST_RESULTS = {
    "runner_on_ok": "ok",
    "runner_on_failed": "failed",
    "runner_on_skipped": "skipped",
    "runner_on_unreachable": "unreachable",
    "runner_on_async_failed": "failed",
}


def apply_event(progress: RunProgress, event: dict[str, Any]) -> RunProgress:
    """Fold one event into the progress. Returns the same object, mutated."""
    name = event.get("event", "")
    data = event.get("event_data") or {}

    if name == "playbook_on_play_start":
        progress.play = data.get("play") or progress.play
    elif name == "playbook_on_task_start":
        progress.task = data.get("task") or progress.task
        progress.tasks_started += 1
    elif name in _HOST_RESULTS:
        host = data.get("host")
        if host:
            state = _host(progress, host)
            outcome = _HOST_RESULTS[name]
            if outcome == "ok" and (data.get("res") or {}).get("changed"):
                state.changed += 1
            else:
                setattr(state, outcome, getattr(state, outcome) + 1)
            state.last_task = data.get("task") or progress.task

        task = data.get("task")
        seconds = data.get("duration")
        if task and isinstance(seconds, int | float):
            progress.durations[task] = max(progress.durations.get(task, 0.0), seconds)
    elif name == "playbook_on_stats":
        # Ansible's recap. Its presence is the whole signal: a run that ends
        # without it did not finish, it stopped existing.
        progress.final_status_seen = True
        _apply_stats(progress, data)

    return progress


def _apply_stats(progress: RunProgress, data: dict[str, Any]) -> None:
    for field, key in (
        ("ok", "ok"),
        ("changed", "changed"),
        ("failed", "failures"),
        ("skipped", "skipped"),
        ("unreachable", "dark"),
    ):
        for host, count in (data.get(key) or {}).items():
            setattr(_host(progress, host), field, count)


def _host(progress: RunProgress, host: str) -> HostProgress:
    if host not in progress.hosts:
        progress.hosts[host] = HostProgress()
    return progress.hosts[host]


def summarise(event: dict[str, Any]) -> dict[str, Any] | None:
    """The shape the run view receives over the event stream.

    A reduction, not a passthrough: the raw stream carries the full result of
    every task on every host, which is megabytes of JSON nobody reads and a
    place for a secret to leak into a browser.
    """
    name = event.get("event", "")
    data = event.get("event_data") or {}

    if name == "playbook_on_play_start":
        return {"kind": "play", "play": data.get("play")}
    if name == "playbook_on_task_start":
        return {"kind": "task", "task": data.get("task"), "play": data.get("play")}
    if name in _HOST_RESULTS:
        outcome = _HOST_RESULTS[name]
        if outcome == "ok" and (data.get("res") or {}).get("changed"):
            outcome = "changed"
        return {
            "kind": "result",
            "host": data.get("host"),
            "task": data.get("task"),
            "outcome": outcome,
            "seconds": data.get("duration"),
            # The operator needs to know why a task failed, and nothing else
            # from the result payload.
            "message": _failure_message(data) if outcome == "failed" else None,
        }
    if name == "playbook_on_stats":
        return {"kind": "stats", "stats": _stats(data)}
    return None


def _failure_message(data: dict[str, Any]) -> str | None:
    result = data.get("res") or {}
    for key in ("msg", "stderr", "reason"):
        value = result.get(key)
        if value:
            return str(value)[:2000]
    return None


def _stats(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        key: dict(data.get(key) or {})
        for key in ("ok", "changed", "failures", "dark", "skipped", "rescued")
    }
