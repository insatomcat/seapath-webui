# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Real time conformance: what the inventory declared, and what the machine got.

This is the half of the real time story that is a reading. The other half is a
measurement, `cyclictest`, which runs on the machine through an Ansible run and
has a record of its own.

The framing matters more than the checks. `prometheus-node-exporter` already
publishes the machine's live state, so a page repeating it earns nothing (D13).
What no exporter answers is whether this machine matches the inventory it was
converged from: the inventory says `isolcpus: 4-7`, `configure_hypervisor`
writes the tuned profile and the kernel command line, and the question is
whether the machine came back with them. That question is about the desired
state, which lives here and nowhere else.

So every check below is one of two kinds, and says which it is:

- **conformance**, where the inventory declares a value. The check compares,
  and a mismatch is a finding an operator can act on: edit and converge again.
- **advice**, where nothing in the inventory has an opinion. SMT, transparent
  hugepages and interrupt affinity are of this kind. They are reported at
  `info` or `warning` and never as a failure, because a site is entitled to
  its own answer and this service does not get a vote.

No check here writes anything, and none of them may grow into a fix button.
The fix for a mismatch is an inventory edit and a run, which is the whole
design.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.hosts.local import parse_cpu_list
from app.hosts.models import CpuReading, Reading, RealtimeReading
from app.hosts.reader import HostReader
from app.inventory.model import NodeConfig, Role
from app.inventory.service import InventoryService
from app.runs.cyclictest import CyclictestResult
from app.runs.hwlatdetect import HwlatdetectResult
from app.runs.models import RunRecord, RunState
from app.runs.service import RunService

# What `configure_hypervisor` selects once the inventory carries `isolcpus`.
# The role gates the whole tuned block on that variable, so a hypervisor with
# no isolation declared is expected to carry no profile, and saying so is the
# difference between a useful check and a red badge on a machine nobody
# configured yet.
SEAPATH_PROFILE = "seapath-rt-host"


class Status(str, Enum):
    OK = "ok"
    WARNING = "warning"
    INFO = "info"
    UNKNOWN = "unknown"
    """The reading failed. Never rendered as a pass, and never as a failure."""


class Kind(str, Enum):
    CONFORMANCE = "conformance"
    ADVICE = "advice"


