# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Where a run finds the files the inventory names.

The problem this solves has one cause, and it is worth stating exactly, since
everything below is shaped by it. When Ansible resolves a relative `src`, the
only anchors it uses are the role's own directories and **the directory the
playbook sits in** (`DataLoader.path_dwim_relative_stack`). That list is
complete: the directory holding the inventory has no say in it, and neither
does the working directory or the command line.

On a control machine that is a checkout of `seapath-ansible`, the playbooks
live in `<checkout>/playbooks`, so `../files/guest.qcow2` in an inventory means
`<checkout>/files/guest.qcow2`. That is the convention every SEAPATH inventory
in the wild is written against, and the reference inventories use it:
`vm_template: "../templates/vm/guest.xml.j2"`.

This service runs the playbooks out of the installed collection, where the same
path would mean a file inside `seapath.ansible` itself, which the site does not
own and `galaxy.yml` does not even ship. So a run builds a **mirror** of the
collection whose root is the site's own tree:

    <run>/collections/ansible_collections/seapath/ansible/
        playbooks/           a real directory, one symlink per upstream playbook
        roles/               a symlink to the collection's own
        ...                  a symlink per remaining entry of the collection
        inventory.yaml       the site's, copied
        files/               the site's, overlaid
        inventories_private/ whatever else the site keeps beside it

`playbooks/` has to be a real directory rather than a symlink to the
collection's: `..` is resolved by the kernel after the symlink, so a symlinked
`playbooks/` would put `../files` back inside the installed collection. That
one detail is the whole trick.

The playbook is still addressed by its fully qualified name.
`ANSIBLE_COLLECTIONS_PATH` puts the mirror first and the real collections root
second, so the dependency collections (`ansible.posix`, `community.general`)
resolve as they always did. Nothing is written into the image's collection, and
the mirror is left in the run directory afterwards, where it is part of the
trace.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from app.inventory import files as tree
from app.runs.models import StagedFile

logger = logging.getLogger(__name__)

NAMESPACE = "seapath"
COLLECTION = "ansible"

# The site tree, copied into the run so the trace says what was pushed rather
# than what the repository holds now.
SITE_DIRECTORY = "site"
MIRROR_DIRECTORY = "collections"
INVENTORY_FILENAME = "inventory.yaml"

# The one directory that must be real rather than a symlink, because a play's
# basedir is where every relative `src` starts from.
_PLAYBOOKS = "playbooks"

_SKIPPED = {".git"}


class StagedEntry(BaseModel):
    """One name at the root of the mirror, and where it came from."""

    name: str
    source: str


class Staging(BaseModel):
    """What a run was given, recorded next to its events."""

    site_root: Path
    inventory_file: Path
    collections_paths: list[Path]
    entries: list[StagedEntry] = []
    files: list[StagedFile] = []


@dataclass
class _Source:
    root: Path
    label: str
    entries: dict[str, Path] = field(default_factory=dict)


def stage(
    directory: Path,
    inventory_dir: Path,
    collections_path: Path,
    artefacts_dir: Path | None = None,
) -> Staging:
    """Build the tree the run reads, and say what went into it."""
    site = _copy_inventory(directory, inventory_dir)
    collection = collections_path / "ansible_collections" / NAMESPACE / COLLECTION
    mirror = directory / MIRROR_DIRECTORY
    root = mirror / "ansible_collections" / NAMESPACE / COLLECTION

    sources = [
        _source(site, "inventory"),
        _source(artefacts_dir, "artefacts") if artefacts_dir else None,
        _source(collection, "collection"),
    ]
    present = [source for source in sources if source is not None and source.entries]

    entries = _overlay(root, present, force_real={_PLAYBOOKS})
    # `playbooks/` exists even where the collection has none, so that a run
    # against a broken image fails on the missing playbook rather than on a
    # path that resolves nowhere.
    (root / _PLAYBOOKS).mkdir(parents=True, exist_ok=True)

    staging = Staging(
        site_root=root,
        inventory_file=site / INVENTORY_FILENAME,
        collections_paths=[mirror, collections_path],
        entries=entries,
        files=_listing(site, "inventory") + _listing(artefacts_dir, "artefacts"),
    )
    logger.info(
        "Staged %d entries under %s for the run in %s",
        len(entries),
        root,
        directory.name,
    )
    return staging


def _copy_inventory(directory: Path, inventory_dir: Path) -> Path:
    """The inventory folder as it is now, frozen into the run.

    Copied rather than pointed at, for the reason the run has kept a copy of
    the inventory since the first version: the repository moves on, and a trace
    that changed afterwards is not a trace.
    """
    site = directory / SITE_DIRECTORY
    if site.exists():
        shutil.rmtree(site)
    if inventory_dir.is_dir():
        shutil.copytree(
            inventory_dir,
            site,
            symlinks=False,
            ignore=shutil.ignore_patterns(*_SKIPPED),
        )
    else:
        site.mkdir(parents=True)
    return site


def _listing(root: Path | None, label: str) -> list[StagedFile]:
    """Every file the run was given from one store, for the record."""
    if root is None or not root.is_dir():
        return []
    return [
        StagedFile(
            path=path.relative_to(root).as_posix(),
            size=path.stat().st_size,
            source=label,
        )
        for path in tree.walk(root)
    ]


def _source(root: Path | None, label: str) -> _Source | None:
    if root is None or not root.is_dir():
        return None
    return _Source(
        root=root,
        label=label,
        entries={entry.name: entry for entry in sorted(root.iterdir())},
    )


def _overlay(
    destination: Path,
    sources: list[_Source],
    force_real: set[str] | None = None,
) -> list[StagedEntry]:
    """Lay the sources over each other, highest priority first.

    A name only one source has becomes a symlink to it, which costs nothing and
    keeps the mirror honest about where the bytes are. A name several sources
    have becomes a real directory holding the merge, when they are all
    directories. Where one of them is a file the first source wins, since a
    file cannot be merged and the site's copy is the one an operator put there.
    """
    destination.mkdir(parents=True, exist_ok=True)
    force_real = force_real or set()
    ordered: list[str] = []
    for source in sources:
        for name in source.entries:
            if name not in ordered:
                ordered.append(name)

    staged: list[StagedEntry] = []
    for name in sorted(ordered):
        holders = [source for source in sources if name in source.entries]
        paths = [source.entries[name] for source in holders]
        directories = [path for path in paths if path.is_dir()]
        target = destination / name

        if name in force_real or (
            len(directories) > 1 and len(directories) == len(paths)
        ):
            nested = [
                _source(path, source.label)
                for path, source in zip(paths, holders, strict=True)
                if path.is_dir()
            ]
            _overlay(target, [source for source in nested if source is not None])
        else:
            target.symlink_to(paths[0])
        staged.append(
            StagedEntry(name=name, source=", ".join(source.label for source in holders))
        )
    return staged
