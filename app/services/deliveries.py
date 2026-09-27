# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Container deliveries: uploaded, checked, then installed as one commit.

A supplier hands over a workload as an archive in the shape
`roles/deploy_containers_cluster/DELIVERY.md` describes. Installing one is two
steps, because the second needs the operator: the upload is unpacked and
checked, and answered with the site values the delivery asks for; the
installation takes those values, and writes the workload the way an operator
editing the inventory by hand would.

- The quadlets, the RBD seed files, `values.yaml` and the README go to
  `inventories/<name>/` in the versioned folder.
- The image archives go to the artefacts, `files/<archive>`, which a run
  mounts where `../files/` resolves and git never carries.
- The `cluster_containers` entry names them, with the site values.

All of the inventory side is one commit. Nothing reaches a machine: that is
the run of `deploy_containers_cluster`, named at the end.

A new version of a workload already installed is the same path. Its entry is
replaced by the delivery's, with the site values and the placement the form
answers, prefilled with what it had. The files the old entry named and nothing
names any more are removed, wherever they were in the folder. Asked to, the
installation also launches the run that starts the workload again from
nothing, its RBD image included.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.inventory import delivery, quadlets, references
from app.inventory.service import InventoryService, RefusedFile
from app.services.containers import WORKLOAD_PLAYBOOK, ContainerService

logger = logging.getLogger(__name__)

# Where a workload runs, which the site decides and no delivery knows. The
# form answers the first two; the colocations are kept as the entry has them.
_PLACEMENT = ("preferred_host", "pinned_host", "colocated_with", "strong_colocation")

# A staged delivery nobody installed is removed after this long.
_STALE_SECONDS = 24 * 3600


class ValueField(BaseModel):
    """One site value, for the form an operator fills."""

    key: str
    description: str
    format: str
    example: Any = None
    default: Any = None
    has_default: bool = False
    minimum: int | None = None
    maximum: int | None = None
    current: Any = None
    """What the installed workload has, when there is one."""


class Placement(BaseModel):
    """Where the site runs a workload: one of the two, or neither."""

    preferred_host: str | None = None
    """The member it runs on while that member is up."""
    pinned_host: str | None = None
    """The member it runs on, and nowhere else."""


class StagedDelivery(BaseModel):
    id: str
    name: str = ""
    version: str = ""
    """The delivery's directory name, which the contract makes
    `<application>-<version>`."""
    update: bool = False
    """A workload of that name is already installed."""
    images: list[str] = Field(default_factory=list)
    quadlets: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    readme: str = ""
    values: list[ValueField] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)
    """The members a placement may name."""
    placement: Placement = Field(default_factory=Placement)
    """What the installed workload has, when there is one."""
    findings: list[str] = Field(default_factory=list)
    """Why it cannot be installed. Empty when it can."""


class WorkloadValues(BaseModel):
    name: str
    values: list[ValueField] = Field(default_factory=list)


class Installed(BaseModel):
    name: str
    commit: str | None = None
    message: str | None = None
    playbook: str = WORKLOAD_PLAYBOOK
    run_id: str | None = None
    """The run starting the workload again from nothing, when it was asked."""


class RefusedPlacement(Exception):
    """A placement naming no member, or two rules at once."""


class RefusedValues(Exception):
    """Site values that do not fit, by key."""

    def __init__(self, refused: dict[str, str]) -> None:
        super().__init__(
            "; ".join(f"{key} {reason}" for key, reason in sorted(refused.items()))
        )
        self.refused = refused


class UnknownDelivery(Exception):
    """No staged delivery has that id."""


class UnknownValues(Exception):
    """The workload has no `values.yaml` in the inventory folder."""