class Check(BaseModel):
    id: str
    title: str
    kind: Kind
    status: Status
    observed: str
    declared: str | None = None
    """What the inventory asks for, on a conformance check that has an answer."""
    detail: str = ""
    """One sentence saying what an operator does about it, or nothing."""


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
    ) -> None:
        self._reader = reader
        self._inventory = inventory
        self._runs = runs
        self._hostname = hostname

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

        checks = [
            _isolation(cpu, declared),
            _tuned(reading, declared),
            _preemption(reading),
            _kernel_cmdline(cpu, declared),
            _sched_rt(reading),
            _hugepages(reading),
            _smt(reading, cpu),
            _transparent_hugepages(reading),
            _irq_affinity(reading),
            _acpi(reading),
        ]

        return RealtimeConformance(
            hostname=self._hostname,
            this_host=this_host,
            inventory_commit=commit,
            role=declared.role if declared else None,
            checks=checks,
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


def _isolation(cpu: CpuReading, declared: NodeConfig | None) -> Check:
    """The flagship check: the isolated set the kernel booted with.

    `isolcpus` is the one inventory variable that changes the latency
    guarantee, and it only takes effect at boot. A machine whose inventory was
    edited and converged without a reboot reads exactly like one where the
    change never happened, which is the case this check exists to catch.
    """
    observed = _cpu_list(cpu.isolated) or "none"
    if declared is None or declared.isolcpus is None:
        return Check(
            id="cpu_isolation",
            title="CPU isolation",
            kind=Kind.ADVICE,
            status=Status.OK if cpu.isolated else Status.INFO,
            observed=observed,
            detail=(
                ""
                if cpu.isolated
                else "No CPU is isolated. Set isolcpus in the inventory and "
                "converge, then reboot: the kernel reads it at boot only."
            ),
        )

    wanted = parse_cpu_list(declared.isolcpus)
    if sorted(wanted) == sorted(cpu.isolated):
        return Check(
            id="cpu_isolation",
            title="CPU isolation",
            kind=Kind.CONFORMANCE,
            status=Status.OK,
            observed=observed,
            declared=declared.isolcpus,
        )
    return Check(
        id="cpu_isolation",
        title="CPU isolation",
        kind=Kind.CONFORMANCE,
        status=Status.WARNING,
        observed=observed,
        declared=declared.isolcpus,
        detail=(
            "The running kernel is not isolating what the inventory declares. "
            "isolcpus takes effect at boot, so a convergence that has not been "
            "followed by a reboot reads exactly like this."
        ),
    )


def _tuned(reading: RealtimeReading, declared: NodeConfig | None) -> Check:
    expects_profile = declared is not None and declared.isolcpus is not None
    observed = reading.tuned_profile or "none"

    if reading.tuned_profile is None:
        return Check(
            id="tuned",
            title="tuned profile",
            kind=Kind.CONFORMANCE if expects_profile else Kind.ADVICE,
            status=Status.WARNING if expects_profile else Status.INFO,
            observed=observed,
            declared=SEAPATH_PROFILE if expects_profile else None,
            detail=(
                "The inventory declares isolcpus, so configure_hypervisor "
                "should have selected the SEAPATH profile. Converge this "
                "machine."
                if expects_profile
                else "No tuned profile is selected, which is what a machine "
                "carrying no isolcpus is expected to look like."
            ),
        )

    if reading.tuned_profile_installed is False:
        return Check(
            id="tuned",
            title="tuned profile",
            kind=Kind.CONFORMANCE,
            status=Status.WARNING,
            observed=reading.tuned_profile,
            declared=SEAPATH_PROFILE if expects_profile else None,
            detail=(
                f"{reading.tuned_profile} is selected but no profile of that "
                "name is installed, so nothing is tuning this machine. "
                "tuned-adm on the host reports the name either way."
            ),
        )

    if expects_profile and reading.tuned_profile != SEAPATH_PROFILE:
        return Check(
            id="tuned",
            title="tuned profile",
            kind=Kind.CONFORMANCE,
            status=Status.WARNING,
            observed=reading.tuned_profile,
            declared=SEAPATH_PROFILE,
            detail=(
                "A site profile is legitimate, through "
                "custom_tuned_profile_path. Anything else means this machine "
                "was tuned by something other than its inventory."
            ),
        )

    return Check(
        id="tuned",
        title="tuned profile",
        kind=Kind.CONFORMANCE if expects_profile else Kind.ADVICE,
        status=Status.OK,
        observed=reading.tuned_profile,
        declared=SEAPATH_PROFILE if expects_profile else None,
    )


def _preemption(reading: RealtimeReading) -> Check:
    """Which kernel this machine booted, which no inventory variable picks.

    The kernel comes from the image. Nothing this service can edit changes it,
    so a machine on the wrong one is a machine to reinstall rather than to
    converge, and the check says that instead of pointing at the inventory.
    """
    if reading.preemption is None:
        return Check(
            id="preemption",
            title="Preemption",
            kind=Kind.ADVICE,
            status=Status.UNKNOWN,
            observed="unknown",
            detail="/proc/version could not be read.",
        )
    if reading.preemption == "PREEMPT_RT":
        return Check(
            id="preemption",
            title="Preemption",
            kind=Kind.ADVICE,
            status=Status.OK,
            observed="PREEMPT_RT",
        )
    return Check(
        id="preemption",
        title="Preemption",
        kind=Kind.ADVICE,
        status=Status.WARNING,
        observed=reading.preemption,
        detail=(
            "This is not a PREEMPT_RT kernel. Latency here is best effort, and "
            "no inventory variable changes that: the kernel comes from the "
            "installed image."
        ),
    )


# The kernel command line parameters that carry a real time intent, and the
# sentence each one is worth. `isolcpus` is not here: it has a check of its
# own, against the inventory, which is a stronger statement than its presence.
_CMDLINE_WANTED = (
    ("nohz_full", "Ticks are stopped on the isolated CPUs."),
    ("rcu_nocbs", "RCU callbacks are kept off the isolated CPUs."),
)
_CMDLINE_CSTATES = ("processor.max_cstate", "intel_idle.max_cstate", "idle")


def _kernel_cmdline(cpu: CpuReading, declared: NodeConfig | None) -> Check:
    cmdline = cpu.kernel_cmdline
    if not cmdline:
        return Check(
            id="kernel_cmdline",
            title="Boot parameters",
            kind=Kind.ADVICE,
            status=Status.UNKNOWN,
            observed="unknown",
            detail="/proc/cmdline could not be read.",
        )

    present = [name for name, _ in _CMDLINE_WANTED if f"{name}=" in cmdline]
    missing = [name for name, _ in _CMDLINE_WANTED if f"{name}=" not in cmdline]
    cstates = any(f"{name}=" in cmdline for name in _CMDLINE_CSTATES)
    if not cstates:
        missing.append("a C-state limit")

    # Only meaningful once isolation is asked for: on a machine with no
    # isolated CPU, nohz_full and rcu_nocbs have nothing to apply to.
    isolating = bool(cpu.isolated) or (
        declared is not None and declared.isolcpus is not None
    )
    observed = ", ".join(present) if present else "none of them"
    if cstates:
        observed += ", C-states limited"

    return Check(
        id="kernel_cmdline",
        title="Boot parameters",
        kind=Kind.ADVICE,
        status=Status.WARNING if missing and isolating else Status.OK,
        observed=observed,
        detail=(
            f"Missing: {', '.join(missing)}. The tuned profile writes the "
            "C-state limit through its [bootloader] section, and the rest "
            "comes from the kernel parameters the image or the Yocto role "
            "sets."
            if missing and isolating
            else ""
        ),
    )


def _sched_rt(reading: RealtimeReading) -> Check:
    runtime = reading.sched_rt_runtime_us
    period = reading.sched_rt_period_us
    if runtime is None or period is None:
        return Check(
            id="sched_rt",
            title="RT throttling",
            kind=Kind.ADVICE,
            status=Status.UNKNOWN,
            observed="unknown",
            detail="The sched_rt_* sysctls could not be read.",
        )
    if runtime < 0:
        return Check(
            id="sched_rt",
            title="RT throttling",
            kind=Kind.ADVICE,
            status=Status.OK,
            observed="disabled",
            detail=(
                "sched_rt_runtime_us is -1, so a real time task may use a "
                "whole CPU. This is what the realtime tuned profile sets."
            ),
        )
    return Check(
        id="sched_rt",
        title="RT throttling",
        kind=Kind.ADVICE,
        status=Status.WARNING,
        observed=f"{runtime}/{period}us",
        detail=(
            "Real time tasks are throttled, so a busy guest is preempted by "
            "the scheduler rather than by anything it can be tuned around. "
            "The realtime tuned profile sets sched_rt_runtime_us to -1."
        ),
    )


def _hugepages(reading: RealtimeReading) -> Check:
    machine = [pool for pool in reading.hugepages if pool.node is None]
    reserved = [pool for pool in machine if pool.total > 0]
    if not machine:
        return Check(
            id="hugepages",
            title="Hugepages",
            kind=Kind.ADVICE,
            status=Status.UNKNOWN,
            observed="unknown",
            detail="No hugepage pool is exposed under /sys/kernel/mm/hugepages.",
        )
    if not reserved:
        return Check(
            id="hugepages",
            title="Hugepages",
            kind=Kind.ADVICE,
            status=Status.INFO,
            observed="none reserved",
            detail=(
                "No hugepage is reserved. A guest whose libvirt XML asks for "
                "them will fail to start."
            ),
        )
    observed = ", ".join(
        f"{pool.total} x {_page_size(pool.size_kb)} ({pool.free} free)"
        for pool in reserved
    )
    # Per NUMA node, because a guest pinned to one socket draws from that
    # socket's pool. A machine with the right total and nothing on the node the
    # guest sits on fails to start with the total looking correct.
    starved = [
        pool
        for pool in reading.hugepages
        if pool.node is not None
        and pool.total == 0
        and pool.size_kb in _sizes(reserved)
    ]
    if starved:
        nodes = ", ".join(str(pool.node) for pool in starved)
        return Check(
            id="hugepages",
            title="Hugepages",
            kind=Kind.ADVICE,
            status=Status.WARNING,
            observed=observed,
            detail=(
                f"NUMA node {nodes} has none. A guest pinned to that node "
                "draws from its pool and not from the machine total."
            ),
        )
    return Check(
        id="hugepages",
        title="Hugepages",
        kind=Kind.ADVICE,
        status=Status.OK,
        observed=observed,
    )


def _smt(reading: RealtimeReading, cpu: CpuReading) -> Check:
    if reading.smt_active is None:
        return Check(
            id="smt",
            title="Hyperthreading",
            kind=Kind.ADVICE,
            status=Status.INFO,
            observed="unknown",
            detail="This machine exposes no SMT control, which is usual on AMD "
            "and on a machine where the firmware disabled it.",
        )
    if not reading.smt_active:
        return Check(
            id="smt",
            title="Hyperthreading",
            kind=Kind.ADVICE,
            status=Status.OK,
            observed="off",
        )
    return Check(
        id="smt",
        title="Hyperthreading",
        kind=Kind.ADVICE,
        status=Status.WARNING,
        observed="on",
        detail=(
            "Two threads of one core share its execution units, so an isolated "
            "CPU is only isolated if its sibling is idle or isolated too. Check "
            "the pairs on the CPU map before trusting the isolated set."
            if cpu.isolated
            else "Nothing is isolated yet, so this costs nothing today."
        ),
    )


def _transparent_hugepages(reading: RealtimeReading) -> Check:
    value = reading.transparent_hugepages
    if value is None:
        return Check(
            id="transparent_hugepages",
            title="Transparent hugepages",
            kind=Kind.ADVICE,
            status=Status.INFO,
            observed="unknown",
            detail="This kernel exposes no transparent hugepage control.",
        )
    if value == "never":
        return Check(
            id="transparent_hugepages",
            title="Transparent hugepages",
            kind=Kind.ADVICE,
            status=Status.OK,
            observed="never",
        )
    return Check(
        id="transparent_hugepages",
        title="Transparent hugepages",
        kind=Kind.ADVICE,
        status=Status.WARNING,
        observed=value,
        detail=(
            "khugepaged compacts memory in the background, which is a source "
            "of jitter an isolated CPU does not escape."
        ),
    )


def _irq_affinity(reading: RealtimeReading) -> Check:
    if reading.irq_count is None:
        return Check(
            id="irq_affinity",
            title="IRQ affinity",
            kind=Kind.ADVICE,
            status=Status.UNKNOWN,
            observed="unknown",
            detail="/proc/irq could not be read.",
        )
    offenders = reading.irqs_on_isolated_cpus
    if not offenders:
        return Check(
            id="irq_affinity",
            title="IRQ affinity",
            kind=Kind.ADVICE,
            status=Status.OK,
            observed=f"none of {reading.irq_count} reaches an isolated CPU",
        )
    named = ", ".join(
        f"{entry.name or entry.number} on {_cpu_list(entry.cpus)}"
        for entry in offenders[:4]
    )
    if len(offenders) > 4:
        named += f", and {len(offenders) - 4} more"
    return Check(
        id="irq_affinity",
        title="IRQ affinity",
        kind=Kind.ADVICE,
        status=Status.WARNING,
        observed=f"{len(offenders)} of {reading.irq_count} reach an isolated CPU",
        detail=(
            f"{named}. An affinity mask is a permission rather than a "
            "measurement: the interrupt may not have fired there yet. NIC "
            "queues are configure_nic_irq_affinity's to place, and the rest "
            "follows isolcpus=managed_irq where the kernel supports it."
        ),
    )


def _acpi(reading: RealtimeReading) -> Check:
    return Check(
        id="acpi",
        title="ACPI",
        kind=Kind.ADVICE,
        status=Status.INFO,
        observed="present" if reading.acpi_present else "absent",
        detail=(
            "System management interrupts are invisible to the kernel and to "
            "this page. hwlatdetect is what measures them."
        ),
    )


def _sizes(pools: list) -> set[int]:
    return {pool.size_kb for pool in pools}


def _page_size(size_kb: int) -> str:
    if size_kb >= 1024 * 1024:
        return f"{size_kb // (1024 * 1024)}GiB"
    return f"{size_kb // 1024}MiB"


def _cpu_list(cpus: list[int]) -> str:
    """The kernel's own range notation, `4-7` rather than `4, 5, 6, 7`.

    The same shape the inventory writes, so a comparison an operator makes by
    eye between the two columns is a comparison of like with like.
    """
    if not cpus:
        return ""
    ordered = sorted(cpus)
    ranges: list[str] = []
    start = previous = ordered[0]
    for cpu in ordered[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = cpu
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)
