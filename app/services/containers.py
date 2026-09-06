# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The containers the inventory declares, and what the machines do with them.

A container in SEAPATH is a quadlet: a `.container` file the inventory uploads
to `/etc/containers/systemd/`, which podman's generator turns into a systemd
unit. That gives one object with three faces, and this joins them:

- **declared**, by `upload_extra_files_upload_files`, which says which machines
  receive the file and which file it is. `app/inventory/quadlets.py` reads it.
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

This writes to the inventory and to nothing else. Deploying a quadlet is the
prerequisites playbook, which is where `upload_extra_files` runs; making one a
Pacemaker resource is `cluster_setup_ha`, which is where `extra_crm_cmd_to_run`
is loaded into the CIB. Both are ordinary runs, named here and launched through
`/runs` like every other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.cluster import systemd
from app.cluster.exporters import MetricsClient, UrllibMetricsClient, read_all
from app.cluster.ha import PacemakerResource
from app.cluster.pool import DEFAULT_PORT
from app.inventory import quadlets
from app.inventory.editor import Scope
from app.inventory.model import Mode
from app.inventory.references import Reference
from app.inventory.repository import Commit
from app.inventory.resolve import ROOT, depths, groups, resolve
from app.inventory.service import InventoryService
from app.runs.catalogue import CATALOGUE
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
CLUSTER_PLAYBOOK = "cluster_setup_ha"

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
    "`.container` file uploaded to /etc/containers/systemd, which is an entry "
    "of `upload_extra_files_upload_files`. Add one here and the file is "
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
    dest: str
    mode: str = ""
    scope_kind: str = "host"
    scope_name: str = ""
    """Where the entry that declares it sits, which is what the page shows and
    what a second declaration is written into."""
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


class ScopeOption(BaseModel):
    """Somewhere a declaration can be written, offered to the form."""

    kind: str
    name: str
    machines: list[str] = Field(default_factory=list)
    available: bool = True
    reason: str = ""
    """Why this scope is refused, when it is: the variable it would append to
    is not the one those machines receive."""


class ContainersView(BaseModel):
    mode: str = Mode.STANDALONE.value
    containers: list[ContainerView] = Field(default_factory=list)
    undeclared: list[PacemakerResource] = Field(default_factory=list)
    """Units the cluster runs as resources and no quadlet here explains."""
    scopes: list[ScopeOption] = Field(default_factory=list)
    upload_playbook: str = FULL_CONVERGENCE
    """The run that puts the files on the machines and reloads systemd."""
    cluster_playbook: str = CLUSTER_PLAYBOOK
    """The run that loads the primitives into the CIB."""
    runtime_note: str = ""
    note: str = ""
    warnings: list[str] = Field(default_factory=list)
    inventory_commit: str | None = None


class InvalidContainer(Exception):
    """The declaration cannot become an entry, and the message says why."""