class DeliveryService:
    def __init__(
        self,
        inventory: InventoryService,
        containers: ContainerService,
        imports_dir: Path,
    ) -> None:
        self._inventory = inventory
        self._containers = containers
        self._imports = imports_dir

    # Staging

    async def stage(self, chunks: AsyncIterable[bytes]) -> StagedDelivery:
        """Receive an archive, unpack it and check it."""
        self._forget_stale()
        staged = StagedDelivery(id=uuid.uuid4().hex)
        home = self._imports / staged.id
        home.mkdir(parents=True)
        archive = home / "delivery.tar"
        with archive.open("wb") as handle:
            async for chunk in chunks:
                handle.write(chunk)
        try:
            root = delivery.unpack(archive, home / "tree")
        except delivery.InvalidDelivery as error:
            staged.findings = error.findings
            return staged
        finally:
            archive.unlink(missing_ok=True)
        return self._describe(staged.id, root)

    def staged(self, staged_id: str) -> StagedDelivery:
        root = self._tree(staged_id)
        return self._describe(staged_id, root)

    def discard(self, staged_id: str) -> None:
        shutil.rmtree(self._home(staged_id), ignore_errors=True)

    def _describe(self, staged_id: str, root: Path) -> StagedDelivery:
        staged = StagedDelivery(id=staged_id, version=root.name)
        try:
            found = delivery.read(root)
        except delivery.InvalidDelivery as error:
            staged.findings = error.findings
            return staged
        current = self._containers.workload(found.name) or {}
        staged.name = found.name
        staged.update = bool(current)
        staged.images = [image.name for image in found.images]
        staged.quadlets = found.quadlets
        staged.files = list(found.files)
        staged.readme = found.readme
        staged.values = _fields(found.values, current)
        staged.nodes = self._containers.workload_hosts()
        staged.placement = Placement(
            preferred_host=current.get("preferred_host"),
            pinned_host=current.get("pinned_host"),
        )
        if found.name in self._containers.known() and not current:
            staged.findings.append(
                f"{found.name} is already declared by upload_extra_files, so "
                "a workload of that name would be a second declaration of the "
                "same unit."
            )
        return staged

    # Installing

    def install(
        self,
        staged_id: str,
        given: dict[str, Any],
        author: str,
        expected_head: str | None = None,
        placement: Placement | None = None,
    ) -> Installed:
        """Write the staged delivery into the inventory, as one commit.

        The entry is the delivery's, the site values and the placement. Keys
        the old entry had beyond those are dropped with it. `placement` None
        keeps the one the workload has.
        """
        root = self._tree(staged_id)
        found = delivery.read(root)
        values, refused = delivery.site_values(found.values, given)
        if refused:
            raise RefusedValues(refused)

        current = self._containers.workload(found.name) or {}
        spec = delivery.entry(found, values)
        spec.update({key: current[key] for key in _PLACEMENT if key in current})
        if placement is not None:
            spec.pop("preferred_host", None)
            spec.pop("pinned_host", None)
            spec.update(self._placement(placement))
        rendering = delivery.render(found, spec)
        if rendering:
            raise delivery.InvalidDelivery(rendering)

        files = delivery.inventory_files(found)
        home = delivery.folder(found.name)
        # What the old entry named outside the workload's folder, a first
        # installation made by hand included, goes when nothing names it any
        # more. The folder itself is the delivery's, and what it no longer has
        # goes whoever wrote it.
        document = self._inventory.raw()
        orphans = (
            references.workload_in_folder(current)
            - references.workload_in_folder(spec)
            - references.in_use(document, leaving=found.name)
        )
        removed = [
            item.path
            for item in self._inventory.files()
            if item.path not in files
            and (item.path.startswith(f"{home}/") or item.path in orphans)
        ]
        replaced = _archives(current) - {image.archive for image in found.images}

        moved = self._move_images(found)
        try:
            commit = self._containers.write_workload(
                found.name,
                spec,
                author,
                expected_head,
                files=files,
                removed=removed,
                message=(
                    f"containers: {'update' if current else 'install'} "
                    f"{found.name} from {root.name}"
                ),
            )
        except Exception:
            self._move_back(moved, root)
            raise
        for archive in replaced:
            if archive not in self._archives_in_use():
                self._inventory.remove_artefact(f"files/{archive}")
        self.discard(staged_id)
        logger.info("Installed the delivery %s as %s", root.name, found.name)
        return Installed(
            name=found.name,
            commit=commit.hash if commit else None,
            message=commit.message if commit else None,
        )

    def _placement(self, placement: Placement) -> dict[str, str]:
        preferred = (placement.preferred_host or "").strip()
        pinned = (placement.pinned_host or "").strip()
        if preferred and pinned:
            raise RefusedPlacement(
                "A workload is either preferred on a member or pinned to one, "
                "not both."
            )
        chosen = preferred or pinned
        if not chosen:
            return {}
        members = self._containers.workload_hosts()
        if chosen not in members:
            raise RefusedPlacement(
                f"{chosen} is not a member the workload can run on. The "
                f"cluster's are {', '.join(members) or 'none'}."
            )
        return {"preferred_host": preferred} if preferred else {"pinned_host": pinned}

    def _move_images(self, found: delivery.Delivery) -> list[Path]:
        moved: list[Path] = []
        for image in found.images:
            target = self._inventory.artefact_path(f"files/{image.archive}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(found.root / "images" / image.archive, target)
            moved.append(target)
        return moved

    def _move_back(self, moved: list[Path], root: Path) -> None:
        for target in moved:
            shutil.move(target, root / "images" / target.name)

    def _archives_in_use(self) -> set[str]:
        document = self._inventory.raw()
        found: set[str] = set()
        for quadlet in quadlets.workloads(document):
            found |= _archives(self._containers.workload(quadlet.name) or {})
        return found

    # The site values of an installed workload

    def values(self, name: str) -> WorkloadValues:
        described = self._described(name)
        current = self._containers.workload(name) or {}
        return WorkloadValues(name=name, values=_fields(described, current))

    def set_values(
        self,
        name: str,
        given: dict[str, Any],
        author: str,
        expected_head: str | None = None,
    ) -> Installed:
        described = self._described(name)
        current = self._containers.workload(name)
        if current is None:
            raise UnknownValues(f"No workload called {name} is installed here.")
        values, refused = delivery.site_values(described, given)
        if refused:
            raise RefusedValues(refused)
        commit = self._containers.write_workload(
            name,
            {**current, **values},
            author,
            expected_head,
            message=f"containers: set the site values of {name}",
        )
        return Installed(
            name=name,
            commit=commit.hash if commit else None,
            message=commit.message if commit else None,
        )

    def _described(self, name: str) -> list[delivery.Value]:
        path = f"{delivery.folder(name)}/{delivery.VALUES_FILE}"
        try:
            text = self._inventory.read_file(path)
        except (OSError, RefusedFile) as error:
            raise UnknownValues(
                f"{name} has no {path} in the inventory folder: it was declared "
                "by hand, and its values are edited on the Inventory page."
            ) from error
        findings: list[str] = []
        described = delivery.parse_values(yaml.safe_load(text), findings)
        if described is None or findings:
            raise UnknownValues(f"{path} cannot be read: {'; '.join(findings)}")
        return described

    # Staging area

    def _home(self, staged_id: str) -> Path:
        if not staged_id.isalnum():
            raise UnknownDelivery(f"{staged_id!r} is not a staged delivery.")
        return self._imports / staged_id

    def _tree(self, staged_id: str) -> Path:
        home = self._home(staged_id)
        if not (home / "tree").is_dir():
            raise UnknownDelivery(f"No delivery {staged_id} is staged.")
        return delivery.root_of(home / "tree")

    def _forget_stale(self) -> None:
        if not self._imports.is_dir():
            return
        limit = time.time() - _STALE_SECONDS
        for home in self._imports.iterdir():
            if home.is_dir() and home.stat().st_mtime < limit:
                shutil.rmtree(home, ignore_errors=True)


def _fields(
    described: list[delivery.Value], current: dict[str, Any]
) -> list[ValueField]:
    return [
        ValueField(
            key=value.key,
            description=value.description,
            format=value.format,
            example=value.example,
            default=value.default,
            has_default=value.has_default,
            minimum=value.minimum,
            maximum=value.maximum,
            current=current.get(value.key),
        )
        for value in described
    ]


def _archives(spec: dict[str, Any]) -> set[str]:
    """The archive file names a workload's images come from."""
    images = spec.get("images")
    found: set[str] = set()
    for image in images if isinstance(images, list) else []:
        if isinstance(image, dict) and isinstance(image.get("archive"), str):
            found.add(Path(image["archive"]).name)
    return found
