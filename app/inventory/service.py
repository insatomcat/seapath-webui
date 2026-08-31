# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The inventory as the API and the forms see it."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.hosts.reader import HostReader
from app.inventory.discovery import Discovery, discover, seed_inventory
from app.inventory.fidelity import Divergence, divergences
from app.inventory.model import Inventory, NodeConfig
from app.inventory.parser import InvalidInventory, parse
from app.inventory.renderer import render
from app.inventory.repository import Commit, InventoryRepository
from app.inventory.validation import ValidationResult, validate

logger = logging.getLogger(__name__)

# Which form a variable belongs to, used to generate a commit message an
# operator can recognise a week later.
_SECTIONS = {
    "ansible_host": "network",
    "network_interface": "network",
    "subnet": "network",
    "gateway_addr": "network",
    "dns_servers": "network",
    "ptp_interface": "time",
    "ptp_domain_number": "time",
    "ntp_servers": "time",
    "admin_user": "accounts",
    "grub_password": "accounts",
    "isolcpus": "realtime",
    "role": "cluster",
}


class ReadOnlyInventory(Exception):
    """A write was refused because the file was written elsewhere.

    The service earns the right to rewrite an inventory by proving it can
    reproduce it. Until then the file is served and exported, and never
    written, because a form save that silently dropped thirty group variables
    would be a configuration change nobody asked for and nobody would see until
    a run behaved differently.
    """

    def __init__(self, message: str, divergences: list[Divergence]) -> None:
        super().__init__(message)
        self.divergences = divergences


class InventoryState(BaseModel):
    """What `GET /inventory` answers."""

    inventory: Inventory | None = None
    commit: str | None = None
    validation: ValidationResult = Field(default_factory=ValidationResult)
    seeded: bool = Field(
        default=False, description="Whether an inventory exists at all yet"
    )
    parse_error: str | None = None
    writable: bool = Field(
        default=True,
        description="Whether a form save can rewrite this file faithfully",
    )
    read_only_reason: str | None = None
    divergences: list[Divergence] = Field(
        default_factory=list,
        description="What a rewrite would change, when it would change anything",
    )


class InventoryService:
    def __init__(self, repository: InventoryRepository, reader: HostReader) -> None:
        self._repository = repository
        self._reader = reader

    # Reading

    def state(self) -> InventoryState:
        document = self._repository.read()
        if not document.strip():
            return InventoryState(seeded=False)
        try:
            inventory = parse(document)
        except InvalidInventory as error:
            # A hand edit that broke the file must not make the whole view
            # fail: the operator needs to see what is wrong in order to fix it.
            return InventoryState(
                seeded=True,
                commit=self._repository.head(),
                parse_error=str(error),
                writable=False,
                read_only_reason="The inventory could not be read.",
            )
        found = divergences(document)
        return InventoryState(
            inventory=inventory,
            commit=self._repository.head(),
            validation=validate(inventory),
            seeded=True,
            writable=not found,
            read_only_reason=read_only_reason(found),
            divergences=found,
        )

    def raw(self) -> str:
        return self._repository.read()

    def discovery(self) -> Discovery:
        return discover(self._reader)

    def history(self, limit: int = 50) -> list[Commit]:
        return self._repository.history(limit)

    def diff(self, from_ref: str | None, to_ref: str | None) -> str:
        return self._repository.diff(from_ref, to_ref)

    def preview(self, candidate: Inventory) -> str:
        self._refuse_if_read_only()
        return self._repository.diff_against(render(candidate))

    def export(self) -> bytes:
        return self._repository.export()

    # Writing

    def validate(self, candidate: Inventory) -> ValidationResult:
        return validate(candidate)

    def save(
        self,
        candidate: Inventory,
        author: str,
        expected_head: str | None = None,
        message: str | None = None,
    ) -> tuple[Commit | None, ValidationResult]:
        """Validate, then commit. An invalid inventory never reaches git."""
        self._refuse_if_read_only()
        result = validate(candidate)
        if not result.valid:
            return None, result

        if message is None:
            message = self._message_for(candidate)
        commit = self._repository.commit(
            content=render(candidate),
            message=message,
            author=author,
            expected_head=expected_head,
        )
        return commit, result

    def revert(self, commit: str, author: str) -> Commit:
        self._refuse_if_read_only()
        return self._repository.revert(commit, author)

    def _refuse_if_read_only(self) -> None:
        document = self._repository.read()
        if not document.strip():
            return
        found = divergences(document)
        if found:
            raise ReadOnlyInventory(read_only_reason(found) or "", found)

    def ensure_seed(self) -> bool:
        """Write the inventory a node produces about itself at first boot.

        Only ever writes when the repository has none. Discovery proposes, so
        the seed is a starting point the operator confirms through the form,
        not a desired state anybody asked for.
        """
        self._repository.initialise()
        if self._repository.read().strip():
            return False
        candidate = seed_inventory(self.discovery())
        if candidate is None:
            logger.warning(
                "This machine could not describe itself, so no seed inventory "
                "was written. The form starts empty."
            )
            return False
        self._repository.commit(
            content=render(candidate),
            message="discovery: seed this machine's own entry at first boot",
            author="seapath-webui",
        )
        logger.info("Wrote the seed inventory for %s", ",".join(candidate.hosts))
        return True

    # Commit messages

    def _message_for(self, candidate: Inventory) -> str:
        """A message generated from what actually changed.

        `git log` is the audit trail, and an audit trail of "update inventory"
        forty times over is not one.
        """
        state = self.state()
        previous = state.inventory
        if previous is None:
            return f"inventory: describe {', '.join(candidate.hosts)}"

        added = set(candidate.hosts) - set(previous.hosts)
        removed = set(previous.hosts) - set(candidate.hosts)
        if added:
            return f"cluster: add {', '.join(sorted(added))}"
        if removed:
            return f"cluster: remove {', '.join(sorted(removed))}"

        changes: list[tuple[str, str, str]] = []
        for name, node in candidate.hosts.items():
            changes.extend(
                (section, field, name)
                for section, field in _changed_fields(previous.hosts[name], node)
            )
        if not changes:
            return "inventory: no change"

        sections = sorted({section for section, _, _ in changes})
        fields = sorted({field for _, field, _ in changes})
        hosts = sorted({host for _, _, host in changes})
        return f"{', '.join(sections)}: set {', '.join(fields)} on {', '.join(hosts)}"


def read_only_reason(found: list[Divergence]) -> str | None:
    """Why the file cannot be written, in the words the operator reads."""
    if not found:
        return None
    if any(divergence.kind == "unsupported" for divergence in found):
        return found[0].message
    return (
        "This inventory was written somewhere else, and rewriting it here "
        "would change what Ansible resolves for the machines below. It is "
        "served read only until this service can reproduce it exactly. The "
        "file itself is untouched, and exporting the repository gives it back "
        "as it is."
    )


def _changed_fields(before: NodeConfig, after: NodeConfig) -> list[tuple[str, str]]:
    changed: list[tuple[str, str]] = []
    for field in type(before).model_fields:
        if field == "extra":
            continue
        if _value(before, field) != _value(after, field):
            changed.append((_SECTIONS.get(field, "inventory"), field))
    return changed


def _value(node: NodeConfig, field: str) -> Any:
    return getattr(node, field)
