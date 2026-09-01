# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The large files a run pushes, beside the repository rather than in it.

A VM image is referenced by the inventory exactly like a quadlet file is, and
Ansible resolves both the same way, so a run needs them in the same tree. Git
is where the resemblance stops: a twenty gigabyte qcow2 in a repository stays
in its history forever, one version per upload, and takes the export, the
clone and the whole "the inventory is a small git repository" idea with it.

So this store holds them, unversioned, and the run overlays it under the same
root as the repository. What that costs is honest and worth writing down: a
change to an artefact leaves no trace in `git log`, and the export carries the
desired state without the images it names. A run records what it staged, which
is the trace that remains.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import AsyncIterable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from app.inventory import files as tree

logger = logging.getLogger(__name__)


class ArtefactStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def files(self) -> list[tree.StoredFile]:
        return [tree.describe(self._root, path) for path in tree.walk(self._root)]

    def file_path(self, path: str) -> Path:
        return tree.resolve_within(self._root, path)

    @contextmanager
    def _receiving(self, path: str) -> Iterator[tuple[BinaryIO, Path]]:
        """An upload in progress, landing beside its target.

        Renamed once the last chunk is in, so a run launched during an upload
        reads either the previous file or the new one. Half a VM image, pushed
        to three hypervisors, is the failure this prevents.
        """
        target = self.file_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.uploading")
        try:
            with temporary.open("wb") as handle:
                yield handle, target
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        logger.info("Stored the artefact %s (%d bytes)", path, target.stat().st_size)

    def write(self, path: str, chunks: Iterable[bytes]) -> tree.StoredFile:
        """Stream an upload to disk.

        Streamed rather than read whole: this is the store that exists for the
        files too big to hold in memory, and a service that read one into a
        buffer would die of it on a node with 16 GB of RAM.
        """
        with self._receiving(path) as (handle, target):
            for chunk in chunks:
                handle.write(chunk)
        return tree.describe(self._root, target)

    async def write_stream(
        self, path: str, chunks: AsyncIterable[bytes]
    ) -> tree.StoredFile:
        """The same, fed by a request body as it arrives.

        The write between two chunks is short enough to leave the event loop
        responsive, which matters: the page polling a run in progress is served
        by the same process as the upload of a twenty gigabyte image.
        """
        with self._receiving(path) as (handle, target):
            async for chunk in chunks:
                handle.write(chunk)
        return tree.describe(self._root, target)

    def delete(self, path: str) -> bool:
        target = self.file_path(path)
        if not target.is_file():
            return False
        target.unlink()
        parent = target.parent
        while parent != self._root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        logger.info("Removed the artefact %s", path)
        return True

    def free_bytes(self) -> int:
        """Room left where the artefacts live.

        Shown before an upload rather than after it: an image that fills the
        partition holding the run traces is a node that can no longer say what
        it did.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self._root).free
