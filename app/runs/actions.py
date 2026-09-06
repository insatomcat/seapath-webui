# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Starting a guest, moving one, putting a node in standby: the runtime plane.

Everything else this service runs is a whole playbook of the collection, for
the reason D8 gives: the tags of `seapath-ansible` were never designed as a
public interface, and a tag selector produces combinations nobody has executed.

A runtime action is a different shape and D30 settles it. It is one task
calling an upstream module by its documented interface, one command value at a
time, and the module is the same `cluster_vm` that `deploy_vms_cluster` calls.
The play is generated into the run's own staged tree and the run is otherwise
an ordinary one: the same lock, the same event stream, the same record, the
same SSH path. Nothing here reaches a machine except through `ansible-runner`.

Where no module covers the act, the task is the `crm` command an operator
would type on the machine, with `argv` so no shell parses it and the name
checked against what the cluster reported before it arrives here. Placement is
that case and [D34](../../docs/decisions.md) has its bounds: a move writes the
same `cli-prefer` constraint `preferred_host` writes, because upstream
implements that field by running this very command.

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
    UNIT_START = "unit_start"
    UNIT_STOP = "unit_stop"
    RESOURCE_START = "resource_start"
    RESOURCE_STOP = "resource_stop"
    MOVE = "move"
    CLEAR = "clear"
    STANDBY = "standby"
    ONLINE = "online"


@dataclass(frozen=True)
class ActionSpec:
    verb: str
    """What the button says."""
    disruption: str
    """What it acts on, in the sentence a confirmation carries."""
    subject: str = "guest"
    """What the name in the play is: a guest of the inventory, a resource
    Pacemaker reports, or a cluster member. The first two are usually the same
    object and never the same list, since a cluster carries resources no
    inventory declares. `cluster` means the action takes no name at all."""
    prefix: str = "vm"
    """What the run record calls this play, before the action's own name.

    A guest is `vm_start`; a Pacemaker resource is not always a guest, and the
    whole cluster is neither, so those say what they act on instead."""
    record: str = ""
    """What the run is filed under, when `<prefix>_<action>` would repeat
    itself. `unit_start` rather than `unit_unit_start`."""
    on_host: bool = False
    """The play runs on one named machine rather than on a group of the
    inventory. A quadlet is a systemd unit on the machine holding it, so the
    machine is part of the act and part of the confirmation."""
    title: str = ""
    """What the run is called, when `<verb> <name>` does not read as English.

    A format string over `name` and `node`. "Standby elabo2" and "Move
    vm-guest1 elabo2" are the two the verb alone produces, and an operator
    scanning the run list reads the title before anything else."""


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
    Action.UNIT_START: ActionSpec(
        verb="Start",
        subject="container",
        prefix="unit",
        record="unit_start",
        on_host=True,
        disruption=(
            "Starts the systemd unit podman's generator wrote from the "
            "quadlet, on that machine. The container comes up with whatever "
            "the file on the machine says, which is the version the last "
            "convergence uploaded."
        ),
    ),
    Action.UNIT_STOP: ActionSpec(
        verb="Stop",
        subject="container",
        prefix="unit",
        record="unit_stop",
        on_host=True,
        disruption=(
            "Stops the container on that machine, and whatever it was serving "
            "stops with it. It is a stop and not a way of switching the "
            "container off: a quadlet carrying an `[Install]` section is "
            "started again at the next boot, and removing it is an inventory "
            "change."
        ),
    ),
    Action.RESOURCE_START: ActionSpec(
        verb="Start",
        subject="resource",
        prefix="resource",
        record="resource_start",
        disruption=(
            "Clears the resource's target role, so Pacemaker starts it and "
            "chooses the node. Which member it lands on is the cluster's "
            "decision and not this one."
        ),
    ),
    Action.RESOURCE_STOP: ActionSpec(
        verb="Stop",
        subject="resource",
        prefix="resource",
        record="resource_stop",
        disruption=(
            "Sets the resource's target role to Stopped, so Pacemaker stops it "
            "wherever it is running and leaves it down until it is started "
            "again, a node failure included. Whatever it was serving stops "
            "with it."
        ),
    ),
    Action.MOVE: ActionSpec(
        verb="Move",
        subject="resource",
        prefix="resource",
        record="resource_move",
        title="Move {name} to {node}",
        disruption=(
            "Writes the cli-prefer constraint that names the node, which is "
            "the same object `preferred_host` produces and written by the same "
            "command. A guest whose image allows live migration moves without "
            "stopping; one that does not is stopped where it runs and started "
            "on the other node, and whatever it was serving stops in between. "
            "The constraint stays until it is returned, and it overrides the "
            "placement the inventory declares for as long as it is there."
        ),
    ),
    Action.CLEAR: ActionSpec(
        verb="Return",
        subject="resource",
        prefix="resource",
        record="resource_clear",
        title="Return {name} to the placement the inventory declares",
        disruption=(
            "Removes the cli-prefer constraint, and writes the placement the "
            "inventory declares back where there is one. Pacemaker may move "
            "the resource as a result, at the same cost the move had."
        ),
    ),
    Action.STANDBY: ActionSpec(
        verb="Put in standby",
        subject="node",
        prefix="node",
        record="node_standby",
        title="Put {name} in standby",
        disruption=(
            "Pacemaker moves every resource off that machine and places "
            "nothing there until it is brought back online. It is what an "
            "operator does before rebooting a hypervisor, and on a live "
            "cluster it moves every guest the node was running: those whose "
            "image allows live migration move without stopping, the rest are "
            "stopped there and started elsewhere. Quorum is untouched, because "
            "a node in standby is still a Corosync member and still votes."
        ),
    ),
    Action.ONLINE: ActionSpec(
        verb="Bring online",
        subject="node",
        prefix="node",
        record="node_online",
        title="Bring {name} back online",
        disruption=(
            "Ends the standby, so Pacemaker may place resources on that "
            "machine again. What moves back is Pacemaker's decision and not "
            "this one: a resource with no constraint holding it elsewhere may "
            "well stay where it is."
        ),
    ),
}


