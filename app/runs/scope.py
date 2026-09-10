# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Which machines a run plays, and how an operator narrows it.

Two rules live here, and they answer two different questions.

**The guests are not played by default.** Several playbooks list `VMs` in their
`hosts:` line, so a convergence reaches into every guest the inventory
declares, over SSH, as the `ansible` account. That holds for a guest built from
a SEAPATH image, and a site's guests are mostly not that: they are appliances,
Windows machines, images someone else ships. With `any_errors_fatal` one of
them refusing a connection ends the whole convergence, and the machines are
what the operator came for. So the default limit subtracts the group, and the
subtraction is visible: `--limit all:!VMs` on the command line the run records,
rather than a catalogue edited to say the playbook plays something else than it
plays. `targets` stays copied from the playbook.

A limit decides which hosts a play runs on and leaves `groups['VMs']` alone, so
the roles that loop over the guest list to create and start the guests keep
seeing all of them. What the subtraction removes is the plays that reach *into*
a guest: `detect_seapath_distro`, the prerequisites, the hardening, and the
`wait_for_connection` of `deploy_vms_standalone`.

**A run can be narrowed, to any set of groups and machines.** [D8](decisions.md#d8)
refuses a tag selector and that stands: tags were never a public interface, and
a combination nobody has run is not a smaller version of a playbook. A host
pattern is a different thing. It is the interface Ansible documents, an
operator running the same playbook from a control machine reaches for `--limit`
first, and the narrowing is checked here against the inventory rather than
typed: groups the file declares, hosts it declares, and nothing else. Several
of them join with `:`, which is the union Ansible reads and what checking
several boxes means.

What a narrowed run is not is a smaller playbook. `cluster_setup_ha` limited to
one member of three still forms no cluster. The UI says so where the choice is
made, and the check that a scope resolves to at least one host is the only
thing refused here: the rest is the operator's judgement, the way it is on a
control machine.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.inventory.model import GUEST_GROUP
from app.inventory.resolve import ROOT, Group, groups, members


class RunScope(BaseModel):
    """What a caller asked the run to be narrowed to.

    Two lists of names rather than a pattern string, because a pattern string
    is the free form field this service refuses to have: every name here is
    checked against the inventory before it reaches a command line. Both empty
    means the playbook's own scope, minus the guests.
    """

    groups: list[str] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list)

    @property
    def narrowed(self) -> bool:
        return bool(self.groups or self.hosts)

    @model_validator(mode="before")
    @classmethod
    def _accept_the_single_choice(cls, data: Any) -> Any:
        """The shape 0.3.66 wrote, which is still on disk.

        That version took one group or one host, as `{"kind", "name"}`. The
        run history is files, so a record written then has to keep parsing
        into the record a run view reads.
        """
        if not isinstance(data, dict) or not data.get("name"):
            return data
        kind, name = data.get("kind"), data["name"]
        if kind == "group":
            return {"groups": [name], "hosts": []}
        if kind == "host":
            return {"groups": [], "hosts": [name]}
        return data


@dataclass(frozen=True)
class Scope:
    """The scope resolved against an inventory: what runs, and what does not."""

    limit: str | None
    """The `--limit` value, or None when the playbook needs none."""
    hosts: list[str] | None = None
    """The hosts this run plays, or None when the patterns are not readable.

    None is honest rather than empty: an entry whose `hosts:` line uses a
    wildcard is not resolved here, and a confirmation that named no machine
    would read as a run that plays none.
    """
    excluded: list[str] = field(default_factory=list)
    """The guests the default scope keeps out, named so the operator sees them."""
    requested: RunScope = field(default_factory=RunScope)


class ScopeRefused(Exception):
    """A scope that names something the inventory does not declare."""

    def __init__(self, code: str, message: str, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class ScopeGroup(BaseModel):
    name: str
    hosts: list[str] = Field(default_factory=list)


class ScopeChoices(BaseModel):
    """What `GET /playbooks/scopes` offers, straight from the inventory."""

    groups: list[ScopeGroup] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list)
    guests: list[str] = Field(default_factory=list)
    guest_group: str = GUEST_GROUP
    unreachable: list[str] = Field(default_factory=list)
    """The machines this node holds no way of reaching, named where it matters.

    A run of the whole inventory is refused while one of them is in it, and a
    run narrowed to the machines that answer is not. The chooser is where an
    operator acts on that, so it is where the list belongs.
    """


def table(document: str | dict[str, Any]) -> dict[str, Group]:
    """The inventory's groups, which every function here takes.

    Read once by the caller and passed in, rather than parsed per entry: the
    catalogue is a few hundred playbooks and each one asks the same question of
    the same file.
    """
    return groups(document)


def choices(table: dict[str, Group], unreachable: Sequence[str] = ()) -> ScopeChoices:
    """Every group and every host a scope may name.

    Read from the file rather than from the typed model, because an adopted
    inventory carries groups this service never writes and an operator narrowing
    a run wants the ones their file actually has.
    """
    named = [
        ScopeGroup(name=name, hosts=sorted(members(table, name)))
        for name in sorted(table)
        if name != ROOT
    ]
    return ScopeChoices(
        groups=[group for group in named if group.hosts],
        hosts=sorted(members(table, ROOT)),
        guests=sorted(members(table, GUEST_GROUP)),
        unreachable=sorted(unreachable),
    )


