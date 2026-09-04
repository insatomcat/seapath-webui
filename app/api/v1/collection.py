# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Which collection this node runs, and installing another one.

The collection decides which playbooks exist and what they do. Replacing it is
therefore an administrator's act with the same weight as editing the desired
state: the next apply runs the code that lands here. Reading which one is
installed is open to viewers, because it is half the answer to "what did this
run do".

See D23 in docs/decisions.md for why the node carries one at all.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.logging import audit_event
from app.core.security import require_role
from app.inventory.service import InventoryService
from app.runs import catalogue
from app.runs.install import (
    CollectionInstaller,
    InstallFailed,
    RefusedArchive,
)
from app.runs.store import RunLocked

router = APIRouter(prefix="/collection", tags=["collection"])

viewer = Depends(require_role(Role.VIEWER))
admin = Depends(require_role(Role.ADMIN))

# The archive `ansible-galaxy collection build` writes is a few megabytes. The
# limit is what stops an upload that is obviously something else, a VM image
# above all, from filling the partition that holds the run traces.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def _installer(request: Request) -> CollectionInstaller:
    return request.app.state.collection_installer


def _inventory(request: Request) -> InventoryService:
    return request.app.state.inventory_service


class CollectionState(BaseModel):
    """What the next run will execute."""

    source: str = Field(description="`site` for one installed here, `image` otherwise")
    path: Path
    version: str | None = Field(
        description="The version and the fingerprint of the tree, as a run records it"
    )
    image_version: str = Field(
        description="What the image was built with, reported even when it is not used"
    )
    site_installed: bool


def _state(request: Request) -> CollectionState:
    installer = _installer(request)
    root = request.app.state.collections_root()
    return CollectionState(
        source="site" if root == installer.site_dir else "image",
        path=root,
        version=catalogue.identity(root),
        image_version=request.app.state.settings.collection_version,
        site_installed=installer.installed(),
    )


@router.get("")
def read_collection(request: Request, user: User = viewer) -> CollectionState:
    """Which collection this node runs, and where it came from."""
    return _state(request)


@router.put("")
async def install_collection(request: Request, user: User = admin) -> CollectionState:
    """Install an uploaded `seapath.ansible` collection on this node.

    The body is the tarball `ansible-galaxy collection build` writes. The
    installed tree seeds from the one the image ships, so the dependency
    collections the roles call are there whatever the archive carries, and it
    is built beside the live one and renamed over it, so a refusal leaves the
    node running what it ran before.
    """
    installer = _installer(request)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as handle:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_ARCHIVE_BYTES:
                raise ApiError(
                    "archive_too_large",
                    f"A collection archive above {MAX_ARCHIVE_BYTES // (1024 * 1024)} "
                    "MB is something other than a collection.",
                    413,
                )
            handle.write(chunk)
        handle.flush()
        archive = Path(handle.name)
        if not received:
            raise ApiError(
                "empty_archive", "The request carried no archive at all.", 400
            )
        try:
            info = installer.install(archive)
        except RefusedArchive as error:
            raise ApiError("refused_archive", str(error), 400) from error
        except RunLocked as error:
            raise ApiError("run_in_progress", str(error), 409) from error
        except (InstallFailed, OSError) as error:
            raise ApiError("install_failed", str(error), 500) from error

    state = _state(request)
    audit_event(
        "collection.installed",
        user=user.username,
        collection=info.collection,
        version=info.version,
        fingerprint=state.version,
        sha256=info.digest,
    )
    # The desired state did not move and the code that applies it did, so the
    # audit trail carries an empty commit rather than nothing at all.
    _inventory(request).record_event(
        f"Install the {info.collection} collection {info.version} "
        f"({state.version}) on this node",
        user.username,
    )
    return state


@router.delete("")
def remove_collection(request: Request, user: User = admin) -> CollectionState:
    """Fall back to the collection the image ships.

    The undo of an install, and the reason an install is safe to attempt on a
    live node: what it falls back to is the tree the site was running before.
    """
    installer = _installer(request)
    try:
        removed = installer.remove()
    except RunLocked as error:
        raise ApiError("run_in_progress", str(error), 409) from error
    except OSError as error:
        raise ApiError("remove_failed", str(error), 500) from error
    if not removed:
        raise ApiError(
            "no_site_collection",
            "This node runs the collection its image ships, so there is "
            "nothing to remove.",
            409,
        )

    state = _state(request)
    audit_event("collection.removed", user=user.username, fingerprint=state.version)
    _inventory(request).record_event(
        f"Remove the collection installed on this node, back to the image's "
        f"({state.version})",
        user.username,
    )
    return state
