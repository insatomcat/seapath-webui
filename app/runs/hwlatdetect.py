# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading the result the `hwlatdetect` role fetched.

`hwlatdetect` answers the one question the conformance page cannot. Every check
there reads something the kernel knows, and a System Management Interrupt is
precisely what the kernel is never told about: the CPU goes into firmware and
comes back, and the time is missing from its accounting and from `cyclictest`'s
view of it. So a machine that is correctly isolated and still misses its
deadline is either a firmware problem or a configuration one, and this is the
measurement that separates them.

The role writes its own command line, then the output of

    hwlatdetect --duration=N --window=N --width=N --threshold=N

which is a header of the parameters the tool resolved, a line per sample above
the threshold, and a footer counting what it saw. A machine whose kernel has no
`hwlat` tracer gets a sentence instead, and that case is reported rather than
charted: nothing was measured, which is different from measuring nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

_FILENAME = re.compile(r"^hwlatdetect_(?P<host>.+)\.txt$")

# `ts: 1773668004.982201597, inner:0, outer:13, cpu:5`, which is what the
# tracer emits per sample. The older form carries no timestamp and no CPU.
_SAMPLE = re.compile(
    r"inner\s*:\s*(?P<inner>\d+)\s*(?:us)?\s*,\s*outer\s*:\s*(?P<outer>\d+)",
    re.IGNORECASE,
)
_TIMESTAMPED = re.compile(
    r"ts\s*:\s*(?P<ts>[\d.]+)\s*,\s*inner\s*:\s*(?P<inner>\d+)\s*,\s*"
    r"outer\s*:\s*(?P<outer>\d+)\s*,\s*cpu\s*:\s*(?P<cpu>\d+)",
    re.IGNORECASE,
)
_COUNT = re.compile(
    r"^#?\s*(?P<label>Samples recorded|Samples exceeding threshold)\s*:\s*"
    r"(?P<value>\d+)",
    re.IGNORECASE | re.MULTILINE,
)
_COMMAND = re.compile(r"^hwlatdetect[^\n]*", re.MULTILINE)
_UNSUPPORTED = re.compile(r"no\s+hwlat\s+tracer", re.IGNORECASE)
# `hwlatdetect` is a Python script, and it dies like one. Seen in the field
# when `rdmsr` is installed and prints its SMI counts labelled, which the tool
# reads as bare integers: one machine of a cluster measures and its neighbour
# stops before the tracer is ever started.
_TRACEBACK = re.compile(r"^Traceback \(most recent call last\)", re.MULTILINE)


class Interruption(BaseModel):
    """One gap the detector saw, in microseconds.

    `inner` is measured inside the tracer's own loop and `outer` between two
    loops. They are reported separately because the tool does: only their
    maximum bounds how long the CPU was actually taken away.
    """

    timestamp: float | None = None
    cpu: int | None = None
    inner_us: int
    outer_us: int

    @property
    def latency_us(self) -> int:
        return max(self.inner_us, self.outer_us)


class HwlatdetectResult(BaseModel):
    host: str
    command: str | None = None
    supported: bool = True
    """This machine's kernel carries the hwlat tracer.

    False is a result rather than a failure, and the page says so: a kernel
    built without CONFIG_HWLAT_TRACER cannot be asked the question, which is
    different from being asked and answering zero.
    """
    samples_recorded: int | None = None
    """How many gaps the tracer recorded.

    A count of what it *saw*, so zero is what a clean machine reports. It says
    nothing about how much of the run was actually watched, which is width over
    window, and presenting it as coverage would read as "nothing was measured"
    on exactly the machine that was measured and came back clean.
    """
    samples_over_threshold: int | None = None
    interruptions: list[Interruption] = Field(default_factory=list)
    message: str | None = None
    """Why this machine returned nothing, when it returned nothing."""
    output: list[str] = Field(default_factory=list)
    """What the machine printed, when this module could not read it.

    Two machines converged from the same inventory answering differently is
    the finding a measurement is run for, and "no report was found" on one of
    them is not an answer an operator can act on. So the lines themselves are
    carried, and the panel shows them: a tool that is missing, a tracer that
    is held by something else and a kernel too old all say so in the first
    line they print.
    """

    @property
    def worst_us(self) -> int | None:
        values = [item.latency_us for item in self.interruptions]
        return max(values) if values else None


def read(results_dir: Path) -> list[HwlatdetectResult]:
    """Every result a run brought back, one per machine, by host name."""
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
                HwlatdetectResult(host=match.group("host"), message=str(error))
            )
            continue
        results.append(parse(match.group("host"), raw))
    return results


def parse(host: str, raw: str) -> HwlatdetectResult:
    command_match = _COMMAND.search(raw)
    command = command_match.group(0).strip() if command_match else None

    if _UNSUPPORTED.search(raw):
        return HwlatdetectResult(
            host=host,
            command=command,
            supported=False,
            message=(
                "This kernel carries no hwlat tracer, so the machine could not "
                "be asked. CONFIG_HWLAT_TRACER is what provides it."
            ),
        )

    counts = {
        match.group("label").lower(): int(match.group("value"))
        for match in _COUNT.finditer(raw)
    }
    interruptions = _interruptions(raw)

    result = HwlatdetectResult(
        host=host,
        command=command,
        samples_recorded=counts.get("samples recorded"),
        samples_over_threshold=counts.get("samples exceeding threshold"),
        interruptions=interruptions,
    )
    if result.samples_recorded is None and not interruptions:
        # No footer and no sample. The run produced nothing this module knows
        # how to read, and saying so beats an empty chart that reads as a clean
        # machine.
        result.message = (
            _crash(raw)
            or "No hwlatdetect report was found in the fetched file. What the "
            "machine printed is below, and the run log has the rest."
        )
        result.output = _excerpt(raw)
    return result


def _crash(raw: str) -> str | None:
    """The tool died before it measured, named by the exception it died on.

    Worth telling apart from an unreadable file: the machine is not dirty, is
    not clean and was never asked, and the answer is on the machine rather than
    in the firmware. Ansible reports the task as failed, so the run says so
    too, and this is the same fact where an operator is reading the results.
    """
    if not _TRACEBACK.search(raw):
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return (
        "hwlatdetect stopped on an error before it measured anything, so this "
        "machine was never asked: " + lines[-1] + "."
    )


# Enough to carry a usage message or a whole Python traceback, and short enough
# to sit in a card beside the machines that answered properly. Indentation is
# kept: a traceback read as a flat list of lines is harder than it needs to be.
_EXCERPT_LINES = 12
_EXCERPT_WIDTH = 200


def _excerpt(raw: str) -> list[str]:
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    return [line[:_EXCERPT_WIDTH] for line in lines[:_EXCERPT_LINES]]


def _interruptions(raw: str) -> list[Interruption]:
    found: list[Interruption] = []
    for line in raw.splitlines():
        stripped = line.strip()
        timestamped = _TIMESTAMPED.search(stripped)
        if timestamped:
            found.append(
                Interruption(
                    timestamp=float(timestamped.group("ts")),
                    cpu=int(timestamped.group("cpu")),
                    inner_us=int(timestamped.group("inner")),
                    outer_us=int(timestamped.group("outer")),
                )
            )
            continue
        plain = _SAMPLE.search(stripped)
        if plain:
            found.append(
                Interruption(
                    inner_us=int(plain.group("inner")),
                    outer_us=int(plain.group("outer")),
                )
            )
    return found
