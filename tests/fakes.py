# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Fakes for what the tests must not depend on: PAM and the host."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.auth import Role
from app.hosts.reader import CommandResult
from app.runs import catalogue


def write_fake_collection(
    collections_path: Path,
    entries: list[str] | None = None,
    version: str = "2.0.0",
    contents: str = "---\n",
) -> Path:
    """Lay out a `seapath.ansible` collection with empty playbook files.

    The service checks that a catalogue entry exists in the collection its
    image ships, so the tests need somewhere for it to look. Passing `entries`
    leaves the others out, which is how the version skew case is exercised.

    `MANIFEST.json` and `FILES.json` are written the way `ansible-galaxy`
    writes them, because what a run records about the code it ran is read from
    those two files.
    """
    root = collections_path / "ansible_collections/seapath/ansible"
    playbooks = root / "playbooks"
    playbooks.mkdir(parents=True, exist_ok=True)
    (root / "MANIFEST.json").write_text(
        json.dumps({"collection_info": {"name": "ansible", "version": version}})
    )
    # ansible-galaxy records a checksum per file, so two collections holding
    # different code have different FILES.json. The fake has to have that
    # property or the fingerprint it feeds means nothing.
    digest = hashlib.sha256(contents.encode()).hexdigest()
    (root / "FILES.json").write_text(
        json.dumps(
            {
                "files": [
                    {"name": f"playbooks/{entry.id}.yaml", "chksum_sha256": digest}
                    for entry in catalogue.CATALOGUE
                ]
            }
        )
    )
    wanted = entries if entries is not None else [e.id for e in catalogue.CATALOGUE]
    for entry in catalogue.CATALOGUE:
        if entry.id in wanted:
            name = entry.playbook.rsplit(".", 1)[-1]
            (playbooks / f"{name}.yaml").write_text(contents)
    return collections_path


class FakeAuthenticator:
    def __init__(self, accounts: dict[str, str] | None = None) -> None:
        self.accounts = accounts or {"admin": "secret"}

    def authenticate(self, username: str, password: str) -> bool:
        return self.accounts.get(username) == password


class FakeRoleDirectory:
    def __init__(self, roles: dict[str, Role] | None = None) -> None:
        self.roles = roles or {"admin": Role.ADMIN}

    def role_for(self, username: str) -> Role | None:
        return self.roles.get(username)


class FakeCommandRunner:
    """Replays recorded output, keyed by the first two words of the command.

    An unregistered command fails the way a missing binary does, so a test that
    forgets to record one exercises the degraded path rather than crashing.
    """

    def __init__(self, responses: dict[str, CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout: float = 5.0) -> CommandResult:
        del timeout
        self.calls.append(list(argv))
        for key, result in self.responses.items():
            if " ".join(argv).startswith(key):
                return result
        return CommandResult(127, "", f"{argv[0]}: not found in this image")