class UnknownContainer(Exception):
    """No container of that name is declared here."""


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

        declared = quadlets.declared(document)
        resources, note = self._resources()
        view.runtime_note = note
        units, warnings = self._units(document, declared, resources)
        view.warnings = warnings

        files = {
            (reference.host, reference.value): reference
            for reference in self._inventory.references()
            if reference.variable == quadlets.UPLOAD_VARIABLE
        }

        for name in sorted({quadlet.name for quadlet in declared}):
            entries = [quadlet for quadlet in declared if quadlet.name == name]
            first = entries[0]
            hosts = [quadlet.host for quadlet in entries]
            resource = resources.get(first.unit)
            scope = self._scope_of(document, hosts)
            view.containers.append(
                ContainerView(
                    name=name,
                    kind=first.kind,
                    unit=first.unit,
                    actionable=first.actionable,
                    src=first.src,
                    dest=first.dest,
                    mode=first.mode,
                    scope_kind=scope.kind,
                    scope_name=scope.name,
                    hosts=hosts,
                    file=files.get((first.host, first.src)),
                    managed="pacemaker" if resource else "systemd",
                    resource=resource,
                    units=[units[(host, first.unit)] for host in hosts],
                    warnings=self._container_warnings(first, resource, files),
                )
            )

        explained = {quadlet.unit for quadlet in declared}
        view.undeclared = [
            resource
            for unit, resource in sorted(resources.items())
            if unit not in explained
        ]
        if not view.containers:
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
        found: dict[str, quadlets.Quadlet] = {}
        for quadlet in quadlets.declared(self._inventory.raw()):
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

    def resource_for(self, unit: str) -> PacemakerResource | None:
        """The Pacemaker resource holding this unit, when the cluster has one."""
        return self._resources()[0].get(unit)

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

    @property
    def cluster_playbook(self) -> str:
        """The run that loads the primitives into the CIB.

        `configure_ha` is the only place `extra_crm_cmd_to_run` is read, and it
        is the last thing that playbook does, so a container handed to
        Pacemaker becomes a resource on the next `cluster_setup_ha` and not
        before.
        """
        return CLUSTER_PLAYBOOK

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

        Three variables and no new schema, which is the point: the entry that
        uploads the file, the `daemon-reload` that makes systemd read it, and
        for a cluster container the `crm` primitive that hands it to Pacemaker.
        Every one of them is a variable the upstream roles already read, and an
        operator who exports this inventory and runs the playbooks from a
        control machine gets the same containers.
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
        if pacemaker:
            self._cluster_writes(document, name, writes, intended)

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

    def _cluster_writes(
        self,
        document: str,
        name: str,
        writes: list[tuple[Scope, dict[str, Any]]],
        intended: dict[str, dict[str, Any]],
    ) -> None:
        """The Pacemaker half: one primitive, appended to `extra_crm_cmd_to_run`.

        Written where that variable already lives, and on `cluster_machines`
        when nothing holds it yet. `configure_ha` loads it with `run_once`, so
        the value that counts is the one the member Ansible happens to play
        first: a primitive written on a host, or on a group shadowing the one
        the cluster already reads, would be a coin toss between the two values.
        """
        table = groups(document)
        if "cluster_machines" not in table:
            raise InvalidContainer(
                "This inventory declares no cluster_machines group, so there "
                "is no cluster to hold a resource. A container on a standalone "
                "machine is a systemd unit and starts as one."
            )
        members = sorted(_members(table, "cluster_machines"))
        scope = _home(
            table, quadlets.CRM_VARIABLE, members, Scope("group", "cluster_machines")
        )
        affected = _affected(table, scope)
        unit = quadlets.unit_for(f"{quadlets.QUADLET_DIR}/{name}.container")
        current = _at(document, scope, quadlets.CRM_VARIABLE) or ""
        if not isinstance(current, str):
            raise InvalidContainer(
                "extra_crm_cmd_to_run holds something other than text in this "
                "inventory, and appending a primitive to it would rewrite what "
                "is there."
            )
        line = quadlets.primitive(name, unit)
        if line in current:
            raise InvalidContainer(f"{name} is already a primitive in this file.")
        value = f"{current.rstrip()}\n{line}\n" if current.strip() else f"{line}\n"

        variables = {quadlets.CRM_VARIABLE: value}
        writes.append((scope, variables))
        for host in affected:
            intended.setdefault(host, {}).update(variables)

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

    def _scope_of(self, document: str, hosts: list[str]) -> Scope:
        """Where the entry that declares a container sits, for the page."""
        table = groups(document)
        if hosts:
            source = _source_of(table, hosts[0], quadlets.UPLOAD_VARIABLE)
            if source is not None:
                return source
        return Scope("host", hosts[0] if hosts else "")

    def _resources(self) -> tuple[dict[str, PacemakerResource], str]:
        """Pacemaker's systemd resources, by the unit each one holds.

        Keyed on the unit rather than on the resource id, because the id is the
        site's to choose and the unit is what the agent was given. A resource
        called `mqtt` holding `mosquitto.service` is the same container as the
        quadlet called `mosquitto`, and matching on names would have missed it.
        """
        cluster = self._cluster.pacemaker()
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
