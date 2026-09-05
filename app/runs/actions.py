# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Starting and stopping a guest, which is the runtime plane.

Everything else this service runs is a whole playbook of the collection, for
the reason D8 gives: the tags of `seapath-ansible` were never designed as a
public interface, and a tag selector produces combinations nobody has executed.

A runtime action is a different shape and D30 settles it. It is one task
calling an upstream module by its documented interface, one command value at a
time, and the module is the same `cluster_vm` that `deploy_vms_cluster` calls.
The play is generated into the run's own staged tree and the run is otherwise
an ordinary one: the same lock, the same event stream, the same record, the
same SSH path. Nothing here reaches a machine except through `ansible-runner`.

The alternative was the libvirt socket and `vm_manager` in process. It reaches
the local node alone, and the guests of a three node cluster move between all
three, so the page would answer for one machine and stay silent about the
others.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import yaml

from app.inventory.model import Mode
from app.runs.catalogue import PlaybookEntry, Precondition, Preview, Reboots

# What the recorded playbook name says. Deliberately not `seapath.ansible.*`:
# this play was written here, and a run record that claimed otherwise would
# make the collection answer for it.
GENERATOR = "seapath-webui"


class Action(str, Enum):
    START = "start"
    STOP = "stop"


@dataclass(frozen=True)
class ActionSpec:
    verb: str
    """What the button says."""
    disruption: str
    """What it does to the guest, in the sentence a confirmation carries."""


_SPECS: dict[Action, ActionSpec] = {
    Action.START: ActionSpec(
        verb="Start",
        disruption=(
            "Starts the guest. In a cluster this asks Pacemaker to run it and "
            "Pacemaker chooses the node, which is not necessarily the one it "
            "last ran on."
        ),
    ),
    Action.STOP: ActionSpec(
        verb="Stop",
        disruption=(
            "Stops the guest, and whatever it was serving stops with it. In a "
            "cluster the resource is disabled as well as stopped, so Pacemaker "
            "leaves it down until it is started again, a node failure "
            "included."
        ),
    ),
}


def spec(action: Action) -> ActionSpec:
    return _SPECS[action]


def entry(action: Action, guest: str, mode: Mode) -> PlaybookEntry:
    """The catalogue shape of one action, built for one guest.

    It is never in the catalogue and never offered on the Deployment page: it
    names a guest, and a run that plays every machine of the inventory is a
    different act from one that starts a VM. What it exists for is the
    preconditions, the lock and the record, which a runtime action wants
    exactly as a convergence does.
    """
    detail = _SPECS[action]
    return PlaybookEntry(
        id=f"vm_{action.value}",
        playbook=f"{GENERATOR}.vm_{action.value}",
        title=f"{detail.verb} {guest}",
        targets=(
            ["cluster_machines[0]"] if mode is Mode.CLUSTER else ["standalone_machine"]
        ),
        # There is nothing to preview: the play makes one call and the answer
        # is what the cluster does with it.
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=detail.disruption,
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER if mode is Mode.CLUSTER else Precondition.STANDALONE,
        ],
        reviewed=True,
    )


def play(action: Action, guest: str, mode: Mode) -> str:
    """The one task play, as YAML.

    Dumped rather than templated, so a guest name cannot become YAML of its
    own. The caller checks the name against the guests this node knows about
    before it reaches here, and this is the second lock on the same door.
    """
    title = f"{_SPECS[action].verb} {guest}"
    document = [
        {
            "name": title,
            "hosts": _hosts(mode),
            "gather_facts": False,
            "become": True,
            "tasks": [{"name": title, **_task(action, guest, mode)}],
        }
    ]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def _hosts(mode: Mode) -> str:
    # The same host `deploy_vms_cluster` plays. `cluster_vm` reaches Pacemaker,
    # which answers for the whole cluster, so which member drives is not a
    # decision anybody makes.
    if mode is Mode.CLUSTER:
        return "{{ groups['cluster_machines'][0] }}"
    return "standalone_machine"


def _task(action: Action, guest: str, mode: Mode) -> dict:
    if mode is Mode.CLUSTER:
        return {
            "seapath.ansible.cluster_vm": {
                "name": guest,
                "command": action.value,
            }
        }
    # A standalone machine has no Pacemaker, so the guest is a libvirt domain
    # and `community.libvirt.virt` is what `deploy_vms_standalone` already
    # uses. `shutdown` asks the guest through ACPI rather than cutting its
    # power, which is why a guest that ignores ACPI keeps running and the page
    # says so.
    return {
        "community.libvirt.virt": {
            "name": guest,
            "state": "running" if action is Action.START else "shutdown",
        }
    }
