# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The catalogue of docs/playbooks.md, and the collection behind it.

Two halves. The entries below are the reviewed half: a human read the playbook,
wrote the sentence an operator needs before converging a live machine, and
decided what check mode is worth. `analysis.py` is the derived half, reading
every other playbook the collection ships and answering the same questions from
the YAML. `resolve` merges them, a reviewed entry winning wherever there is
one, so the list an operator sees is the collection they are running.

Adding an entry here is a deliberate act, not a consequence of a playbook
existing upstream. What the UI runs is a whole playbook, never a free form
selection of tags: the tags in `seapath-ansible` were not designed as a public
interface, and a tag selector produces combinations nobody has ever run.

`targets` is copied from the playbook's own `hosts:` lines and is not a
parameter a caller can override. Narrowing `cluster_setup_ha.yaml` to one
member of three would be accepted by Ansible and would mean nothing.

`preview` is read off the modules the playbook's roles use, so the value can be
checked against the collection rather than argued about:

`full`    every task runs a module check mode understands. The preview is a
          real diff of what an apply would write.
`partial` some tasks are `command` or `shell`. Check mode skips them, the run
          still reaches the end, and what it reports is a subset.
`none`    a preview would crash or say nothing. Either the playbook is command
          driven from end to end, or a later task reads the `.stdout` of a
          command check mode skipped, which fails on an undefined attribute.
          Such an entry offers no preview button at all.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from app.runs import analysis

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
    DISTRIBUTION_MATCHES = "distribution_matches"
    """This node runs the distribution the playbook configures.

    Only the five `prerequisites` entries carry it, and only because none of
    them looks at what it landed on: launched on a Yocto machine, the Debian
    one runs `configure_seapath_distro` with `update-grub` anyway. A run plays
    every machine the inventory declares, this node among them, so the wrong
    one of the five is wrong for at least this machine and can be refused
    before it starts.

    What this node runs is read from `/etc/os-release`. It says nothing about
    the other machines: an inventory mixing distributions still needs
    `seapath_setup_main`, which picks per machine.
    """
    VARIABLES_SUPPORTED = "variables_supported"
    """The variables the playbook needs have a field on this page.

    A derived entry can find that a playbook refuses to start without a
    variable, and finding it is a long way from knowing how to ask for it.
    `seapath_update_yocto_cluster` plays `{{ machine_to_update }}`: nothing
    here knows what that variable may hold, and a free text field wired to an
    Ansible run is the extra vars box this service refuses to have. Such an
    entry is listed with its variable named, and stays unavailable.
    """


class VariableType(str, Enum):
    BOOLEAN = "boolean"
    MACHINE = "machine"
    """A machine name, which the UI offers as a list and the API checks.

    A run is launched against the whole inventory, so a machine named in a
    variable is the playbook's own business rather than a way of narrowing the
    run. `cluster_remove_machine` is the case: it plays every cluster member
    and needs to be told which one of them is leaving.
    """
    SECONDS = "seconds"
    PRIORITY = "priority"
    CPU_LIST = "cpu_list"
    """`smp`, or a CPU list in the kernel notation the inventory already uses.

    The three above are the measurement parameters, and they are the first
    variables here that are a number an operator picks rather than a switch or
    a machine name. They are typed rather than free text for the reason D8
    gives about tags: a value this service cannot check is a free form extra
    vars box, and this one refuses to have one.
    """
    UNKNOWN = "unknown"
    """A variable analysis found and nothing here knows how to ask for.

    Only ever produced by a derived entry. A reviewed entry types the variables
    it accepts, and accepts nothing else.
    """


class VariableSpec(BaseModel):
    name: str
    type: VariableType
    description: str
    required: bool = False


class Derivation(BaseModel):
    """What reading the playbook off the disk found, for the UI to show.

    Present on every entry, reviewed ones included, because the numbers are how
    an operator judges a description nobody wrote: eleven roles and four
    hundred tasks is a different act from one template and a restart.
    """

    plays: int
    tasks: int
    command_tasks: int
    roles: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    parsed: bool = True


