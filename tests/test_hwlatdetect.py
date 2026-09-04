# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading the report the `hwlatdetect` role fetched.

The distinction every test here holds is between a machine that was asked and
answered zero, and a machine that could not be asked at all. Reading the second
as a clean bill of health is the one mistake this parser must never make: a
kernel with no hwlat tracer has measured nothing, and a page that shows it as
"no interruptions" tells an operator their firmware is clean when nobody
looked.
"""

from __future__ import annotations

from pathlib import Path

from app.runs.hwlatdetect import parse, read

_COMMAND = "hwlatdetect --duration=120 --window=1000000 --width=500000 --threshold=10"

CLEAN = f"""{_COMMAND}
hwlatdetect:  test duration 120 seconds
   detector: tracer
   parameters:
        Latency threshold: 10us
        Sample window:     1000000us
        Sample width:      500000us
     Non-sampling period:  500000us
        Output File:       None

Starting test
test finished
Max Latency: Below threshold
Samples recorded: 0
Samples exceeding threshold: 0
"""

DIRTY = f"""{_COMMAND}
hwlatdetect:  test duration 120 seconds
   detector: tracer
Starting test
test finished
ts: 1773668004.982201597, inner:0, outer:13, cpu:5
ts: 1773668021.114402311, inner:41, outer:7, cpu:5
ts: 1773668066.771830012, inner:0, outer:22, cpu:2
Max Latency: 41us
Samples recorded: 3
Samples exceeding threshold: 3
"""

UNSUPPORTED = f"""{_COMMAND}
hwlatdetect did not run on node1: this kernel offers no
hwlat tracer, which is what CONFIG_HWLAT_TRACER provides. Nothing was
measured here.
"""


def test_a_clean_machine_reports_no_interruption_and_stays_supported() -> None:
    result = parse("node1", CLEAN)

    assert result.supported is True
    assert result.interruptions == []
    assert result.samples_recorded == 0
    assert result.samples_over_threshold == 0
    # Nothing to explain: the machine answered, and the answer was zero.
    assert result.message is None


def test_a_kernel_with_no_tracer_is_reported_apart_from_a_clean_one() -> None:
    # The mistake this guards: an unmeasurable machine shown as a machine with
    # no interruptions tells an operator their firmware is clean when nobody
    # looked at it.
    result = parse("node1", UNSUPPORTED)

    assert result.supported is False
    assert result.interruptions == []
    assert "CONFIG_HWLAT_TRACER" in result.message


def test_each_interruption_carries_its_time_cpu_and_both_halves() -> None:
    result = parse("node1", DIRTY)

    assert [item.cpu for item in result.interruptions] == [5, 5, 2]
    assert [item.inner_us for item in result.interruptions] == [0, 41, 0]
    assert [item.outer_us for item in result.interruptions] == [13, 7, 22]
    assert result.samples_over_threshold == 3


def test_the_worst_gap_is_the_larger_of_inner_and_outer() -> None:
    # Only the maximum bounds how long the CPU was actually taken away, and the
    # worst sample here is an inner one while the first is an outer one.
    result = parse("node1", DIRTY)

    assert [item.latency_us for item in result.interruptions] == [13, 41, 22]
    assert result.worst_us == 41


def test_a_sample_without_a_timestamp_is_still_read() -> None:
    # The older tracer prints no ts and no cpu. Dropping those lines would
    # report a machine with interruptions as clean.
    older = DIRTY.replace(
        "ts: 1773668004.982201597, inner:0, outer:13, cpu:5", "inner:0, outer:13"
    )

    result = parse("node1", older)

    assert len(result.interruptions) == 3
    assert result.interruptions[0].timestamp is None
    assert result.interruptions[0].cpu is None
    assert result.interruptions[0].outer_us == 13


def test_the_command_line_the_role_recorded_is_read_back() -> None:
    assert parse("node1", CLEAN).command == _COMMAND


def test_a_file_with_no_report_says_so_rather_than_reading_as_clean() -> None:
    result = parse("node1", "hwlatdetect --duration=120\nsome unexpected output\n")

    assert result.supported is True
    assert result.samples_recorded is None
    assert "run log" in result.message


def test_the_results_of_a_run_are_read_by_host_name(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "hwlatdetect_node1.txt").write_text(CLEAN)
    (results / "hwlatdetect_node2.txt").write_text(DIRTY)
    # A cyclictest artefact in the same directory, which a run of both would
    # produce. Each parser reads only its own.
    (results / "cyclictest_node1.txt").write_text("noise\n")

    read_back = read(results)

    assert [item.host for item in read_back] == ["node1", "node2"]
    assert read_back[0].worst_us is None
    assert read_back[1].worst_us == 41


def test_a_run_that_fetched_nothing_reads_as_no_results(tmp_path: Path) -> None:
    assert read(tmp_path / "absent") == []
