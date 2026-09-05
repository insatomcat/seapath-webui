# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A guest's metadata, read and written where it lives, which is Ceph.

`vm_manager` stores what Pacemaker needs to know about a guest as metadata on
its system disk image: `_preferred_host`, `_priority`, `_live_migration`,
`_seapath_alloc` and the rest. It writes them at `create` and `enable_vm` reads
them all back; nothing upstream changes one on a guest that exists. D31 has the
search and the bounds.

This asks Ceph directly, with `rbd image-meta`, the way the Cluster page asks
`ha_cluster_exporter` for Pacemaker and the Ceph manager for the pool. The
state belongs to Ceph, so it is read over Ceph's own client rather than through
a machine that would be asked to read it on this service's behalf. The quadlet
has mounted `/etc/ceph` from the start for exactly this, and the container runs
on the host network, so the monitors are reachable.

Two rules keep it as narrow as it looks. Commands go through an injected
runner, so the list of things this service may execute stays short and
reviewable in one place and a test can replay recorded output. And every
invocation is `argv`, a list, so no shell parses it: a value holding a quote or
a newline is one argument either way.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol

from app.hosts.reader import CommandRunner, SubprocessRunner

logger = logging.getLogger(__name__)

# `vm_manager` hardcodes both (`POOL_NAME = "rbd"`, `OS_DISK_PREFIX =
# "system_"`), so they are hardcoded here rather than guessed at, and named so
# the two can be compared when one of them moves.
POOL = "rbd"
IMAGE_PREFIX = "system_"

# What an RBD metadata key may be called. `rbd` itself takes almost anything,
# and this is narrower on purpose: the keys SEAPATH uses are `_preferred_host`
# and its family, and a site's own are letters and digits. A key outside this
# is refused rather than sent, so what lands on an image stays greppable.
KEY = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Long enough for a pinning profile, which is the biggest value SEAPATH stores:
# a few lines of YAML under `_seapath_alloc`.
MAX_VALUE_BYTES = 64 * 1024

# Ceph answers or it does not. Long enough for a monitor election, short enough
# that a page waiting on it says so rather than hanging.
TIMEOUT = 10.0


def image_of(guest: str) -> str:
    """The RBD image holding a guest's system disk, and its metadata."""
    return f"{IMAGE_PREFIX}{guest}"


class RbdUnavailable(Exception):
    """Ceph could not be asked, and the message says what it answered."""


class RbdClient(Protocol):
    """The metadata of one image, listed and changed."""

    def list_metadata(self, image: str) -> dict[str, str]: ...

    def set_metadata(self, image: str, key: str, value: str) -> None: ...

    def remove_metadata(self, image: str, key: str) -> None: ...


class CommandRbdClient:
    """`rbd image-meta`, run against the Ceph configuration the quadlet mounts."""

    def __init__(self, runner: CommandRunner | None = None, pool: str = POOL) -> None:
        self._runner = runner or SubprocessRunner()
        self._pool = pool

    def list_metadata(self, image: str) -> dict[str, str]:
        result = self._run(["image-meta", "list", image, "--format", "json"])
        try:
            document = json.loads(result or "{}")
        except ValueError as error:
            raise RbdUnavailable(
                f"rbd answered something that is not JSON: {error}"
            ) from error
        if not isinstance(document, dict):
            raise RbdUnavailable("rbd answered something that is not a mapping.")
        return {str(key): str(value) for key, value in document.items()}

    def set_metadata(self, image: str, key: str, value: str) -> None:
        self._run(["image-meta", "set", image, key, value])
        logger.info("Set %s on %s", key, image)

    def remove_metadata(self, image: str, key: str) -> None:
        self._run(["image-meta", "remove", image, key])
        logger.info("Removed %s from %s", key, image)

    def _run(self, arguments: list[str]) -> str:
        result = self._runner.run(
            ["rbd", "-p", self._pool, *arguments], timeout=TIMEOUT
        )
        if not result.ok:
            # The operator reads this, so it carries what Ceph said rather than
            # a return code. A missing binary and an unreachable monitor look
            # nothing alike and are repaired differently.
            reason = (result.stderr or result.stdout).strip() or "no reason given"
            raise RbdUnavailable(reason)
        return result.stdout
