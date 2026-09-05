# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The Ceph cluster, as its active manager reports it.

Read only, like `cluster`. Adding storage is `ceph_osd_disks` in the inventory
and a run of `cluster_setup_cephadm`; removing an OSD is not offered at all,
for the reason `docs/ceph.md` gives.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.cluster.ceph import CephCluster
from app.core.auth import Role
from app.core.security import require_role
from app.services.storage import StorageService

router = APIRouter(
    prefix="/storage",
    tags=["storage"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _service(request: Request) -> StorageService:
    return request.app.state.storage_service


@router.get("", response_model=CephCluster)
def storage(request: Request) -> CephCluster:
    """Health, capacity, daemons, OSDs, pools and placement groups.

    Served by whichever machine holds the active manager, which is why every
    node is asked and `source` names the one that answered.

    A cluster with no Ceph answers `available: false` and a sentence saying so.
    Local storage is a supported SEAPATH configuration and this must never be
    read as a failure.
    """
    return _service(request).ceph()
