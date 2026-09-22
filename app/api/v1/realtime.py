# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Real time conformance, and the measurements that back it.

Read only, like the node view, with one exception. The conformance half
reports what the tuning came out as, and the measurement half reads the
histogram a `cyclictest` run fetched. Launching that run is `POST /runs` like
any other playbook, which is what keeps one lock, one confirmation and one
history across everything that touches a machine.

The exception is a machine's allocation strategy, offered beside the CPU pool
it governs. It is still a commit and a run: the variable goes to the inventory
and `deploy_seapath_alloc` puts it on the machine.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from app.api.v1 import reads
from app.cluster.pool import ClusterPool
from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.hosts.models import RealtimeReading
from app.inventory.service import ImportRefused, RefusedWrite
from app.services.realtime import (
    AllocationStrategy,
    Measurement,
    MeasurementKind,
    RealtimeConformance,
    RealtimeService,
    UnknownMachine,
)

router = APIRouter(
    prefix="/realtime",
    tags=["realtime"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

admin = Depends(require_role(Role.ADMIN))


def _service(request: Request) -> RealtimeService:
    return request.app.state.realtime_service


@router.get("", response_model=RealtimeConformance)
def conformance(request: Request) -> RealtimeConformance:
    """What this machine's real time tuning is, against what it was told.

    A check is either conformance, where the inventory declares a value and
    the two are compared, or advice, where nothing declares one. The
    difference is in the payload, because only the first kind has an action
    behind it: edit the inventory and converge.
    """
    return _service(request).conformance()


@router.get("/reading", response_model=RealtimeReading)
def reading(request: Request) -> RealtimeReading:
    """The raw values the checks are formed from, for an automation client."""
    return _service(request).conformance().reading


@router.get("/pool", response_model=ClusterPool, dependencies=[reads.reading])
def pool(request: Request) -> ClusterPool:
    """The CPU pool of every machine the inventory declares.

    Read from each node's `prometheus-node-exporter`, which serves the
    `seapath_alloc_*` textfile the allocator writes every fifteen seconds. This
    service asks the exporter rather than computing it: occupancy is the
    affinity of every QEMU thread in `/proc`, which this container's PID
    namespace hides and which the quadlet may not be given.

    A node that cannot be reached is reported with the reason and the others
    are still returned, because a cluster half built is the ordinary state of a
    cluster being built.
    """
    return _service(request).pool()


class StrategyWrite(BaseModel):
    strategy: AllocationStrategy = Field(
        description="The `seapath_alloc_strategy` the machine is to allocate with"
    )


class StrategyResponse(BaseModel):
    host: str
    strategy: AllocationStrategy
    commit: str | None = None
    message: str | None = None
    run_id: str | None = Field(
        default=None, description="The run writing it to the machine"
    )
    state: str | None = None


@router.put("/pool/{host}/strategy", response_model=StrategyResponse)
def write_strategy(
    request: Request,
    host: str,
    payload: StrategyWrite,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> StrategyResponse:
    """Set how one machine's `seapath-alloc` hands out its isolated CPUs.

    One commit writing `seapath_alloc_strategy` on the machine's entry, then
    one run of `seapath_setup_deploy_seapath_alloc` narrowed to it, which
    templates `/etc/seapath/alloc.yaml`. The allocator reads that file at each
    allocation, so what is pinned already stays where it is. No commit and no
    run when the machine already receives this value. `409
    precondition_failed` when the playbook cannot run from here, before
    anything is committed.
    """
    try:
        commit, record = _service(request).set_strategy(
            host, payload.strategy, user.username, if_match
        )
    except UnknownMachine as error:
        raise ApiError("unknown_host", str(error), 404) from error
    except RefusedWrite as error:
        raise ApiError(
            "refused_write",
            str(error),
            409,
            {"divergences": [d.model_dump() for d in error.divergences]},
        ) from error
    except ImportRefused as error:
        raise ApiError(
            "invalid_inventory",
            str(error),
            422,
            {"findings": [f.model_dump() for f in error.validation.findings]},
        ) from error
    if commit is None:
        return StrategyResponse(host=host, strategy=payload.strategy)
    return StrategyResponse(
        host=host,
        strategy=payload.strategy,
        commit=commit.hash,
        message=commit.message,
        run_id=record.id if record else None,
        state=record.state.value if record else None,
    )


@router.get("/measurements", response_model=list[Measurement])
def measurements(
    request: Request,
    kind: MeasurementKind | None = None,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[Measurement]:
    """The measurement runs launched from this node, newest first.

    Both kinds by default, or one when `kind` names it. `cyclictest` reports
    what the scheduler delivered and `hwlatdetect` what the firmware took
    without telling the kernel: complementary answers rather than alternatives,
    which is why they are one history with a discriminator rather than two.

    Each carries the inventory commit its machines were converged from, so a
    latency figure can be read against the isolation that produced it.
    """
    return _service(request).measurements(kind=kind, limit=limit)
