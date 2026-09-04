# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Fakes for what the tests must not depend on: PAM and the host."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from app.core.auth import Role
from app.hosts.reader import CommandResult
from app.runs import catalogue


def write_fake_collection(
    collections_path: Path,
    entries: list[str] | None = None,
    version: str = "2.0.0",
    contents: str = "---\n",
    extras: dict[str, str] | None = None,
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
        # The namespace matters to more than this service: `ansible-galaxy`
        # reads every collection already under the path it installs into, and
        # a manifest missing it takes the whole install down.
        json.dumps(
            {
                "collection_info": {
                    "namespace": "seapath",
                    "name": "ansible",
                    "version": version,
                }
            }
        )
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
    # A playbook the catalogue has never heard of, which is the ordinary case
    # for a collection released after this service was written.
    for name, body in (extras or {}).items():
        (playbooks / f"{name}.yaml").write_text(body)
    return collections_path


def build_collection_archive(
    directory: Path,
    version: str = "2.0.1",
    namespace: str = "seapath",
    name: str = "ansible",
    contents: str = "---\n# the site's own\n",
    entries: list[str] | None = None,
) -> Path:
    """The tarball `ansible-galaxy collection build` writes, by hand.

    Written rather than built from a `galaxy.yml` so a test needs no source
    collection anywhere, and laid out exactly as `ansible-galaxy` expects it:
    the installer this exercises is the real one, and it reads both manifests.
    """
    source = directory / "source"
    playbooks = source / "playbooks"
    playbooks.mkdir(parents=True, exist_ok=True)
    (source / "README.md").write_text("A collection for the tests.\n")
    wanted = entries if entries is not None else [e.id for e in catalogue.CATALOGUE]
    for entry in catalogue.CATALOGUE:
        if entry.id in wanted:
            (playbooks / f"{entry.playbook.rsplit('.', 1)[-1]}.yaml").write_text(
                contents
            )

    listing = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_dir():
            listing.append({"name": relative, "ftype": "dir", "format": 1})
        else:
            listing.append(
                {
                    "name": relative,
                    "ftype": "file",
                    "chksum_type": "sha256",
                    "chksum_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "format": 1,
                }
            )
    (source / "FILES.json").write_text(json.dumps({"files": listing, "format": 1}))
    (source / "MANIFEST.json").write_text(
        json.dumps(
            {
                "collection_info": {
                    "namespace": namespace,
                    "name": name,
                    "version": version,
                    "dependencies": {},
                    "license": ["Apache-2.0"],
                    "authors": ["the tests"],
                    "readme": "README.md",
                },
                "file_manifest_file": {
                    "name": "FILES.json",
                    "ftype": "file",
                    "chksum_type": "sha256",
                    "chksum_sha256": hashlib.sha256(
                        (source / "FILES.json").read_bytes()
                    ).hexdigest(),
                    "format": 1,
                },
                "format": 1,
            }
        )
    )

    archive = directory / f"{namespace}-{name}-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(source.rglob("*")):
            tar.add(path, arcname=path.relative_to(source).as_posix())
    return archive


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
