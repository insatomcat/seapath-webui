# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The trust this node has with itself, kept up to its addresses between starts.

A service that started before the administration address was up recorded no
address at all, and the first run into this very machine ended on `Host key
verification failed`. The refresh before every run is what makes the start's
timing irrelevant.
"""

from __future__ import annotations

from pathlib import Path

from app.core.bootstrap import refresh_local_trust, run_startup_tasks
from app.core.settings import Settings
from app.hosts.fake import FakeHostReader
from app.hosts.models import NetworkReading
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService
from app.runs.service import RunPaths, RunService
from app.runs.store import RunStore
from app.trust import authorized_keys, known_hosts
from app.trust.service import TrustService
from tests.fakes import write_fake_collection

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIhostkey"
SITE_KEY = "ssh-rsa AAAAsite ansible@control-machine"


class AddresslessReader(FakeHostReader):
    """The machine as it reads before its static address is configured."""

    def network(self) -> NetworkReading:
        reading = super().network()
        for interface in reading.interfaces:
            if interface.name == reading.default_route_interface:
                interface.addresses = []
        return reading


def trust_for(settings: Settings) -> TrustService:
    settings.ansible_ssh_dir.mkdir(parents=True, exist_ok=True)
    settings.authorized_keys_file.write_text(SITE_KEY + "\n")
    return TrustService(
        ssh_dir=settings.ssh_dir,
        authorized_keys_file=settings.authorized_keys_file,
    )


def self_trust_line(settings: Settings) -> str:
    return next(
        line
        for line in settings.authorized_keys_file.read_text().splitlines()
        if "from=" in line
    )


def test_an_address_that_came_up_after_the_start_is_trusted_at_the_next_run(
    settings: Settings, host_tree: Path
) -> None:
    trust = trust_for(settings)
    known_hosts.ensure_local(
        settings.known_hosts_file, settings.ssh_config_dir, ["seapath-machine"]
    )
    trust.ensure_self_trust("seapath-machine", [])
    assert "192.168.200.125" not in settings.known_hosts_file.read_text()

    refresh_local_trust("seapath-machine", FakeHostReader(), trust, settings)

    assert (
        f"192.168.200.125 {HOST_KEY}"
        in settings.known_hosts_file.read_text().splitlines()
    )
    assert "192.168.200.125" in self_trust_line(settings)
    # The ISO's site key is the site's, and stays exactly where it was.
    assert settings.authorized_keys_file.read_text().splitlines()[0] == SITE_KEY


def test_a_refresh_keeps_the_keys_ssh_learnt_by_itself(
    settings: Settings, host_tree: Path
) -> None:
    # A guest accepting its host key on first use has it written by ssh into
    # the live file. Rewriting the file before each run would make every run a
    # first connection again.
    trust = trust_for(settings)
    settings.known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
    settings.known_hosts_file.write_text(
        f"seapath-machine {HOST_KEY}\n10.132.159.193 ssh-ed25519 AAAAguest\n"
    )

    refresh_local_trust("seapath-machine", FakeHostReader(), trust, settings)

    assert "10.132.159.193 ssh-ed25519 AAAAguest" in (
        settings.known_hosts_file.read_text().splitlines()
    )


def test_a_refresh_with_nothing_moved_writes_nothing(
    settings: Settings, host_tree: Path
) -> None:
    names = ["seapath-machine", "192.168.200.125", "127.0.0.1", "localhost"]
    known_hosts.ensure_local(settings.known_hosts_file, settings.ssh_config_dir, names)

    assert not known_hosts.add_local(
        settings.known_hosts_file, settings.ssh_config_dir, names
    )


def test_the_start_without_an_address_still_trusts_the_loopback(
    settings: Settings, host_tree: Path, tmp_path: Path
) -> None:
    # What the start records when it comes too early: enough for the console,
    # and nothing the run into the administration address could use.
    trust = trust_for(settings)
    reader = AddresslessReader()
    inventory = InventoryService(InventoryRepository(tmp_path / "inventory"), reader)
    runs = RunService(
        store=RunStore(tmp_path / "runs"),
        adapter=None,
        inventory=inventory,
        trust=trust,
        paths=RunPaths(
            collections_root=write_fake_collection(tmp_path / "collections"),
            private_key_file=settings.self_private_key_file,
            known_hosts_file=settings.known_hosts_file,
            ssh_config_file=tmp_path / "root/.ssh/config",
        ),
        hostname="seapath-machine",
    )

    run_startup_tasks("seapath-machine", reader, trust, inventory, runs, settings)

    live = settings.known_hosts_file.read_text()
    assert f"127.0.0.1 {HOST_KEY}" in live
    assert "192.168.200.125" not in live
    assert "192.168.200.125" not in self_trust_line(settings)
    assert authorized_keys.installed(settings.authorized_keys_file)

    refresh_local_trust("seapath-machine", FakeHostReader(), trust, settings)

    assert f"192.168.200.125 {HOST_KEY}" in settings.known_hosts_file.read_text()
    assert "192.168.200.125" in self_trust_line(settings)