def spec(action: Action) -> ActionSpec:
    return _SPECS[action]


def record(action: Action) -> str:
    """What the run is filed under, which is also the generated play's name."""
    detail = _SPECS[action]
    return detail.record or f"{detail.prefix}_{action.value}"


def entry(
    action: Action, guest: str, mode: Mode, host: str = "", node: str = ""
) -> PlaybookEntry:
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
    identifier = record(action)
    requires = [Precondition.INVENTORY_VALID, Precondition.SELF_TRUST]
    if not detail.on_host:
        # A quadlet is a systemd unit on the machine that holds it, in either
        # mode, so an action on one asks for neither. Nor does it ask for
        # PEER_REACHABLE: this play names one machine, and a container on this
        # node must stay startable while another node is down.
        requires.append(Precondition.CLUSTER if cluster else Precondition.STANDALONE)
    return PlaybookEntry(
        id=identifier,
        playbook=f"{GENERATOR}.{identifier}",
        title=_title(action, guest, host, node),
        targets=[host] if detail.on_host else _targets(cluster),
        # There is nothing to preview: the play makes one call and the answer
        # is what the cluster does with it.
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=detail.disruption,
        requires=requires,
        reviewed=True,
    )


def _targets(cluster: bool) -> list[str]:
    return ["cluster_machines[0]"] if cluster else ["standalone_machine"]


def play(action: Action, guest: str, mode: Mode, host: str = "", node: str = "") -> str:
    """The one task play, as YAML.

    Dumped rather than templated, so a guest name cannot become YAML of its
    own. The caller checks the name against the guests this node knows about
    before it reaches here, and this is the second lock on the same door.
    """
    document = [
        {
            "name": _title(action, guest, host, node),
            "hosts": _hosts(Mode.CLUSTER if action in _CLUSTER_ONLY else mode, host),
            "gather_facts": False,
            "become": True,
            "tasks": _tasks(action, guest, mode, node),
        }
    ]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


# The actions that are cluster acts whatever the inventory's own mode says. A
# Pacemaker resource is the plainest case of it: on a standalone machine there
# is no cluster to hold one.
_CLUSTER_ONLY = (
    Action.RECONFIGURE,
    Action.REFRESH,
    Action.REFRESH_ALL,
    Action.RESOURCE_START,
    Action.RESOURCE_STOP,
    Action.MOVE,
    Action.CLEAR,
    Action.STANDBY,
    Action.ONLINE,
)


def _title(action: Action, guest: str, host: str = "", node: str = "") -> str:
    """What the run is called, in the run list and in the play."""
    detail = _SPECS[action]
    verb = detail.verb
    if detail.title and guest:
        return detail.title.format(name=guest, node=node)
    if not guest:
        return verb
    # The machine is part of the act for a unit: the same container is a unit
    # on each machine the inventory sends it to, and starting it on one of them
    # says nothing about the others.
    return f"{verb} {guest} on {host}" if host else f"{verb} {guest}"


