# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The ten real time checks, and the two kinds of answer they give.

Split out of `app/services/realtime.py` when the checks stopped being about
the local machine. They read a `RealtimeReading` and a `CpuReading` and
compare them with the inventory entry of the machine those readings came from,
and they no longer care which machine that is: the local node reads its own
files, and every other node's readings arrive from its exporter
(`app/cluster/tuning.py`). One implementation answers for the whole cluster,
which is the only way the two can be trusted to say the same thing.

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

from collections.abc import Iterable
from enum import Enum

from pydantic import BaseModel

from app.cluster.timesync import ClockReading, PtpReading
from app.hosts.local import format_cpu_list, parse_cpu_list
from app.hosts.models import CpuReading, NicIrqPin, RealtimeReading
from app.inventory.model import NodeConfig, Role, nics_affinity

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


def run(
    reading: RealtimeReading, cpu: CpuReading, declared: NodeConfig | None
) -> list[Check]:
    """Every check, in the order the page reads them.

    `declared` is the inventory entry of the machine these readings came from,
    or None when the inventory has no entry for it. Everything degrades to
    advice in that case: a freshly installed machine has no inventory at all,
    and the page has to be useful on it, because reading the tuning is exactly
    what an operator does before writing the isolation down.
    """
    return [
        _isolation(cpu, declared),
        _tuned(reading, declared),
        _preemption(reading),
        _kernel_cmdline(cpu, declared),
        _sched_rt(reading),
        _hugepages(reading),
        _smt(reading, cpu),
        _transparent_hugepages(reading),
        _irq_affinity(reading, cpu, declared),
        _acpi(reading),
    ]


def _isolation(cpu: CpuReading, declared: NodeConfig | None) -> Check:
    """The flagship check: the isolated set the kernel booted with.

    `isolcpus` is the one inventory variable that changes the latency
    guarantee, and it only takes effect at boot. A machine whose inventory was
    edited and converged without a reboot reads exactly like one where the
    change never happened, which is the case this check exists to catch.
    """
    observed = cpu_list(cpu.isolated) or "none"
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


def _irq_affinity(
    reading: RealtimeReading, cpu: CpuReading, declared: NodeConfig | None
) -> Check:
    """Where the NIC interrupts are, against where the inventory put them.

    The check a substation runs on: the process bus card has to raise its
    interrupts on an isolated CPU, or a sampled value waits behind whatever the
    housekeeping cores are doing, and the guest that was going to publish a
    GOOSE within four milliseconds waits with it. `nics_affinity` is where a
    site writes that placement and `configure_nic_irq_affinity` is what applies
    it, on every link up rather than once at boot, because the driver resets
    the mask each time the interface is opened.

    Counting the interrupts allowed on an isolated CPU is what this check used
    to do, and it answered a question nobody acts on: the non managed ones keep
    the boot mask that covers every CPU, so the count is permanently non zero
    on a correctly tuned machine and the deliberate NIC interrupts were counted
    among the offenders. That count survives as the last sentence of the
    detail, with the interrupts a site asked for taken out of it.
    """
    wanted = nics_affinity(declared) if declared else {}
    observed = {pin.iface: pin for pin in reading.nic_irqs}

    if not wanted:
        if not observed:
            return Check(
                id="irq_affinity",
                title="NIC IRQ affinity",
                kind=Kind.ADVICE,
                status=Status.INFO,
                observed="not configured",
                detail=(
                    "No interface has its interrupts pinned. A machine "
                    "receiving sampled values wants nics_affinity in the "
                    "inventory, naming the process bus interface and an "
                    "isolated CPU. "
                )
                + _other_irqs(reading, observed),
            )
        return Check(
            id="irq_affinity",
            title="NIC IRQ affinity",
            kind=Kind.ADVICE,
            status=Status.INFO,
            observed=_pins(observed.values()),
            detail=(
                "Pinned on the machine and declared nowhere. The inventory "
                "has no nics_affinity for it, so the next convergence on a "
                "reinstalled machine places nothing. "
            )
            + _other_irqs(reading, observed),
        )

    isolated = set(cpu.isolated)
    failures: list[str] = []
    for iface in sorted(wanted):
        asked = wanted[iface]
        pin = observed.get(iface)
        if pin is None:
            outside = [one for one in asked if one not in isolated]
            if outside and isolated:
                # The one cause the reading cannot show: a pin to a
                # housekeeping core is invisible to a reading that only
                # describes what reached an isolated CPU.
                failures.append(
                    f"{iface} is declared on {cpu_list(asked)}, which "
                    f"{'is' if len(outside) == 1 else 'are'} not isolated"
                )
            else:
                failures.append(f"{iface} carries no interrupt on {cpu_list(asked)}")
            continue
        if sorted(pin.cpus) != sorted(asked):
            failures.append(
                f"{iface} is on {cpu_list(pin.cpus)} and declared "
                f"on {cpu_list(asked)}"
            )

    if failures:
        return Check(
            id="irq_affinity",
            title="NIC IRQ affinity",
            kind=Kind.CONFORMANCE,
            status=Status.WARNING,
            observed=_pins(observed.values()) or "nothing pinned",
            declared=_declaration(wanted),
            detail=(
                ". ".join(failures)
                + ". configure_nic_irq_affinity applies the placement on every "
                "link up, so a machine reading like this was either never "
                "converged with it or is declaring a CPU the kernel is not "
                "isolating. "
            )
            + _other_irqs(reading, observed),
        )

    return Check(
        id="irq_affinity",
        title="NIC IRQ affinity",
        kind=Kind.CONFORMANCE,
        status=Status.OK,
        observed=_pins(observed.values()),
        declared=_declaration(wanted),
        detail=_other_irqs(reading, observed),
    )


