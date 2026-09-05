# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The inventory as the API and the forms see it."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterable, Callable, Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.hosts.reader import HostReader
from app.inventory import files as tree
from app.inventory import references
from app.inventory.artefacts import ArtefactStore
from app.inventory.discovery import Discovery, discover, seed_inventory
from app.inventory.editor import UneditableInventory, edit
from app.inventory.fidelity import Divergence, unintended_changes
from app.inventory.model import Inventory, NodeConfig
from app.inventory.parser import InvalidInventory, parse
from app.inventory.renderer import render
from app.inventory.repository import INVENTORY_FILENAME, Commit, InventoryRepository
from app.inventory.resolve import resolve
from app.inventory.validation import Finding, Level, ValidationResult, validate

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


class RefusedFile(Exception):
    """A file could not be stored, and the message says which rule refused."""


class InventoryService:
    def __init__(
        self,
        repository: InventoryRepository,
        reader: HostReader,
        artefacts: ArtefactStore | None = None,
        # The path, or a callable resolving it at each access: which collection
        # the node runs can change while the service is up. See D23.
        collections_path: Path | Callable[[], Path] | None = None,
        max_file_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._artefacts = artefacts
        self._collections_path = collections_path
        self._max_file_bytes = max_file_bytes

    # The folder, and the two stores under it

    @property
    def folder(self) -> Path:
        """The versioned inventory folder, which a run copies whole."""
        return self._repository.path

    @property
    def artefacts_root(self) -> Path | None:
        return self._artefacts.root if self._artefacts is not None else None

    @property
    def max_file_bytes(self) -> int:
        return self._max_file_bytes

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
        validation = validate(inventory)
        validation.findings.extend(self._missing_files())
        return InventoryState(
            inventory=inventory,
            commit=self._repository.head(),
            validation=validation,
            seeded=True,
            adopted=not _is_ours(document),
            this_host=self.identify(document, inventory),
        )

    def _missing_files(self) -> list[Finding]:
        """The files the inventory names and nothing here holds.

        A warning rather than an error: uploading the quadlet after committing
        the variable that names it is a normal order of work, and refusing the
        commit would forbid it. The run is where it stops mattering, and the
        page says so before the operator gets there.
        """
        findings = []
        for reference in self.references():
            if reference.found:
                continue
            where = (
                f" Upload it as {reference.expected}."
                if reference.expected
                else " It points above the inventory folder, where no run can "
                "reach it."
            )
            findings.append(
                Finding(
                    level=Level.WARNING,
                    rule="referenced_file_present",
                    host=reference.host,
                    field=reference.variable,
                    message=(
                        f"{reference.variable} names {reference.value}, which "
                        f"is not in the inventory folder.{where}"
                    ),
                )
            )
        return findings

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

    def check_document(self, document: str) -> ValidationResult:
        """Everything that can be said about a whole file, committing nothing.

        Three opinions in order: it parses as YAML into something that looks
        like an inventory, it satisfies the rules, and Ansible itself accepts
        it. The last one is the reason this exists: `ansible-inventory --list`
        catches what a schema cannot, and finding out at the first task of a
        convergence is finding out late.
        """
        inventory = parse(document)
        result = validate(inventory)
        finding = _ansible_opinion(document)
        if finding is not None:
            result.findings.append(finding)
        return result

    def replace_document(
        self, document: str, author: str, message: str | None = None
    ) -> tuple[Commit | None, Inventory]:
        """Replace the whole file, the one write that legitimately rewrites all
        of it: the operator is replacing the desired state rather than editing
        one variable of it.
        """
        result = self.check_document(document)
        if not result.valid:
            raise ImportRefused(result.errors()[0].message, result)

        inventory = parse(document)
        if message is None:
            names = ", ".join(inventory.hosts)
            message = f"inventory: import a {inventory.mode.value} inventory of {names}"
            if inventory.guests:
                count = len(inventory.guests)
                message += f" and {count} guest{'s' if count > 1 else ''}"
        commit = self._repository.commit(
            content=document, message=message, author=author
        )
        return commit, inventory

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

    # The files beside the inventory
    #
    # An inventory is rarely alone. `upload_extra_files`, `iptables`,
    # `syslog_ng_client`, `cephadm` and the VM roles all take a path to a file
    # the control machine holds, and a folder that held `inventory.yaml` alone
    # would describe machines no playbook here could converge.

    def files(self) -> list[tree.StoredFile]:
        """The companion files, the inventory itself excluded.

        Excluded because it has a page of its own: it is the one file here that
        is parsed, validated and checked against Ansible before it is written,
        and offering it as an upload beside the quadlets would be a way around
        all of that.
        """
        return [
            entry
            for entry in self._repository.files()
            if entry.path != INVENTORY_FILENAME
        ]

    def read_file(self, path: str) -> bytes:
        return self._repository.read_file(path)

    def file_path(self, path: str) -> Path:
        return self._repository.file_path(path)

    def save_file(self, path: str, content: bytes, author: str) -> Commit | None:
        """Commit one companion file. Returns None when it did not change."""
        self._refuse_the_inventory(path)
        if len(content) > self._max_file_bytes:
            raise RefusedFile(
                f"{path} is {_megabytes(len(content))}, and the versioned "
                f"folder takes files up to {_megabytes(self._max_file_bytes)}. "
                "A file this size belongs in the artefacts, which a run mounts "
                "in the same place and git does not carry."
            )
        existed = self._repository.file_path(path).exists()
        verb = "update" if existed else "add"
        return self._repository.write_file(
            path=path,
            content=content,
            message=f"files: {verb} {path}",
            author=author,
        )

    def remove_file(self, path: str, author: str) -> Commit | None:
        self._refuse_the_inventory(path)
        return self._repository.delete_file(
            path=path, message=f"files: remove {path}", author=author
        )

    def _refuse_the_inventory(self, path: str) -> None:
        if tree.relative_path(path).as_posix() == INVENTORY_FILENAME:
            raise RefusedFile(
                "The inventory itself is written through the editor, where it "
                "is parsed, checked against the rules and put to Ansible "
                "before it is committed."
            )

    # The artefacts, which are the same files without the history

    def artefacts(self) -> list[tree.StoredFile]:
        return self._artefacts.files() if self._artefacts is not None else []

    def artefacts_free_bytes(self) -> int | None:
        return self._artefacts.free_bytes() if self._artefacts is not None else None

    def store_artefact(self, path: str, chunks: Iterable[bytes]) -> tree.StoredFile:
        return self._store().write(path, chunks)

    async def receive_artefact(
        self, path: str, chunks: AsyncIterable[bytes]
    ) -> tree.StoredFile:
        return await self._store().write_stream(path, chunks)

    def _store(self) -> ArtefactStore:
        if self._artefacts is None:
            raise RefusedFile("This node has nowhere to keep artefacts.")
        return self._artefacts

    def artefact_path(self, path: str) -> Path:
        return self._store().file_path(path)

    def remove_artefact(self, path: str) -> bool:
        return self._artefacts.delete(path) if self._artefacts is not None else False

    # What the inventory points at

    def references(self) -> list[references.Reference]:
        """Every file the inventory names, and whether a run would find it.

        Asked here rather than by the run, because the answer is only useful
        while an operator is still looking at the inventory. A missing file
        stops a convergence at a task that failed on every host at once.
        """
        document = self._repository.read()
        if not document.strip():
            return []
        return references.check(
            document,
            references.Roots(
                inventory=self.folder,
                artefacts=self.artefacts_root,
                collection=self._collection_root(),
            ),
        )

    def _collection_root(self) -> Path | None:
        root = self._collections_path
        if callable(root):
            root = root()
        if root is None:
            return None
        return root / "ansible_collections/seapath/ansible"

    # Writing

    def record_event(self, message: str, author: str) -> Commit:
        """An empty commit, for a change to the node that is not the inventory."""
        return self._repository.record(message, author)

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

    def proposed_document(self) -> str | None:
        """The standalone inventory this machine would write about itself.

        The seed, rendered on demand and committed by nobody: the page puts it
        in the editor and the operator decides. A machine re-cabled after
        installation, one whose discovery failed at first boot, and one whose
        file somebody emptied all want the same thing, and none of them wants a
        service that writes it for them.

        None when the machine cannot describe itself, which beats a file full
        of placeholders that look like decisions.
        """
        candidate = seed_inventory(self.discovery())
        return render(candidate) if candidate is not None else None

    def ensure_seed(self) -> bool:
        """Write the inventory a node produces about itself at first boot.

        Only ever writes when the repository has none. Discovery proposes, so
        the seed is a starting point the operator confirms in the editor,
        rather than a desired state anybody asked for.
        """
        self._repository.initialise()
        if self._repository.read().strip():
            return False
        candidate = seed_inventory(self.discovery())
        if candidate is None:
            logger.warning(
                "This machine could not describe itself, so no seed inventory "
                "was written. The editor starts on an empty file."
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


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _ansible_opinion(document: str) -> Finding | None:
    """What `ansible-inventory --list` thinks, or nothing if it cannot be asked.

    Run against a temporary copy, so a file that Ansible refuses never touches
    the repository. An image without ansible-core is not a reason to refuse a
    file, so a missing binary is silence rather than an error.
    """
    binary = shutil.which("ansible-inventory")
    if binary is None:
        logger.warning(
            "ansible-inventory is not on PATH, so an imported inventory is "
            "checked against this service's rules alone."
        )
        return None

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "inventory.yaml"
        path.write_text(document)
        completed = subprocess.run(
            [binary, "--list", "-i", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    # The return code lies. `ansible-inventory` exits 0 on a file it could not
    # read, having printed a warning and an empty inventory, so the exit status
    # alone would wave through exactly the files this check exists to catch.
    stderr = completed.stderr.strip()
    refused = any(marker in stderr for marker in ("Failed to parse", "Unable to parse"))
    if completed.returncode == 0 and not refused:
        return None
    reason = next(
        (line.strip() for line in stderr.splitlines() if line.strip()),
        "no reason given",
    )
    return Finding(
        level=Level.ERROR,
        rule="ansible_parses_it",
        message=f"Ansible cannot read this inventory: {reason}",
    )


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
