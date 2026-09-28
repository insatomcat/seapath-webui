# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""PTP on a set of machines, as the variables the roles read.

Two roles read PTP from the inventory. `timemaster` takes `ptp_interface`, and
`ptp_vlanid` when the frames arrive tagged, and listens on
`<interface>.<vlan>`. Nothing creates that VLAN device, though: it is
`network_systemdnetworkd` that does, from two entries the site writes itself in
`custom_network` and `custom_netdev`. Forgetting them is the usual way a VLAN
PTP setup fails, with timemaster waiting forever for an interface that never
appears.

So a PTP setup here is four variables, and the two dictionaries are the hard
part. Ansible replaces a dictionary rather than merging it, so the entries have
to go into the very dictionary each machine receives, next to whatever else
the site declared in it, and nowhere that would hide it. This module works out
where that is, and refuses rather than guesses when the machines disagree.

The entries are the ones the reference setup writes, templated on the two
variables, so a group can carry them while each machine names its own port.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.inventory.editor import Scope
from app.inventory.model import GUEST_GROUP
from app.inventory.resolve import Group, depths, groups, members, resolve

INTERFACE = "ptp_interface"
VLAN = "ptp_vlanid"
NETWORK = "custom_network"
NETDEV = "custom_netdev"

# The systemd-networkd files the entries become. `01-` sorts before the files
# the role writes for the port, so this is the one systemd-networkd applies.
PORT_ENTRY = "01-physicalptpinterface"
_VLAN_ENTRY = re.compile(r"^00-vlan\d+$")

_TAGGED = "{{ ptp_interface + '.' + ptp_vlanid|string }}"

# What the kernel accepts as a name: IFNAMSIZ is 16 with the terminating zero,
# and a slash or a blank breaks the paths and the templates that carry it.
_INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


def vlan_entry(vlan: int) -> str:
    return f"00-vlan{vlan}"


def port_network() -> list[dict[str, Any]]:
    return [
        {"Match": [{"Name": "{{ ptp_interface }}"}, {"Type": "ether"}]},
        {
            "Network": [
                {"VLAN": _TAGGED},
                {
                    "Description": (
                        "ptp interface {{ ptp_interface }} vlan {{ ptp_vlanid }}"
                    )
                },
            ]
        },
    ]


def vlan_netdev() -> list[dict[str, Any]]:
    return [
        {"NetDev": [{"Name": _TAGGED}, {"Kind": "vlan"}]},
        {"VLAN": [{"Id": "{{ ptp_vlanid }}"}]},
    ]


class PtpRefused(Exception):
    """The setup cannot be written as asked, and the message says why."""


class PtpSetup(BaseModel):
    """What the wizard asks: where, which port on each machine, and the VLAN."""

    group: str | None = None
    """The group that carries what the machines share. None writes everything
    on each machine."""
    interfaces: dict[str, str]
    """Each machine and the port its PTP frames arrive on."""
    vlan: int | None = Field(default=None, ge=1, le=4094)


class MachinePtp(BaseModel):
    """What one machine receives now, and where each value is written."""

    host: str
    interface: str | None = None
    interface_on: str | None = None
    vlan: Any = None
    vlan_on: str | None = None
    vlan_entries: bool = False
    """Whether the dictionaries it receives already carry the VLAN device."""


class GroupChoice(BaseModel):
    name: str
    hosts: list[str]


class PtpSurvey(BaseModel):
    machines: list[MachinePtp] = Field(default_factory=list)
    groups: list[GroupChoice] = Field(default_factory=list)


@dataclass
class PtpPlan:
    """The writes, and what each machine must receive once they are made."""

    writes: list[tuple[Scope, dict[str, Any]]]
    intended: dict[str, dict[str, Any]]
    removals: dict[str, list[str]]
    message: str
    hosts: list[str] = field(default_factory=list)


