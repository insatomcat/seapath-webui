# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""One `journalctl` invocation, and what comes back from it.

The whole of what this service knows about the journal. It builds an argv from
a checked query and parses `-o json`, and it reaches nothing: the machine is
[D63](../../docs/decisions.md)'s business, in `app/services/logs.py`, over the
`ssh` every other reading uses.

**A query carries at least one match on a field.** This is the rule the whole
design rests on, and it is worth the number. On a 517 MB journal, unit matched
and bounded by time, `journalctl` answers in 6 to 9 ms, because a match on
`FIELD=VALUE` is served from the journal's own index. The same search written
as a regex over `MESSAGE` scans every entry and takes 2.9 seconds. So `-g`
narrows what the matches already selected, and never stands alone. `Refused` is
what a caller gets for asking otherwise.

**Times are absolute and in UTC.** `journalctl` reads a bare timestamp in the
machine's local time, and the machines of one inventory need not agree on a
timezone. Every bound this builds carries `UTC`, and every timestamp read back
is built from `__REALTIME_TIMESTAMP`, which is microseconds since the epoch and
carries no timezone at all.

**The command is built here, never by a caller.** What a browser sends reaches
a value inside the command, checked against the patterns below before it gets
there, and quoted once for each of the two shells it crosses. The rule, and the
two shells, are `app/console/service.py`'s.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

#: The fields every entry is asked for. `--output-fields` also always carries
#: `__CURSOR`, `__REALTIME_TIMESTAMP`, `__MONOTONIC_TIMESTAMP` and `_BOOT_ID`,
#: whatever is named here. Asking for a short list rather than taking the
#: default is worth it: a full `-o json` entry is three times the size, and the
#: answer crosses an administration network once per machine.
#:
#: `UNIT` and `_SYSTEMD_UNIT` are both here because they answer two different
#: questions and a journal reads wrong without the first. `_SYSTEMD_UNIT` is
#: the unit of the process that wrote the line; `UNIT` is the unit the line is
#: *about*, which systemd sets on its own messages. `journalctl -u
#: pacemaker.service` returns both, so "pacemaker.service: Deactivated
#: successfully." arrives with `_SYSTEMD_UNIT=init.scope`, because PID 1 wrote
#: it. A table drawn from `_SYSTEMD_UNIT` alone labels the line that says a
#: cluster daemon stopped as coming from `init.scope`, which is true and
#: useless. `_HOSTNAME` is deliberately not asked for: a machine is named here
#: by the inventory's name for it, which `Entry` explains.
FIELDS = (
    "MESSAGE",
    "PRIORITY",
    "SYSLOG_IDENTIFIER",
    "UNIT",
    "_SYSTEMD_UNIT",
    "_PID",
)

#: A systemd unit name, as the units of a SEAPATH machine are named. The `@`
#: and the long hexadecimal in `ceph-<fsid>@mon.ccv1.service` are why this is
#: wider than a first guess.
UNIT = re.compile(r"^[A-Za-z0-9:_.@-]{1,128}$")

#: A syslog identifier, which is what a program that writes to the journal
#: without a unit is found by.
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

#: How long a regex may be. A bound rather than a judgement: `journalctl` runs
#: it with PCRE2 on a machine whose CPUs belong to its guests, and a pattern
#: that takes a paragraph to write is one the field matches should have made
#: unnecessary.
MAX_GREP = 200

#: The most entries a reading returns, whatever the caller sent. It bounds
#: the answer rather than one machine's part of it: every machine is asked for
#: this many and the merge keeps the most recent of them, so a five node
#: cluster costs what a one node cluster costs. At the 550 bytes an entry
#: measured, two thousand is about a megabyte, which is a page an operator
#: waits for once and not a page they wait for on every visit.
MAX_LINES = 2000
DEFAULT_LINES = 500

#: The widest window a query may span. A match on a field makes even a long
#: window cheap, since `journalctl` seeks rather than scans, so this is a guard
#: against a query with no bound at all rather than a performance setting.
MAX_WINDOW_SECONDS = 7 * 24 * 3600

#: The lowest and highest syslog priorities, `emerg` and `debug`.
MIN_PRIORITY = 0
MAX_PRIORITY = 7


class Refused(Exception):
    """The query will not be run, and the message names the condition.

    Carried to the caller as a `409` naming the offending condition, which is
    what `docs/api.md` asks of a precondition a node does not satisfy.
    """


