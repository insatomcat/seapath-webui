# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What the vocabulary can say about a file somebody is typing.

An inventory is YAML with no schema. `cephadm_netwrok` is a name the file
accepts, `ansible-inventory --list` parses it, every rule in `validation.py`
passes, and the answer arrives three minutes into a convergence from a role
that read a variable nobody set. The same holds for `vm_disk` written on a
hypervisor: it is a guest's variable, the machine is not a guest, and nothing
reads it.

Neither is a reason to refuse a commit. A site's own variable is a legitimate
name this service has never read, and this returns remarks rather than
findings for that reason: the page shows them beside the file, and the operator
decides. Nothing here reaches `validate()`, so no commit is ever blocked by it.

Read off the document rather than off the model, the way `references.py` reads
it. The model keeps what it does not know in `extra` and loses which group a
variable was written on, and that is exactly what a remark has to name: one
misspelling on `all` is one mistake, and reporting it once per machine reads
like three.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.inventory.model import GUEST_GROUP
from app.inventory.resolve import ROOT, groups, members
from app.inventory.vocabulary import BY_NAME, Scope

# How close a name has to be before it is offered as the one that was meant.
# `difflib` scores on the whole string, so 0.8 keeps `cephadm_netwrok` for
# `cephadm_network` and drops the pairs that merely share a prefix, of which
# this vocabulary has many: `cluster_next_ip_addr` and `cluster_ip_addr` are a
# 0.86 match and two different addresses on two different machines.
_NEAR = 0.88


class Remark(BaseModel):
    """One thing worth saying about a variable, and where it is written."""

    kind: str
    """`unknown` or `misplaced`."""
    name: str
    where: list[str]
    """The groups and machines that write it, in the order the file does."""
    message: str
    suggestion: str | None = None
    """The name that was probably meant, when one is close enough."""


class Assistance(BaseModel):
    remarks: list[Remark]
    known: int
    """How many of the file's variables the vocabulary accounted for."""


def assist(document: str | dict[str, Any]) -> Assistance:
    """Every remark the vocabulary has about this document."""
    written = _written(document)
    remarks: list[Remark] = []
    known = 0

    for name, places in written.items():
        term = BY_NAME.get(name)
        if term is None:
            remarks.append(_unknown(name, places))
            continue
        known += 1
        misplaced = _misplaced(term.scope, places)
        if misplaced:
            remarks.append(_misplaced_remark(name, term.scope, misplaced))

    return Assistance(remarks=remarks, known=known)


@dataclass(frozen=True)
class _Place:
    """One spot in the file that writes a variable, and who it reaches.

    Both flags are false for an empty group, which the reference inventories
    carry to keep Ansible from warning about a group nobody declared. Such a
    spot reaches nothing and is never judged.
    """

    label: str
    guests: bool
    machines: bool


def _written(document: str | dict[str, Any]) -> dict[str, list[_Place]]:
    """Every variable the file sets, and where, in the order the file has them.

    A guest is told from a machine by membership of `VMs`, which is the rule
    the rest of this service uses: the group decides, and a host key under it
    is a libvirt domain rather than a machine. A group is judged by the hosts
    it reaches, so `cluster_VMs` and a `VMs` declared through `children` are
    covered without either being named here.
    """
    table = groups(document)
    guests = members(table, GUEST_GROUP) if GUEST_GROUP in table else set()

    written: dict[str, list[_Place]] = {}
    for group in table.values():
        reached = members(table, group.name)
        _collect(
            written,
            group.variables,
            _Place(_label(group.name), bool(reached & guests), bool(reached - guests)),
        )
        for host, variables in group.hosts.items():
            _collect(
                written, variables, _Place(host, host in guests, host not in guests)
            )
    return written


def _collect(
    written: dict[str, list[_Place]],
    variables: dict[str, Any],
    place: _Place,
) -> None:
    for name in variables:
        places = written.setdefault(name, [])
        if all(known.label != place.label for known in places):
            places.append(place)


def _label(name: str) -> str:
    return "the whole inventory" if name == ROOT else f"the {name} group"


def _unknown(name: str, places: list[_Place]) -> Remark:
    near = difflib.get_close_matches(name, list(BY_NAME), n=1, cutoff=_NEAR)
    suggestion = near[0] if near else None
    if suggestion:
        message = (
            f"No role reads {name}. Did you mean {suggestion}? "
            "A name this service has never read is written the same way a "
            "misspelling is, and only a run tells them apart."
        )
    else:
        message = (
            f"No role of the collection reads {name}. That is fine for a "
            "variable of your own, and it is what a misspelling looks like."
        )
    return Remark(
        kind="unknown",
        name=name,
        where=[place.label for place in places],
        message=message,
        suggestion=suggestion,
    )


def _misplaced(scope: Scope, places: list[_Place]) -> list[_Place]:
    """The spots that write this variable where nothing will read it.

    Reaching a reader is the test, rather than reaching only readers. A guest
    variable written on `all` is untidy and it does reach the guests, so it is
    left alone; written on the `hypervisors` group it reaches none, and that is
    the mistake worth a remark.

    Only the guest boundary is judged. Everything else is a matter of taste: a
    host variable written on a group sets it for every machine of that group,
    which is a legitimate way to write an inventory and what the reference
    cluster does with `isolcpus`.
    """
    if scope is Scope.CONNECTION:
        return []
    if scope is Scope.GUEST:
        return [place for place in places if place.machines and not place.guests]
    return [place for place in places if place.guests and not place.machines]


def _misplaced_remark(name: str, scope: Scope, places: list[_Place]) -> Remark:
    where = [place.label for place in places]
    if scope is Scope.GUEST:
        message = (
            f"{name} is a guest's variable, and it is written on a machine "
            "here. The deploy_vms roles read it off an entry of the VMs "
            "group, so nothing reads this one."
        )
    else:
        message = (
            f"{name} describes a machine, and it is written on a guest entry "
            "here. A guest is a libvirt domain, so nothing reads this one."
        )
    return Remark(kind="misplaced", name=name, where=where, message=message)
