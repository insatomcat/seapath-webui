# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Installing a collection on the node, so a fix does not wait for an image.

The collection decides which playbooks exist and what they do, and it is
released on its own schedule by a repository this service does not own. Baking
it into the image alone means a corrected playbook reaches a substation through
an image build, a registry and a restart, and a substation may reach no
registry at all. So the node takes one as a file: the tarball
`ansible-galaxy collection build` produces, uploaded the way a VM image is.

Three properties this module owes the rest of the service. See D23.

**The site tree is self contained.** A run resolves `community.general` and
`ansible.posix` from the same root it resolves `seapath.ansible` from, so the
install starts from a copy of what the image ships and lays the uploaded
collection over it. The alternative, searching the image's root for what the
site's lacks, would make the code a run executes a function of two trees.

**Nothing is swapped under a run.** The install takes the run lock, for the
same reason a second run cannot start: a mirror staged for a running
convergence is symlinks into the collection tree, and renaming that tree away
mid run breaks it in the middle of a substation hypervisor.

**The archive is inspected before `ansible-galaxy` is handed it.** It arrives
over HTTP from an administrator, and what is checked is what a mistake looks
like rather than what an attack does: the wrong file entirely, the wrong
collection, an archive that would write outside the directory it is unpacked
into.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.runs import catalogue
from app.runs.store import RunLocked, RunStore

logger = logging.getLogger(__name__)

NAMESPACE = "seapath"
NAME = "ansible"

# What the lock says while this is going, so a run refused during an install is
# told what is holding it rather than given a run id that names nothing.
LOCK_ID = "collection-install"
LOCK_DESCRIPTION = "A collection installation"

# The command that unpacks the archive. Kept here, in one line, because it is
# the only command this module runs and the list of commands the service may
# run has to stay reviewable.
Runner = Callable[[list[str], Path], str]


class RefusedArchive(Exception):
    """The upload is not a seapath.ansible collection, and says why."""


class InstallFailed(Exception):
    """`ansible-galaxy` refused the archive, with its own output."""


@dataclass(frozen=True)
class ArchiveInfo:
    """What the archive says it is, read from its own manifest."""

    namespace: str
    name: str
    version: str
    digest: str

    @property
    def collection(self) -> str:
        return f"{self.namespace}.{self.name}"


