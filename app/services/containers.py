# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The containers the inventory declares, and what the machines do with them.

A container in SEAPATH is a quadlet: a `.container` file the inventory uploads
to `/etc/containers/systemd/`, which podman's generator turns into a systemd
unit. That gives one object with three faces, and this joins them:

- **declared**, by `upload_extra_files_upload_files`, which says which machines
  receive the file and which file it is, or on a cluster by
  `cluster_containers`, which `deploy_containers_cluster` deploys on every
  hypervisor. `app/inventory/quadlets.py` reads both.
- **a unit**, on each of those machines, published by the `systemd` collector
  of the `node_exporter` every node already runs. The same exposition the CPU
  pool is read from, on the same port, in the same GET.
- **a resource**, when the cluster was told to hold one, published by
  `ha_cluster_exporter` with a `systemd:` agent. That is the whole of what
  Pacemaker's systemd agent does: it starts and stops the unit and decides
  where.

Which of the last two owns a container is the question that decides everything
on the page. A resource means Pacemaker places it and starting it is `crm
resource start`; no resource means it is an ordinary unit on each machine and
starting it is systemd's, one machine at a time. A page that offered the same
button for both would be asking Pacemaker and systemd to disagree.

This writes to the inventory and to nothing else. Deploying an uploaded
quadlet is the prerequisites playbook, which is where `upload_extra_files`
runs; deploying a cluster workload, its Pacemaker resource included, is
`deploy_containers_cluster`. Both are ordinary runs, named here and launched
through `/runs` like every other. Removing a workload is the role's own
`state: absent`, applied by the same run; the entry and its files leave the
inventory once that run has taken the workload off the machines. See D72.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.cluster import ha, systemd
from app.cluster.exporters import MetricsClient, UrllibMetricsClient, read_all
from app.cluster.ha import LocationConstraint, PacemakerCluster, PacemakerResource
from app.cluster.pool import DEFAULT_PORT
from app.core.logging import audit_event
from app.inventory import quadlets, references
from app.inventory.editor import Scope
from app.inventory.files import UnsafePath
from app.inventory.model import Mode
from app.inventory.references import Reference, Where
from app.inventory.repository import Commit, RepositoryError
from app.inventory.resolve import ROOT, depths, groups, resolve
from app.inventory.service import InventoryService, RefusedFile
from app.runs.catalogue import CATALOGUE
from app.runs.models import RunRecord, RunState
from app.services.cluster import ClusterService

# The agent Pacemaker's systemd resource agent is named by, as
# `ha_cluster_exporter` publishes it. A resource with this agent is a unit the
# cluster starts, which on a SEAPATH machine is a quadlet almost every time.
SYSTEMD_AGENT = "systemd"

# What applies a declaration, per half. The upload happens in the prerequisites
# playbook of the machine's own distribution, which is where
# `upload_extra_files` runs; `seapath_setup_main` picks between the five and is
# the honest answer for an inventory that mixes distributions.
FULL_CONVERGENCE = "seapath_setup_main"
WORKLOAD_PLAYBOOK = "deploy_containers_cluster"
# Where deploy_containers_cluster writes the configuration of each workload on
# every node (deploy_containers_cluster_config_dir).
CONFIG_DIR = "/etc/seapath-containers"

# How much of a quadlet is read to look for its `[Install]` section. A quadlet
# is a few hundred bytes; anything past this is not one, and reading a file the
# inventory happens to name is not a licence to load it whole.
_MAX_QUADLET_BYTES = 64 * 1024

_NO_INVENTORY = (
    "There is no inventory yet, so no container is declared. The Inventory "
    "page is where the machines are described."
)
_NO_CONTAINERS = (
    "This inventory declares no container. A container is a quadlet: a "
    "`.container` file uploaded to /etc/containers/systemd by an entry of "
    "`upload_extra_files_upload_files`, or on a cluster a workload of "
    "`cluster_containers` that Pacemaker runs. Add one here and the file is "
    "committed with the inventory."
)
_FROM_EXPORTERS = (
    "The state column is read from each machine's own exporters: the systemd "
    "collector of prometheus-node-exporter for the unit, and "
    "ha_cluster_exporter for the Pacemaker resource where there is one."
)
_NO_COLLECTOR = (
    "answered without any systemd unit metrics, so its node_exporter runs "
    "without the systemd collector and the unit state cannot be read there"
)


class NodeUnit(BaseModel):
    """One machine's answer about one unit."""

    host: str
    address: str = ""
    reachable: bool = False
    error: str = ""
    """Why this machine said nothing, in the operator's terms."""
    known: bool = False
    """The exporter published this unit at all.

    False on a machine whose convergence has not run yet: the file is in the
    inventory, the machine has never received it, and systemd has never heard
    of the unit. Reporting that as stopped would describe it as deployed.
    """
    state: str = "unknown"
    active: bool = False
    failed: bool = False
    started_at: str | None = None


