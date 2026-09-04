# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading the real time tuning of a machine this service cannot log into.

The eight checks that used to answer only for the local node read files: the
tuned profile, the boot parameters, the sysctls, the hugepage pools, the
interrupt masks. `seapath-alloc` publishes them beside the pool (D27), and
this is the reader that turns the exposition back into the same
`RealtimeReading` the local reader produces, so one implementation of the
checks answers for the whole cluster.

What the tests hold is the difference between "read, and there is none" and
"nobody looked". An exporter says the first with an empty label and the second
by not publishing the family at all, and a reader that collapsed the two would
draw a pass over a machine nobody read.
"""

from __future__ import annotations

from app.cluster import metrics, tuning
from app.hosts.models import HugepagePool

_FULL = """
seapath_rt_tuned_info{profile="seapath-rt-host",\
source="/etc/tuned/active_profile",installed="1"} 1
seapath_rt_kernel_cmdline_info{cmdline="BOOT_IMAGE=/vmlinuz isolcpus=4-7 \
nohz_full=4-7 rcu_nocbs=4-7 intel_idle.max_cstate=0"} 1
seapath_rt_sched_rt_runtime_us -1
seapath_rt_sched_rt_period_us 1000000
seapath_rt_hugepages_total{size_kb="1048576",node=""} 16
seapath_rt_hugepages_free{size_kb="1048576",node=""} 8
seapath_rt_hugepages_total{size_kb="1048576",node="0"} 16
seapath_rt_hugepages_free{size_kb="1048576",node="0"} 8
seapath_rt_hugepages_total{size_kb="1048576",node="1"} 0
seapath_rt_hugepages_free{size_kb="1048576",node="1"} 0
seapath_rt_transparent_hugepages_info{enabled="never",defrag="never"} 1
seapath_rt_smt_info{control="on"} 1
seapath_rt_smt_active 1
seapath_rt_acpi_present 1
seapath_rt_irqs_total 214
seapath_rt_irqs_on_isolated_cpus 2
seapath_rt_irq_on_isolated_info{irq="181",name="eno1-TxRx-0",cpus="4,5"} 1
seapath_rt_irq_on_isolated_info{irq="182",name="nvme0q3",cpus="6"} 1
node_uname_info{sysname="Linux",release="6.1.0-rt-amd64",\
version="#1 SMP PREEMPT_RT Debian 6.1.0-1"} 1
"""


def read(text: str):
    return tuning.read(metrics.parse(text))


# The block as a whole


def test_a_node_publishing_no_block_reads_as_nothing_rather_than_as_empty() -> None:
    # The one case that must not become ten unknowns on the page: a node whose
    # collector predates the block is a node to upgrade, and saying "unreadable"
    # ten times would bury that.
    reading, cmdline = read('seapath_alloc_cpu_detail{cpu="0"} 1\n')

    assert reading is None
    assert cmdline == ""


def test_the_marker_family_alone_is_enough_to_say_the_block_is_there() -> None:
    # Published even when every reading in it came back empty, which is what
    # separates "read, and there is none" from "nobody looked".
    reading, _ = read('seapath_rt_tuned_info{profile="",source="",installed=""} 1\n')

    assert reading is not None
    assert reading.tuned_profile is None
    assert reading.tuned_profile_installed is None


# tuned


def test_the_tuned_profile_comes_back_with_its_source() -> None:
    reading, _ = read(_FULL)

    assert reading.tuned_profile == "seapath-rt-host"
    assert reading.tuned_profile_source == "/etc/tuned/active_profile"
    assert reading.tuned_profile_installed is True


def test_a_profile_installed_nowhere_is_not_a_profile_that_was_not_read() -> None:
    installed_nowhere, _ = read(
        'seapath_rt_tuned_info{profile="seapath-rt-host",source="/etc",'
        'installed="0"} 1\n'
    )
    unknown, _ = read(
        'seapath_rt_tuned_info{profile="seapath-rt-host",source="/etc",'
        'installed=""} 1\n'
    )

    assert installed_nowhere.tuned_profile_installed is False
    assert unknown.tuned_profile_installed is None


# The kernel and its command line


def test_the_command_line_comes_back_verbatim() -> None:
    _, cmdline = read(_FULL)

    assert "nohz_full=4-7" in cmdline
    assert "intel_idle.max_cstate=0" in cmdline


def test_a_command_line_carrying_a_quote_survives_the_exposition() -> None:
    # The exporter escapes it and the parser unescapes it. A boot parameter
    # with a quoted value is unusual and entirely legal, and losing the rest of
    # the exposition to one would cost the node its whole column.
    _, cmdline = read(
        'seapath_rt_tuned_info{profile="p",source="s",installed="1"} 1\n'
        'seapath_rt_kernel_cmdline_info{cmdline="quiet dyndbg=\\"nvme +p\\""} 1\n'
    )

    assert cmdline == 'quiet dyndbg="nvme +p"'


def test_the_preemption_model_is_read_before_the_plain_flag() -> None:
    # PREEMPT_RT contains PREEMPT, so an ordered match is the difference
    # between an RT kernel and an ordinary one.
    reading, _ = read(_FULL)

    assert reading.preemption == "PREEMPT_RT"
    assert reading.kernel_version == "6.1.0-rt-amd64"


# The sysctls, where a value and a silence must not look alike


def test_the_throttling_window_is_read_including_its_disabled_value() -> None:
    reading, _ = read(_FULL)

    assert reading.sched_rt_runtime_us == -1
    assert reading.sched_rt_period_us == 1000000


def test_a_sysctl_the_node_did_not_publish_is_none_rather_than_zero() -> None:
    reading, _ = read('seapath_rt_tuned_info{profile="p",source="s",installed="1"} 1\n')

    assert reading.sched_rt_runtime_us is None
    assert reading.sched_rt_period_us is None


# Hugepages, which are two families joined on the pool they name


def test_hugepages_are_read_machine_wide_and_per_numa_node() -> None:
    reading, _ = read(_FULL)

    machine = [pool for pool in reading.hugepages if pool.node is None]
    starved = [pool for pool in reading.hugepages if pool.node == 1]

    assert machine == [HugepagePool(size_kb=1048576, total=16, free=8)]
    assert starved[0].total == 0


def test_a_pool_with_no_free_count_is_kept_rather_than_dropped() -> None:
    reading, _ = read(
        'seapath_rt_tuned_info{profile="p",source="s",installed="1"} 1\n'
        'seapath_rt_hugepages_total{size_kb="2048",node=""} 4\n'
    )

    assert reading.hugepages[0].total == 4
    assert reading.hugepages[0].free == 0


# SMT, transparent hugepages, ACPI


def test_smt_is_read_as_the_machine_reports_it() -> None:
    reading, _ = read(_FULL)

    assert reading.smt_active is True
    assert reading.smt_control == "on"


def test_a_machine_exposing_no_smt_control_is_not_a_machine_with_smt_off() -> None:
    reading, _ = read(
        'seapath_rt_tuned_info{profile="p",source="s",installed="1"} 1\n'
        'seapath_rt_smt_info{control=""} 1\n'
    )

    assert reading.smt_active is None
    assert reading.smt_control is None


def test_the_transparent_hugepage_controls_are_read() -> None:
    reading, _ = read(_FULL)

    assert reading.transparent_hugepages == "never"
    assert reading.transparent_hugepage_defrag == "never"


def test_acpi_is_read_as_a_flag() -> None:
    reading, _ = read(_FULL)

    assert reading.acpi_present is True


# Interrupts, where the count and the list say different things


def test_the_interrupts_reaching_isolated_cores_are_counted_and_named() -> None:
    reading, _ = read(_FULL)

    assert reading.irq_count == 214
    assert reading.irqs_on_isolated == 2
    assert [entry.number for entry in reading.irqs_on_isolated_cpus] == ["181", "182"]
    assert reading.irqs_on_isolated_cpus[0].name == "eno1-TxRx-0"
    assert reading.irqs_on_isolated_cpus[0].cpus == [4, 5]


def test_the_count_is_kept_when_the_exporter_named_only_some_of_them() -> None:
    # A machine that keeps nothing off its isolated cores has every interrupt
    # on the list, so the exporter caps what it names and publishes the true
    # number beside it. Reporting the length of the list would tell an operator
    # eight where the machine said two hundred.
    reading, _ = read(
        'seapath_rt_tuned_info{profile="p",source="s",installed="1"} 1\n'
        "seapath_rt_irqs_total 214\n"
        "seapath_rt_irqs_on_isolated_cpus 200\n"
        'seapath_rt_irq_on_isolated_info{irq="181",name="eno1",cpus="4"} 1\n'
    )

    assert reading.irqs_on_isolated == 200
    assert len(reading.irqs_on_isolated_cpus) == 1


def test_a_node_that_published_no_interrupt_family_reports_nothing_read() -> None:
    reading, _ = read('seapath_rt_tuned_info{profile="p",source="s",installed="1"} 1\n')

    assert reading.irq_count is None
    assert reading.irqs_on_isolated is None
