# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Real time conformance: what the inventory declared, and what the machines got.

This is the half of the real time story that is a reading. The other half is a
measurement, `cyclictest`, which runs on the machine through an Ansible run and
has a record of its own.

The checks themselves are in `app/services/checks.py`, and the framing they
carry is worth reading there. This module is what feeds them: it decides which
readings each machine's checks are formed from, and which inventory entry they
are held against.

One source, one implementation. Every node's readings arrive from its
exporter, where `seapath-alloc` publishes them beside the pool (D27), the
machine serving this page included (D36). Each reading carries its age, and a
node running a collector too old to publish the block says so.

The local node keeps a reading of its own for the machine that has no other:
its `/proc`, `/sys` and the host `/etc` PAM already brought in answer where
nothing has been deployed yet, which is what a silent exporter falls back to
and what `GET /api/v1/realtime` reports on its own.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.cluster.pool import ClusterPool, NodePool, PoolReader
from app.core.errors import ApiError
from app.core.logging import audit_event
from app.hosts.models import CpuReading, Reading, RealtimeReading
from app.hosts.reader import HostReader
from app.inventory.editor import Scope
from app.inventory.model import NodeConfig, Role
from app.inventory.repository import Commit
from app.inventory.resolve import Group, groups, members
from app.inventory.service import InventoryService
from app.runs.cyclictest import CyclictestResult
from app.runs.hwlatdetect import HwlatdetectResult
from app.runs.models import RunRecord, RunState
from app.runs.scope import RunScope
from app.runs.service import RunService
from app.services import checks as checks_module
from app.services.checks import SEAPATH_PROFILE, Check, Kind, Status, cpu_list

# Re-exported: the checks moved to `app/services/checks.py` when they stopped
# being about the local machine, and importing them from here still reads
# correctly.
__all__ = [
    "SEAPATH_PROFILE",
    "Check",
    "Kind",
    "MeasurementKind",
    "Measurement",
    "RealtimeConformance",
    "RealtimeService",
    "Status",
]


class RealtimeConformance(Reading):
    """What `GET /api/v1/realtime` answers."""

    hostname: str
    this_host: str | None = None
    """The inventory entry describing this machine, when there is one."""
    inventory_commit: str | None = None
    role: Role | None = None
    checks: list[Check] = Field(default_factory=list)
    reading: RealtimeReading
    cpu: CpuReading

    @property
    def warning_count(self) -> int:
        return sum(1 for check in self.checks if check.status is Status.WARNING)


class AllocationStrategy(str, Enum):
    """How `seapath-alloc` orders the isolated threads it hands out.

    The three values `deploy_seapath_alloc` accepts for
    `seapath_alloc_strategy`, which it writes to `/etc/seapath/alloc.yaml`.
    The allocator reads that file at each allocation, so a new strategy
    applies to the next guest started or container pinned on the machine, and
    what is already pinned stays where it is.
    """

    SPREADING = "spreading"
    """One thread per physical core, the role's default."""
    PACKING = "packing"
    """Both hyperthreads of a core before the next core."""
    REPACKING = "repacking"
    """Spreading, but an `exclusive_physical` request with no free pair
    compacts the logical threads of the guests already running to free one."""


STRATEGY_VARIABLE = "seapath_alloc_strategy"
STRATEGY_PLAYBOOK = "seapath_setup_deploy_seapath_alloc"


class UnknownMachine(Exception):
    """The inventory declares no machine by that name."""


class MeasurementKind(str, Enum):
    """Which question a measurement run asked.

    The three are complementary, and the page keeps them apart because their
    answers are of different kinds. `cyclictest` measures what the scheduler
    delivered, which the tuning can change. `hwlatdetect` measures what the
    firmware took without telling the kernel, which no variable in the
    inventory reaches.

    `guest_cyclictest` is the same tool run inside a guest rather than on the
    machine, over the SSH path the operator opened to that guest. It is a
    history of its own because it answers for a different thing: the number
    includes the scheduling of the vCPU threads, the VM exits and the
    virtualised timer, and it belongs to one guest rather than to the machines
    the inventory declares. Averaging it with the host figures, or listing the
    two under one heading, would hide exactly the difference the pair exists to
    show.
    """

    CYCLICTEST = "cyclictest"
    HWLATDETECT = "hwlatdetect"
    GUEST_CYCLICTEST = "guest_cyclictest"


