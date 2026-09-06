# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The peers a laptop has: directories, holding real git repositories.

The other fakes of this service stand in for a machine that is not there. This
one stands in for the two other nodes of a cluster that is not there, so the
replication page can be developed, and its failure paths seen, with no cluster
and no ssh anywhere.

What it fakes is the transport and nothing else. The push is a real `git push`
into a real repository with a real worktree, so a fast forward lands and a
divergence is refused here exactly as it is on a substation.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from threading import Lock

from app.inventory.replication import Target
from app.inventory.repository import InventoryRepository

logger = logging.getLogger(__name__)


class FakePeerTransport:
    """Every machine of the inventory is a clone of this node's repository."""

    def __init__(self, root: Path, source: Path) -> None:
        self._root = root
        self._source = source
        # The fan out reaches the machines in parallel, and a first contact
        # creates a directory.
        self._lock = Lock()

    def url(self, target: Target) -> str:
        path = self._root / target.host
        with self._lock:
            if not path.exists():
                self._clone(path)
        return str(path)

    def ssh_command(self) -> str | None:
        return None

    def upload_pack(self) -> str | None:
        return None

    def receive_pack(self) -> str | None:
        return None

    def _clone(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(  # noqa: S603 - fixed argv, never a shell
            ["git", "clone", "--quiet", str(self._source), str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            # A node whose repository was never created is a state the page has
            # to render, and it is what this leaves behind.
            logger.info("The fake peer at %s holds no repository yet", path)
            return
        InventoryRepository(path).accept_replication()