@dataclass(frozen=True)
class Query:
    """What to ask a machine's journal for.

    `since` is required and `lines` is always bounded, so there is no way to
    spell "everything this machine has ever logged". An hour of one node with
    no match on a field is 4.6 MB of JSON, and a cluster wide version of that
    is thirteen megabytes for one click.
    """

    since: datetime
    until: datetime | None = None
    units: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    priority: int | None = None
    grep: str | None = None
    lines: int = DEFAULT_LINES

    #: Set by `checked()`, so that a Query built by hand in a test cannot reach
    #: a command by accident.
    _checked: bool = field(default=False, repr=False, compare=False)

    @property
    def matches(self) -> bool:
        """Whether anything here is served from the journal's index."""
        return bool(self.units or self.identifiers or self.priority is not None)


def checked(query: Query) -> Query:
    """The same query, refused unless every value is one this may run.

    Every string below reaches a command line, so this is the only place that
    decides what may. A value that fails here never becomes a command, and the
    message names the value rather than the pattern, because the person reading
    it is an operator rather than the author of the regular expression.
    """
    if not query.matches:
        raise Refused(
            "A log query carries at least one match on a field: a unit, a "
            "syslog identifier or a priority. Searching the text of every "
            "entry of a hypervisor's journal takes seconds of its CPU, and "
            "the matches are what make it milliseconds."
        )
    for unit in query.units:
        if not UNIT.match(unit):
            raise Refused(f"{unit!r} is not a unit name.")
    for identifier in query.identifiers:
        if not IDENTIFIER.match(identifier):
            raise Refused(f"{identifier!r} is not a syslog identifier.")
    if query.priority is not None and not (
        MIN_PRIORITY <= query.priority <= MAX_PRIORITY
    ):
        raise Refused(
            f"{query.priority} is not a syslog priority, which runs from "
            f"{MIN_PRIORITY} (emerg) to {MAX_PRIORITY} (debug)."
        )
    if query.lines < 1 or query.lines > MAX_LINES:
        raise Refused(f"A query asks for 1 to {MAX_LINES} entries.")
    if query.grep is not None:
        _check_grep(query.grep)
    _check_window(query)
    return Query(
        since=query.since,
        until=query.until,
        units=query.units,
        identifiers=query.identifiers,
        priority=query.priority,
        grep=query.grep,
        lines=query.lines,
        _checked=True,
    )


def _check_grep(grep: str) -> None:
    if not grep.strip():
        raise Refused("An empty pattern matches everything, which is no filter.")
    if len(grep) > MAX_GREP:
        raise Refused(f"A pattern is at most {MAX_GREP} characters.")
    try:
        re.compile(grep)
    except re.error as error:
        # A proxy for the far end, which matches with PCRE2. It catches an
        # unclosed group or a stray quantifier, which is what a person typing
        # into a box produces, and it costs nothing.
        raise Refused(f"{grep!r} is not a valid pattern: {error}.") from error


def _check_window(query: Query) -> None:
    until = query.until
    if until is not None and until <= query.since:
        raise Refused("The window ends before it starts.")
    end = until or datetime.now(UTC)
    if (end - query.since).total_seconds() > MAX_WINDOW_SECONDS:
        days = MAX_WINDOW_SECONDS // 86400
        raise Refused(f"A window spans at most {days} days.")


def argv(query: Query) -> list[str]:
    """`journalctl` and its arguments, for a query that has been checked."""
    if not query._checked:  # noqa: SLF001 - the flag is this module's own
        raise Refused("A query reaches a command line only once it is checked.")
    built = [
        "journalctl",
        "--no-pager",
        # Info messages out of the way. The one that matters is the notice
        # about seeing no system messages, which an account outside the
        # `systemd-journal` group gets and which would otherwise land in the
        # middle of the JSON.
        "--quiet",
        "--output=json",
        f"--output-fields={','.join(FIELDS)}",
        f"--since={_stamp(query.since)}",
        f"--lines={query.lines}",
    ]
    if query.until is not None:
        built.append(f"--until={_stamp(query.until)}")
    for unit in query.units:
        built += ["--unit", unit]
    for identifier in query.identifiers:
        built += ["--identifier", identifier]
    if query.priority is not None:
        built += ["--priority", str(query.priority)]
    if query.grep is not None:
        built += ["--grep", query.grep]
    return built


def command(query: Query) -> str:
    """What the far end runs, quoted once for each shell it crosses.

    `sudo -n /bin/sh -c` because that is the whole of the rule the ISO grants
    the `ansible` account, which is in neither `adm` nor `systemd-journal` and
    therefore sees nothing of the system journal otherwise. `-n` so a machine
    where the rule is missing answers with an error rather than waiting on a
    password prompt nothing will type into. D62 reached the same place for
    `virsh`, and D63 records why a group membership would be no smaller a
    grant: that account holds arbitrary root already, because Ansible needs it.
    """
    inner = shlex.join(argv(query))
    return f"sudo -n /bin/sh -c {shlex.quote(inner)}"


