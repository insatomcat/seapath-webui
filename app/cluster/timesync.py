# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Whether a machine's clock is synchronised, from the exposition it serves.

A sampled value is stamped with the time of the machine that produced it, so
on a SEAPATH hypervisor the clock is part of the real time question as much as
the isolated cores are. Three readings answer it, and all three are on the
`node_exporter` port the CPU pool is already read from, so this adds no scrape.

- **The kernel clock**, from `node_exporter`'s default `timex` collector. chrony
  runs under `timemaster` whatever the source, NTP or the PTP hardware clock,
  and it clears the kernel's unsynchronised flag and sets the error bounds when
  it holds a source it trusts. That is the answer to "is this clock
  synchronised" whichever protocol feeds it.
- **The units**, from the `systemd` collector: `timemaster`, which starts
  chrony and ptp4l, and `ptpstatus`, which derives the level below.
- **The PTP level**, from `seapath_ptp_*`, which the `ptpstatus` service of
  `ptp_status_vsock` writes to the textfile directory beside the pool. It is
  the IEC 61850-9-2 `SmpSynch` a guest puts in its sampled values: 2 for a
  grandmaster traceable to a global reference, 1 for a grandmaster that is
  not, 0 for none. `ptpstatus.sh` computes it from `pmc` every second; this
  reads what it published rather than asking `pmc` a second time.

Nothing here judges, for the reason `tuning.py` gives: a reading that could not
be made and a reading that came back bad are kept apart all the way to the
check.
"""

from __future__ import annotations

import time

from pydantic import BaseModel

from app.cluster import metrics, systemd

_SYNC = "node_timex_sync_status"
_OFFSET = "node_timex_offset_seconds"
_ESTIMATED = "node_timex_estimated_error_seconds"
_MAX_ERROR = "node_timex_maxerror_seconds"

PTP_PUBLISHED = "seapath_ptp_info"
_PTP_LEVEL = "seapath_ptp_smpsynch"
_PTP_OFFSET = "seapath_ptp_master_offset_seconds"
_PTP_TIME = "seapath_ptp_timestamp_seconds"

TIMEMASTER = "timemaster.service"
PTPSTATUS = "ptpstatus.service"


class PtpReading(BaseModel):
    """What `ptpstatus` last derived from `pmc`, as it published it."""

    smpsynch: int | None = None
    """0, 1 or 2, the IEC 61850-9-2 `SmpSynch` of this machine."""
    gm_present: bool | None = None
    gm_identity: str = ""
    clock_class: int | None = None
    """The grandmaster's clockClass: 6 locked, 7 in holdover, 248 default."""
    clock_accuracy: str = ""
    """The grandmaster's clockAccuracy as `pmc` prints it, `0x21` for 100 ns."""
    port_state: str = ""
    """This machine's ptp4l port: SLAVE when it follows a grandmaster."""
    offset_seconds: float | None = None
    """ptp4l's offset from its master, which is how far the PHC is off."""
    age_seconds: float | None = None
    """How long ago `ptpstatus` wrote it. A stopped service leaves the file."""


class ClockReading(BaseModel):
    """The clock of one machine, as its exporter published it."""

    synchronised: bool | None = None
    """The kernel's own flag, None when the timex collector published nothing."""
    offset_seconds: float | None = None
    estimated_error_seconds: float | None = None
    max_error_seconds: float | None = None
    timemaster: str | None = None
    """The state of `timemaster.service`, None when the unit is not published."""
    ptpstatus: str | None = None
    ptp: PtpReading | None = None
    """None when this node publishes no `seapath_ptp_*` block."""

    @property
    def published(self) -> bool:
        """Whether the exposition said anything about the clock at all.

        An exporter with none of the three is a test fixture or a stripped
        collector, and two grey rows would say nothing its column does not.
        """
        return (
            self.synchronised is not None
            or self.ptp is not None
            or self.timemaster is not None
        )


def read(series: dict[str, list[metrics.Sample]]) -> ClockReading:
    units = systemd.read(series, {TIMEMASTER, PTPSTATUS})
    sync = _value(series, _SYNC)
    return ClockReading(
        synchronised=None if sync is None else sync == 1,
        offset_seconds=_value(series, _OFFSET),
        estimated_error_seconds=_value(series, _ESTIMATED),
        max_error_seconds=_value(series, _MAX_ERROR),
        timemaster=units[TIMEMASTER].state if TIMEMASTER in units else None,
        ptpstatus=units[PTPSTATUS].state if PTPSTATUS in units else None,
        ptp=_ptp(series),
    )


def _ptp(series: dict[str, list[metrics.Sample]]) -> PtpReading | None:
    samples = series.get(PTP_PUBLISHED, [])
    if not samples:
        return None
    labels = samples[0].labels
    level = _value(series, _PTP_LEVEL)
    written = _value(series, _PTP_TIME)
    present = labels.get("gm_present", "")
    return PtpReading(
        smpsynch=None if level is None else int(level),
        gm_present=None if not present else present == "true",
        gm_identity=labels.get("gm_identity", ""),
        clock_class=_int(labels.get("clock_class")),
        clock_accuracy=labels.get("clock_accuracy", ""),
        port_state=labels.get("port_state", ""),
        offset_seconds=_value(series, _PTP_OFFSET),
        age_seconds=None if written is None else max(0.0, time.time() - written),
    )


def _value(series: dict[str, list[metrics.Sample]], name: str) -> float | None:
    samples = series.get(name, [])
    return samples[0].value if samples else None


def _int(raw: str | None) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
