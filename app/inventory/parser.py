# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading an inventory back into the model.

The repository holds YAML, not a model, and that is deliberate: the file is the
product, and a second serialisation of the same state would be a second thing
to keep in sync. Parsing back is the price, and it has one rule that matters:
a variable this service does not know about is kept, not dropped.

Variables are read **resolved**, the way Ansible resolves them, so a value the
file holds on a group reaches the form that edits it. Reading host variables
alone was how a real cluster inventory arrived in the UI with an empty
administration interface: that file keeps `network_interface` on
`cluster_machines`, shared by three machines, which is where a hand written
inventory keeps almost everything.
"""

from __future__ import annotations

from typing import Any

import yaml

from app.inventory.model import (
    GUEST_GROUP,
    Guest,
    Inventory,
    Mode,
    NodeConfig,
    Role,
)
from app.inventory.renderer import FIXED_HOST_VAR_NAMES, PTP_DOMAIN_ALIASES
from app.inventory.resolve import Group, resolve
from app.inventory.resolve import groups as declared_groups

# The variables the model owns. Anything else found on a host lands in `extra`
# and is written back out unchanged.
_MODELLED = frozenset(
    {
        "ansible_host",
        "network_interface",
        "subnet",
        "gateway_addr",
        "dns_servers",
        "ptp_interface",
        "ptp_domain_number",
        "ntp_servers",
        "admin_user",
        "grub_password",
        "isolcpus",
    }
)

# The same, for a guest. Everything else the `VMs` group carries lands in
# `extra`: `guest.xml.j2` alone reads some thirty variables, and modelling
# them here would be this service inventing an interface over a template a
# site is expected to replace.
_MODELLED_GUEST = frozenset(
    {
        "vm_disk",
        "vm_template",
        "xml_path",
        "force",
        "enable",
    }
)


class InvalidInventory(Exception):
    """The file is not an inventory this service can work with."""


def parse(document: str) -> Inventory:
    try:
        loaded = yaml.safe_load(document) or {}
    except yaml.YAMLError as error:
        raise InvalidInventory(f"The inventory is not valid YAML: {error}") from error
    if not isinstance(loaded, dict):
        raise InvalidInventory("The inventory must be a mapping of groups.")

    resolved = resolve(loaded)
    if not resolved:
        raise InvalidInventory("The inventory declares no host.")

    # Group membership is read from the whole file, so a group declared under
    # `all.children` counts exactly as much as one declared at the top level.
    # Both shapes are valid Ansible and a hand written file uses either.
    table = declared_groups(loaded)
    cluster_members = _members(table, "cluster_machines")
    observers = _members(table, "observers")
    # A member of `VMs` is a guest, and the machines are everything else.
    # Reading a guest as a machine is how a standalone deployment running two
    # VMs arrived here as three machines, two of them without an administration
    # interface, refused by the rule that a standalone inventory describes
    # exactly one machine.
    guests = _members(table, GUEST_GROUP)

    parsed: dict[str, NodeConfig] = {
        name: _node(name, variables, observers)
        for name, variables in resolved.items()
        if name not in guests
    }
    defined: dict[str, Guest] = {
        name: _guest(variables)
        for name, variables in resolved.items()
        if name in guests
    }

    return Inventory(
        mode=Mode.CLUSTER if cluster_members else Mode.STANDALONE,
        hosts=parsed,
        guests=defined,
    )


def _members(table: dict[str, Group], name: str) -> set[str]:
    """The hosts of a group and of every group below it."""
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


def _node(name: str, variables: dict[str, Any], observers: set[str]) -> NodeConfig:
    known = {name: variables.get(name) for name in _MODELLED if name in variables}
    extra = {
        name: value
        for name, value in variables.items()
        if name not in _MODELLED and name not in FIXED_HOST_VAR_NAMES
        # The aliases are derived from ptp_domain_number by the renderer, so
        # keeping them would duplicate them on the next write.
        and name not in PTP_DOMAIN_ALIASES
    }

    dns = known.get("dns_servers")
    ntp = known.get("ntp_servers")
    return NodeConfig(
        # The role is a group membership, not a variable: that is how the
        # reference inventory expresses it, and how the playbooks read it.
        role=Role.OBSERVER if name in observers else Role.HYPERVISOR,
        ansible_host=str(known.get("ansible_host") or ""),
        network_interface=str(known.get("network_interface") or ""),
        subnet=int(known.get("subnet") or 24),
        gateway_addr=_optional_str(known.get("gateway_addr")),
        dns_servers=_as_list(dns),
        ptp_interface=_optional_str(known.get("ptp_interface")),
        ptp_domain_number=_optional_int(known.get("ptp_domain_number")),
        ntp_servers=_as_list(ntp),
        admin_user=_optional_str(known.get("admin_user")),
        grub_password=_optional_str(known.get("grub_password")),
        isolcpus=_optional_str(known.get("isolcpus")),
        extra=extra,
    )


def _guest(variables: dict[str, Any]) -> Guest:
    """One VM entry, read the way the roles read it.

    Variables are taken resolved here too: the reference VM inventory keeps
    `ansible_user` on the group and a site keeps far more than that, and a
    guest read from its own lines alone would lose it on the way back out.
    """
    return Guest(
        vm_disk=_optional_str(variables.get("vm_disk")),
        vm_template=_optional_str(variables.get("vm_template")),
        xml_path=_optional_str(variables.get("xml_path")),
        force=bool(variables.get("force", False)),
        enable=bool(variables.get("enable", True)),
        extra={
            name: value
            for name, value in variables.items()
            if name not in _MODELLED_GUEST
        },
    )


def _group_hosts(group: Any) -> dict[str, Any]:
    if not isinstance(group, dict):
        return {}
    hosts = group.get("hosts")
    return hosts if isinstance(hosts, dict) else {}


def _group_host_names(group: Any) -> list[str]:
    return list(_group_hosts(group))


def _group_vars(group: Any) -> dict[str, Any]:
    if not isinstance(group, dict):
        return {}
    variables = group.get("vars")
    return variables if isinstance(variables, dict) else {}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