def survey(document: str) -> PtpSurvey:
    table = groups(document)
    resolved = resolve(document)
    guests = members(table, GUEST_GROUP)
    machines = sorted(host for host in resolved if host not in guests)

    found = PtpSurvey()
    for host in machines:
        variables = resolved[host]
        vlan = variables.get(VLAN)
        found.machines.append(
            MachinePtp(
                host=host,
                interface=_text(variables.get(INTERFACE)),
                interface_on=_label(_source(table, host, INTERFACE)),
                vlan=vlan,
                vlan_on=_label(_source(table, host, VLAN)),
                vlan_entries=vlan is not None
                and PORT_ENTRY in _mapping(variables.get(NETWORK))
                and vlan_entry(_int(vlan)) in _mapping(variables.get(NETDEV)),
            )
        )

    # A group is offered when it holds machines and no guest: writing
    # `ptp_interface` on a group holding guests would hand it to the guests
    # as well, and `all` always does when there are any.
    for name in sorted(table):
        held = members(table, name)
        if held and not held & guests:
            found.groups.append(GroupChoice(name=name, hosts=sorted(held)))
    return found


def plan(document: str, setup: PtpSetup) -> PtpPlan:
    table = groups(document)
    resolved = resolve(document)
    guests = members(table, GUEST_GROUP)
    targets = sorted(setup.interfaces)

    if not targets:
        raise PtpRefused("Choose at least one machine.")
    for host in targets:
        if host not in resolved:
            raise PtpRefused(f"{host} is not in the inventory.")
        if host in guests:
            raise PtpRefused(f"{host} is a guest, and PTP is a machine's setting.")
        if not _INTERFACE_NAME.match(setup.interfaces[host].strip()):
            raise PtpRefused(
                f"{setup.interfaces[host]!r} is not an interface name for {host}."
            )
    interfaces = {host: name.strip() for host, name in setup.interfaces.items()}

    group = setup.group
    if group is not None:
        if group not in table:
            raise PtpRefused(f"The inventory declares no group called {group}.")
        held = members(table, group)
        if held & guests:
            raise PtpRefused(f"{group} holds guests, and PTP is a machine's setting.")
        if held != set(targets):
            raise PtpRefused(
                f"Written on {group}, the setup reaches {_names(held)}, so each "
                "of them needs an interface."
            )

    writes: dict[Scope, dict[str, Any]] = {}
    removals: dict[str, list[str]] = {}
    intended: dict[str, dict[str, Any]] = {host: {} for host in targets}
    ordering = depths(table)

    def write(scope: Scope, variable: str, value: Any) -> None:
        held = writes.setdefault(scope, {})
        if variable in held and held[variable] != value:
            raise PtpRefused(
                f"{variable} would need two different values on {scope.name}. "
                "Choose the machines rather than the group."
            )
        held[variable] = value

    def remove_on_hosts(variable: str) -> None:
        for host in targets:
            if variable in _own(table, host):
                removals.setdefault(host, []).append(variable)

    shared = group is not None and len(set(interfaces.values())) == 1
    for host in targets:
        intended[host][INTERFACE] = interfaces[host]
        intended[host][VLAN] = setup.vlan
    if shared:
        write(Scope("group", group), INTERFACE, interfaces[targets[0]])
        remove_on_hosts(INTERFACE)
        _refuse_shadows(table, ordering, targets, INTERFACE, group)
    else:
        for host in targets:
            write(Scope("host", host), INTERFACE, interfaces[host])
        if group is not None and INTERFACE in table[group].variables:
            # Every machine it reached now carries its own, so the group's
            # value would only be a second answer nobody reads.
            write(Scope("group", group), INTERFACE, None)

    if group is not None:
        write(Scope("group", group), VLAN, setup.vlan)
        remove_on_hosts(VLAN)
        _refuse_shadows(table, ordering, targets, VLAN, group)
    else:
        for host in targets:
            # Removing what a machine does not hold is left out: a machine
            # declared only by name has no entry to remove it from.
            if setup.vlan is not None or VLAN in _own(table, host):
                write(Scope("host", host), VLAN, setup.vlan)

    for variable, entry, value in (
        (NETWORK, PORT_ENTRY, port_network),
        (NETDEV, None, vlan_netdev),
    ):
        for host in targets:
            before = resolved[host].get(variable)
            if before is not None and not isinstance(before, dict):
                raise PtpRefused(
                    f"{variable} on {host} is not a dictionary, so the PTP "
                    "entries cannot be added to it."
                )
            after = _without_ours(before or {})
            if setup.vlan is not None:
                after[entry or vlan_entry(setup.vlan)] = value()
            if after == (before or {}):
                continue
            intended[host][variable] = after or None
            scope = _dictionary_scope(
                table, ordering, resolved, targets, host, variable, group
            )
            write(scope, variable, after or None)

    return PtpPlan(
        writes=list(writes.items()),
        intended=intended,
        removals=removals,
        message=_message(interfaces, setup, group),
        hosts=targets,
    )


