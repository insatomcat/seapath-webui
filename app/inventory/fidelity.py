# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Checking a write before it becomes a commit.

A save changes some variables on some hosts. This module asserts that the file
it produced changes exactly those and nothing else, by resolving both versions
the way Ansible resolves them and comparing every host's variables.

It exists because the first real inventory this service met would have lost
thirty group variables, three machine names and a group it had never heard of,
silently, on the first form submission. The editor no longer works that way.
This is what proves it on every write instead of trusting it.

Three failures are caught here, and each is silent without it:

- a variable nobody touched changed anyway, which is a splice landing in the
  wrong place;
- a variable disappeared, which is a block edit swallowing its neighbour;
- a variable the operator asked for did not change, which is a save that
  reports success and does nothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.inventory.resolve import resolve

# Beyond this the list stops being a list and becomes a wall. What was elided is
# counted, because "and 60 more" is itself the finding.
_MAX_REPORTED = 30

# How many hosts a single line names before it stops naming them.
_MAX_NAMED_HOSTS = 4

# The kinds that put a new value on a machine come before the kind that takes
# one away, because a value nobody chose is about to be applied by a role,
# while an absent one usually leaves a default in place. Within that, the order
# is a machine disappearing, a decision overwritten, then a decision invented.
# The list is truncated for display, so this ordering decides what an operator
# actually reads: `hostname` changing and `subnet` appearing are two lines
# among forty, and they are the two that matter.
_ORDER = {
    "unsupported": 0,
    "host_lost": 1,
    "not_applied": 2,
    "changed": 3,
    "invented": 4,
    "lost": 5,
}

# The two variables the roles accept as either a string or a list: the template
# joins a list and passes a string through. The model normalises them to lists,
# so a one element list and the bare string are the same desired state.
_LIST_OR_SCALAR = frozenset({"dns_servers", "ntp_servers"})


class Divergence(BaseModel):
    """One thing a rewrite would change, named precisely enough to act on."""

    kind: str = Field(description="lost, changed, invented, host_lost or unsupported")
    message: str
    variable: str | None = None
    hosts: list[str] = Field(default_factory=list)


def unintended_changes(
    before: str, after: str, intended: dict[str, dict[str, Any]]
) -> list[Divergence]:
    """What this write changed beyond what was asked. Empty means the write is
    exactly its intent."""
    resolved_before = resolve(before)
    resolved_after = resolve(after)

    raw: list[tuple[str, str | None, str, Any, Any]] = []

    for host, variables in resolved_before.items():
        if host not in resolved_after:
            raw.append(("host_lost", None, host, None, None))
            continue
        asked = intended.get(host, {})
        for change in _compare(host, variables, resolved_after[host]):
            _, variable, _, _, produced = change
            if variable in asked and _equivalent(variable, produced, asked[variable]):
                continue
            raw.append(change)

    # A change that was asked for and did not happen is the quietest failure of
    # the three: the commit lands, the diff is empty, and the operator believes
    # the machine is about to be configured differently.
    for host, asked in intended.items():
        produced = resolved_after.get(host, {})
        for variable, value in asked.items():
            got = produced.get(variable)
            if _wanted_gone(value):
                if variable in produced:
                    raw.append(("not_applied", variable, host, None, got))
            elif not _equivalent(variable, got, value):
                raw.append(("not_applied", variable, host, value, got))

    return _collapse(raw)


def _wanted_gone(value: Any) -> bool:
    return value is None or value == [] or value == ""


def _compare(
    host: str, before: dict[str, Any], after: dict[str, Any]
) -> list[tuple[str, str | None, str, Any, Any]]:
    found: list[tuple[str, str | None, str, Any, Any]] = []
    for variable in sorted(set(before) - set(after)):
        found.append(("lost", variable, host, before[variable], None))
    for variable in sorted(set(after) - set(before)):
        found.append(("invented", variable, host, None, after[variable]))
    for variable in sorted(set(before) & set(after)):
        if not _equivalent(variable, before[variable], after[variable]):
            found.append(("changed", variable, host, before[variable], after[variable]))
    return found


def _collapse(
    raw: list[tuple[str, str | None, str, Any, Any]],
) -> list[Divergence]:
    """One line per variable rather than one per variable and host.

    Three machines losing the same thirty group variables is one fact reported
    thirty times, and a truncated list of it hides the one line that matters.
    """
    grouped: dict[tuple[str, str | None], list[tuple[str, Any, Any]]] = {}
    for kind, variable, host, before, after in raw:
        grouped.setdefault((kind, variable), []).append((host, before, after))

    collected = [
        Divergence(
            kind=kind,
            variable=variable,
            hosts=[host for host, _, _ in occurrences],
            message=_message(kind, variable, occurrences),
        )
        for (kind, variable), occurrences in grouped.items()
    ]
    collected.sort(key=lambda d: (_ORDER.get(d.kind, 9), d.variable or ""))

    if len(collected) > _MAX_REPORTED:
        elided = len(collected) - _MAX_REPORTED
        collected = collected[:_MAX_REPORTED]
        collected.append(
            Divergence(
                kind="lost",
                message=f"and {elided} further variables, not listed here.",
            )
        )
    return collected


def _message(
    kind: str, variable: str | None, occurrences: list[tuple[str, Any, Any]]
) -> str:
    hosts = _named([host for host, _, _ in occurrences])

    if kind == "host_lost":
        return f"{hosts} would disappear from the inventory."
    if kind == "not_applied":
        wanted = {repr(before) for _, before, _ in occurrences}
        got = {repr(after) for _, _, after in occurrences}
        return (
            f"{variable} on {hosts} was asked to become {', '.join(sorted(wanted))} "
            f"and stayed {', '.join(sorted(got))}."
        )
    if kind == "lost":
        return f"{variable} would be dropped from {hosts}."
    if kind == "invented":
        values = {repr(after) for _, _, after in occurrences}
        if len(values) == 1:
            return (
                f"{variable} would appear on {hosts} with the value "
                f"{values.pop()}, which this inventory never sets."
            )
        return f"{variable} would appear on {hosts}, which this inventory never sets."

    pairs = {(repr(before), repr(after)) for _, before, after in occurrences}
    if len(pairs) == 1:
        before, after = pairs.pop()
        return f"{variable} on {hosts} would change from {before} to {after}."
    detail = ", ".join(
        f"{host} from {before!r} to {after!r}"
        for host, before, after in occurrences[:_MAX_NAMED_HOSTS]
    )
    return f"{variable} would change: {detail}."


def _named(hosts: list[str]) -> str:
    ordered = sorted(hosts)
    if len(ordered) <= _MAX_NAMED_HOSTS:
        return ", ".join(ordered)
    named = ", ".join(ordered[:_MAX_NAMED_HOSTS])
    return f"{named} and {len(ordered) - _MAX_NAMED_HOSTS} more"


def _equivalent(variable: str, before: Any, after: Any) -> bool:
    if before == after:
        return True
    if variable in _LIST_OR_SCALAR:
        return _as_list(before) == _as_list(after)
    return False


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]
