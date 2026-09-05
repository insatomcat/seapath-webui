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

import re
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


# The Ceph pool and the image name `vm_manager` uses. Hardcoded there
# (`POOL_NAME = "rbd"`, `OS_DISK_PREFIX = "system_"`), so they are hardcoded
# here rather than guessed at, and named so the two can be compared.
CEPH_POOL = "rbd"
IMAGE_PREFIX = "system_"

# The variable a metadata play is told to write its answer into, filled by the
# run service with this run's own results directory.
RESULTS_VARIABLE = "webui_results_dir"

# What the play brings back, and what `app/services/metadata.py` reads.
RESULTS_FILE = "metadata.json"


def image_of(guest: str) -> str:
    """The RBD image holding a guest's system disk, and its metadata."""
    return f"{IMAGE_PREFIX}{guest}"


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
    cluster = mode is Mode.CLUSTER or action is Action.RECONFIGURE
    return PlaybookEntry(
        id=f"vm_{action.value}",
        playbook=f"{GENERATOR}.vm_{action.value}",
        title=f"{detail.verb} {guest}",
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
    title = f"{_SPECS[action].verb} {guest}"
    document = [
        {
            "name": title,
            "hosts": _hosts(Mode.CLUSTER if action is Action.RECONFIGURE else mode),
            "gather_facts": False,
            "become": True,
            "tasks": _tasks(action, guest, mode),
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


def _tasks(action: Action, guest: str, mode: Mode) -> list[dict]:
    """The task or tasks the action is, named for the operator reading them."""
    title = f"{_SPECS[action].verb} {guest}"
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


class MetadataOp(str, Enum):
    READ = "read"
    SET = "set"
    REMOVE = "remove"


# What an RBD metadata key may be called. `rbd` itself takes almost anything,
# and this is narrower on purpose: the keys SEAPATH uses are `_preferred_host`
# and its family, and a site's own are letters and digits. A key outside this
# is refused rather than sent, so what lands on an image stays greppable.
KEY = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Long enough for a pinning profile, which is the biggest value SEAPATH stores:
# a few lines of YAML under `_seapath_alloc`.
MAX_VALUE_BYTES = 64 * 1024


def metadata_entry(op: MetadataOp, guest: str) -> PlaybookEntry:
    """The catalogue shape of a metadata operation.

    Cluster only, and that is a property of the thing rather than a limit of
    this page: the metadata lives on an RBD image, and a standalone machine has
    no Ceph to hold one.
    """
    verb = {
        MetadataOp.READ: "Read the metadata of",
        MetadataOp.SET: "Change the metadata of",
        MetadataOp.REMOVE: "Remove a metadata key of",
    }[op]
    return PlaybookEntry(
        id=f"vm_metadata_{op.value}",
        playbook=f"{GENERATOR}.vm_metadata_{op.value}",
        title=f"{verb} {guest}",
        targets=["cluster_machines[0]"],
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=(
            "Reads the metadata of the guest's RBD image and changes nothing."
            if op is MetadataOp.READ
            else (
                "Writes the guest's RBD image metadata. The guest keeps "
                "running and keeps the configuration it started with: "
                "Pacemaker reads these keys when the resource is created, so "
                "nothing changes until the resource is created again."
            )
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER,
        ],
        results_variable=RESULTS_VARIABLE,
        reviewed=True,
    )


def metadata_play(op: MetadataOp, guest: str, key: str = "", value: str = "") -> str:
    """The play that reads, and optionally changes, one image's metadata.

    It reads before and after in the same run and brings both back, so
    "did this actually change anything" is answered by the image rather than
    by what a browser believed the value was a minute ago.

    Every `rbd` invocation is `argv`, a list, so no shell parses it and a value
    holding a quote or a newline is one argument either way. The key is checked
    against `KEY` before it reaches here, which is the other half of the same
    promise.
    """
    image = image_of(guest)

    def read() -> list[str]:
        # A fresh list each time: two tasks sharing one object make the dumper
        # write a YAML anchor, which is valid and unreadable in a play an
        # operator is expected to be able to inspect.
        return ["rbd", "-p", CEPH_POOL, "image-meta", "list", image, "--format", "json"]

    tasks: list[dict] = [
        {
            "name": f"Read the metadata of {image}",
            "ansible.builtin.command": {"argv": read()},
            "register": "webui_before",
            "changed_when": False,
        }
    ]

    if op is MetadataOp.SET:
        tasks.append(
            {
                "name": f"Set {key} on {image}",
                "ansible.builtin.command": {
                    "argv": [
                        "rbd",
                        "-p",
                        CEPH_POOL,
                        "image-meta",
                        "set",
                        image,
                        key,
                        value,
                    ]
                },
            }
        )
    elif op is MetadataOp.REMOVE:
        tasks.append(
            {
                "name": f"Remove {key} from {image}",
                "ansible.builtin.command": {
                    "argv": ["rbd", "-p", CEPH_POOL, "image-meta", "remove", image, key]
                },
            }
        )

    after = "webui_before"
    if op is not MetadataOp.READ:
        after = "webui_after"
        tasks.append(
            {
                "name": f"Read the metadata of {image} again",
                "ansible.builtin.command": {"argv": read()},
                "register": after,
                "changed_when": False,
            }
        )

    tasks.append(
        {
            "name": "Bring the metadata back",
            "ansible.builtin.copy": {
                "content": (
                    "{{ {'before': webui_before.stdout | from_json,"
                    f" 'after': {after}.stdout | from_json}} | to_nice_json }}}}"
                ),
                "dest": f"{{{{ {RESULTS_VARIABLE} }}}}/{RESULTS_FILE}",
                "mode": "0640",
            },
            # Onto the machine serving this page, which is where the answer is
            # read from. `become: false` because the run's own directory
            # belongs to this service.
            "delegate_to": "localhost",
            "become": False,
            "changed_when": False,
        }
    )

    return yaml.safe_dump(
        [
            {
                "name": f"Metadata of {image}",
                "hosts": _hosts(Mode.CLUSTER),
                "gather_facts": False,
                "become": True,
                "tasks": tasks,
            }
        ],
        sort_keys=False,
        default_flow_style=False,
    )
