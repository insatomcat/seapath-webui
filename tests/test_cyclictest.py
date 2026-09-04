# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading the histogram the upstream `cyclictest` role fetched.

The format is fixed by `roles/cyclictest/files/run_cyclictest.py`, which writes
its own command line and then the output of `cyclictest ... -h400 -q`. The
samples below are that shape, including the two details that trip a parser: the
command line is written with no trailing newline, so cyclictest's first line is
glued to it, and the per thread figures live in a footer that the histogram
itself cannot reproduce.
"""

from __future__ import annotations

from pathlib import Path

from app.runs.cyclictest import parse, read

SMP = """cyclictest -l100000 -m -S -p90 -i200 -h400 -q# /dev/cpu_dma_latency set to 0us
000000 000000 000000 000000 000000
000001 000000 000000 000000 000000
000002 012000 011000 010500 011200
000003 060000 061000 062000 061000
000004 020000 021000 020500 020800
000015 000001 000000 000000 000000
# Total: 000092001 000093000 000093000 000093000
# Min Latencies: 00002 00002 00002 00002
# Avg Latencies: 00003 00003 00003 00003
# Max Latencies: 00015 00012 00009 00011
# Histogram Overflows: 00000 00000 00000 00000
# Histogram Overflow at cycle number:
# Thread 0:
# Thread 1:
"""


def test_the_per_thread_figures_come_from_the_footer() -> None:
    result = parse("node1", SMP)

    assert [thread.max_us for thread in result.threads] == [15, 12, 9, 11]
    assert [thread.min_us for thread in result.threads] == [2, 2, 2, 2]
    assert [thread.avg_us for thread in result.threads] == [3, 3, 3, 3]


def test_the_worst_latency_survives_a_histogram_that_truncated_it() -> None:
    # `-h400` stops at 400us. A machine that produced one 900us sample has it
    # in the footer and nowhere else, and recomputing the maximum from the
    # buckets would report 400us on exactly the machine where the number
    # matters.
    truncated = SMP.replace("# Max Latencies: 00015", "# Max Latencies: 00900")

    result = parse("node1", truncated)

    assert result.threads[0].max_us == 900
    assert result.max_us == 900


def test_an_smp_run_maps_each_thread_to_its_own_cpu() -> None:
    # `-S` is the upstream default and gives one thread per online CPU in
    # order, which is what makes the thread index a CPU number.
    result = parse("node1", SMP)

    assert [thread.cpu for thread in result.threads] == [0, 1, 2, 3]


def test_a_pinned_run_maps_the_threads_to_the_cpus_it_was_given() -> None:
    pinned = SMP.replace("-m -S -p90", "-m -a 4-7 -t -p90")

    result = parse("node1", pinned)

    assert [thread.cpu for thread in result.threads] == [4, 5, 6, 7]


def test_a_run_with_no_affinity_flag_reports_threads_rather_than_cpus() -> None:
    unpinned = SMP.replace("-m -S -p90", "-m -p90")

    result = parse("node1", unpinned)

    assert [thread.cpu for thread in result.threads] == [None] * 4


def test_the_command_line_is_recovered_from_the_glued_first_line() -> None:
    # The role's script writes the command with no newline after it, so the
    # first line of the file is the command and cyclictest's first output line
    # run together.
    result = parse("node1", SMP)

    assert result.command == "cyclictest -l100000 -m -S -p90 -i200 -h400 -q"


def test_overflowing_samples_are_reported_beside_the_counts() -> None:
    # A histogram with overflows stops short of its own worst case, and a
    # chart that stays quiet about it flatters the machine.
    overflowing = SMP.replace(
        "# Histogram Overflows: 00000 00000 00000 00000",
        "# Histogram Overflows: 00000 00000 00003 00000",
    )

    result = parse("node1", overflowing)

    assert [thread.overflows for thread in result.threads] == [0, 0, 3, 0]


def test_a_file_with_no_histogram_says_so_rather_than_charting_nothing() -> None:
    # The run is over and the machines were loaded either way. An empty chart
    # would waste the only thing the operator paid for.
    result = parse("node1", "cyclictest -l100 -m -S -p90 -i200 -h400 -q\n")

    assert result.threads == []
    assert result.parse_error is not None
    assert "run log" in result.parse_error


def test_a_short_histogram_line_is_dropped_rather_than_padded() -> None:
    # A bucket padded with an invented zero reads as measured.
    ragged = SMP.replace(
        "000004 020000 021000 020500 020800", "000004 020000 021000 020500"
    )

    result = parse("node1", ragged)

    assert 4 not in result.histogram.buckets
    assert len(result.histogram.buckets) == 5


def test_the_results_of_a_run_are_read_by_host_name(tmp_path: Path) -> None:
    # `fetch` with `flat: true` writes `cyclictest_<inventory_hostname>.txt`,
    # which is the only place the machine's name survives into the artefact.
    results = tmp_path / "results"
    results.mkdir()
    (results / "cyclictest_node1.txt").write_text(SMP)
    (results / "cyclictest_node2.txt").write_text(SMP)
    (results / "something-else.log").write_text("noise\n")

    read_back = read(results)

    assert [item.host for item in read_back] == ["node1", "node2"]


def test_a_run_that_fetched_nothing_reads_as_no_results(tmp_path: Path) -> None:
    assert read(tmp_path / "absent") == []
