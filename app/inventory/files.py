# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Paths inside a directory this service owns, and nothing outside it.

Two stores hold the files a run pushes: the inventory repository, which is
versioned, and the artefact store, which is not. Both take a path from a
browser, and both would be a way out of their own directory if they took it
literally. The rules are here once rather than twice.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

# Written next to the inventory by `diff_against`, and never a file an operator
# put there.
_INTERNAL = (".git",)


class UnsafePath(Exception):
    """A path that would leave the directory, or name something git owns."""


class StoredFile(BaseModel):
    """One file of a store, as the API and the page see it."""

    path: str
    size: int
    modified: datetime
    # Read to decide whether the page offers to show the file or only to
    # download it. Guessed from the bytes rather than from the extension: a
    # site's `.conf` is text and its `.qcow2` is not, whatever they are called.
    text: bool


def relative_path(value: str) -> PurePosixPath:
    """The path as a store may use it, or a refusal saying why.

    Everything that could name a file outside the store is refused rather than
    normalised, because a normalised traversal is a traversal an operator did
    not see happen.
    """
    cleaned = (value or "").strip().replace("\\", "/")
    if not cleaned:
        raise UnsafePath("A file needs a name.")
    path = PurePosixPath(cleaned)
    if path.is_absolute():
        raise UnsafePath(
            f"{cleaned} is an absolute path. Files are named relative to the "
            "inventory folder."
        )
    parts = path.parts
    if any(part in ("..", ".") for part in parts):
        raise UnsafePath(f"{cleaned} walks out of the inventory folder.")
    if parts[0] in _INTERNAL:
        raise UnsafePath(f"{parts[0]} belongs to git, and is not yours to write.")
    return path


def resolve_within(root: Path, value: str) -> Path:
    """The absolute path of `value` inside `root`, symlinks included.

    `relative_path` refuses the traversal a caller writes. This refuses the one
    a symlink already in the tree would perform, which is the same escape with
    one more step in it.
    """
    candidate = root / relative_path(value)
    resolved = Path(os.path.normpath(candidate))
    try:
        real_root = root.resolve()
        real = candidate.resolve()
    except OSError as error:  # pragma: no cover - a broken mount
        raise UnsafePath(str(error)) from error
    if not resolved.is_relative_to(root) or not real.is_relative_to(real_root):
        raise UnsafePath(f"{value} points outside the folder.")
    return candidate


def walk(root: Path) -> Iterator[Path]:
    """Every regular file of the store, in a stable order.

    Symlinks are skipped rather than followed: a store lists what it holds, and
    a link into the host filesystem is not something it holds.
    """
    if not root.is_dir():
        return
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in _INTERNAL and not Path(directory, name).is_symlink()
        )
        for name in sorted(names):
            path = Path(directory, name)
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def describe(root: Path, path: Path) -> StoredFile:
    stat = path.stat()
    return StoredFile(
        path=path.relative_to(root).as_posix(),
        size=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        text=is_text(path),
    )


def is_text(path: Path, sample: int = 8192) -> bool:
    """Whether the first bytes look like something a browser can show.

    A NUL byte is the test, which is the one `git` itself uses. It is a guess,
    and it only decides whether the page offers an editor or a download link.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(sample)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode()
    except UnicodeDecodeError:
        return False
    return True