class PlaybookEntry(BaseModel):
    id: str
    playbook: str
    title: str
    targets: list[str]
    preview: Preview
    reboots: Reboots
    reboot_variables: list[str] = Field(default_factory=list)
    """Every switch that has to be set for `gated` to mean what it says.

    A list because `seapath_setup_main` reboots in two places, its own last
    play and the network playbook it imports, and each has a switch of its own.
    Setting one alone reboots the machine after the operator declined it.
    """
    disruption: str
    requires: list[Precondition] = Field(default_factory=list)
    variables: list[VariableSpec] = Field(default_factory=list)
    notes: str = ""
    reviewed: bool = True
    """A human wrote this entry. False for one read off the collection."""
    distribution: str | None = None
    """The one SEAPATH distribution this playbook configures.

    Set on the five `prerequisites` entries and on nothing else.
    `seapath_setup_main` picks between them per machine, so it carries none.
    """
    derivation: Derivation | None = None
    restarts_service: bool = False
    """This playbook replaces the container serving this page.

    A run of one ends without a final status on the machine it was launched
    from, the way a reboot does, and the record says so rather than calling it
    a failure. See D23.
    """
    results_variable: str | None = None
    """The variable naming where this playbook fetches what it measured.

    A measuring playbook brings a file back to the controller, and every one
    upstream takes the destination as an ordinary variable:
    `cyclictest_result_folder` for cyclictest, `cukinia_test_prefix` for the
    functional tests. The run service fills it with the run's own results
    directory, so a measurement is kept, listed and deleted with the run that
    produced it.

    Filled by the service and never by the caller: it is a path inside this
    container, not an operator's decision, so it is absent from `variables` and
    the API refuses it there like any other undeclared name.
    """
    measures: bool = False
    """This run measures the machines rather than converging them.

    It changes what the confirmation has to say. A convergence is dangerous
    because of what it writes; a cyclictest is dangerous because of what it
    runs, which is a thread per CPU at real time priority for the duration of
    the test. Neither sentence covers the other, and an operator on a live
    substation needs the right one.
    """

    @property
    def previewable(self) -> bool:
        # A `none` playbook offers no preview button at all, rather than a
        # button that lies.
        return self.preview is not Preview.NONE


def _skip_reboot(name: str) -> VariableSpec:
    return VariableSpec(
        name=name,
        type=VariableType.BOOLEAN,
        description=(
            "Converge without rebooting. The configuration is not fully "
            "applied until a reboot happens, and the node view keeps saying so."
        ),
    )


# The two switches `seapath_setup_main` reboots behind. The first holds back
# its own last play, the second the reboot of the network playbook it imports,
# which fires whenever a role decided the new configuration needs a boot to
# take effect. Upstream sets both together in `ci_configure.yaml`, which is
# where the pair was confirmed.
_SKIP_REBOOT_SETUP = _skip_reboot("skip_reboot_setup")
_SKIP_REBOOT_NETWORK = _skip_reboot("skip_reboot_setup_network")

_MACHINE_TARGETS = ["cluster_machines", "standalone_machine"]

# The prerequisites, one playbook per distribution. `seapath_setup_main` picks
# between them after `detect_seapath_distro`, and that choice is the only thing
# standing between a machine and the wrong one: launched on its own, none of
# these five checks what it landed on. The Debian playbook runs
# `configure_seapath_distro` with `update-grub` and `/etc/vim` wherever it is
# sent. Every entry says so, because the operator launching one directly is
# exactly the operator who has bypassed the choice.
_WRONG_DISTRIBUTION = (
    "This playbook does not check the distribution it lands on. It applies its "
    "roles to every machine the inventory declares, so an inventory that mixes "
    "distributions needs Configure every machine, which picks the right one per "
    "machine."
)


def _prerequisites(
    distribution: str,
    playbook_id: str,
    targets: list[str],
    disruption: str,
    reboots: Reboots = Reboots.NO,
    notes: str = "",
) -> PlaybookEntry:
    """One of the five prerequisites entries.

    They differ by distribution and by which of the roles the distribution has,
    and they share what an operator has to be told: the machines they play, the
    fact that check mode reads only part of them, and that nothing in them
    looks at what the machine actually runs.
    """
    return PlaybookEntry(
        id=playbook_id,
        playbook=f"{COLLECTION}.{playbook_id}",
        title=f"Prepare the {distribution} machines",
        distribution=distribution,
        targets=targets,
        # `configure_seapath_distro` and `configure_physical_machine` both run
        # commands, so check mode reports the templates and the packages and
        # skips those.
        preview=Preview.PARTIAL,
        reboots=reboots,
        disruption=disruption,
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
            Precondition.DISTRIBUTION_MATCHES,
        ],
        notes=" ".join(part for part in (_WRONG_DISTRIBUTION, notes) if part),
    )


