# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Starting a guest, stopping one, refreshing a resource: the runtime plane.

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
    RECONFIGURE = "reconfigure"
    REFRESH = "refresh"
    REFRESH_ALL = "refresh_all"


@dataclass(frozen=True)
class ActionSpec:
    verb: str
    """What the button says."""
    disruption: str
    """What it acts on, in the sentence a confirmation carries."""
    subject: str = "guest"
    """What the name in the play is: a guest of the inventory, or a resource
    Pacemaker reports. They are usually the same object and never the same
    list, since a cluster carries resources no inventory declares. `cluster`
    means the action takes no name at all."""
    prefix: str = "vm"
    """What the run record calls this play, before the action's own name.

    A guest is `vm_start`; a Pacemaker resource is not always a guest, and the
    whole cluster is neither, so those say what they act on instead."""


_SPECS: dict[Action, ActionSpec] = {
    Action.START: ActionSpec(
        verb="Start",
        disruption=(
            "Starts the guest. In a cluster this asks Pacemaker to run it and "
            "Pacemaker chooses the node, which is not necessarily the one it "
            "last ran on."
        ),
    ),
    Action.RECONFIGURE: ActionSpec(
        verb="Apply",
        disruption=(
            "Stops the guest, removes its Pacemaker resource and creates it "
            "again from the metadata. That is what makes a metadata change "
            "take effect, and it is an outage: `enable` reads those keys only "
            "when the guest is not already a resource, so there is no way to "
            "apply one without the guest going down and coming back."
        ),
    ),
    Action.REFRESH_ALL: ActionSpec(
        verb="Refresh every resource",
        subject="cluster",
        prefix="cluster",
        disruption=(
            "Deletes the operation history of every resource on every node, "
            "failures included, and asks Pacemaker to probe them all again. "
            "This is the whole cluster at once rather than the one resource a "
            "failure is on: it costs a probe per resource per node, and on a "
            "large cluster that is a burst of monitor operations. What is "
            "running keeps running, and anything genuinely still broken fails "
            "again on the next probe."
        ),
    ),
    Action.REFRESH: ActionSpec(
        verb="Refresh",
        subject="resource",
        prefix="resource",
        disruption=(
            "Deletes the resource's operation history on every node, failures "
            "included, and asks Pacemaker to probe its real state again. It is "
            "the cluster's own recovery from a failure that has been dealt "
            "with: a resource held down by a fail count that reached the "
            "migration threshold can be placed again afterwards. A resource "
            "that is running keeps running, and one that is genuinely still "
            "broken fails again on the next probe."
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
    # Rebuilding the Pacemaker resource is a cluster act whatever the file
    # says: there is no resource on a standalone machine, and the metadata it
    # would be rebuilt from lives on an RBD image that machine has not got.
    # Refreshing is the same, for the first half of that reason.
    cluster = mode is Mode.CLUSTER or action in _CLUSTER_ONLY
    return PlaybookEntry(
        id=f"{detail.prefix}_{action.value}",
        playbook=f"{GENERATOR}.{detail.prefix}_{action.value}",
        title=_title(action, guest),
        targets=["cluster_machines[0]"] if cluster else ["standalone_machine"],
        # There is nothing to preview: the play makes one call and the answer
        # is what the cluster does with it.
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=detail.disruption,
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER if cluster else Precondition.STANDALONE,
        ],
        reviewed=True,
    )


def play(action: Action, guest: str, mode: Mode) -> str:
    """The one task play, as YAML.

    Dumped rather than templated, so a guest name cannot become YAML of its
    own. The caller checks the name against the guests this node knows about
    before it reaches here, and this is the second lock on the same door.
    """
    document = [
        {
            "name": _title(action, guest),
            "hosts": _hosts(Mode.CLUSTER if action in _CLUSTER_ONLY else mode),
            "gather_facts": False,
            "become": True,
            "tasks": _tasks(action, guest, mode),
        }
    ]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


# The actions that are cluster acts whatever the inventory's own mode says.
_CLUSTER_ONLY = (Action.RECONFIGURE, Action.REFRESH, Action.REFRESH_ALL)


def _title(action: Action, guest: str) -> str:
    """What the run is called, in the run list and in the play."""
    verb = _SPECS[action].verb
    return f"{verb} {guest}" if guest else verb


def _hosts(mode: Mode) -> str:
    # The same host `deploy_vms_cluster` plays. `cluster_vm` reaches Pacemaker,
    # which answers for the whole cluster, so which member drives is not a
    # decision anybody makes.
    if mode is Mode.CLUSTER:
        return "{{ groups['cluster_machines'][0] }}"
    return "standalone_machine"


def _tasks(action: Action, guest: str, mode: Mode) -> list[dict]:
    """The task or tasks the action is, named for the operator reading them."""
    title = _title(action, guest)
    if action in (Action.REFRESH, Action.REFRESH_ALL):
        # `crm resource refresh`, which is what an operator would type on the
        # machine, and `vm_manager` reaches Pacemaker through the same `crm`.
        # No module covers it: `cluster_vm` builds and moves guests, and a
        # resource is not always a guest.
        #
        # `argv` rather than a string, so no shell parses it and a resource
        # name holding a quote is one argument either way. The name is checked
        # against what the cluster reports before it arrives here.
        # A bare `crm resource refresh` is the whole cluster: no resource
        # named means every resource on every node.
        argv = ["crm", "resource", "refresh"]
        if action is Action.REFRESH:
            argv.append(guest)
        return [
            {
                "name": title,
                "ansible.builtin.command": {"argv": argv},
                # It writes to the CIB every time, so there is nothing for
                # Ansible to call unchanged, and saying so is honest.
                "changed_when": True,
            }
        ]
    if action is Action.RECONFIGURE:
        # Two calls and no logic between them. `disable` removes the Pacemaker
        # resource, `enable` builds it again, and `enable` is the only thing
        # that reads the `_` metadata keys.
        return [
            {
                "name": f"Remove the Pacemaker resource of {guest}",
                "seapath.ansible.cluster_vm": {
                    "name": guest,
                    "command": "disable",
                },
            },
            {
                "name": f"Create it again from the metadata of {guest}",
                "seapath.ansible.cluster_vm": {
                    "name": guest,
                    "command": "enable",
                },
            },
        ]
    return [{"name": title, **_task(action, guest, mode)}]


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