class ContainerView(BaseModel):
    """One container: what the inventory says, and what the machines say."""

    name: str
    kind: str = ".container"
    unit: str
    actionable: bool = True
    """A `.container` or a `.kube`, which is what an operator starts. The other
    quadlet kinds are dependencies of one of those."""

    src: str
    """Where the control machine keeps the file, which is what the upload
    entry copies from."""
    file_name: str = ""
    """What the file is called under /etc/containers/systemd, which is the name
    podman's generator reads and the one an operator finds on the machine."""
    dest: str
    mode: str = ""
    scope_kind: str = "host"
    scope_name: str = ""
    """Where the entry that declares it sits, which is what the page shows and
    what a second declaration is written into."""
    variable: str = ""
    """The variable declaring it: `upload_extra_files_upload_files`, or
    `cluster_containers` for a workload `deploy_containers_cluster` deploys."""
    playbook: str = ""
    """The run that puts this container's declaration on the machines."""
    values_editable: bool = False
    """A workload installed from a delivery, whose `values.yaml` in the
    inventory folder describes the site values the page may edit."""
    rbd: bool = False
    """A workload keeping its state on an RBD image named after it, which its
    removal deletes only when asked to."""
    hosts: list[str] = Field(default_factory=list)
    """The machines this inventory sends it to."""

    file: Reference | None = None
    """The quadlet file itself, and whether a run would find it."""

    managed: str = "systemd"
    """`pacemaker` when the cluster holds a resource for the unit, `systemd`
    otherwise. It decides which start button the row carries."""
    resource: PacemakerResource | None = None
    units: list[NodeUnit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    readable: bool = False
    """The quadlet can be shown: this node holds the file and it is text.

    Read from the reference rather than from the name, because a container
    whose file the inventory does not carry is the ordinary state of an
    inventory somebody is still writing.
    """

    placement: str = ""
    """What decides which member runs it, for a container Pacemaker holds.

    The four states of the VMs page: `free` where neither a rule nor the entry
    names a member, `kept` where the rule in force names the member it runs on
    and the entry's `preferred_host` names the same one, `adrift` where the
    rule and the entry disagree, a missing rule included, and `displaced` where
    the rule in force names another member than the one it runs on. Empty for
    a container systemd owns, where the machines are the ones the inventory
    uploads it to and there is nothing to place.
    """
    preferred_host: str = ""
    """Where the workload's entry asks the cluster to run it, when it asks."""
    constraint: LocationConstraint | None = None
    """The `cli-prefer` rule holding it, which a return removes."""
    preference: LocationConstraint | None = None
    """The rule a deployment wrote from `preferred_host`.

    What the cluster weighs once no `cli-prefer` overrides it, and what a
    return therefore gives the container back to."""
    pinned: str = ""
    """The id of the `pin-` constraint holding it, when a site wrote one.

    A pinned resource runs there or nowhere: a move would leave two mandatory
    rules pulling in opposite directions and a return would not remove this
    one, so the page offers neither. The entry's `pinned_host` is what a
    deployment run writes or removes.
    """
    destinations: list[str] = Field(default_factory=list)
    """The members a move may send it to, which is where one can be offered."""


class QuadletFile(BaseModel):
    """One file of a container, read from where a run would read it."""

    file_name: str
    src: str
    dest: str = ""
    """Where the run puts it: under /etc/containers/systemd for a quadlet,
    relative to the workload's RBD image for one of its files."""
    on_rbd: bool = False
    config: bool = False
    """A configuration file, written to every node and mounted read only."""
    where: str = ""
    """Which store holds it: the versioned folder, the artefacts, or the
    installed collection."""
    path: str = ""
    """What this node resolved the reference to."""
    content: str = ""
    error: str = ""
    """Why this file cannot be shown. Carried per file, so the one a site has
    not uploaded yet does not hide the others."""


class QuadletFiles(BaseModel):
    """Every file one container is made of, the one its unit comes from
    first."""

    name: str
    files: list[QuadletFile] = Field(default_factory=list)


class ScopeOption(BaseModel):
    """Somewhere a declaration can be written, offered to the form."""

    kind: str
    name: str
    machines: list[str] = Field(default_factory=list)
    available: bool = True
    reason: str = ""
    """Why this scope is refused, when it is: the variable it would append to
    is not the one those machines receive."""


class RemovedWorkload(BaseModel):
    """A workload `cluster_containers` marks `state: absent`."""

    name: str
    remove_rbd: bool = False
    """Its RBD image goes with it, with its snapshots and the images put
    aside, where otherwise it stays in the pool and in the backups."""


class ForgottenWorkload(BaseModel):
    """A workload a run removed, whose entry the inventory history still holds."""

    name: str
    commit: str
    """The commit that took its entry out once the removal run succeeded. Its
    parent holds the entry and the files, as they were."""
    missing_archives: list[str] = Field(default_factory=list)
    """The image archives it loads that the artefacts no longer hold, which
    a run would stop on."""


class ContainersView(BaseModel):
    mode: str = Mode.STANDALONE.value
    containers: list[ContainerView] = Field(default_factory=list)
    removing: list[RemovedWorkload] = Field(default_factory=list)
    """Workloads the inventory marks for removal, which the next run of
    `deploy_containers_cluster` takes off the machines."""
    undeclared: list[PacemakerResource] = Field(default_factory=list)
    """Units the cluster runs as resources and no quadlet here explains."""
    scopes: list[ScopeOption] = Field(default_factory=list)
    upload_playbook: str = FULL_CONVERGENCE
    """The run that puts the files on the machines and reloads systemd."""
    workload_playbook: str = WORKLOAD_PLAYBOOK
    """The run that deploys the workloads of `cluster_containers`."""
    runtime_note: str = ""
    note: str = ""
    warnings: list[str] = Field(default_factory=list)
    inventory_commit: str | None = None


class InvalidContainer(Exception):
    """The declaration cannot become an entry, and the message says why."""


class UnknownContainer(Exception):
    """No container of that name is declared here."""


class UnreadableQuadlet(Exception):
    """The file behind a container cannot be shown, and the message says why."""


class ContainerService:
    def __init__(
        self,
        inventory: InventoryService,
        cluster: ClusterService,
        client: MetricsClient | None = None,
        port: int = DEFAULT_PORT,
        timeout: float = 2.0,
        distribution: Any = None,
    ) -> None:
        self._inventory = inventory
        self._cluster = cluster
        self._client = client or UrllibMetricsClient()
        self._port = port
        self._timeout = timeout
        # Which SEAPATH distribution this node runs, read through the adapter
        # on each call, so a machine reinstalled under a running service is
        # read again rather than remembered.
        self._distribution = distribution or (lambda: None)

    def containers(self) -> ContainersView:
        document = self._inventory.raw()
        state = self._inventory.state()
        if not document.strip() or state.inventory is None:
            return ContainersView(note=_NO_INVENTORY, inventory_commit=state.commit)

        mode = state.inventory.mode
        view = ContainersView(
            mode=mode.value,
            scopes=self.scopes(),
            upload_playbook=self.upload_playbook(),
            inventory_commit=state.commit,
        )

        declared = [*quadlets.declared(document), *quadlets.workloads(document)]
        # One reading of the cluster for the whole page: the resources it holds
        # and the rules placing them come out of the same exposition, and
        # asking twice would be one fan out per column.
        cluster = self._cluster.pacemaker()
        resources, note = self._resources(cluster)
        view.runtime_note = note
        units, warnings = self._units(document, declared, resources)
        view.warnings = warnings

        files = {
            (reference.host, reference.value): reference
            for reference in self._inventory.references()
            if reference.variable in _DECLARING
        }

        # The workloads a delivery installed keep their `values.yaml` there.
        described = {
            item.path.split("/")[1]
            for item in self._inventory.files()
            if item.path.startswith("inventories/")
            and item.path.endswith("/values.yaml")
            and item.path.count("/") == 2
        }
        for name in sorted({quadlet.name for quadlet in declared}):
            entries = [quadlet for quadlet in declared if quadlet.name == name]
            first = entries[0]
            hosts = [quadlet.host for quadlet in entries]
            resource = resources.get(first.unit)
            scope = self._scope_of(document, hosts, _variable(first))
            entry = ContainerView(
                name=name,
                kind=first.kind,
                unit=first.unit,
                actionable=first.actionable,
                src=first.src,
                file_name=first.file_name,
                dest=first.dest,
                mode=first.mode,
                scope_kind=scope.kind,
                scope_name=scope.name,
                variable=_variable(first),
                playbook=(
                    WORKLOAD_PLAYBOOK if first.workload else view.upload_playbook
                ),
                values_editable=first.workload and name in described,
                rbd=first.rbd,
                hosts=hosts,
                file=files.get((first.host, first.src)),
                managed="pacemaker" if resource else "systemd",
                resource=resource,
                units=[units[(host, first.unit)] for host in hosts],
                warnings=self._container_warnings(first, resource, files),
                readable=_readable(files.get((first.host, first.src))),
                preferred_host=first.preferred_host,
            )
            if resource is not None:
                _place(entry, cluster, resource)
            view.containers.append(entry)

        view.removing = [
            RemovedWorkload(name=name, remove_rbd=spec.get("remove_rbd") is True)
            for name, spec in sorted(quadlets.removed(document).items())
        ]

        explained = {quadlet.unit for quadlet in declared}
        view.undeclared = [
            resource
            for unit, resource in sorted(resources.items())
            if unit not in explained
        ]
        if not view.containers and not view.removing:
            view.note = _NO_CONTAINERS
        return view

    def known(self) -> dict[str, quadlets.Quadlet]:
        """The containers this node can act on, by name.

        The declared ones alone. A Pacemaker resource with a systemd agent that
        no quadlet explains is listed on the page and carries no button: this
        service knows the unit's name and nothing about what it is, and a stop
        aimed at a name read out of an exposition is a stop this service cannot
        describe before it runs.
        """
        document = self._inventory.raw()
        found: dict[str, quadlets.Quadlet] = {}
        for quadlet in [*quadlets.declared(document), *quadlets.workloads(document)]:
            found.setdefault(quadlet.name, quadlet)
        return found

    def check_known(self, name: str) -> quadlets.Quadlet:
        quadlet = self.known().get(name)
        if quadlet is None:
            raise UnknownContainer(
                f"No container called {name!r} is declared in this inventory."
            )
        return quadlet

    def hosts_of(self, name: str) -> list[str]:
        return quadlets.hosts_of(self._inventory.raw(), name)

    def quadlet_files(self, name: str) -> QuadletFiles:
        """Every file one container is made of, read where a run would read it.

        A container is rarely one file. A workload names its pod, its
        containers and their networks in `quadlets`, and the settings its RBD
        image carries in `rbd.files`; an `upload_extra_files` container is
        joined to the `.network`, `.volume` and `.pod` files it names, and a
        pod to the containers that name it. The page shows all of them,
        because the question it answers, what podman is about to be handed,
        is not answered by one of them alone.

        Each file is bounded the same two ways. A path this inventory names
        outside the folders a run overlays is refused rather than served, so
        an entry pointing at `/etc/shadow` cannot turn this page into a reader
        of the filesystem; and a file too large to be a quadlet is refused with
        its size, because a few hundred bytes is what one is.
        """
        quadlet = self.check_known(name)
        # One reading of the references for every file, since each one is a
        # walk of the stores.
        found = {
            (reference.host, reference.value): reference
            for reference in self._inventory.references()
            if reference.variable == _variable(quadlet)
        }
        if quadlet.workload:
            entries = self._workload_entries(quadlet)
        else:
            entries = self._upload_entries(quadlet, found)
        for entry in entries:
            if not entry.path and not entry.error:
                _fill(entry, found.get((quadlet.host, entry.src)))
        return QuadletFiles(name=name, files=entries)

    def _workload_entries(self, quadlet: quadlets.Quadlet) -> list[QuadletFile]:
        """The quadlets, configuration and RBD files of a workload, the unit's
        own first."""
        spec = (
            resolve(self._inventory.raw())
            .get(quadlet.host, {})
            .get(quadlets.WORKLOADS_VARIABLE, {})
            .get(quadlet.name)
        )
        entries = [
            QuadletFile(
                file_name=quadlets.on_machine(source),
                src=source,
                dest=f"{quadlets.QUADLET_DIR}/{quadlets.on_machine(source)}",
            )
            for source in quadlets.workload_sources(spec)
        ]
        entries.sort(key=lambda entry: entry.src != quadlet.src)
        entries += [
            QuadletFile(
                file_name=quadlets.on_machine(source),
                src=source,
                dest=f"{CONFIG_DIR}/{quadlet.name}/{quadlets.on_machine(source)}",
                config=True,
            )
            for source in quadlets.workload_config(spec)
        ]
        entries += [
            QuadletFile(
                file_name=dest.rsplit("/", 1)[-1],
                src=source,
                dest=dest,
                on_rbd=True,
            )
            for source, dest in quadlets.workload_files(spec)
        ]
        return entries

    def _upload_entries(
        self, quadlet: quadlets.Quadlet, found: dict[tuple[str, str], Reference]
    ) -> list[QuadletFile]:
        """This quadlet and the ones the same machine receives beside it that
        it is joined to, found by what the files name.

        Followed both ways and to the end: a container names its pod, the pod
        names its networks, and the other containers of the pod name it.
        """
        beside = {
            item.file_name: item
            for item in quadlets.declared(self._inventory.raw())
            if item.host == quadlet.host
        }
        entries = {
            file_name: _fill(
                QuadletFile(file_name=file_name, src=item.src, dest=item.dest),
                found.get((item.host, item.src)),
            )
            for file_name, item in beside.items()
        }
        names = {
            file_name: quadlets.names_in(entry.content)
            for file_name, entry in entries.items()
        }

        joined = [quadlet.file_name]
        for file_name in joined:
            more = names.get(file_name, set())
            if file_name.endswith(".pod"):
                more = more | {
                    other for other, named in names.items() if file_name in named
                }
            joined += sorted(
                item for item in more if item in beside and item not in joined
            )
        return [entries[file_name] for file_name in joined]

    def resource_for(self, unit: str) -> PacemakerResource | None:
        """The Pacemaker resource holding this unit, when the cluster has one."""
        return self._resources(self._cluster.pacemaker())[0].get(unit)

    def upload_playbook(self) -> str:
        """The run that uploads the quadlets and reloads systemd.

        The prerequisites playbook of this node's own distribution when it is
        known, since that is the narrow act and the one that actually carries
        `upload_extra_files`. `seapath_setup_main` otherwise, which picks
        between the five per machine and is the only right answer for an
        inventory that mixes distributions.
        """
        running = self._distribution()
        if not running:
            return FULL_CONVERGENCE
        for entry in CATALOGUE:
            if entry.distribution == running:
                return entry.id
        return FULL_CONVERGENCE

    def scopes(self) -> list[ScopeOption]:
        """Where a declaration may be written, with what each one reaches.

        The groups the file declares and the machines it holds, which is the
        shape of the inventory rather than a list this service invents. A scope
        whose machines do not all receive the same
        `upload_extra_files_upload_files` is offered as unavailable with the
        reason, because appending to the list it holds would shadow the list
        one of its machines is actually getting.
        """
        document = self._inventory.raw()
        if not document.strip():
            return []
        state = self._inventory.state()
        machines = set(state.inventory.hosts) if state.inventory else set()
        guests = set(state.inventory.guests) if state.inventory else set()

        options: list[ScopeOption] = []
        table = groups(document)
        for name in sorted(table):
            members = sorted(_members(table, name) & machines)
            if not members:
                # A group holding only guests, or nothing at all. A quadlet
                # goes on a machine, and `VMs` is not one.
                continue
            options.append(
                ScopeOption(kind="group", name=name, machines=members),
            )
        for host in sorted(machines - guests):
            options.append(ScopeOption(kind="host", name=host, machines=[host]))

        for option in options:
            scope = Scope(option.kind, option.name)
            refusal = self._shadowed(document, sorted(_affected(table, scope)), scope)
            if refusal:
                option.available = False
                option.reason = refusal
        return options

    def declare(
        self,
        name: str,
        scope: Scope,
        source: str,
        author: str,
        expected_head: str | None = None,
        pacemaker: bool = False,
    ) -> Commit:
        """Write one container into the inventory, as a commit.

        Variables the upstream roles already read, and no schema of this
        service: the entry that uploads the file and the `daemon-reload` that
        makes systemd read it, or for a container Pacemaker runs, a workload of
        `cluster_containers`. An operator who exports this inventory and runs
        the playbooks from a control machine gets the same containers.
        """
        document = self._inventory.raw()
        if not document.strip():
            raise InvalidContainer(
                "There is no inventory on this node yet, so there is nothing "
                "to declare a container in."
            )
        if not quadlets.NAME.match(name):
            raise InvalidContainer(
                f"{name!r} cannot be a container name. It becomes the quadlet "
                "file, the systemd unit and, in a cluster, the Pacemaker "
                "resource, so it takes letters, digits, dots, dashes and "
                "underscores."
            )
        if name in self.known():
            raise InvalidContainer(
                f"{name} is already declared in this inventory. A container is "
                "named after the unit it becomes, so two of them cannot share "
                "a name."
            )

        if pacemaker:
            return self.write_workload(
                name, {"quadlets": [source]}, author, expected_head
            )

        table = groups(document)
        # Raises where the scope names a group this file does not declare, or
        # one holding no machine, which is a container uploaded nowhere.
        self._scope_hosts(document, scope, table)
        # Everything the scope reaches, guests included: the prerequisites
        # playbooks play the `VMs` group too, so a variable written on `all`
        # lands on a guest as much as on a machine, and the check that this
        # write did what it said has to know that.
        affected = _affected(table, scope)
        refusal = self._shadowed(document, sorted(affected), scope)
        if refusal:
            raise InvalidContainer(refusal)

        writes, intended = self._writes(document, name, scope, source, affected)

        commit, _ = self._inventory.declare_container(
            name, writes, intended, author, expected_head
        )
        return commit

    def _writes(
        self,
        document: str,
        name: str,
        scope: Scope,
        source: str,
        affected: set[str],
    ) -> tuple[list[tuple[Scope, dict[str, Any]]], dict[str, dict[str, Any]]]:
        """The upload half: the entry, and the reload that makes it a unit."""
        current = _at(document, scope, quadlets.UPLOAD_VARIABLE) or []
        entry = quadlets.upload_entry(name, source)
        uploads = [*current, entry]

        commands = _at(document, scope, quadlets.COMMANDS_VARIABLE) or []
        if not any(
            isinstance(command, str) and quadlets.reloads(command)
            for command in commands
        ):
            commands = [*commands, quadlets.DAEMON_RELOAD]

        variables = {
            quadlets.UPLOAD_VARIABLE: uploads,
            quadlets.COMMANDS_VARIABLE: commands,
        }
        intended = {host: dict(variables) for host in affected}
        return [(scope, variables)], intended

    def write_workload(
        self,
        name: str,
        spec: dict[str, Any],
        author: str,
        expected_head: str | None = None,
        files: dict[str, bytes] | None = None,
        removed: list[str] | None = None,
        message: str | None = None,
    ) -> Commit:
        """Write one workload of `cluster_containers`, its files with it.

        The entry replaces the one of the same name, so a caller updating a
        workload passes the whole of it.
        """
        document = self._inventory.raw()
        writes, intended = self._workload_writes(document, name, spec)
        commit, _ = self._inventory.declare_container(
            name,
            writes,
            intended,
            author,
            expected_head,
            files=files,
            removed=removed,
            message=message,
        )
        return commit

    def workload_hosts(self) -> list[str]:
        """The members a workload runs on, which its placement may name."""
        document = self._inventory.raw()
        if not document.strip():
            return []
        table = groups(document)
        if any(group not in table for group in quadlets.WORKLOAD_GROUPS):
            return []
        return sorted(
            set.intersection(
                *(_members(table, group) for group in quadlets.WORKLOAD_GROUPS)
            )
        )

    def workload(self, name: str) -> dict[str, Any] | None:
        """A workload's entry as the file writes it, or None."""
        document = self._inventory.raw()
        if not document.strip():
            return None
        table = groups(document)
        if "cluster_machines" not in table:
            return None
        machines = sorted(_members(table, "cluster_machines"))
        try:
            scope = _home(
                table,
                quadlets.WORKLOADS_VARIABLE,
                machines,
                Scope("group", "cluster_machines"),
            )
        except InvalidContainer:
            return None
        current = _at(document, scope, quadlets.WORKLOADS_VARIABLE)
        found = current.get(name) if isinstance(current, dict) else None
        return found if isinstance(found, dict) else None

    def remove(
        self,
        name: str,
        remove_rbd: bool,
        author: str,
        expected_head: str | None = None,
    ) -> Commit:
        """Mark a workload `state: absent`, as one commit.

        The role's own way of removing one: its next run stops the resource and
        deletes it with its constraints, and takes the quadlets, the
        configuration, the image archives and the images off every node.
        `remove_rbd` deletes the RBD image too, which is where the workload
        kept its state. Every other key stays, because the role reads the
        quadlets and the images to know what to take away. The entry and its
        files leave the inventory once a run has done it: `forget_removed`.
        """
        spec = self.workload(name)
        if spec is None:
            raise UnknownContainer(
                f"No workload called {name!r} is declared in cluster_containers. "
                "A container uploaded by upload_extra_files has no role to take "
                "it off the machines, so it is removed on the Inventory page."
            )
        if spec.get("state", "present") != "present":
            raise InvalidContainer(
                f"{name} is already marked for removal. The next run of "
                f"{WORKLOAD_PLAYBOOK} takes it off the machines."
            )
        beside = self._colocated_with(name)
        if beside:
            raise InvalidContainer(
                f"{', '.join(beside)} is kept beside {name} through "
                "colocated_with or colocated_vms. Take it out of that list first."
            )
        marked = {**spec, "state": "absent"}
        if remove_rbd:
            marked["remove_rbd"] = True
        else:
            marked.pop("remove_rbd", None)
        kept = (
            "Its RBD image is deleted with it, its snapshots and the images "
            "put aside included."
            if remove_rbd
            else "Its RBD image stays in the pool, and in the backups."
        )
        return self.write_workload(
            name,
            marked,
            author,
            expected_head,
            message=(
                f"containers: remove {name}\n\n"
                f"The next run of {WORKLOAD_PLAYBOOK} stops it and takes it off "
                f"every node. {kept}"
            ),
        )

    def _colocated_with(self, name: str) -> list[str]:
        """The workloads and guests asking to run on the same node as `name`.

        A workload and a guest are both Pacemaker resources and share one
        namespace, so either list may name it.
        """
        found = {
            quadlet.name
            for quadlet in quadlets.workloads(self._inventory.raw())
            if quadlet.name != name
            and name
            in ((self.workload(quadlet.name) or {}).get("colocated_with") or [])
        }
        state = self._inventory.state()
        for guest, entry in state.inventory.guests.items() if state.inventory else []:
            if name in (entry.extra.get("colocated_vms") or []):
                found.add(guest)
        return sorted(found)

    def forget_removed(self, record: RunRecord) -> Commit | None:
        """Take out of the inventory the workloads a run has removed.

        Called when any run ends. A workload counts as removed when the
        inventory the run was given marked it `state: absent` and the run was
        a full run of `deploy_containers_cluster`, over the whole cluster,
        that succeeded: the role has then taken it off every node, and the
        entry names nothing a later run needs. A run that failed keeps the
        entry, so the next run finishes the removal.

        One commit, authored by the operator who launched the run and naming
        it, which takes out the entry and the files it named that nothing else
        names, the folder a delivery installed included. The image archives
        stay in the artefacts, which git does not keep: a restore from the
        Backup page declares the workload again from the history and loads
        them, and the Inventory page deletes one nobody wants back.
        """
        if (
            record.playbook_id != WORKLOAD_PLAYBOOK
            or record.check
            or record.state is not RunState.SUCCESS
            or record.scope.narrowed
            or not record.inventory_commit
        ):
            return None
        applied = quadlets.removed(self._inventory.raw_at(record.inventory_commit))
        document = self._inventory.raw()
        marked = quadlets.removed(document)
        # Still marked as the run read it: an entry put back since is the
        # operator's, and one marked since is for the next run.
        names = sorted(
            name for name in applied if name in marked and marked[name] == applied[name]
        )
        if not names:
            return None

        writes, intended = self._workloads_writes(
            document,
            lambda current: {
                key: value for key, value in current.items() if key not in names
            },
        )
        # A workload marked absent names no file (`references.in_use`), so
        # what it named is read from the entry as if it were still deployed.
        named = set().union(
            *(
                references.workload_in_folder({**marked[name], "state": "present"})
                for name in names
            )
        )
        still = references.in_use(document)
        removed = sorted(
            item.path
            for item in self._inventory.files()
            if item.path not in still
            and (
                item.path in named
                or any(item.path.startswith(f"{_FOLDER}/{name}/") for name in names)
            )
        )

        one = len(names) == 1
        message = (
            f"{FORGET_SUBJECT}{', '.join(names)}\n\n"
            f"Run {record.id} of {WORKLOAD_PLAYBOOK} took "
            f"{'it' if one else 'them'} off every node. "
            f"{'Its entry' if one else 'Their entries'}, marked state: absent, "
            f"and the files {'it' if one else 'they'} named leave the inventory."
        )
        commit, _ = self._inventory.declare_container(
            ", ".join(names),
            writes,
            intended,
            record.launched_by,
            removed=removed,
            message=message,
        )
        audit_event(
            "containers.forgotten",
            run=record.id,
            workloads=",".join(names),
            commit=commit.hash if commit else "",
            user=record.launched_by,
        )
        return commit

    def forgotten(self) -> list[ForgottenWorkload]:
        """The workloads a removal took out of the inventory, newest first.

        Read from the commits `forget_removed` writes: each one's parent still
        holds the entry, marked `state: absent`, and the files it named. A
        name the inventory declares again, in any state, is not listed, and
        only the newest removal of a name counts.
        """
        document = self._inventory.raw()
        if not document.strip():
            return []
        current = {quadlet.name for quadlet in quadlets.workloads(document)}
        current |= set(quadlets.removed(document))
        found: list[ForgottenWorkload] = []
        seen: set[str] = set()
        for commit, message in self._inventory.messages(FORGET_SUBJECT):
            subject = message.splitlines()[0][len(FORGET_SUBJECT) :]
            names = [name.strip() for name in subject.split(",") if name.strip()]
            fresh = [name for name in names if name not in seen | current]
            seen.update(names)
            if not fresh:
                continue
            try:
                before = quadlets.removed(self._inventory.raw_at(f"{commit}^"))
            except RepositoryError:
                continue
            for name in fresh:
                spec = before.get(name)
                if spec is None:
                    continue
                found.append(
                    ForgottenWorkload(
                        name=name,
                        commit=commit,
                        missing_archives=self._missing_archives(spec),
                    )
                )
        return found

    def check_redeclare(self, name: str) -> ForgottenWorkload:
        """The removal `redeclare` would undo, or why it cannot."""
        item = next((entry for entry in self.forgotten() if entry.name == name), None)
        if item is None:
            raise UnknownContainer(
                f"No workload called {name!r} was removed by a run of "
                f"{WORKLOAD_PLAYBOOK} and left undeclared since."
            )
        state = self._inventory.state()
        guests = state.inventory.guests if state.inventory else {}
        if name in guests:
            raise InvalidContainer(
                f"{name} is the name of a guest now, and a workload and a guest "
                "share the one namespace of Pacemaker resources."
            )
        if item.missing_archives:
            raise InvalidContainer(
                f"{name} loads its images from {', '.join(item.missing_archives)}, "
                "which the artefacts no longer hold. Upload "
                f"{'it' if len(item.missing_archives) == 1 else 'them'} again "
                "under files/ on the Inventory page, then restore again."
            )
        return item

    def redeclare(self, name: str, author: str, reason: str) -> Commit:
        """Declare again a workload a removal took out, as it was, one commit.

        The entry its removal marked absent, without `state` and `remove_rbd`,
        and the files that same removal took out of the folder, read from the
        commit before it. A file the folder holds again since is left as it
        is. `reason` ends the message: what deploys it next.
        """
        item = self.check_redeclare(name)
        parent = f"{item.commit}^"
        spec = quadlets.removed(self._inventory.raw_at(parent))[name]
        entry = {
            key: value
            for key, value in spec.items()
            if key not in ("state", "remove_rbd")
        }
        named = references.workload_in_folder(entry)
        present = {stored.path for stored in self._inventory.files()}
        files = {
            path: self._inventory.read_file_at(parent, path)
            for path in self._inventory.deleted_in(item.commit)
            if path not in present
            and (path in named or path.startswith(f"{_FOLDER}/{name}/"))
        }
        return self.write_workload(
            name,
            entry,
            author,
            files=files,
            message=(
                f"containers: declare {name} again\n\n"
                f"Its entry and the files commit {item.commit[:12]} took out of "
                f"the inventory are back as they were before its removal. "
                f"{reason}"
            ),
        )

    def _missing_archives(self, spec: dict[str, Any]) -> list[str]:
        missing = []
        for archive in sorted(_archives(spec)):
            try:
                held = self._inventory.artefact_path(f"files/{archive}").is_file()
            except (OSError, UnsafePath, RefusedFile):
                held = False
            if not held:
                missing.append(archive)
        return missing

    def _workload_writes(
        self, document: str, name: str, spec: dict[str, Any]
    ) -> tuple[list[tuple[Scope, dict[str, Any]]], dict[str, dict[str, Any]]]:
        """A workload of `cluster_containers`, written where the cluster reads it.

        `deploy_containers_cluster` puts it on every hypervisor of the cluster
        and creates its Pacemaker resource, so there is no machine to choose:
        the mapping is written where the cluster members already read it, on
        `cluster_machines` when nothing holds it yet.
        """
        return self._workloads_writes(document, lambda current: {**current, name: spec})

    def _workloads_writes(
        self,
        document: str,
        change: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> tuple[list[tuple[Scope, dict[str, Any]]], dict[str, dict[str, Any]]]:
        """`cluster_containers` as `change` makes it, written where it is read."""
        table = groups(document)
        if any(group not in table for group in quadlets.WORKLOAD_GROUPS):
            raise InvalidContainer(
                "This inventory declares no cluster_machines group among its "
                "hypervisors, so there is no cluster to hold a resource. A "
                "container on a standalone machine is a systemd unit and "
                "starts as one."
            )
        machines = sorted(_members(table, "cluster_machines"))
        scope = _home(
            table,
            quadlets.WORKLOADS_VARIABLE,
            machines,
            Scope("group", "cluster_machines"),
        )
        current = _at(document, scope, quadlets.WORKLOADS_VARIABLE) or {}
        if not isinstance(current, dict):
            raise InvalidContainer(
                "cluster_containers holds something other than a mapping in "
                "this inventory, and adding a workload to it would rewrite "
                "what is there."
            )
        variables = {quadlets.WORKLOADS_VARIABLE: change(current)}
        intended = {host: dict(variables) for host in _affected(table, scope)}
        return [(scope, variables)], intended

    def _scope_hosts(self, document: str, scope: Scope, table: Any) -> list[str]:
        state = self._inventory.state()
        machines = set(state.inventory.hosts) if state.inventory else set()
        if scope.is_group:
            if scope.name not in table:
                raise InvalidContainer(
                    f"This inventory declares no group called {scope.name}."
                )
            hosts = sorted(_members(table, scope.name) & machines)
            if not hosts:
                raise InvalidContainer(
                    f"{scope.name} holds no machine of this inventory, so a "
                    "container declared there would be uploaded nowhere."
                )
            return hosts
        if scope.name not in machines:
            raise InvalidContainer(f"{scope.name} is not a machine of this inventory.")
        return [scope.name]

    def _shadowed(self, document: str, hosts: list[str], scope: Any) -> str:
        """Whether appending at this scope would append to the wrong list.

        Ansible does not merge a variable, it replaces it: a host that receives
        `upload_extra_files_upload_files` from a group and gets one written on
        itself keeps only the second, and the group's other uploads stop
        happening on that machine. So the entry goes where the value those
        machines actually receive already is, and anything else is refused with
        the place named.
        """
        table = groups(document)
        resolved = resolve(document)
        for host in hosts:
            if quadlets.UPLOAD_VARIABLE not in resolved.get(host, {}):
                continue
            source = _source_of(table, host, quadlets.UPLOAD_VARIABLE)
            if source is None or _same(source, scope):
                continue
            written = f"{scope.kind} {scope.name}"
            return (
                f"{host} already receives upload_extra_files_upload_files from "
                f"{source.kind} {source.name}, and Ansible replaces a variable "
                f"rather than merging it. Writing it on {written} would leave "
                f"{host} with this container alone and drop what "
                f"{source.name} uploads. Declare it on {source.name}, or "
                "move that variable first."
            )
        return ""

    def _scope_of(self, document: str, hosts: list[str], variable: str) -> Scope:
        """Where the entry that declares a container sits, for the page."""
        table = groups(document)
        if hosts:
            source = _source_of(table, hosts[0], variable)
            if source is not None:
                return source
        return Scope("host", hosts[0] if hosts else "")

    def _resources(
        self, cluster: PacemakerCluster
    ) -> tuple[dict[str, PacemakerResource], str]:
        """Pacemaker's systemd resources, by the unit each one holds.

        Keyed on the unit rather than on the resource id, because the id is the
        site's to choose and the unit is what the agent was given. A resource
        called `mqtt` holding `mosquitto.service` is the same container as the
        quadlet called `mosquitto`, and matching on names would have missed it.
        """
        if cluster.error:
            return {}, cluster.error
        found: dict[str, PacemakerResource] = {}
        for resource in cluster.resources:
            unit = _unit_of(resource.agent)
            if unit:
                found.setdefault(unit, resource)
        return found, _FROM_EXPORTERS

    def _units(
        self,
        document: str,
        declared: list[quadlets.Quadlet],
        resources: dict[str, PacemakerResource],
    ) -> tuple[dict[tuple[str, str], NodeUnit], list[str]]:
        """Every declared unit's state, on every machine that declares it.

        One GET per machine, on the port the CPU pool is already read from, and
        the units asked about are the ones this inventory declares. A machine
        that does not answer costs its own rows and nothing else.
        """
        state = self._inventory.state()
        if state.inventory is None:
            return {}, []
        wanted: dict[str, set[str]] = {}
        for quadlet in declared:
            wanted.setdefault(quadlet.host, set()).add(quadlet.unit)

        targets = [
            (name, node.ansible_host)
            for name, node in state.inventory.hosts.items()
            if node.ansible_host and name in wanted
        ]
        addresses = dict(targets)
        found: dict[tuple[str, str], NodeUnit] = {}
        warnings: list[str] = []

        for exposition in read_all(
            self._client, targets, self._port, timeout=self._timeout
        ):
            host = exposition.host
            units = wanted.get(host, set())
            if exposition.series is None:
                for unit in units:
                    found[(host, unit)] = NodeUnit(
                        host=host,
                        address=exposition.address,
                        reachable=False,
                        error=exposition.error,
                    )
                continue
            if not systemd.reporting(exposition):
                warnings.append(f"{host} {_NO_COLLECTOR}.")
            states = systemd.read(exposition.series, units)
            for unit in units:
                published = states.get(unit)
                found[(host, unit)] = NodeUnit(
                    host=host,
                    address=exposition.address,
                    reachable=True,
                    known=published is not None,
                    state=published.state if published else "unknown",
                    active=bool(published and published.active),
                    failed=bool(published and published.failed),
                    started_at=(
                        published.started_at.isoformat()
                        if published and published.started_at
                        else None
                    ),
                )

        for host in wanted:
            if host not in addresses:
                for unit in wanted[host]:
                    found[(host, unit)] = NodeUnit(
                        host=host,
                        reachable=False,
                        error=(
                            "This machine has no address in the inventory, so "
                            "its exporter cannot be asked."
                        ),
                    )
        return found, warnings

    def _container_warnings(
        self,
        quadlet: quadlets.Quadlet,
        resource: PacemakerResource | None,
        files: dict[tuple[str, str], Reference],
    ) -> list[str]:
        """What this container's own file says that its management contradicts."""
        found: list[str] = []
        reference = files.get((quadlet.host, quadlet.src))
        if reference is not None and not reference.found:
            found.append(
                "The quadlet file is not in the inventory, so a convergence "
                "would fail on every machine at once when it tried to copy it."
            )
        if resource is not None and self._starts_itself(reference):
            found.append(
                "This quadlet carries an [Install] section and Pacemaker holds "
                "a resource for it, so systemd starts the container at boot "
                "and the cluster starts it too. One of the two has to go: on a "
                "cluster the [Install] section is what to remove."
            )
        return found

    def _starts_itself(self, reference: Reference | None) -> bool:
        if reference is None or not reference.found or not reference.resolved:
            return False
        path = Path(reference.resolved)
        try:
            if path.stat().st_size > _MAX_QUADLET_BYTES:
                return False
            return quadlets.starts_itself(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            # A file this service could not read is not a finding about the
            # container. The reference above already says whether it is there.
            return False


# The variables a container is declared by, which are the references its file
# is found through.
_DECLARING = (quadlets.UPLOAD_VARIABLE, quadlets.WORKLOADS_VARIABLE)


# The subject of the commit `forget_removed` writes, which `forgotten` reads
# the history by.
FORGET_SUBJECT = "containers: forget "


# Where a delivery installs a workload's small files, `inventories/<name>/`
# (`delivery.folder`), which goes whole with the workload.
_FOLDER = "inventories"


def _archives(spec: dict[str, Any]) -> set[str]:
    """The archive file names a workload's images are loaded from."""
    images = spec.get("images")
    found: set[str] = set()
    for image in images if isinstance(images, list) else []:
        if isinstance(image, dict) and isinstance(image.get("archive"), str):
            found.add(Path(image["archive"]).name)
    return found


def _variable(quadlet: quadlets.Quadlet) -> str:
    """The variable that declares this container."""
    return quadlets.WORKLOADS_VARIABLE if quadlet.workload else quadlets.UPLOAD_VARIABLE


def _fill(entry: QuadletFile, reference: Reference | None) -> QuadletFile:
    """The file's text in `entry`, or the sentence saying why it is not."""
    try:
        entry.content = _read(entry.src, reference)
    except UnreadableQuadlet as error:
        entry.error = str(error)
        return entry
    entry.where = reference.where.value if reference and reference.where else ""
    entry.path = reference.resolved if reference else ""
    return entry


def _read(source: str, reference: Reference | None) -> str:
    """One file the inventory names, where a run would read it."""
    if "{{" in source:
        raise UnreadableQuadlet(
            f"{source} is templated, so which file it names is "
            "Ansible's answer while the run is happening rather than this "
            "service's before it starts."
        )

    if reference is None or not reference.found or not reference.resolved:
        expected = reference.expected if reference else None
        raise UnreadableQuadlet(
            f"Nothing this node holds answers to {source}, so there "
            "is no file to show and a convergence would fail on every "
            "machine at once when it tried to copy it."
            + (f" Upload it as {expected} on the Inventory page." if expected else "")
        )
    if reference.where is Where.NODE:
        raise UnreadableQuadlet(
            f"{source} is an absolute path on this machine, outside "
            "the folders a run overlays. This page shows what the "
            "inventory carries, and a file beside it is read where it "
            "lives."
        )

    path = Path(reference.resolved)
    try:
        size = path.stat().st_size
    except OSError as error:
        raise UnreadableQuadlet(
            f"{source} could not be read: {error.strerror or error}."
        ) from error
    if size > _MAX_QUADLET_BYTES:
        raise UnreadableQuadlet(
            f"{source} is {size} bytes. A quadlet is a few hundred, "
            f"so anything past {_MAX_QUADLET_BYTES} is something else and "
            "the Inventory page is where the folder is read."
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise UnreadableQuadlet(
            f"{source} is not text this page can show: {error}."
        ) from error


def _readable(reference: Reference | None) -> bool:
    """Whether the page can offer to open the quadlet.

    The same three refusals `_read` raises, asked of the reading rather
    than of the act: a name that opens a window saying the file is not here is
    a name an operator clicks once and stops trusting.
    """
    if reference is None or not reference.found or not reference.resolved:
        return False
    return reference.where is not Where.NODE


def _place(
    view: ContainerView, cluster: PacemakerCluster, resource: PacemakerResource
) -> None:
    """What decides where a container runs, and where a move could send it.

    Three things are held against each other, as on the VMs page: the entry's
    `preferred_host`, the rule in force, and the node the resource is on. The
    rule in force is the `cli-prefer` a move writes where there is one, since
    its infinite score overrides everything else, and otherwise the rule a
    deployment wrote from `preferred_host`.

    A rule naming another node than the one the resource is on is the cluster
    not following it, which outranks any disagreement about where the
    container belongs. An entry naming a member no rule carries is the
    inventory ahead of the cluster, the state between an edit and the
    deployment run that writes it, and it disagrees the same way a move away
    from the declared member does.
    """
    constraint = ha.preference(cluster, resource.id)
    preference = ha.declared(cluster, resource.id)
    pinned = ha.pin(cluster, resource.id)
    view.constraint = constraint
    view.preference = preference
    view.pinned = pinned.id if pinned else ""
    held = constraint or preference
    declared = view.preferred_host
    if pinned is not None:
        view.placement = "pinned"
    elif held is not None and held.node != resource.node:
        view.placement = "displaced"
    elif held is None:
        view.placement = "adrift" if declared else "free"
    else:
        view.placement = "kept" if held.node == declared else "adrift"
    # A pinned resource runs where it is pinned or nowhere, and a clone runs
    # one instance per member: neither has a node to be sent to, and the whole
    # placement pair is withheld rather than offered and refused.
    if pinned is None and not resource.clone:
        view.destinations = _destinations(cluster, resource)


def _destinations(cluster: PacemakerCluster, resource: PacemakerResource) -> list[str]:
    """The members a move may name, which is what makes a Move button honest.

    A member that is online and out of standby, minus the ones this resource is
    banned from, minus the node it is already running on: Pacemaker refuses a
    move to the node a resource is active on, and a preference on a banned node
    is a rule that would change nothing.

    The ban a move writes on the node the deployment's rule names is the
    move's own, and that node stays a destination: `crm resource move` clears
    the bans on the node it names, so a move back there removes it.
    """
    kept = ha.declared(cluster, resource.id)
    banned = {
        item.node
        for item in cluster.constraints
        if item.resource == resource.id
        and item.id.startswith(ha.BAN_PREFIX)
        and not (kept is not None and item.node == kept.node)
    }
    return [
        node.name
        for node in cluster.nodes
        if node.type != "ping"
        and node.online
        and "standby" not in node.flags
        and node.name not in banned
        and node.name != resource.node
    ]


def _unit_of(agent: str) -> str:
    """The unit a Pacemaker resource holds, when its agent is systemd's.

    `systemd:mosquitto.service` and `systemd:mosquitto` are the same resource
    written two ways, and `crm` accepts both, so the suffix is added back where
    the site left it out.
    """
    if not agent:
        return ""
    parts = agent.split(":")
    if parts[0] != SYSTEMD_AGENT or len(parts) < 2:
        return ""
    unit = parts[-1].strip()
    if not unit:
        return ""
    return unit if "." in unit else f"{unit}.service"


def _at(document: str, scope: Scope, variable: str) -> Any:
    """The value a scope holds for a variable, or None when it holds none."""
    table = groups(document)
    if scope.is_group:
        group = table.get(scope.name)
        return group.variables.get(variable) if group else None
    for group in table.values():
        if scope.name in group.hosts and variable in group.hosts[scope.name]:
            return group.hosts[scope.name][variable]
    return None


def _source_of(table: dict[str, Any], host: str, variable: str) -> Scope | None:
    """Where a host's value for a variable comes from, in Ansible's order.

    The host's own entry wins, then the deepest group, which is the order
    `resolve` applies. What this answers is "the list this machine receives is
    written where", and that is the only place another entry can be appended.
    """
    for group in table.values():
        if host in group.hosts and variable in group.hosts[host]:
            return Scope("host", host)
    holders = [
        group.name
        for group in table.values()
        if variable in group.variables and host in _members(table, group.name)
    ]
    if not holders:
        # `all` holds every host whatever the file says about membership.
        root = table.get(ROOT)
        if root and variable in root.variables:
            return Scope("group", ROOT)
        return None
    # The deepest group, then the last alphabetically, which is the order
    # `resolve` applies them in and therefore the one whose value the host
    # actually receives.
    order = depths(table)
    return Scope("group", sorted(holders, key=lambda name: (order[name], name))[-1])


def _affected(table: dict[str, Any], scope: Scope) -> set[str]:
    """Every host a write at this scope reaches, guests included."""
    return _members(table, scope.name) if scope.is_group else {scope.name}


def _home(
    table: dict[str, Any], variable: str, hosts: list[str], default: Scope
) -> Scope:
    """Where a variable these hosts share already lives.

    The place to append to, which is the place they read it from. Hosts that
    read it from two different places are refused rather than picked between:
    one of the two values would stop being the one that counts, and nothing on
    a page would say which.
    """
    sources = {_source_of(table, host, variable) for host in hosts}
    found = {source for source in sources if source is not None}
    if not found:
        return default
    if len(found) > 1:
        places = ", ".join(sorted(f"{source.kind} {source.name}" for source in found))
        raise InvalidContainer(
            f"The cluster members do not all read {variable} from the same "
            f"place: it is written on {places}. Appending to one of them would "
            "leave the other saying something else, so this is a change to "
            "make in the inventory first."
        )
    return found.pop()


def _same(source: Scope, scope: Any) -> bool:
    return source.kind == scope.kind and source.name == scope.name


def _members(table: dict[str, Any], name: str) -> set[str]:
    """The hosts of a group and of everything below it."""
    if name == ROOT:
        return {host for group in table.values() for host in group.hosts}
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