CATALOGUE: tuple[PlaybookEntry, ...] = (
    PlaybookEntry(
        id="seapath_setup_main",
        playbook=f"{COLLECTION}.seapath_setup_main",
        title="Configure every machine",
        # It imports every other playbook, so it plays every group they play.
        targets=[*_MACHINE_TARGETS, "VMs", "hypervisors"],
        preview=Preview.PARTIAL,
        reboots=Reboots.GATED,
        reboot_variables=["skip_reboot_setup", "skip_reboot_setup_network"],
        disruption=(
            "The full convergence: prerequisites, network, time, libvirt, "
            "monitoring and real time tuning. On a live machine it restarts "
            "whatever the roles decide to restart, and it reboots unless you "
            "ask it not to: once in the network playbook it imports, when a "
            "role decided the new configuration needs a boot, and once at the "
            "end."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        variables=[_SKIP_REBOOT_SETUP, _SKIP_REBOOT_NETWORK],
        notes=(
            "This is the commissioning path and what the CI runs, which makes "
            "it the granularity with evidence behind it. Declining the reboot "
            "sets both switches, and one case survives it: a Yocto machine "
            "whose inventory carries kernel_parameters_restart reboots from "
            "the kernel parameters role when those parameters changed, which "
            "is a reboot the inventory asked for."
        ),
    ),
    _prerequisites(
        "Debian",
        "seapath_setup_prerequisitesdebian",
        ["cluster_machines", "standalone_machine", "VMs", "hypervisors"],
        (
            "The base a SEAPATH machine is built on: syslog, the distribution "
            "configuration, the kernel modules and the initramfs, tuned on the "
            "hypervisors, and vm_manager. It also uninstalls packages the "
            "installation left behind, ceph and ifupdown among them, and stops "
            "the apt timers. On a live machine that is a package removal and "
            "several service restarts."
        ),
        notes=(
            "The only one of the five that removes packages. `ceph`, `fdisk`, "
            "`ifupdown` and, on trixie, four libraries are purged with "
            "`autoremove`, which is what the ISO expects and what a machine "
            "installed some other way may not survive unexamined."
        ),
    ),
    _prerequisites(
        "CentOS",
        "seapath_setup_prerequisitescentos",
        ["cluster_machines", "standalone_machine", "VMs", "hypervisors"],
        (
            "Syslog, the distribution configuration with `grub2-mkconfig` and "
            "dracut, tuned on the hypervisors, and vm_manager. Restarts "
            "whatever those roles restart, and removes nothing."
        ),
    ),
    _prerequisites(
        "OracleLinux",
        "seapath_setup_prerequisitesoraclelinux",
        ["cluster_machines", "standalone_machine", "VMs"],
        (
            "Syslog, the distribution configuration with `grub2-mkconfig`, the "
            "kernel modules and vm_manager."
        ),
        notes=(
            "The one that plays no hypervisor group: it configures no tuned "
            "profile, so a machine prepared with it has had none of the "
            "hypervisor tuning applied."
        ),
    ),
    _prerequisites(
        "SLES",
        "seapath_setup_prerequisitessles",
        ["cluster_machines", "standalone_machine", "VMs", "hypervisors"],
        (
            "Syslog, the distribution configuration with `grub2-mkconfig` and "
            "dracut, tuned on the hypervisors, and vm_manager."
        ),
    ),
    _prerequisites(
        "Yocto",
        "seapath_setup_prerequisitesyocto",
        ["cluster_machines", "standalone_machine", "hypervisors", "VMs"],
        (
            "A different playbook from the other four: kernel command line, "
            "hugepages and SR-IOV, with none of the package or syslog work. It "
            "mounts the boot partition to edit the kernel parameters, and "
            "reboots when they changed."
        ),
        reboots=Reboots.YES,
        notes=(
            "The reboot happens only if the kernel parameters actually changed "
            "and `kernel_parameters_restart` is set in the inventory. Declared "
            "here as a reboot rather than as a gated one, because the "
            "confirmation has to name the worse of the two outcomes: on a "
            "machine whose parameters are already right, nothing restarts."
        ),
    ),
    PlaybookEntry(
        id="seapath_setup_network",
        playbook=f"{COLLECTION}.seapath_setup_network",
        title="Apply the network configuration",
        # `hypervisors` is in the list because two of the plays are: the SR-IOV
        # network pools and `configure_nic_irq_affinity`. The entry said
        # cluster and standalone alone until the collection was read, and the
        # scope line an operator reads before an apply was wrong by two plays.
        targets=[*_MACHINE_TARGETS, "hypervisors"],
        preview=Preview.PARTIAL,
        # The reboot sits in a block, and the switch that declines it sits on
        # the block. It fires only when a role set `need_reboot`, which is what
        # the roles do when they wrote a configuration they could not apply to
        # the running machine.
        reboots=Reboots.GATED,
        reboot_variables=["skip_reboot_setup_network"],
        disruption=(
            "The playbook most likely to cut the connection under the run. It "
            "always writes the network configuration. apply_network_config, "
            "which every inventory this service writes carries as true, is "
            "what decides whether the roles apply it to the running machine, "
            "restarting OVS and systemd-networkd under whatever is using them, "
            "or leave it for the next boot and ask for one at the end."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        variables=[_SKIP_REBOOT_NETWORK],
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
        title="Configure libvirt",
        targets=["hypervisors"],
        # One template and a restart, which check mode reads exactly.
        preview=Preview.FULL,
        reboots=Reboots.NO,
        disruption=(
            "Writes libvirtd.conf and restarts libvirtd. Running guests keep "
            "running."
        ),
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
        # `deploy_seapath_alloc` copies files and enables a unit. No command.
        preview=Preview.FULL,
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
        id="seapath_setup_deploy_seapath_webui",
        playbook=f"{COLLECTION}.seapath_setup_deploy_seapath_webui",
        title="Apply the management UI, including this one",
        targets=list(_MACHINE_TARGETS),
        # `deploy_seapath_webui` templates the quadlet and enables the unit.
        preview=Preview.FULL,
        reboots=Reboots.NO,
        restarts_service=True,
        disruption=(
            "Replaces the seapath-webui container on every machine the "
            "inventory declares, this one included. The page you are reading "
            "goes away for a few seconds and comes back on the new version, "
            "and this run ends without a final status because the service "
            "writing its trace is the service being replaced."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        notes=(
            "The version each machine gets is `seapath_webui_image` in the "
            "inventory, so an update is an edit and an apply, like every other "
            "change here. The role restarts the unit through a detached "
            "systemd job, which is what lets this run reach its last task "
            "before the container it is running in goes away."
        ),
    ),
    PlaybookEntry(
        id="seapath_setup_hardening",
        playbook=f"{COLLECTION}.seapath_setup_hardening",
        title="Harden every machine",
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
    # The second entry that touches libvirt, and a different playbook upstream.
    # `seapath_setup_libvirt` configures the daemon on every hypervisor; this
    # one only hands libvirt the credential it needs to open a disk that lives
    # in the Ceph pool, and it exists separately because a standalone machine
    # has no Ceph and must not run it.
    PlaybookEntry(
        id="cluster_setup_libvirt",
        playbook=f"{COLLECTION}.cluster_setup_libvirt",
        title="Give libvirt access to the Ceph pool",
        targets=["hypervisors:&cluster_machines"],
        # `configure_libvirt_rdb_secret` reads the existing secret with
        # `virsh secret-list` and the next task reads that result's `.stdout`.
        # Check mode skips the shell and the play dies on the missing
        # attribute, so there is nothing to preview.
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=(
            "Defines the RBD secret libvirt presents to Ceph. Running guests "
            "keep running."
        ),
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
        # `add_libvirtadmin_user` finds root's home with a shell and fetches a
        # key from it. Check mode skips the shell and the fetch then reads a
        # `.stdout` that is not there.
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=(
            "Creates libvirtadmin and exchanges the root keys between the "
            "cluster members, which is what live migration and console access "
            "use."
        ),
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
        disruption=(
            "Evicts the machine you name from Pacemaker and from Ceph. The "
            "run plays every cluster member and the commands are sent to a "
            "surviving one, so the machine being removed is usually reported "
            "unreachable, which is what a dead machine looks like."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.CLUSTER,
        ],
        variables=[
            VariableSpec(
                name="machine_to_remove",
                type=VariableType.MACHINE,
                description=(
                    "The machine leaving the cluster, by the name the "
                    "inventory gives it. Usually a machine that has died, "
                    "which is why nothing here asks it to cooperate."
                ),
                required=True,
            )
        ],
        notes=(
            "Runs from a surviving node and refuses to name this one: the "
            "playbook sends `crm_node -R` and `ceph orch host rm` to a member "
            "that stays, and a node cannot both drive the eviction and be its "
            "subject."
        ),
    ),
    PlaybookEntry(
        id="test_run_cyclictest",
        playbook=f"{COLLECTION}.test_run_cyclictest",
        title="Measure the latency (cyclictest)",
        targets=list(_MACHINE_TARGETS),
        # One `command` from end to end, and the task after it fetches the file
        # that command wrote. Check mode skips the command and the fetch then
        # has nothing to bring back, so a preview would report a green run that
        # measured nothing.
        preview=Preview.NONE,
        reboots=Reboots.NO,
        measures=True,
        results_variable="cyclictest_result_folder",
        disruption=(
            "Runs cyclictest on every machine the inventory declares, which is "
            "a measuring thread per measured CPU at real time priority, "
            "competing with whatever those CPUs are already running. On a live "
            "substation the machines are measured alongside their guests, and "
            "the number that comes back includes them."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
            Precondition.PLAYBOOK_PRESENT,
        ],
        variables=[
            VariableSpec(
                name="cyclictest_duration",
                type=VariableType.SECONDS,
                description=(
                    "How long to measure, in seconds. Twenty is the upstream "
                    "default and enough to catch a gross misconfiguration. A "
                    "figure worth quoting takes hours, because the latency "
                    "that matters is the one that happens once."
                ),
            ),
            VariableSpec(
                name="cyclictest_priority",
                type=VariableType.PRIORITY,
                description=(
                    "The SCHED_FIFO priority of the measuring threads, 90 by "
                    "default. Above the priority of a guest's vCPU threads it "
                    "measures the machine, below it measures the queue behind "
                    "the guest."
                ),
            ),
            VariableSpec(
                name="cyclictest_affinity",
                type=VariableType.CPU_LIST,
                description=(
                    "Which CPUs to measure. `smp` runs one thread per online "
                    "CPU, which is the upstream default. A list measures those "
                    "CPUs, and the isolated set is what a SEAPATH machine is "
                    "asked about."
                ),
            ),
        ],
        notes=(
            "Nothing on the machine is changed: the role copies a script to a "
            "temporary directory, runs cyclictest, fetches the histogram and "
            "leaves. The histogram is parsed and charted on the real time "
            "page, and kept with the run."
        ),
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


def installed_in(collections_path: Path) -> bool:
    """Whether this root carries an installed seapath.ansible collection."""
    return identity(collections_path) is not None


def select_root(site: Path, image: Path) -> Path:
    """Which of the two roots this service runs playbooks from.

    The site's collection is the one an administrator installed on the node, in
    the state volume, so that a corrected playbook does not wait for an image
    build. The image's is the fallback, and it is what a node nobody has
    updated runs.

    The choice is on the manifest rather than on the directory existing: the
    quadlet creates the state volume, so an empty `collections/` in it is the
    ordinary shape of a node nobody has updated, and it must never shadow the
    collection the image ships. Whichever root wins, wins whole. See D23.
    """
    if installed_in(site):
        return Path(site)
    return Path(image)


def missing_from(collections_path: Path) -> set[str]:
    """The entries this image's collection does not actually carry."""
    return {
        entry.id
        for entry in CATALOGUE
        if not playbook_file(collections_path, entry).is_file()
    }


# Words the collection writes as an acronym, so a title read off a file name
# says "Cluster setup HA" rather than "Cluster setup ha".
_ACRONYMS = {
    "api": "API",
    "ceph": "Ceph",
    "cpu": "CPU",
    "ha": "HA",
    "irq": "IRQ",
    "nic": "NIC",
    "ptp": "PTP",
    "seapath": "SEAPATH",
    "snmp": "SNMP",
    "ssh": "SSH",
    "vm": "VM",
    "vms": "VMs",
}


def _title_of(playbook_id: str) -> str:
    """A readable name for a playbook nobody has written a title for.

    Deliberately close to the file name. The id is displayed under the title
    everywhere it matters, and an operator told to run
    `seapath_setup_prerequisitesdebian` has to recognise it here.
    """
    words = [_ACRONYMS.get(word, word) for word in playbook_id.split("_") if word]
    if not words:
        return playbook_id
    first = words[0]
    if first == first.lower():
        first = first.capitalize()
    return " ".join([first, *words[1:]])


def _derived_disruption(facts: analysis.PlaybookFacts) -> str:
    """The sentence for an entry nobody has written a sentence for.

    It says what was counted and where the counting stops. An operator reading
    "23 tasks, 4 of them command driven" knows more than one reading a
    confident description of a playbook this service has never been run
    against.
    """
    parts = [
        "Read from the collection, and not reviewed by anyone here: what "
        "follows was counted in the playbook rather than written by a human.",
        f"{facts.play_count} "
        + ("play" if facts.play_count == 1 else "plays")
        + f" over {facts.task_count} "
        + ("task" if facts.task_count == 1 else "tasks")
        + (
            f", {facts.command_tasks} of them command driven."
            if facts.command_tasks
            else "."
        ),
    ]
    if facts.reboots:
        parts.append(
            "It reboots the machines it plays."
            if facts.reboot_state != "gated"
            else (
                "It reboots the machines it plays unless "
                + " and ".join(facts.reboot_variables)
                + " say otherwise."
            )
        )
    parts.append(
        "What it changes on a live machine is whatever those roles decide to " "change."
    )
    return " ".join(parts)


def _derived_notes(facts: analysis.PlaybookFacts) -> str:
    notes = []
    if facts.imports:
        notes.append("Imports " + ", ".join(facts.imports) + ".")
    if facts.roles:
        shown = facts.roles[:10]
        listed = ", ".join(shown)
        if len(facts.roles) > len(shown):
            listed += f" and {len(facts.roles) - len(shown)} more"
        notes.append(f"Roles: {listed}.")
    if not facts.parsed:
        notes.append(
            "Part of this playbook could not be parsed, so the counts above "
            "are a floor rather than a description."
        )
    return " ".join(notes)


def derive(facts: analysis.PlaybookFacts) -> PlaybookEntry:
    """A catalogue entry for a playbook nobody has reviewed."""
    # Only offered where every reboot of the chain is behind one of them.
    # Half the switches of a playbook that reboots twice is a checkbox that
    # reboots the machine anyway.
    switches = facts.reboot_variables if facts.reboot_state == "gated" else []
    variables = [_skip_reboot(name) for name in switches]
    variables.extend(
        VariableSpec(
            name=name,
            type=VariableType.UNKNOWN,
            description=f"{name}, which this playbook refuses to start without",
            required=True,
        )
        for name in facts.required_variables
        if name not in switches
    )

    requires = [
        Precondition.INVENTORY_VALID,
        Precondition.SELF_TRUST,
        Precondition.PEER_REACHABLE,
    ]
    if facts.needs_cluster:
        requires.append(Precondition.CLUSTER)

    return PlaybookEntry(
        id=facts.id,
        playbook=f"{COLLECTION}.{facts.id}",
        title=_title_of(facts.id),
        targets=list(facts.targets),
        preview=Preview(facts.preview),
        reboots=Reboots(facts.reboot_state),
        reboot_variables=list(switches),
        disruption=_derived_disruption(facts),
        requires=requires,
        variables=variables,
        notes=_derived_notes(facts),
        reviewed=False,
        derivation=_derivation(facts),
    )


def _derivation(facts: analysis.PlaybookFacts) -> Derivation:
    return Derivation(
        plays=facts.play_count,
        tasks=facts.task_count,
        command_tasks=facts.command_tasks,
        roles=list(facts.roles),
        imports=list(facts.imports),
        parsed=facts.parsed,
    )


def resolve(collections_path: Path, version: str = "") -> tuple[PlaybookEntry, ...]:
    """Every playbook this node can run, reviewed entries first.

    A reviewed entry keeps its prose and its judgement whole. Analysis of the
    same playbook is attached beside it, and never overrides it: the values a
    human wrote encode what a run actually did on a machine, and the reader
    encodes what the YAML says. Where they disagree, on `seapath_setup_snmp`
    for instance, the reviewed `full` knows that the one command in the chain
    detects a distribution rather than writing anything, and the reader can
    only count it.

    Everything else the collection ships is derived, listed after them and
    marked unreviewed. A playbook this service has never heard of is a playbook
    an operator can still see, which is the point: the catalogue was written
    against one version of a collection that moves without it.
    """
    root = Path(collections_path).joinpath(*_COLLECTION_DIRECTORY[:-1])
    facts = {
        item.id: item
        for item in analysis.read_all(
            root,
            version or identity(collections_path) or "none",
            # The `test_*` entries a human reviewed, so their counted facts sit
            # under the prose like every other entry's. Analysis still refuses
            # to *derive* one: an id absent from this set and starting with
            # `ci_` or `test_` is never read at all.
            frozenset(entry.id for entry in CATALOGUE),
        )
    }

    entries = []
    for entry in CATALOGUE:
        known = facts.pop(entry.id, None)
        entries.append(
            entry.model_copy(update={"derivation": _derivation(known)})
            if known
            else entry
        )
    entries.extend(derive(item) for item in sorted(facts.values(), key=lambda f: f.id))
    return tuple(entries)
