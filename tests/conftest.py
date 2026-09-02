# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures.

Nothing here touches a SEAPATH machine, a cluster, libvirt or a container. The
application is built with the fakes in place of the two host adapters, which is
the whole reason those adapters exist.

The temporary tree below is a small model of the paths the quadlet mounts: the
`ansible` account's `.ssh` as the ISO leaves it, the host's public SSH host
keys, and `/etc/hostname`. Building the application against it means the tests
exercise the real first boot sequence rather than a shortcut around it.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.console.fake import FakeConsoleAdapter
from app.core.auth import Role
from app.core.settings import Settings
from app.hosts.fake import FakeHostReader
from app.main import create_app
from app.runs.fake import FakeRunAdapter
from tests.fakes import FakeAuthenticator, FakeRoleDirectory, write_fake_collection

# The service is HTTPS only and sets its cookies `Secure`, so a test client on
# http:// would silently drop every session cookie.
BASE_URL = "https://testserver"

# What the ISO bakes into the account at build time.
SITE_KEY = "ssh-rsa AAAAB3NzaC1yc2Esite ansible@control-machine"

# Where this interpreter's own ansible-core is, which is the one the
# requirements pin, and which an unactivated virtualenv keeps off PATH. Ansible
# is the only authority on what an inventory means, so several tests ask it
# rather than asserting what we believe it would say.
ANSIBLE_INVENTORY = shutil.which(
    "ansible-inventory",
    path=os.pathsep.join(
        [str(Path(sys.executable).parent), os.environ.get("PATH", "")]
    ),
)


@pytest.fixture
def host_tree(tmp_path: Path) -> Path:
    """The parts of the host the quadlet mounts, in miniature."""
    root = tmp_path / "host"
    (root / "etc").mkdir(parents=True)
    (root / "etc/hostname").write_text("seapath-machine\n")
    (root / "etc/corosync").mkdir()

    ssh = root / "etc/ssh"
    ssh.mkdir()
    (ssh / "ssh_host_ed25519_key.pub").write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIhostkey root@seapath-machine\n"
    )

    account = root / "home/ansible/.ssh"
    account.mkdir(parents=True)
    (account / "authorized_keys").write_text(SITE_KEY + "\n")
    return root


@pytest.fixture
def collections_path(tmp_path: Path) -> Path:
    """The collection the image ships, as the run adapter would find it."""
    return write_fake_collection(tmp_path / "collections")


@pytest.fixture
def settings(tmp_path: Path, host_tree: Path, collections_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        host_root=host_tree,
        inventory_dir=tmp_path / "inventory",
        artefacts_dir=tmp_path / "artefacts",
        runs_dir=tmp_path / "runs",
        collections_path=collections_path,
        ssh_config_dir=host_tree / "etc/ssh",
        ansible_ssh_dir=host_tree / "home/ansible/.ssh",
        client_ssh_config_file=tmp_path / "root/.ssh/config",
        collection_version="test",
    )


@pytest.fixture
def reader() -> FakeHostReader:
    return FakeHostReader()


@pytest.fixture
def run_adapter() -> FakeRunAdapter:
    return FakeRunAdapter()


@pytest.fixture
def console_adapter() -> FakeConsoleAdapter:
    return FakeConsoleAdapter()


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator(
        {"admin": "secret", "viewer": "secret", "nobody": "secret"}
    )


@pytest.fixture
def directory() -> FakeRoleDirectory:
    return FakeRoleDirectory({"admin": Role.ADMIN, "viewer": Role.VIEWER})


@pytest.fixture
def client(
    settings: Settings,
    reader: FakeHostReader,
    authenticator: FakeAuthenticator,
    directory: FakeRoleDirectory,
    run_adapter: FakeRunAdapter,
    console_adapter: FakeConsoleAdapter,
) -> Iterator[TestClient]:
    application = create_app(
        settings=settings,
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=console_adapter,
    )
    with TestClient(application, base_url=BASE_URL) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    return _sign_in(client, "admin")


@pytest.fixture
def signed_in_viewer(client: TestClient) -> TestClient:
    return _sign_in(client, "viewer")


def _sign_in(client: TestClient, username: str) -> TestClient:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "secret"}
    )
    assert response.status_code == 200
    # The front end reads the token from the cookie; the test client does the
    # same rather than trusting the login response body.
    client.headers["X-CSRF-Token"] = client.cookies["seapath_csrf"]
    return client
