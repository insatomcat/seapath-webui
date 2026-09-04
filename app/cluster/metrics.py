# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Just enough of the Prometheus exposition format to read one exporter.

A parser rather than a client library, because what this service needs from
`node_exporter` is a handful of named series and their labels. Writing the
twenty lines is cheaper than an image dependency, and an unused dependency tree
in a substation image is only its CVEs.

What is deliberately not implemented: histograms, summaries, exemplars, the
`# TYPE` and `# HELP` metadata beyond skipping it, and timestamps. Every series
this reads is a gauge whose value is 1 and whose meaning is entirely in its
labels, which is how `seapath-alloc` publishes its pool.
"""

from __future__ import annotations

from collections.abc import Iterator

# Prometheus escapes exactly three characters inside a label value.
_UNESCAPE = {"n": "\n", '"': '"', "\\": "\\"}


class Sample:
    """One series: its labels, and the number after them."""

    __slots__ = ("labels", "value")

    def __init__(self, labels: dict[str, str], value: float) -> None:
        self.labels = labels
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Sample({self.labels!r}, {self.value!r})"


def parse(text: str) -> dict[str, list[Sample]]:
    """Every sample in an exposition, grouped by metric name.

    Unreadable lines are skipped rather than raised on. This reads a file
    another program wrote on another machine, and one malformed series must not
    cost the caller the rest of the exposition: a CPU map missing one core is
    worth more than no CPU map.
    """
    found: dict[str, list[Sample]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _sample(line)
        if parsed is None:
            continue
        name, sample = parsed
        found.setdefault(name, []).append(sample)
    return found


def _sample(line: str) -> tuple[str, Sample] | None:
    brace = line.find("{")
    if brace < 0:
        name, _, rest = line.partition(" ")
        value = _value(rest)
        return (name, Sample({}, value)) if value is not None else None

    name = line[:brace]
    end = _closing_brace(line, brace)
    if end < 0:
        return None
    labels = dict(_labels(line[brace + 1 : end]))
    value = _value(line[end + 1 :])
    return (name, Sample(labels, value)) if value is not None else None


def _closing_brace(line: str, start: int) -> int:
    """The brace that closes the label set, skipping any inside a value.

    `members="a, b"` puts commas in a value and a label value may legitimately
    hold a brace, so the end of the set is found by walking the string rather
    than by searching for the last `}`.
    """
    in_value = False
    escaped = False
    for index in range(start + 1, len(line)):
        char = line[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_value = not in_value
        elif char == "}" and not in_value:
            return index
    return -1


def _labels(raw: str) -> Iterator[tuple[str, str]]:
    index = 0
    length = len(raw)
    while index < length:
        equals = raw.find("=", index)
        if equals < 0:
            return
        key = raw[index:equals].strip().lstrip(",").strip()
        quote = raw.find('"', equals)
        if quote < 0:
            return
        value, index = _quoted(raw, quote + 1)
        if key:
            yield key, value


def _quoted(raw: str, start: int) -> tuple[str, int]:
    out: list[str] = []
    index = start
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            out.append(_UNESCAPE.get(raw[index + 1], raw[index + 1]))
            index += 2
            continue
        if char == '"':
            return "".join(out), index + 1
        out.append(char)
        index += 1
    return "".join(out), index


def _value(raw: str) -> float | None:
    parts = raw.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None
