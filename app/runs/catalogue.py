# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The catalogue of docs/playbooks.md.

Adding an entry here is a deliberate act, not a consequence of a playbook
existing upstream. What the UI runs is a whole playbook, never a free form
selection of tags: the tags in `seapath-ansible` were not designed as a public
interface, and a tag selector produces combinations nobody has ever run.

`targets` is copied from the playbook's own `hosts:` lines and is not a
parameter a caller can override. Narrowing `cluster_setup_ha.yaml` to one
member of three would be accepted by Ansible and would mean nothing.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

# The collection installed in the image. Playbooks are addressed by their fully
# qualified name, which is what makes "the UI runs what the CI tests" literally
# true rather than nearly true.
COLLECTION = "seapath.ansible"


class Preview(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


class Reboots(str, Enum):
    NO = "no"
    YES = "yes"
    GATED = "gated"
    """Rebooted unless a variable says otherwise."""


class Precondition(str, Enum):
    INVENTORY_VALID = "inventory_valid"
    SELF_TRUST = "self_trust"
    PEER_REACHABLE = "peer_reachable"
    """Every machine the run will play can be reached from this one.

    A run plays every host the inventory declares, since the adapter passes no
    `--limit`, and this node starts life with an SSH trust with itself alone.
    Launching against three machines with nothing to reach the other two
    produces a run that dies on `unreachable` after the operator has confirmed
    a disruptive convergence, which is a late and expensive way to learn it.
    """
    STANDALONE = "standalone"
    CLUSTER = "cluster"
    PLAYBOOK_PRESENT = "playbook_present"
    """The playbook exists in the collection this image ships.

    Not a formality. The catalogue is written against a version of
    `seapath-ansible`, and a SEAPATH release can add or rename a playbook: this
    service and the collection move independently, which is exactly why the
    collection version is part of the image identity. An entry the shipped
    collection does not have is reported as unavailable with that reason,
    rather than offered as a button that fails at the first task.
    """


class VariableSpec(BaseModel):
    name: str
    type: str
    description: str
    required: bool = False


class PlaybookEntry(BaseModel):
    id: str
    playbook: str
    title: str
    targets: list[str]
    preview: Preview
    reboots: Reboots
    reboot_variable: str | None = None
    disruption: str
    requires: list[Precondition] = Field(default_factory=list)
    variables: list[VariableSpec] = Field(default_factory=list)
    notes: str = ""

    @property
    def previewable(self) -> bool:
        # A `none` playbook offers no preview button at all, rather than a
        # button that lies.
        return self.preview is not Preview.NONE


_SKIP_REBOOT = VariableSpec(
    name="skip_reboot_setup",
    type="boolean",
    description=(
        "Converge without rebooting. The configuration is not fully applied "
        "until a reboot happens, and the node view keeps saying so."
    ),
)

_MACHINE_TARGETS = ["cluster_machines", "standalone_machine"]

CATALOGUE: tuple[PlaybookEntry, ...] = (
    PlaybookEntry(
        id="seapath_setup_main",
        playbook=f"{COLLECTION}.seapath_setup_main",
        title="Configure this machine",
        targets=[*_MACHINE_TARGETS, "VMs"],
        preview=Preview.PARTIAL,
        reboots=Reboots.GATED,
        reboot_variable="skip_reboot_setup",
        disruption=(
            "The full convergence: prerequisites, network, time, libvirt, "
            "monitoring and real time tuning. On a live machine it restarts "
            "whatever the roles decide to restart, and it reboots at the end "
            "unless you ask it not to."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        variables=[_SKIP_REBOOT],
        notes=(
            "This is the commissioning path and what the CI runs, which makes "
            "it the granularity with evidence behind it."
        ),
    ),
    PlaybookEntry(
        id="seapath_setup_network",
        playbook=f"{COLLECTION}.seapath_setup_network",
        title="Apply the network configuration",
        targets=list(_MACHINE_TARGETS),
        preview=Preview.PARTIAL,
        reboots=Reboots.YES,
        disruption=(
            "The playbook most likely to cut the connection under the run. "
            "Applies only when apply_network_config is true."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        notes=(
            "Run from another node when there is one. Launched from the "
            "machine it reconfigures, the run will very likely be interrupted."
        ),
    ),
    PlaybookEntry(
        id="seapath_setup_timemaster",
        playbook=f"{COLLECTION}.seapath_setup_timemaster",
        title="Apply the time synchronisation",
        targets=list(_MACHINE_TARGETS),
        preview=Preview.FULL,
        reboots=Reboots.NO,
        disruption="Restarts timemaster, which briefly interrupts PTP.",
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
    ),
    PlaybookEntry(
        id="seapath_setup_libvirt",
        playbook=f"{COLLECTION}.seapath_setup_libvirt",
        title="Apply the libvirt configuration",
        targets=["hypervisors"],
        preview=Preview.PARTIAL,
        reboots=Reboots.NO,
        disruption="Restarts libvirt. Running guests keep running.",
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
    ),
    PlaybookEntry(
        id="seapath_setup_prometheus_exporters",
        playbook=f"{COLLECTION}.seapath_setup_prometheus_exporters",
        title="Apply the Prometheus exporters",
        targets=list(_MACHINE_TARGETS),
        preview=Preview.FULL,
        reboots=Reboots.NO,
        disruption="Restarts the exporters. Monitoring has a gap, nothing else.",
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
    ),
    PlaybookEntry(
        id="seapath_setup_snmp",
        playbook=f"{COLLECTION}.seapath_setup_snmp",
        title="Apply the SNMP configuration",
        targets=list(_MACHINE_TARGETS),
        preview=Preview.FULL,
        reboots=Reboots.NO,
        disruption="Restarts snmpd.",
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
    ),
    PlaybookEntry(
        id="seapath_setup_deploy_seapath_alloc",
        playbook=f"{COLLECTION}.seapath_setup_deploy_seapath_alloc",
        title="Apply the dynamic CPU pinning",
        targets=["hypervisors"],
        preview=Preview.PARTIAL,
        reboots=Reboots.NO,
        disruption=(
            "Real time relevant. Changes how guest threads are pinned to "
            "isolated CPUs, which is what the latency guarantee rests on."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
    ),
    PlaybookEntry(
        id="seapath_setup_hardening",
        playbook=f"{COLLECTION}.seapath_setup_hardening",
        title="Harden this machine",
        targets=[*_MACHINE_TARGETS, "VMs"],
        preview=Preview.PARTIAL,
        reboots=Reboots.YES,
        disruption=(
            "Ends with a reboot of every host. Sets PermitRootLogin no and "
            "restricts sshd to the administration and cluster addresses."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        notes=(
            "Offered only once the rest converges cleanly. This is the reason "
            "the SSH trust targets the `ansible` account and not root."
        ),
    ),
    # Cluster entries. Listed so an operator can see what exists and why it is
    # not offered yet, and unavailable until this node is part of a cluster.
    PlaybookEntry(
        id="cluster_setup_ha",
        playbook=f"{COLLECTION}.cluster_setup_ha",
        title="Form the cluster",
        targets=["cluster_machines"],
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=(
            "Corosync, the authkey, Pacemaker, stonith disabled. Command "
            "driven, so check mode would report nothing meaningful."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER,
        ],
    ),
    PlaybookEntry(
        id="cluster_setup_cephadm",
        playbook=f"{COLLECTION}.cluster_setup_cephadm",
        title="Deploy Ceph",
        targets=["cluster_machines"],
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=(
            "Destructive on the selected disks. The inventory diff is the "
            "review step, because check mode cannot be one here."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER,
        ],
    ),
    PlaybookEntry(
        id="cluster_setup_libvirt",
        playbook=f"{COLLECTION}.cluster_setup_libvirt",
        title="Apply the cluster libvirt configuration",
        targets=["hypervisors:&cluster_machines"],
        preview=Preview.FULL,
        reboots=Reboots.NO,
        disruption="Installs the RBD secret for libvirt.",
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER,
        ],
    ),
    PlaybookEntry(
        id="cluster_setup_users",
        playbook=f"{COLLECTION}.cluster_setup_users",
        title="Apply the cluster accounts",
        targets=["hypervisors:&cluster_machines"],
        preview=Preview.FULL,
        reboots=Reboots.NO,
        disruption="Creates libvirtadmin, needed for live migration.",
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER,
        ],
    ),
    PlaybookEntry(
        id="cluster_remove_machine",
        playbook=f"{COLLECTION}.cluster_remove_machine",
        title="Remove a machine from the cluster",
        targets=["cluster_machines"],
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption="Evicts the machine from Pacemaker and from Ceph.",
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER,
        ],
        variables=[
            VariableSpec(
                name="machine_to_remove",
                type="string",
                description="The inventory name of the machine to evict.",
                required=True,
            )
        ],
        notes="Must run from a surviving node, which is why the mesh is full.",
    ),
)

BY_ID = {entry.id: entry for entry in CATALOGUE}

# Where ansible-galaxy lays a collection out under a collections path.
_COLLECTION_DIRECTORY = ("ansible_collections", "seapath", "ansible", "playbooks")


def get(playbook_id: str) -> PlaybookEntry | None:
    return BY_ID.get(playbook_id)


def playbook_file(collections_path: Path, entry: PlaybookEntry) -> Path:
    """Where the shipped collection keeps this entry's playbook."""
    name = entry.playbook.rsplit(".", 1)[-1]
    return Path(collections_path).joinpath(*_COLLECTION_DIRECTORY, f"{name}.yaml")


def identity(collections_path: Path) -> str | None:
    """What the installed collection actually is, precisely enough to compare.

    The version in `MANIFEST.json` is the one `galaxy.yml` declares, and every
    branch of the repository declares the same one, so "2.0.0" answers nothing
    for a site running a branch rather than a release. `FILES.json` carries a
    sha256 for every file in the collection, so hashing that one file
    fingerprints the whole tree: two branches differ, the same content matches,
    and it costs one read of a file the installer already wrote.

    The result is what a run records. "Which code converged this machine" then
    has an answer that survives someone reinstalling the collection.
    """
    root = Path(collections_path).joinpath(*_COLLECTION_DIRECTORY[:-1])
    try:
        manifest = json.loads((root / "MANIFEST.json").read_text())
        version = manifest["collection_info"]["version"]
    except (OSError, ValueError, KeyError):
        return None

    try:
        digest = hashlib.sha256((root / "FILES.json").read_bytes()).hexdigest()
    except OSError:
        return str(version)
    return f"{version}+{digest[:12]}"


def missing_from(collections_path: Path) -> set[str]:
    """The entries this image's collection does not actually carry."""
    return {
        entry.id
        for entry in CATALOGUE
        if not playbook_file(collections_path, entry).is_file()
    }
