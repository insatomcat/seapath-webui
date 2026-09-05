# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Version 1 of the REST API, specified in docs/api.md."""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    cluster,
    collection,
    console,
    inventory,
    node,
    realtime,
    runs,
    storage,
    trust,
)

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(node.router)
router.include_router(cluster.router)
router.include_router(storage.router)
router.include_router(console.router)
router.include_router(collection.router)
router.include_router(inventory.router)
router.include_router(realtime.router)
router.include_router(runs.router)
router.include_router(trust.router)