def inspect(archive: Path) -> ArchiveInfo:
    """Read the archive's manifest, and refuse anything else.

    `ansible-galaxy` would refuse most of this too, after unpacking it. Doing
    it first means the refusal names the file the operator picked instead of
    quoting a tool they did not run.
    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _refuse_escaping_members(tar)
            manifest = tar.extractfile("MANIFEST.json")
            if manifest is None:
                raise RefusedArchive(
                    "This archive carries no MANIFEST.json, so it is not a "
                    "collection built by `ansible-galaxy collection build`."
                )
            info = json.loads(manifest.read())["collection_info"]
    except tarfile.ReadError as error:
        raise RefusedArchive(
            "This file is not a gzipped tar archive. What the upload takes is "
            "the tarball `ansible-galaxy collection build` writes."
        ) from error
    except KeyError as error:
        raise RefusedArchive(
            "This archive carries no MANIFEST.json, so it is not a collection "
            "built by `ansible-galaxy collection build`."
        ) from error
    except (OSError, ValueError) as error:
        raise RefusedArchive(f"This archive cannot be read: {error}") from error

    namespace = str(info.get("namespace", ""))
    name = str(info.get("name", ""))
    if (namespace, name) != (NAMESPACE, NAME):
        raise RefusedArchive(
            f"This archive holds {namespace}.{name}. The playbooks this "
            f"service runs are {NAMESPACE}.{NAME}, and installing anything "
            "else here would leave the node with no playbook at all."
        )
    return ArchiveInfo(
        namespace=namespace,
        name=name,
        version=str(info.get("version", "")),
        digest=_digest(archive),
    )


def _refuse_escaping_members(tar: tarfile.TarFile) -> None:
    """No member may land outside the directory it is unpacked into."""
    for member in tar.getmembers():
        name = Path(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise RefusedArchive(
                f"This archive holds {member.name}, which would be written "
                "outside the directory it is unpacked into."
            )
        if member.issym() or member.islnk():
            target = Path(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise RefusedArchive(
                    f"This archive links {member.name} to {member.linkname}, "
                    "which is outside the collection."
                )


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _galaxy(argv: list[str], cwd: Path) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argv, never a shell
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise InstallFailed(
            (completed.stderr or completed.stdout).strip()
            or f"ansible-galaxy exited with {completed.returncode}"
        )
    return completed.stdout


class CollectionInstaller:
    """Installs, and removes, the collection the node runs.

    Removing is as much of the feature as installing: a collection that turns
    out to be wrong on a live hypervisor has to be undoable without an image,
    and what it falls back to is the tree the image shipped, which is the one
    the site was running before.
    """

    def __init__(
        self,
        site_dir: Path,
        image_dir: Path,
        store: RunStore,
        runner: Runner = _galaxy,
    ) -> None:
        self._site = Path(site_dir)
        self._image = Path(image_dir)
        self._store = store
        self._runner = runner

    @property
    def site_dir(self) -> Path:
        return self._site

    def installed(self) -> bool:
        """Whether the node runs a collection of its own."""
        return catalogue.installed_in(self._site)

    def install(self, archive: Path) -> ArchiveInfo:
        """Unpack an uploaded archive into the site tree, or leave it alone.

        Built beside the tree and renamed over it, so a failure halfway leaves
        the node running exactly what it ran before. A node with a broken
        collection is a node that cannot converge and cannot be repaired by
        converging.
        """
        info = inspect(archive)
        self._store.acquire(LOCK_ID, LOCK_DESCRIPTION)
        try:
            staging = self._site.with_name(f".{self._site.name}.installing")
            previous = self._site.with_name(f".{self._site.name}.previous")
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(previous, ignore_errors=True)

            # From the image's tree rather than from the site's own, so that
            # installing twice never accumulates: what is not in the image and
            # not in the archive has no business being there.
            self._seed(staging)
            self._runner(
                [
                    "ansible-galaxy",
                    "collection",
                    "install",
                    str(archive),
                    "--collections-path",
                    str(staging),
                    "--force",
                    # The dependencies come from the seed. Resolving them would
                    # reach for a galaxy server, which a substation does not
                    # have, and the failure would be a timeout.
                    "--no-deps",
                ],
                staging.parent,
            )
            if not catalogue.installed_in(staging):
                raise InstallFailed(
                    "ansible-galaxy reported success and left no collection "
                    f"under {staging}."
                )

            if self._site.exists():
                self._site.rename(previous)
            staging.rename(self._site)
            shutil.rmtree(previous, ignore_errors=True)
        finally:
            self._store.release(LOCK_ID)

        logger.info(
            "Installed %s %s (%s) under %s",
            info.collection,
            info.version,
            catalogue.identity(self._site),
            self._site,
        )
        return info

    def remove(self) -> bool:
        """Fall back to the collection the image ships. False if already there."""
        if not self._site.exists():
            return False
        self._store.acquire(LOCK_ID, LOCK_DESCRIPTION)
        try:
            shutil.rmtree(self._site)
        finally:
            self._store.release(LOCK_ID)
        logger.info(
            "Removed the collection under %s. The node runs the one the image "
            "ships again.",
            self._site,
        )
        return True

    def _seed(self, staging: Path) -> None:
        """Copy the image's tree, so the site's is self contained.

        `community.general` and `ansible.posix` are what the roles call, and
        they come from `prepare.sh` at build time. A site tree holding only
        `seapath.ansible` would parse until the first task that uses one and
        refuse the whole run with `couldn't resolve module/action`.
        """
        staging.parent.mkdir(parents=True, exist_ok=True)
        if self._image.is_dir():
            shutil.copytree(self._image, staging, symlinks=True)
        else:
            staging.mkdir()
            logger.warning(
                "There is no collection under %s to seed from, so the "
                "installed one carries no dependency collection either.",
                self._image,
            )


__all__ = [
    "ArchiveInfo",
    "CollectionInstaller",
    "InstallFailed",
    "RefusedArchive",
    "RunLocked",
]
