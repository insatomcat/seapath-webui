# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What Ansible sees, variable by variable, host by host.

The rest of this package reads a subset of the inventory into a typed model,
which is what a form needs. That subset cannot answer the question adoption
asks: if this file is rewritten, does every host still receive exactly the
variables it received before? Answering it needs the whole file resolved the
way Ansible resolves it, group variables included.

Two shapes of inventory are in the wild and both are valid. The reference
inventories in `seapath-ansible` declare their groups at the top level, next to
`all`. Ansible's more common shape nests them under `all.children`, and a site
that wrote its inventory by hand is as likely to have used one as the other.
Anything that reads only one of them silently ignores half of a real file.

A test asserts that this module agrees with `ansible-inventory --list` on the
fixtures. That agreement is the only reason to trust the ordering rules below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

ROOT = "all"


@dataclass
class Group:
    """One group, however the file happened to declare it."""

    name: str
    variables: dict[str, Any] = field(default_factory=dict)
    hosts: dict[str, dict[str, Any]] = field(default_factory=dict)
    children: set[str] = field(default_factory=set)


def load(document: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(document, dict):
        return document
    loaded = yaml.safe_load(document) or {}
    return loaded if isinstance(loaded, dict) else {}


def groups(document: str | dict[str, Any]) -> dict[str, Group]:
    """Every group the file declares, merged across both declaration shapes."""
    collected: dict[str, Group] = {}

    def define(name: str, body: Any) -> None:
        group = collected.setdefault(name, Group(name))
        if not isinstance(body, dict):
            # `cluster_machines:` with nothing under it is an empty group, and
            # the reference inventory writes exactly that to keep Ansible from
            # warning about a group nobody declared.
            return
        group.variables.update(_mapping(body.get("vars")))
        for host, variables in _hosts(body.get("hosts")).items():
            group.hosts.setdefault(host, {}).update(variables)
        for child, child_body in _children(body.get("children")).items():
            group.children.add(child)
            define(child, child_body)

    for name, body in load(document).items():
        define(name, body)
    collected.setdefault(ROOT, Group(ROOT))
    return collected


def host_names(document: str | dict[str, Any]) -> list[str]:
    return sorted({host for group in groups(document).values() for host in group.hosts})


def resolve(document: str | dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The effective variables of every host, in Ansible's own order.

    Group variables are applied from the shallowest group to the deepest, `all`
    first, alphabetically between groups of equal depth. Host variables are
    applied last and win, which is what lets a cluster inventory carry the
    common configuration once and override it on the one machine that differs.
    """
    table = groups(document)
    depths = _depths(table)
    members = {name: _members(table, name) for name in table}
    # Every host belongs to `all`, whatever the file says.
    members[ROOT] = {host for group in table.values() for host in group.hosts}

    resolved: dict[str, dict[str, Any]] = {}
    for host in sorted(members[ROOT]):
        containing = sorted(
            (group for group in table.values() if host in members[group.name]),
            key=lambda group: (depths[group.name], group.name),
        )
        variables: dict[str, Any] = {}
        for group in containing:
            variables.update(group.variables)
        for group in containing:
            variables.update(group.hosts.get(host, {}))
        resolved[host] = variables
    return resolved


def _members(table: dict[str, Group], name: str) -> set[str]:
    """The hosts of a group and of everything below it."""
    seen: set[str] = set()
    hosts: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen or current not in table:
            continue
        seen.add(current)
        hosts.update(table[current].hosts)
        stack.extend(table[current].children)
    return hosts


def _depths(table: dict[str, Group]) -> dict[str, int]:
    """Distance from `all`, taking the longest path when there are several.

    A group reached through two paths sits at the deeper of the two, because
    that is the one whose variables Ansible applies last.
    """
    parented = {child for group in table.values() for child in group.children}
    depths = {name: (0 if name == ROOT else 1) for name in table}
    # Relaxation rather than a single traversal: a group can be declared before
    # the group that adopts it, and the file is small enough that this costs
    # nothing.
    for _ in range(len(table)):
        changed = False
        for group in table.values():
            for child in group.children:
                if child in depths and depths[child] <= depths[group.name]:
                    depths[child] = depths[group.name] + 1
                    changed = True
        if not changed:
            break
    for name in depths:
        if name != ROOT and name not in parented:
            depths[name] = max(depths[name], 1)
    return depths


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _hosts(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, dict):
        return {name: _mapping(variables) for name, variables in value.items()}
    if isinstance(value, list):
        return {str(name): {} for name in value}
    return {}


def _children(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return {str(name): None for name in value}
    return {}