# The catalogue entry behind each, and the variable the service fills with the
# run's own results directory. Keyed by playbook id, which is what a run record
# carries, so a run launched from the Deployment page is recognised here too.
MEASUREMENT_PLAYBOOKS = {
    "test_run_cyclictest": MeasurementKind.CYCLICTEST,
    "test_run_hwlatdetect": MeasurementKind.HWLATDETECT,
    "test_run_cyclictest_vms": MeasurementKind.GUEST_CYCLICTEST,
}

# The kinds whose results are cyclictest histograms, whatever they were measured
# on. The parser is the same one: the role fetches `cyclictest_<host>.txt` and
# the host is a guest name here.
_HISTOGRAM_KINDS = frozenset(
    {MeasurementKind.CYCLICTEST, MeasurementKind.GUEST_CYCLICTEST}
)
_RESULTS_VARIABLES = frozenset(
    {"cyclictest_result_folder", "hwlatdetect_result_folder"}
)


class Measurement(BaseModel):
    """One measurement run, and what it brought back."""

    kind: MeasurementKind
    run_id: str
    state: RunState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    launched_by: str
    inventory_commit: str | None = None
    """Which desired state the machines were carrying when this was measured.

    The pair that makes a measurement worth keeping. A latency figure with no
    idea which isolation it was taken under is an anecdote, and the same run
    record already carries the collection version beside it.
    """
    variables: dict[str, object] = Field(default_factory=dict)
    latency: list[CyclictestResult] = Field(default_factory=list)
    """Filled on a cyclictest run, one entry per machine the run measured.

    On a guest measurement the entry is the guest, since the role names the file
    it fetches after the inventory host it ran on.
    """
    interruptions: list[HwlatdetectResult] = Field(default_factory=list)
    """Filled on a hwlatdetect run, one entry per machine."""

    @property
    def machines(self) -> int:
        return len(self.latency) + len(self.interruptions)


