# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Turning the Ansible event stream into something an operator can read.

`ansible-runner` emits a JSON event per task and per host, which is what makes
a progress view possible instead of a wall of text. This module owns the
mapping, and one rule inside it carries most of the value: the recap event is
what tells a finished run from one whose machine went away underneath it.
"""

from __future__ import annotations

import json
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

        task = _qualified(data)
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
        return {
            "kind": "task",
            "task": _qualified(data),
            "play": data.get("play"),
        }
    if name in _HOST_RESULTS:
        outcome = _HOST_RESULTS[name]
        if outcome == "ok" and (data.get("res") or {}).get("changed"):
            outcome = "changed"
        return {
            "kind": "result",
            "host": data.get("host"),
            "task": _qualified(data),
            "outcome": outcome,
            "seconds": data.get("duration"),
            # The operator needs to know why a task failed, and nothing else
            # from the result payload.
            "message": _failure_message(data) if outcome == "failed" else None,
            "output": _debug_output(data),
        }
    if name == "playbook_on_stats":
        return {"kind": "stats", "stats": _stats(data)}
    return None


def _qualified(data: dict[str, Any]) -> str | None:
    """`role : task`, which is how Ansible names a task on screen.

    Twelve tasks called "Detect Debian distribution" and "Copy libvirtd.conf"
    say very little without the role they came from, and the role is in the
    event already.
    """
    task = data.get("task")
    role = data.get("role")
    if task and role and not str(task).startswith(f"{role} :"):
        return f"{role} : {task}"
    return task


# What `debug` returns beside the thing it was asked to print.
_BOOKKEEPING = frozenset(
    {"changed", "failed", "skipped", "rescued", "ignored", "warnings", "deprecations"}
)


def _debug_output(data: dict[str, Any]) -> str | None:
    """What a `debug` task printed, which is the only reason it exists.

    Narrow on purpose. The rest of a result payload stays out of the browser,
    and `no_log` is honoured here as Ansible honours it everywhere else: a task
    marked no_log shows that it ran and nothing more.
    """
    # `task_action` is the resolved name, so it is `ansible.builtin.debug`
    # rather than `debug`, and comparing against the short name silently
    # matched nothing. The last segment is the module either way.
    if str(data.get("task_action") or "").rsplit(".", 1)[-1] != "debug":
        return None
    result = data.get("res") or {}
    if result.get("_ansible_no_log"):
        return None
    shown = {
        key: value
        for key, value in result.items()
        if not key.startswith("_ansible") and key not in _BOOKKEEPING
    }
    if not shown:
        return None
    if list(shown) == ["msg"]:
        return str(shown["msg"])[:2000]
    return json.dumps(shown, ensure_ascii=False, sort_keys=True)[:2000]


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