def _hosts(mode: Mode, host: str = "") -> str:
    if host:
        return host
    # The same host `deploy_vms_cluster` plays. `cluster_vm` reaches Pacemaker,
    # which answers for the whole cluster, so which member drives is not a
    # decision anybody makes.
    if mode is Mode.CLUSTER:
        return "{{ groups['cluster_machines'][0] }}"
    return "standalone_machine"


def _tasks(action: Action, guest: str, mode: Mode, node: str = "") -> list[dict]:
    """The task or tasks the action is, named for the operator reading them."""
    title = _title(action, guest, node=node)
    if action in (Action.UNIT_START, Action.UNIT_STOP):
        # The unit podman's generator wrote from the quadlet, asked of systemd
        # through the module that owns units. `daemon_reload` is deliberately
        # absent: regenerating the units is what the convergence does after it
        # uploads the files, and a start that quietly rewrote them would be a
        # configuration act hiding inside a runtime one.
        return [
            {
                "name": title,
                "ansible.builtin.systemd_service": {
                    "name": guest,
                    "state": ("started" if action is Action.UNIT_START else "stopped"),
                },
            }
        ]
    if action in (Action.RESOURCE_START, Action.RESOURCE_STOP):
        # `crm resource start|stop`, which writes the resource's target role
        # into the CIB and leaves the placement to Pacemaker. The same shape as
        # the refresh above and for the same reason: no module covers a
        # resource that is not a guest, `argv` keeps a shell out of it, and the
        # name has been checked against what the cluster reported.
        verb = "start" if action is Action.RESOURCE_START else "stop"
        return [
            {
                "name": title,
                "ansible.builtin.command": {"argv": ["crm", "resource", verb, guest]},
                # It writes to the CIB whatever the resource was doing, so
                # there is nothing here for Ansible to call unchanged.
                "changed_when": True,
            }
        ]
    if action in (Action.STANDBY, Action.ONLINE):
        # `crm node standby|online`, the pair an operator types before and
        # after taking a hypervisor down. It writes the node's `standby`
        # attribute into the CIB and lets Pacemaker place what it holds, so it
        # is the whole cluster's decision made from one node, exactly as a
        # refresh is. No module covers it: `cluster_vm`'s nineteen commands
        # are all about one guest, and a node is not a guest.
        verb = "standby" if action is Action.STANDBY else "online"
        return [
            {
                "name": title,
                "ansible.builtin.command": {"argv": ["crm", "node", verb, guest]},
                # The attribute is written whatever the node was doing, so
                # there is nothing here for Ansible to call unchanged.
                "changed_when": True,
            }
        ]
    if action is Action.MOVE:
        # `crm resource move <resource> <node>`, which writes the
        # `cli-prefer-<resource>` location constraint. The same object
        # `preferred_host` produces: `vm_manager` implements that field by
        # calling this very command, so a deliberate move introduces no kind of
        # rule the cluster did not already carry. The node is always named,
        # because a bare `crm resource move` bans the resource from the node it
        # is on, which is a different act with the same words.
        return [
            {
                "name": title,
                "ansible.builtin.command": {
                    "argv": ["crm", "resource", "move", guest, node]
                },
                "changed_when": True,
            }
        ]
    if action is Action.CLEAR:
        # `crm resource clear` removes the `cli-prefer-<resource>` constraint,
        # and that is the whole difficulty: the constraint a move overwrote was
        # the one `preferred_host` had put there, so a bare clear would drop a
        # declared placement and leave nothing saying it had gone. The second
        # task writes it back. `node` is what the inventory declares, empty for
        # a resource it says nothing about.
        tasks = [
            {
                "name": title,
                "ansible.builtin.command": {
                    "argv": ["crm", "resource", "clear", guest]
                },
                "changed_when": True,
            }
        ]
        if node:
            tasks.append(
                {
                    "name": f"Write back the placement the inventory declares "
                    f"for {guest}",
                    "ansible.builtin.command": {
                        "argv": ["crm", "resource", "move", guest, node]
                    },
                    "changed_when": True,
                }
            )
        return tasks
    if action in (Action.REFRESH, Action.REFRESH_ALL):
        # `crm resource refresh`, which is what an operator would type on the
        # machine, and `vm_manager` reaches Pacemaker through the same `crm`.
        # No module covers it: `cluster_vm` creates guests and starts them, and
        # a resource is not always a guest.
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
