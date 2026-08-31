# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What rewriting an inventory would change, before anything is rewritten.

The service writes the inventory back from a typed model on every form save.
That is safe for a file the service produced, and it is a loaded gun aimed at a
file somebody else wrote: the model holds the variables the forms edit, and a
hand written inventory carries several times as many.

So the rule is that the service earns the right to write a file by proving it
can reproduce it. The proof is mechanical. Resolve the file the way Ansible
resolves it, parse it into the model, render the model back, resolve that, and
compare. A file the service wrote comes back identical. A file it cannot
reproduce is served read only, with the exact list of what a save would have
destroyed.

This is the check that would have caught the three losses found on the first
real inventory this service ever met: group variables dropped wholesale, groups
it has never heard of erased, and `hostname` overwritten with the host key,
which renames a running machine.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.inventory.parser import InvalidInventory, parse
from app.inventory.renderer import render
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
_ORDER = {"unsupported": 0, "host_lost": 1, "changed": 2, "invented": 3, "lost": 4}

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


def divergences(document: str) -> list[Divergence]:
    """Everything a rewrite of this document would change. Empty means safe."""
    try:
        model = parse(document)
    except InvalidInventory:
        # A file that does not parse is reported as a parse error elsewhere,
        # and it is certainly not writable.
        return [
            Divergence(
                kind="unsupported",
                message="This inventory cannot be read, so it cannot be rewritten.",
            )
        ]

    try:
        rewritten = render(model)
    except NotImplementedError:
        return [
            Divergence(
                kind="unsupported",
                message=(
                    "This is a cluster inventory. Writing one is implemented "
                    "from M3, so the file is served read only until then."
                ),
            )
        ]

    before = resolve(document)
    after = resolve(rewritten)

    raw: list[tuple[str, str | None, str, Any, Any]] = []
    for host, variables in before.items():
        if host not in after:
            raw.append(("host_lost", None, host, None, None))
            continue
        raw.extend(_compare(host, variables, after[host]))

    return _collapse(raw)


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