def _declaration(wanted: dict[str, list[int]]) -> str:
    return ", ".join(
        f"{iface} on {cpu_list(cpus)}" for iface, cpus in sorted(wanted.items())
    )


def _pins(pins: Iterable[NicIrqPin]) -> str:
    return ", ".join(
        f"{pin.iface} on {cpu_list(pin.cpus)}"
        for pin in sorted(pins, key=lambda pin: pin.iface)
    )


def _other_irqs(reading: RealtimeReading, observed: dict[str, NicIrqPin]) -> str:
    """The interrupts on isolated CPUs that nobody asked for, said once.

    The count the exporter publishes covers every interrupt whose mask reaches
    the isolated set, the NIC queues a site pinned there on purpose included.
    Those are taken out here, or the sentence would report the configuration
    working as a problem with it.
    """
    if reading.irq_count is None or reading.irqs_on_isolated is None:
        return ""
    deliberate = sum(len(parse_cpu_list(pin.irqs)) for pin in observed.values())
    others = max(reading.irqs_on_isolated - deliberate, 0)
    if not others:
        return f"No other interrupt of the {reading.irq_count} reaches an isolated CPU."
    return (
        f"{others} other interrupt{'' if others == 1 else 's'} of "
        f"{reading.irq_count} may also be delivered to an isolated CPU. A mask "
        "is a permission rather than a measurement, and isolcpus=managed_irq "
        "is what keeps the kernel's own off the isolated set."
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


# The clock. Two rows, because they answer two questions: whether chrony holds
# the kernel clock at all, whatever feeds it, and what PTP level the guests on
# this machine will stamp into their sampled values.

# A reading `ptpstatus` has not rewritten for this long is a `ptpstatus` that
# stopped: it rewrites the file every few seconds while it runs.
PTP_STALE_SECONDS = 60.0

# IEEE 1588 clockAccuracy, for the values a substation grandmaster announces.
_ACCURACY = {
    "0x20": "25 ns",
    "0x21": "100 ns",
    "0x22": "250 ns",
    "0x23": "1 us",
    "0x24": "2.5 us",
    "0x25": "10 us",
    "0x26": "25 us",
    "0x27": "100 us",
    "0xfe": "unknown",
}


def clock(reading: ClockReading, declared: NodeConfig | None) -> list[Check]:
    """The two clock checks, for a node whose exporter published its clock."""
    return [_clock_sync(reading, declared), _ptp(reading, declared)]


def _sources(declared: NodeConfig | None) -> str:
    """What the inventory asks this machine to synchronise to, as a sentence."""
    if declared is None:
        return "This machine has no inventory entry, so no source is declared for it."
    parts = []
    if declared.ptp_interface:
        parts.append(f"PTP on {declared.ptp_interface}")
    if declared.ntp_servers:
        parts.append(f"the NTP servers {', '.join(declared.ntp_servers)}")
    if not parts:
        return (
            "The inventory declares neither ntp_servers nor a ptp_interface for "
            "this machine, so chrony runs on the distribution's own sources."
        )
    return f"The inventory declares {' and '.join(parts)}."


def _clock_sync(reading: ClockReading, declared: NodeConfig | None) -> Check:
    """Whether chrony, under timemaster, holds the kernel clock.

    The kernel's flag rather than a protocol's view, because chrony is what
    disciplines the clock whether its source is an NTP server or the PTP
    hardware clock, and it is the flag chrony maintains.
    """
    kind = (
        Kind.CONFORMANCE
        if declared is not None and (declared.ptp_interface or declared.ntp_servers)
        else Kind.ADVICE
    )
    sources = _sources(declared)
    base = {"id": "clock_sync", "title": "Clock synchronisation", "kind": kind}
    # The unit first. The kernel keeps its synchronised flag for hours after
    # the last correction, so a clock nobody is disciplining any more still
    # reads as synchronised until the error bound runs out.
    if reading.timemaster is not None and reading.timemaster != "active":
        return Check(
            **base,
            status=Status.WARNING,
            observed=f"timemaster {reading.timemaster}",
            detail=(
                f"timemaster starts chrony, and ptp4l where a PTP interface is "
                f"declared, so with it {reading.timemaster} nothing disciplines "
                "this clock. seapath_setup_timemaster is the playbook that "
                f"configures it. {sources}"
            ),
        )
    if reading.synchronised is None:
        return Check(
            **base,
            status=Status.UNKNOWN,
            observed="unknown",
            detail=(
                "node_exporter published no timex reading, so whether chrony "
                f"holds this clock cannot be read here. {sources}"
            ),
        )
    if not reading.synchronised:
        return Check(
            **base,
            status=Status.WARNING,
            observed="not synchronised",
            detail=(
                "The kernel flags this clock unsynchronised: chrony holds no "
                "source it trusts, so what this machine and its guests stamp "
                f"drifts from the rest of the substation. {sources}"
            ),
        )
    error = reading.estimated_error_seconds
    bounds = []
    if error is not None:
        bounds.append(f"estimated error {_duration(error)}")
    if reading.max_error_seconds is not None:
        bounds.append(f"maximum error {_duration(reading.max_error_seconds)}")
    return Check(
        **base,
        status=Status.OK,
        observed="synchronised" + (f", within {_duration(error)}" if error else ""),
        detail=(
            "chrony holds the kernel clock"
            + (f", {', '.join(bounds)}" if bounds else "")
            + f". {sources}"
        ),
    )


def _ptp(reading: ClockReading, declared: NodeConfig | None) -> Check:
    """The IEC 61850-9-2 `SmpSynch` this machine's guests stamp, from ptpstatus."""
    interface = declared.ptp_interface if declared is not None else None
    ptp = reading.ptp
    base = {"id": "ptp", "title": "PTP (SmpSynch)"}
    if ptp is None:
        if not interface:
            observer = declared is not None and declared.role is Role.OBSERVER
            return Check(
                **base,
                kind=Kind.ADVICE,
                status=Status.INFO,
                observed="not configured",
                detail=(
                    "An observer receives no sampled values, so it needs no PTP."
                    if observer
                    else "The inventory declares no ptp_interface for this "
                    "machine, so timemaster runs chrony on NTP alone and nothing "
                    "derives an SmpSynch. A hypervisor running IEC 61850 guests "
                    "needs one."
                ),
            )
        unit = (
            f" ptpstatus.service is {reading.ptpstatus} on this machine."
            if reading.ptpstatus
            else ""
        )
        return Check(
            **base,
            kind=Kind.CONFORMANCE,
            status=Status.UNKNOWN,
            observed="not published",
            detail=(
                f"The inventory declares PTP on {interface}, and this node "
                "publishes no seapath_ptp_* series. ptp_status_vsock from a "
                "collection whose ptpstatus writes them to node_exporter's "
                "textfile directory is what adds them; until then the clock row "
                f"is the only reading.{unit}"
            ),
        )

    kind = Kind.CONFORMANCE if interface else Kind.ADVICE
    if ptp.age_seconds is not None and ptp.age_seconds > PTP_STALE_SECONDS:
        return Check(
            **base,
            kind=kind,
            status=Status.UNKNOWN,
            observed=f"stale, {int(ptp.age_seconds)}s old",
            detail=(
                "ptpstatus rewrites this every few seconds, so a reading this "
                "old is a ptpstatus that stopped"
                + (
                    f": ptpstatus.service is {reading.ptpstatus}."
                    if reading.ptpstatus
                    else "."
                )
            ),
        )

    grandmaster = _grandmaster(ptp)
    if ptp.smpsynch == 2:
        return Check(
            **base,
            kind=kind,
            status=Status.OK,
            observed="2, global",
            detail=(
                f"{grandmaster} Traceable to a global reference, which is what "
                "SmpSynch 2 in the guests' sampled values says."
            ),
        )
    if ptp.smpsynch == 1:
        return Check(
            **base,
            kind=kind,
            status=Status.WARNING,
            observed="1, local",
            detail=(
                f"{grandmaster} A grandmaster answers and it is not traceable "
                "to a global reference: level 2 needs clockClass 6 or 7 with an "
                "accuracy of 1 us or better. A GPS lost beyond holdover, or a "
                "boundary clock that took over as grandmaster, reads like this. "
                "The guests here agree with each other and not with the grid."
            ),
        )
    if ptp.smpsynch == 0:
        return Check(
            **base,
            kind=kind,
            status=Status.WARNING,
            observed="0, no grandmaster",
            detail=(
                "No grandmaster: ptp4l"
                + (f" on {interface}" if interface else "")
                + " hears no announce, so the PTP hardware clock runs free and "
                "the guests stamp SmpSynch 0."
                + (f" Port state {ptp.port_state}." if ptp.port_state else "")
            ),
        )
    return Check(
        **base,
        kind=kind,
        status=Status.UNKNOWN,
        observed="unknown",
        detail="ptpstatus published its block with no SmpSynch value in it.",
    )


def _grandmaster(ptp: PtpReading) -> str:
    """The grandmaster and this machine's view of it, in one sentence."""
    parts = []
    if ptp.gm_identity:
        parts.append(f"Grandmaster {ptp.gm_identity}")
    if ptp.clock_class is not None:
        parts.append(f"clockClass {ptp.clock_class}")
    if ptp.clock_accuracy:
        meaning = _ACCURACY.get(ptp.clock_accuracy.lower())
        parts.append(
            f"accuracy {ptp.clock_accuracy}" + (f" ({meaning})" if meaning else "")
        )
    if ptp.port_state:
        parts.append(f"port {ptp.port_state}")
    if ptp.offset_seconds is not None:
        parts.append(f"offset {_duration(ptp.offset_seconds)}")
    return (", ".join(parts) + ".") if parts else ""


def _duration(seconds: float) -> str:
    """A small time the way an operator reads it, signed when it is an offset."""
    magnitude = abs(seconds)
    sign = "-" if seconds < 0 else ""
    if magnitude < 1e-6:
        return f"{sign}{magnitude * 1e9:.0f} ns"
    if magnitude < 1e-3:
        return f"{sign}{magnitude * 1e6:.1f} us"
    if magnitude < 1:
        return f"{sign}{magnitude * 1e3:.1f} ms"
    return f"{sign}{magnitude:.1f} s"


def _sizes(pools: list) -> set[int]:
    return {pool.size_kb for pool in pools}


def _page_size(size_kb: int) -> str:
    if size_kb >= 1024 * 1024:
        return f"{size_kb // (1024 * 1024)}GiB"
    return f"{size_kb // 1024}MiB"


def cpu_list(cpus: list[int]) -> str:
    """The kernel's own range notation, `4-7` rather than `4, 5, 6, 7`.

    The reader's own formatter, under the name the checks read it by: the two
    columns of this page are a declared list and an observed one, and they are
    written the same way by the same function or the comparison an operator
    makes by eye is not one.
    """
    return format_cpu_list(cpus)
