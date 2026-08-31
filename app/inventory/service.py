# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The inventory as the API and the forms see it."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.hosts.reader import HostReader
from app.inventory.discovery import Discovery, discover, seed_inventory
from app.inventory.editor import UneditableInventory, edit
from app.inventory.fidelity import Divergence, unintended_changes
from app.inventory.model import Inventory, NodeConfig
from app.inventory.parser import InvalidInventory, parse
from app.inventory.renderer import render
from app.inventory.repository import Commit, InventoryRepository
from app.inventory.resolve import resolve
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


class ImportRefused(Exception):
    """An imported inventory was refused before it reached the repository."""

    def __init__(self, message: str, validation: ValidationResult) -> None:
        super().__init__(message)
        self.validation = validation


class RefusedWrite(Exception):
    """The change could not be made without changing something else.

    Raised when the edit cannot be expressed against this file, and when the
    file it produced would change more than the form asked for. Both are
    refusals rather than best efforts, because a save that quietly rewrote a
    neighbouring line is the failure this whole path exists to prevent.
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
    adopted: bool = Field(
        default=False,
        description="Whether this file was written somewhere other than here",
    )
    this_host: str | None = Field(
        default=None,
        description="Which entry in the inventory describes the machine serving it",
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
            )
        return InventoryState(
            inventory=inventory,
            commit=self._repository.head(),
            validation=validate(inventory),
            seeded=True,
            adopted=not _is_ours(document),
            this_host=self.identify(document, inventory),
        )

    def identify(self, document: str, inventory: Inventory) -> str | None:
        """Which entry describes the machine this service runs on.

        The host key is the obvious answer and it is frequently the wrong one.
        A site is free to key its inventory `node1`, `node2`, `node3` and carry
        the real names in `hostname`, which the first real inventory this
        service met does, and `network_buildhosts` honours: the machine is
        called `elabo1` and its entry is `node1`.

        So the key is tried, then `hostname`, then the administration address
        against the addresses this machine actually answers on. A node that
        recognises none of the entries says so rather than guessing, because
        editing the wrong machine's entry is worse than editing none.
        """
        hostname = self._reader.node_identity().hostname
        if hostname in inventory.hosts:
            return hostname

        # `hostname` is one of the variables the renderer owns, so the model
        # drops it. It is read from the file itself, resolved, which is also
        # how it reaches Ansible.
        resolved = resolve(document)
        for name in inventory.hosts:
            if str(resolved.get(name, {}).get("hostname") or "") == hostname:
                return name

        addresses = {
            address.address
            for interface in self._reader.network().interfaces
            for address in interface.addresses
        }
        for name, node in inventory.hosts.items():
            if node.ansible_host in addresses:
                return name
        return None

    def import_document(self, document: str, author: str) -> tuple[Commit | None, str]:
        """Replace the inventory with one the operator brought.

        The file arrives whole and is committed whole, which is the one write
        that legitimately rewrites everything: the operator is replacing the
        desired state, rather than editing it.
        """
        inventory = parse(document)
        result = validate(inventory)
        if not result.valid:
            raise ImportRefused(result.errors()[0].message, result)

        names = ", ".join(inventory.hosts)
        commit = self._repository.commit(
            content=document,
            message=f"inventory: import a {inventory.mode.value} inventory of {names}",
            author=author,
        )
        return commit, names

    def raw(self) -> str:
        return self._repository.read()

    def discovery(self) -> Discovery:
        return discover(self._reader)

    def history(self, limit: int = 50) -> list[Commit]:
        return self._repository.history(limit)

    def diff(self, from_ref: str | None, to_ref: str | None) -> str:
        return self._repository.diff(from_ref, to_ref)

    def preview(self, candidate: Inventory) -> str:
        return self._repository.diff_against(self.document_for(candidate))

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
        result = validate(candidate)
        if not result.valid:
            return None, result

        if message is None:
            message = self._message_for(candidate)
        commit = self._repository.commit(
            content=self.document_for(candidate),
            message=message,
            author=author,
            expected_head=expected_head,
        )
        return commit, result

    def revert(self, commit: str, author: str) -> Commit:
        return self._repository.revert(commit, author)

    def document_for(self, candidate: Inventory) -> str:
        """The file this candidate becomes, written the way the file allows.

        An inventory this service produced is rendered from the model, which
        keeps it in the canonical shape. An inventory written anywhere else is
        **edited**, one line per changed variable, so that its groups, its
        comments and the fifty variables this model knows nothing about are
        still there afterwards.
        """
        document = self._repository.read()
        if not document.strip():
            return render(candidate)
        if _is_ours(document):
            return render(candidate)

        current = parse(document)
        changes = field_changes(current, candidate)
        if not changes:
            return document
        try:
            edited = edit(document, changes)
        except UneditableInventory as error:
            raise RefusedWrite(str(error), []) from error

        unintended = unintended_changes(document, edited, changes)
        if unintended:
            raise RefusedWrite(
                "This change could not be made without changing other things "
                "in the file, so nothing was written.",
                unintended,
            )
        return edited

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


def _is_ours(document: str) -> bool:
    """Whether this file is one the renderer produces, byte for byte.

    The question decides how a save is written, and it is asked of the file
    rather than of a flag: a marker in a header would be a claim, and this is
    a proof.
    """
    try:
        return render(parse(document)) == document
    except (InvalidInventory, NotImplementedError):
        return False


# The model field names are the inventory variable names, which is what lets a
# form submission become a set of variables to write without a mapping table.
_EDITABLE = (
    "ansible_host",
    "network_interface",
    "subnet",
    "gateway_addr",
    "dns_servers",
    "ptp_interface",
    "ptp_domain_number",
    "ntp_servers",
    "admin_user",
    "grub_password",
    "isolcpus",
)


def field_changes(
    current: Inventory, candidate: Inventory
) -> dict[str, dict[str, Any]]:
    """What the form actually changed, per host and per variable."""
    added = set(candidate.hosts) - set(current.hosts)
    removed = set(current.hosts) - set(candidate.hosts)
    if added or removed:
        raise RefusedWrite(
            "Adding or removing a machine in an inventory written elsewhere "
            "is not something this service does yet. Edit the file and commit "
            "it, or form the cluster from here.",
            [],
        )

    changes: dict[str, dict[str, Any]] = {}
    for name, node in candidate.hosts.items():
        before = current.hosts[name]
        if node.role is not before.role:
            raise RefusedWrite(
                f"Changing the role of {name} means moving it between groups, "
                "which this service does not write yet.",
                [],
            )
        fields = {
            field: getattr(node, field)
            for field in _EDITABLE
            if getattr(node, field) != getattr(before, field)
        }
        if fields:
            changes[name] = fields
    return changes


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