def _stamp(moment: datetime) -> str:
    """A bound `journalctl` reads the same way on every machine.

    Suffixed with `UTC`, because a bare timestamp is read in the machine's
    local time and the machines of one inventory need not share a timezone.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


class Entry(BaseModel):
    """One line of one machine's journal.

    `host` is the inventory's name for the machine rather than the
    `_HOSTNAME` the entry carries, because that is what every other page of
    this service calls it, and because a machine renamed since it wrote the
    line would otherwise appear as a fourth member of a three node cluster.

    `unit` is the unit the line is **about**, which is `UNIT` where systemd set
    one and the writer's own `_SYSTEMD_UNIT` otherwise. See `FIELDS` for why
    the two differ and why taking the second alone reads wrong.
    """

    host: str
    timestamp: datetime
    message: str
    priority: int | None = None
    unit: str | None = None
    identifier: str | None = None
    pid: int | None = None
    cursor: str = ""

    @property
    def sort_key(self) -> tuple[datetime, str, str]:
        """Chronological, and stable when two machines share a microsecond."""
        return (self.timestamp, self.host, self.cursor)


class Unreadable(BaseModel):
    """A line the far end sent that this could not read."""

    reason: str
    excerpt: str = Field(default="", description="The first characters of it")


def parse(text: str, host: str) -> tuple[list[Entry], list[Unreadable]]:
    """Every entry in a `-o json` answer, and whatever could not be read.

    Tolerant on purpose. This is the output of a program on a machine that may
    be running a different systemd than this image was built against, and one
    line it cannot make sense of must cost that line rather than the answer. A
    page that renders nothing because a single entry carried a field in a shape
    this did not expect would be worse than one that renders the other four
    hundred and ninety nine and says so.
    """
    entries: list[Entry] = []
    skipped: list[Unreadable] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except ValueError as error:
            skipped.append(Unreadable(reason=str(error), excerpt=stripped[:120]))
            continue
        if not isinstance(raw, dict):
            skipped.append(
                Unreadable(reason="not a JSON object", excerpt=stripped[:120])
            )
            continue
        entry = _entry(raw, host)
        if entry is None:
            skipped.append(
                Unreadable(reason="no usable timestamp", excerpt=stripped[:120])
            )
            continue
        entries.append(entry)
    return entries, skipped


def _entry(raw: dict[str, Any], host: str) -> Entry | None:
    moment = _moment(raw.get("__REALTIME_TIMESTAMP"))
    if moment is None:
        return None
    return Entry(
        host=host,
        timestamp=moment,
        message=_text(raw.get("MESSAGE")),
        priority=_integer(raw.get("PRIORITY")),
        unit=_unit(raw),
        identifier=_text(raw.get("SYSLOG_IDENTIFIER")) or None,
        pid=_integer(raw.get("_PID")),
        cursor=_text(raw.get("__CURSOR")),
    )


def _unit(raw: dict[str, Any]) -> str | None:
    """The unit the line is about, which is not always the one that wrote it.

    `systemd` reports a unit stopping, failing or being started under its own
    `_SYSTEMD_UNIT=init.scope`, with the unit it is reporting on in `UNIT`. A
    reading of a cluster daemon wants those lines, and wants them labelled with
    the daemon rather than with PID 1's scope.
    """
    return _text(raw.get("UNIT")) or _text(raw.get("_SYSTEMD_UNIT")) or None


def _moment(value: Any) -> datetime | None:
    """`__REALTIME_TIMESTAMP`, which is microseconds since the epoch."""
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _text(value: Any) -> str:
    """A field's value, whichever of the three shapes the journal wrote it in.

    A string is the ordinary case. A list appears when one entry carries the
    same field twice, which a program writing its own `MESSAGE=` can produce,
    and when the value is not valid UTF-8, in which case `journalctl` writes
    the bytes as a list of integers rather than losing them. A mapping is what
    a field too large for the output is replaced with.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if value and all(isinstance(item, int) for item in value):
            return bytes(item & 0xFF for item in value).decode(
                "utf-8", errors="replace"
            )
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return "<field not included in the answer>"
    return str(value)


def _integer(value: Any) -> int | None:
    try:
        return int(_text(value))
    except (TypeError, ValueError):
        return None