class RealtimeService:
    def __init__(
        self,
        reader: HostReader,
        inventory: InventoryService,
        runs: RunService,
        hostname: str,
        pool: PoolReader | None = None,
    ) -> None:
        self._reader = reader
        self._inventory = inventory
        self._runs = runs
        self._hostname = hostname
        self._pool = pool or PoolReader()

    def pool(self) -> ClusterPool:
        """The CPU pool of every machine the inventory declares.

        The one reading here that leaves this machine, and the only one that
        can answer the question at all: occupancy comes from the affinity of
        every QEMU thread in `/proc`, which this container's PID namespace
        hides. `seapath-alloc` computes it on each host and publishes it
        through the exporter every node already runs, so this asks rather than
        duplicates. See `app/cluster/pool.py`.
        """
        state = self._inventory.state()
        if state.inventory is None:
            # A machine with no inventory still has a conformance list, and it
            # is the list an operator reads before writing the isolation down.
            # So the local node is returned alone rather than an empty cluster.
            return ClusterPool(
                nodes=[self._local_node()],
                this_host=state.this_host,
                inventory_commit=state.commit,
                available=False,
            )

        hosts = {
            name: node
            for name, node in state.inventory.hosts.items()
            if node.ansible_host
        }
        nodes = self._pool.read(
            [(name, node.ansible_host) for name, node in hosts.items()]
        )
        # The file is read for where the strategy is written only when some
        # machine receives one, which most inventories never declare.
        table = (
            groups(self._inventory.raw())
            if any(STRATEGY_VARIABLE in node.extra for node in hosts.values())
            else {}
        )
        for node in nodes:
            declared = hosts.get(node.host)
            node.declared_isolcpus = declared.isolcpus if declared else None
            if declared is not None and STRATEGY_VARIABLE in declared.extra:
                node.alloc_strategy = declared.extra[STRATEGY_VARIABLE]
                node.alloc_strategy_on = _declared_on(table, node.host)
            self._judge(node, declared, state.this_host)
        if state.this_host is None or state.this_host not in hosts:
            # The machine the browser is pointed at, when the inventory has no
            # entry for it. It is the one node whose readings need no exporter,
            # and leaving it out would drop the only column that always works.
            nodes.append(self._local_node())
        # By name, because the columns of the conformance matrix and the cards
        # of the pool are read against each other across reloads, and the order
        # the inventory happens to list its hosts in moves under the operator.
        # The local node carries its own tag, so it does not need a position.
        nodes.sort(key=lambda node: node.host)
        return ClusterPool(
            nodes=nodes,
            this_host=state.this_host,
            inventory_commit=state.commit,
            available=any(node.cpus for node in nodes),
        )

    def set_strategy(
        self,
        host: str,
        strategy: AllocationStrategy,
        author: str,
        expected_head: str | None = None,
    ) -> tuple[Commit | None, RunRecord | None]:
        """Write one machine's `seapath_alloc_strategy`, and put it on the machine.

        One commit on the machine's own entry, then one run of
        `seapath_setup_deploy_seapath_alloc` narrowed to it, which templates
        `/etc/seapath/alloc.yaml` from the committed value. Written on the host
        rather than on a group, since the page asks it of one machine and a
        group value would change the others behind the operator's back.

        Nothing is committed when the machine already receives this value, the
        role default included, and nothing is run either. The playbook's
        availability is asked before the commit, so the inventory never says
        something no run from here can apply.
        """
        state = self._inventory.state()
        if state.inventory is None or host not in state.inventory.hosts:
            raise UnknownMachine(f"{host} is not a machine of the inventory.")
        received = state.inventory.hosts[host].extra.get(STRATEGY_VARIABLE)
        if (received or AllocationStrategy.SPREADING.value) == strategy.value:
            return None, None
        offered = self._runs.playbooks({STRATEGY_PLAYBOOK})
        if offered and not offered[0].available:
            raise ApiError(
                "precondition_failed",
                offered[0].unmet[0],
                409,
                {"unmet": offered[0].unmet, "codes": offered[0].unmet_codes},
            )
        commit = self._inventory.write_variables(
            [(Scope(kind="host", name=host), {STRATEGY_VARIABLE: strategy.value})],
            {host: {STRATEGY_VARIABLE: strategy.value}},
            f"realtime: {strategy.value} CPU allocation on {host}\n\n"
            "Written to /etc/seapath/alloc.yaml by deploy_seapath_alloc, and "
            "read by seapath-alloc at the next allocation on the machine.",
            author,
            expected_head=expected_head,
        )
        if commit is None:
            return None, None
        audit_event(
            "realtime.alloc_strategy",
            host=host,
            strategy=strategy.value,
            commit=commit.hash,
            user=author,
        )
        record = self._runs.launch(
            STRATEGY_PLAYBOOK, author, scope=RunScope(hosts=[host])
        )
        return commit, record

    def _judge(
        self, node: NodePool, declared: NodeConfig | None, this_host: str | None
    ) -> None:
        """Run the ten checks against one node, from the best reading available.

        Every node is judged on what its exporter published, the machine
        serving this page included. One reading per machine is what keeps it
        from disagreeing with itself, which is the whole of D36: the local
        files and the exporter answer the same ten checks, and two readers of
        one question drift apart where the container masks a path.

        A node that published no tuning gets no checks rather than ten
        unknowns. `tuning_error` already says what to do about it, and a column
        of grey dots would bury it. The local machine is the one exception, in
        the direction that costs nothing: its files are the only ones this
        service can read, so a silent exporter there falls back to them instead
        of emptying the column. That is the machine D27 was defending, the one
        where nothing has been deployed yet.
        """
        # The clock is judged apart from the tuning, and on any node whose
        # exporter said something about it: timex is node_exporter's own, so a
        # node with no seapath-alloc yet still has a clock worth reading.
        clock = (
            checks_module.clock(node.clock, declared)
            if node.clock is not None and node.clock.published
            else []
        )
        if node.reading is None:
            if node.host == this_host:
                local = self.conformance()
                node.reading = local.reading
                node.kernel_cmdline = local.cpu.kernel_cmdline or ""
                node.tuning_error = ""
                node.checks = local.checks
            node.checks = [*node.checks, *clock]
            return
        node.checks = (
            checks_module.run(
                node.reading,
                CpuReading(
                    isolated=node.isolated,
                    kernel_cmdline=node.kernel_cmdline or None,
                ),
                declared,
            )
            + clock
        )

    def _local_node(self) -> NodePool:
        """This machine as a node of the cluster view, read from its own files.

        No exporter, no address, no CPU grid: the grid is the affinity of every
        thread in `/proc`, which this container cannot see, and that is the one
        thing on this page a node has to publish to answer. Everything else is
        a file this container already reads.
        """
        local = self.conformance()
        return NodePool(
            host=local.this_host or self._hostname,
            address="",
            reachable=True,
            reading=local.reading,
            kernel_cmdline=local.cpu.kernel_cmdline or "",
            checks=local.checks,
            observed_isolcpus=cpu_list(local.cpu.isolated),
            declared_isolcpus=None,
            kernel=local.reading.kernel_version or "",
            preemption=local.reading.preemption or "",
        )

    def measurements(
        self, kind: MeasurementKind | None = None, limit: int = 10
    ) -> list[Measurement]:
        """The measurement runs this node has launched, newest first.

        Runs, not readings. The measurement happened on the machines through
        Ansible and left a record like any other run, so the history is the run
        history filtered rather than a second store of results.
        """
        found = []
        for record in self._runs.list(limit=200):
            of = MEASUREMENT_PLAYBOOKS.get(record.playbook_id)
            if of is None or (kind is not None and of is not kind):
                continue
            found.append(self._measurement(record, of))
            if len(found) >= limit:
                break
        return found

    def _measurement(self, record: RunRecord, kind: MeasurementKind) -> Measurement:
        return Measurement(
            kind=kind,
            run_id=record.id,
            state=record.state,
            started_at=record.started_at,
            finished_at=record.finished_at,
            launched_by=record.launched_by,
            inventory_commit=record.inventory_commit,
            # Records written before the injected path was kept out of them
            # still carry it, and it is a path inside this container that means
            # nothing to a reader of the page. New records hold only what the
            # operator chose, which is what a relaunch replays.
            variables={
                name: value
                for name, value in record.variables.items()
                if name not in _RESULTS_VARIABLES
            },
            latency=(
                self._runs.latency_results(record.id)
                if kind in _HISTOGRAM_KINDS
                else []
            ),
            interruptions=(
                self._runs.interruption_results(record.id)
                if kind is MeasurementKind.HWLATDETECT
                else []
            ),
        )

    def conformance(self) -> RealtimeConformance:
        reading = self._reader.realtime()
        cpu = self._reader.cpu()
        declared, this_host, commit = self._declared()

        return RealtimeConformance(
            hostname=self._hostname,
            this_host=this_host,
            inventory_commit=commit,
            role=declared.role if declared else None,
            checks=checks_module.run(reading, cpu, declared),
            reading=reading,
            cpu=cpu,
            warnings=[*reading.warnings, *cpu.warnings],
        )

    def _declared(self) -> tuple[NodeConfig | None, str | None, str | None]:
        """This machine's entry in the inventory, when the inventory has one.

        Everything degrades to advice when it does not: a freshly installed
        machine has no inventory at all, and the page has to be useful on it,
        because reading the tuning is exactly what an operator does before
        writing the isolation down.
        """
        state = self._inventory.state()
        if state.inventory is None or state.this_host is None:
            return None, state.this_host, state.commit
        return (
            state.inventory.hosts.get(state.this_host),
            state.this_host,
            state.commit,
        )


def _declared_on(table: dict[str, Group], host: str) -> str | None:
    """Where the inventory sets the strategy one machine receives.

    `host` for its own entry, which wins over any group. Otherwise the first
    group carrying it that the machine belongs to, which is where a site that
    followed the role's README wrote it: `group_vars/hypervisors`.
    """
    for group in table.values():
        if STRATEGY_VARIABLE in group.hosts.get(host, {}):
            return "host"
    for name, group in sorted(table.items()):
        if STRATEGY_VARIABLE in group.variables and host in members(table, name):
            return name
    return None
