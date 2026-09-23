# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What every machine, and each guest and container on it, is consuming.

The last few minutes of it, as this service read them: a reading every few
seconds while somebody signed in is using the service, and what each pair of
readings says per second. Kept in memory for the window and nowhere else. See
D67.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.core.auth import Role
from app.core.security import require_role
from app.services.usage import UsageHistory, UsageRecorder

router = APIRouter(
    prefix="/usage",
    tags=["usage"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _recorder(request: Request) -> UsageRecorder:
    return request.app.state.usage_recorder


@router.get("", response_model=UsageHistory)
def usage(
    request: Request,
    since: float | None = Query(
        default=None,
        description=(
            "Only the points taken after this moment, on the clock `now` and "
            "`at` are given on. What a page already holding the window sends, "
            "with the `at` of the last point it has."
        ),
    ),
) -> UsageHistory:
    """Every machine's last few minutes, and each workload's share of them.

    `points` are the readings of the window, oldest first: each carries the
    memory, and, against the reading before it, the CPUs busy on the
    housekeeping and the isolated side, the disk and physical port bytes per
    second, and each running workload's CPU and memory, by `vm:<name>` or
    `container:<name>`. A point after a gap in the readings has no rates.

    `latest` is the last reading as the exporters gave it, and `workloads`,
    `disks` and `interfaces` are the rates it gave, for the tables.

    Asking marks somebody as using the service, which keeps the readings
    going. The first request after a quiet spell takes a reading itself, so
    its answer carries one point and the rates start with the next.
    """
    return _recorder(request).history(since)