def _dictionary_scope(
    table: dict[str, Group],
    ordering: dict[str, int],
    resolved: dict[str, dict[str, Any]],
    targets: list[str],
    host: str,
    variable: str,
    group: str | None,
) -> Scope:
    """Where a new value of the dictionary reaches this machine and no other.

    The chosen scope, unless the machine receives the dictionary from somewhere
    that overrides it: its own entry, or a group applied after the chosen one.
    Written on the chosen scope there, the entries would be hidden. That other
    place is written instead, as long as it reaches none of the machines left
    out of the setup, which would receive entries naming a `ptp_interface`
    they do not have.
    """
    source = _source(table, host, variable)
    base = Scope("group", group) if group is not None else Scope("host", host)
    if source is None or not _overrides(ordering, source, base):
        return base
    if source.is_group:
        reached = {
            other
            for other in resolved
            if other not in targets and _source(table, other, variable) == source
        }
        if reached:
            raise PtpRefused(
                f"{host} receives {variable} from the group {source.name}, "
                f"which also gives it to {_names(reached)}. Include them, or "
                f"move {variable} off {source.name} first."
            )
    return source


def _refuse_shadows(
    table: dict[str, Group],
    ordering: dict[str, int],
    targets: list[str],
    variable: str,
    group: str,
) -> None:
    """A group applied after the chosen one, still holding its own value."""
    base = Scope("group", group)
    for host in targets:
        source = _source(table, host, variable)
        if (
            source is not None
            and source.is_group
            and _overrides(ordering, source, base)
        ):
            raise PtpRefused(
                f"{variable} is also written on the group {source.name}, which "
                f"overrides {group} for {host}. Choose the machines rather than "
                f"the group, or remove it from {source.name} first."
            )


def _source(table: dict[str, Group], host: str, variable: str) -> Scope | None:
    """Where the value this machine receives is written, the way Ansible picks."""
    if variable in _own(table, host):
        return Scope("host", host)
    ordering = depths(table)
    containing = sorted(
        (
            group
            for group in table.values()
            if host in members(table, group.name) and variable in group.variables
        ),
        key=lambda group: (ordering[group.name], group.name),
    )
    return Scope("group", containing[-1].name) if containing else None


def _overrides(ordering: dict[str, int], source: Scope, base: Scope) -> bool:
    """Whether a value written on `source` wins over one written on `base`."""
    if source == base:
        return False
    if not source.is_group:
        return True
    if not base.is_group:
        return False
    return (ordering[source.name], source.name) > (ordering[base.name], base.name)


def _own(table: dict[str, Group], host: str) -> dict[str, Any]:
    own: dict[str, Any] = {}
    for group in table.values():
        own.update(group.hosts.get(host, {}))
    return own


def _without_ours(entries: dict[str, Any]) -> dict[str, Any]:
    """The dictionary without the entries a previous PTP setup wrote."""
    return {
        key: value
        for key, value in entries.items()
        if key != PORT_ENTRY
        and not (_VLAN_ENTRY.match(str(key)) and INTERFACE in repr(value))
    }


def _message(interfaces: dict[str, str], setup: PtpSetup, group: str | None) -> str:
    ports = sorted(set(interfaces.values()))
    port = ports[0] if len(ports) == 1 else "each machine's port"
    tagged = f", VLAN {setup.vlan}" if setup.vlan is not None else ", untagged"
    where = f"the group {group}" if group else _names(set(interfaces))
    body = (
        "Read by timemaster, and by network_systemdnetworkd for the VLAN "
        "device it listens on."
        if setup.vlan is not None
        else "Read by timemaster."
    )
    return f"time: PTP on {port}{tagged}, for {where}\n\n{body}"


def _names(hosts: set[str]) -> str:
    return ", ".join(sorted(hosts))


def _label(scope: Scope | None) -> str | None:
    if scope is None:
        return None
    return scope.name if scope.is_group else "the machine"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