def plan(
    targets: Sequence[str],
    table: dict[str, Group],
    requested: RunScope | None = None,
) -> Scope:
    """The scope a run is launched with, checked against the inventory."""
    asked = requested or RunScope()
    played = _matching(table, targets)
    guests = members(table, GUEST_GROUP)

    if not asked.narrowed:
        return _default(targets, played, guests, asked)

    chosen = sorted(set(asked.groups)), sorted(set(asked.hosts))
    selected: set[str] = set()
    for name in chosen[0]:
        selected |= _group(table, name)
    for name in chosen[1]:
        selected |= _host(table, name)

    narrowed = selected if played is None else selected & played
    if not narrowed:
        # Ansible would accept this and play nothing, ending green on a
        # convergence that converged nothing. Refused here, where the reason
        # can still be read.
        raise ScopeRefused(
            "empty_scope",
            f"{', '.join(chosen[0] + chosen[1])} holds none of the hosts this "
            f"playbook plays ({', '.join(targets)}).",
            {"scope": chosen[0] + chosen[1], "targets": list(targets)},
        )
    return Scope(
        # A union, which is what checking several boxes means and what Ansible
        # reads a colon separated list as. Sorted, so the same selection is
        # the same command line twice.
        limit=":".join(chosen[0] + chosen[1]),
        hosts=sorted(narrowed),
        excluded=[],
        requested=asked,
    )


def _default(
    targets: Sequence[str],
    played: set[str] | None,
    guests: set[str],
    asked: RunScope,
) -> Scope:
    if played is None:
        # An unreadable `hosts:` line. The group is subtracted whenever the
        # playbook names it, which is all this can honestly say.
        excluded = sorted(guests) if guests and _names_guests(targets) else []
    else:
        excluded = sorted(guests & played)
    if not excluded:
        return Scope(
            limit=None, hosts=_without(played, guests), excluded=[], requested=asked
        )
    return Scope(
        limit=f"{ROOT}:!{GUEST_GROUP}",
        hosts=_without(played, guests),
        excluded=excluded,
        requested=asked,
    )


def _names_guests(targets: Sequence[str]) -> bool:
    """Whether a `hosts:` line names the guest group, operators and all."""
    return any(
        token.strip().lstrip("!&") == GUEST_GROUP
        for target in targets
        for token in _SEPARATOR.split(target)
    )


def _without(played: set[str] | None, guests: set[str]) -> list[str] | None:
    return None if played is None else sorted(played - guests)


def _group(table: dict[str, Group], name: str) -> set[str]:
    if name not in table or (name != ROOT and not members(table, name)):
        raise ScopeRefused(
            "unknown_group",
            f"{name} is not a group of this inventory, or holds no host.",
            {"groups": sorted(n for n in table if n != ROOT)},
        )
    return members(table, name)


def _host(table: dict[str, Group], name: str) -> set[str]:
    hosts = members(table, ROOT)
    if name not in hosts:
        raise ScopeRefused(
            "unknown_host",
            f"{name} is not a host of this inventory.",
            {"hosts": sorted(hosts)},
        )
    return {name}


# A group name followed by a subscript, which is how `cluster_setup_cephadm`
# names the machine that bootstraps: `cluster_machines[0]`.
_SUBSCRIPT = re.compile(r"^(?P<name>[^\[\]]+)\[(?P<start>-?\d+)(?::(?P<end>-?\d*))?\]$")

# The pattern separator, ignoring the colon of a `[0:2]` subscript.
_SEPARATOR = re.compile(r":(?![^\[\]]*\])")

# What this module does not read, and says so rather than guessing: a wildcard,
# a regular expression, or a comma separated list.
_UNREADABLE = ("*", "~", ",", "?")


def _matching(table: dict[str, Group], patterns: Sequence[str]) -> set[str] | None:
    """The hosts a playbook's `hosts:` lines name, or None if unreadable.

    Ansible's own rules, restricted to the operators the catalogue uses: union
    by `:`, intersection by `&`, exclusion by `!`, and the subscript. This is
    not a reimplementation of Ansible's matcher and does not need to be: it
    feeds the sentence an operator reads before confirming, and a pattern it
    cannot read is reported as unread rather than answered with a guess.
    """
    found: set[str] = set()
    for pattern in patterns:
        one = _match(table, pattern)
        if one is None:
            return None
        found |= one
    return found


def _match(table: dict[str, Group], pattern: str) -> set[str] | None:
    tokens = [token.strip() for token in _SEPARATOR.split(pattern) if token.strip()]
    unions = [t for t in tokens if t[0] not in "!&"]
    # Ansible's own ordering: with no union pattern, the set starts at `all`.
    selected: set[str] = set() if unions else members(table, ROOT)
    for token in unions:
        hosts = _hosts_of(table, token)
        if hosts is None:
            return None
        selected |= hosts
    for token in tokens:
        if token[0] not in "!&":
            continue
        hosts = _hosts_of(table, token[1:])
        if hosts is None:
            return None
        selected = selected - hosts if token[0] == "!" else selected & hosts
    return selected


def _hosts_of(table: dict[str, Group], token: str) -> set[str] | None:
    if any(character in token for character in _UNREADABLE):
        return None

    subscript = _SUBSCRIPT.match(token)
    if subscript is None:
        if token in table:
            return members(table, token)
        # A bare host name, which is what a generated action play targets. A
        # name the file does not carry matches nothing, the way Ansible would.
        return {token} if token in members(table, ROOT) else set()

    ordered = _ordered(table, subscript.group("name"))
    if ordered is None:
        return None
    start = int(subscript.group("start"))
    end = subscript.group("end")
    if end is None:
        return {ordered[start]} if -len(ordered) <= start < len(ordered) else set()
    return set(ordered[start : int(end) + 1] if end else ordered[start:])


def _ordered(table: dict[str, Group], name: str) -> list[str] | None:
    """A group's hosts in declaration order, which is what a subscript indexes.

    Only for a group holding its hosts directly. `Group.children` is a set, so
    a group built from children has no order to index and is reported as
    unreadable rather than sorted into a plausible looking answer.
    """
    group = table.get(name)
    if group is None or group.children:
        return None
    return list(group.hosts)
