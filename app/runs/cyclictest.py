# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading the histogram the `cyclictest` role fetched.

The upstream role copies `run_cyclictest.py` to the machine, runs it, and
fetches `cyclictest_<host>.txt` to the controller. That file is what this
module parses, and parsing an artefact a role produced is the whole of this
service's involvement in the measurement: nothing here runs cyclictest, builds
its command line, or decides its parameters.

The format is fixed by the role rather than guessed at. The script writes its
own command line, then the output of

    cyclictest -l<cycles> -m {-S | -a <list> -t} -p<prio> -i200 -h400 -q

which is the histogram form: one line per microsecond bucket, one column per
thread, then a footer of `# Min/Avg/Max Latencies` lines. The footer is used
for the per thread figures rather than recomputed from the buckets, because
`-h400` truncates at 400us and a machine that produced one 900us sample has it
in the footer and nowhere else. Recomputing would report the worst latency as
400us on exactly the machine where the number matters.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.hosts.local import parse_cpu_list

# `cyclictest_<inventory_hostname>.txt`, which is what the role's `fetch`
# writes with `flat: true`.
_FILENAME = re.compile(r"^cyclictest_(?P<host>.+)\.txt$")

_FOOTER = re.compile(r"^#\s*(?P<label>[A-Za-z ]+?)\s*:\s*(?P<values>[\d\s]*)$")
_HISTOGRAM_LINE = re.compile(r"^(?P<bucket>\d+)(?P<counts>(?:\s+\d+)+)\s*$")
_COMMAND = re.compile(r"cyclictest\s[^\n]*")


class ThreadLatency(BaseModel):
    """One measuring thread, and the CPU it was pinned to."""

    thread: int
    cpu: int | None = None
    min_us: int | None = None
    avg_us: int | None = None
    max_us: int | None = None
    samples: int = 0
    overflows: int = 0
    """Samples above the last histogram bucket.

    Reported rather than folded into the counts. A run with overflows has a
    histogram that stops short of its own worst case, and a chart that does not
    say so is a chart that flatters the machine.
    """


class Histogram(BaseModel):
    buckets: list[int] = Field(default_factory=list)
    """The microsecond value of each bucket, in order."""
    counts: list[list[int]] = Field(default_factory=list)
    """One list per thread, parallel to `buckets`."""


class CyclictestResult(BaseModel):
    host: str
    command: str | None = None
    threads: list[ThreadLatency] = Field(default_factory=list)
    histogram: Histogram = Field(default_factory=Histogram)
    parse_error: str | None = None
    """Why this file yielded nothing, when it yielded nothing.

    A measurement that could not be read is reported as such. The run is over
    and the machines were loaded either way, so silently showing an empty chart
    would waste the only thing the operator paid for.
    """

    @property
    def max_us(self) -> int | None:
        values = [t.max_us for t in self.threads if t.max_us is not None]
        return max(values) if values else None


def read(results_dir: Path) -> list[CyclictestResult]:
    """Every histogram a run brought back, one per machine, by host name."""
    if not results_dir.is_dir():
        return []
    results = []
    for path in sorted(results_dir.iterdir()):
        match = _FILENAME.match(path.name)
        if match is None:
            continue
        try:
            raw = path.read_text(errors="replace")
        except OSError as error:
            results.append(
                CyclictestResult(host=match.group("host"), parse_error=str(error))
            )
            continue
        results.append(parse(match.group("host"), raw))
    return results


def parse(host: str, raw: str) -> CyclictestResult:
    command = _command_of(raw)
    buckets, counts = _histogram_of(raw)
    footer = _footer_of(raw)

    thread_count = len(counts) or max(
        (len(values) for values in footer.values()), default=0
    )
    if thread_count == 0:
        return CyclictestResult(
            host=host,
            command=command,
            parse_error=(
                "No histogram was found in the fetched file. cyclictest may "
                "have failed on the machine; the run log has its output."
            ),
        )

    cpus = _cpu_of_thread(command, thread_count)
    threads = [
        ThreadLatency(
            thread=index,
            cpu=cpus[index],
            min_us=_at(footer.get("Min Latencies"), index),
            avg_us=_at(footer.get("Avg Latencies"), index),
            max_us=_at(footer.get("Max Latencies"), index),
            samples=sum(counts[index]) if index < len(counts) else 0,
            overflows=_at(footer.get("Histogram Overflows"), index) or 0,
        )
        for index in range(thread_count)
    ]
    return CyclictestResult(
        host=host,
        command=command,
        threads=threads,
        histogram=Histogram(buckets=buckets, counts=counts),
    )


def _command_of(raw: str) -> str | None:
    """The command the script recorded ahead of the output.

    It is written without a trailing newline, so the first line of the output
    is glued to it. The regex stops at the `#` that begins cyclictest's own
    first line for that reason.
    """
    match = _COMMAND.search(raw)
    if match is None:
        return None
    return match.group(0).split("#")[0].strip()


def _histogram_of(raw: str) -> tuple[list[int], list[list[int]]]:
    buckets: list[int] = []
    columns: list[list[int]] = []
    for line in raw.splitlines():
        match = _HISTOGRAM_LINE.match(line.strip())
        if match is None:
            continue
        values = [int(value) for value in match.group("counts").split()]
        if not columns:
            columns = [[] for _ in values]
        if len(values) != len(columns):
            # A truncated or interleaved line. Dropped rather than padded: a
            # bucket with an invented zero in it is a bucket that reads as
            # measured.
            continue
        buckets.append(int(match.group("bucket")))
        for index, value in enumerate(values):
            columns[index].append(value)
    return buckets, columns


def _footer_of(raw: str) -> dict[str, list[int]]:
    footer: dict[str, list[int]] = {}
    for line in raw.splitlines():
        match = _FOOTER.match(line.strip())
        if match is None:
            continue
        values = match.group("values").split()
        if values:
            footer[match.group("label")] = [int(value) for value in values]
    return footer


def _cpu_of_thread(command: str | None, thread_count: int) -> list[int | None]:
    """Which CPU each thread ran on, worked out from the command line.

    `-S` gives one thread per online CPU in order, which is the upstream
    default and makes the thread index the CPU number. `-a <list> -t` pins the
    threads to that list in order, and the list is the isolated set whenever
    this page asked the question. Without either, the mapping is unknown and
    the chart says thread rather than inventing a CPU.
    """
    if not command:
        return [None] * thread_count
    affinity = re.search(r"-a\s+([\d,\-]+)", command)
    if affinity:
        cpus = parse_cpu_list(affinity.group(1))
        return [
            cpus[index] if index < len(cpus) else None for index in range(thread_count)
        ]
    if re.search(r"(?:^|\s)-S(?:\s|$)", command):
        return list(range(thread_count))
    return [None] * thread_count


def _at(values: list[int] | None, index: int) -> int | None:
    if values is None or index >= len(values):
        return None
    return values[index]
