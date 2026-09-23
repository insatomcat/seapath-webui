# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What every machine, and each guest and container on it, is consuming.

Counters, and the moment each was read. A rate is the difference between two
readings of this endpoint divided by the difference between their times, and
the caller keeps the first: this service answers each reading and remembers
none. See D67.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.auth import Role
from app.core.security import require_role
from app.services.usage import UsageService, UsageView

router = APIRouter(
    prefix="/usage",
    tags=["usage"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _service(request: Request) -> UsageService:
    return request.app.state.usage_service


@router.get("", response_model=UsageView)
def usage(request: Request) -> UsageView:
    """Every machine's counters, its guests' and its containers'.

    Every machine the inventory names with an address is asked, on three
    ports at once: node_exporter, libvirt-exporter and
    prometheus-podman-exporter. Each is read now, never from the few seconds
    of answers the other pages share, because a rate divides by the time
    between two readings. So this takes no `fresh`: every reading is one.

    Each counter is as the exporter published it, in bytes or seconds since
    some start, and None where it published none. `node.read_at` is the
    machine's own clock at the scrape; `guests_read_at` and
    `containers_read_at` are this service's, since neither exporter publishes
    a clock. A counter lower than in the previous reading was reset, by a
    reboot or a restarted guest, and gives no rate for that interval.
    """
    return _service(request).usage()
