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

Two sources, one implementation. The local node reads its own `/proc`, `/sys`
and the host `/etc` PAM already brought in, which works on a machine where
nothing has been deployed yet. Every other node's readings arrive from its
exporter, where `seapath-alloc` publishes them beside the pool (D27). A node
answers for itself either way, and the difference is reported rather than
hidden: a reading from an exporter carries its age, and a node running a
collector too old to publish the block says so.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.cluster.pool import ClusterPool, NodePool, PoolReader
from app.hosts.models import CpuReading, Reading, RealtimeReading
from app.hosts.reader import HostReader
from app.inventory.model import NodeConfig, Role
from app.inventory.service import InventoryService
from app.runs.cyclictest import CyclictestResult
from app.runs.hwlatdetect import HwlatdetectResult
from app.runs.models import RunRecord, RunState
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


class MeasurementKind(str, Enum):
    """Which question a measurement run asked.

    The two are complementary rather than alternatives, and the page keeps them
    apart because their answers are of different kinds. `cyclictest` measures
    what the scheduler delivered, which the tuning can change. `hwlatdetect`
    measures what the firmware took without telling the kernel, which no
    variable in the inventory reaches.
    """

    CYCLICTEST = "cyclictest"
    HWLATDETECT = "hwlatdetect"


# The catalogue entry behind each, and the variable the service fills with the
# run's own results directory. Keyed by playbook id, which is what a run record
# carries, so a run launched from the System page is recognised here too.
MEASUREMENT_PLAYBOOKS = {
    "test_run_cyclictest": MeasurementKind.CYCLICTEST,
    "test_run_hwlatdetect": MeasurementKind.HWLATDETECT,
}
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
    """Filled on a cyclictest run, one entry per machine."""
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
        for node in nodes:
            declared = hosts.get(node.host)
            node.declared_isolcpus = declared.isolcpus if declared else None
            self._judge(node, declared, state.this_host)
        if state.this_host is None or state.this_host not in hosts:
            # The machine the browser is pointed at, when the inventory has no
            # entry for it. It is the one node whose readings need no exporter,
            # and leaving it out would drop the only column that always works.
            nodes.insert(0, self._local_node())
        # The local node first, because it is the machine the operator is
        # standing on and the one column that answers without an exporter.
        nodes.sort(key=lambda node: node.host != state.this_host)
        return ClusterPool(
            nodes=nodes,
            this_host=state.this_host,
            inventory_commit=state.commit,
            available=any(node.cpus for node in nodes),
        )

    def _judge(
        self, node: NodePool, declared: NodeConfig | None, this_host: str | None
    ) -> None:
        """Run the ten checks against one node, from the best reading available.

        The local machine reads its own files, which works on a node whose
        exporters were never deployed and is never stale. Every other node is
        judged on what its exporter published, and a node that published no
        tuning gets no checks rather than ten unknowns: `tuning_error` already
        says what to do about it, and a column of grey dots would bury it.
        """
        if node.host == this_host:
            local = self.conformance()
            node.reading = local.reading
            node.kernel_cmdline = local.cpu.kernel_cmdline or ""
            node.tuning_error = ""
            node.checks = local.checks
            return
        if node.reading is None:
            return
        node.checks = checks_module.run(
            node.reading,
            CpuReading(
                isolated=node.isolated,
                kernel_cmdline=node.kernel_cmdline or None,
            ),
            declared,
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
                if kind is MeasurementKind.CYCLICTEST
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
