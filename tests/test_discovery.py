# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Hardware discovery, and the seed inventory it produces."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import __version__
from app.hosts.fake import FakeHostReader
from app.hosts.models import NetworkReading
from app.inventory.discovery import discover, seed_inventory
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService


def test_the_administration_interface_is_proposed_from_the_default_route() -> None:
    discovery = discover(FakeHostReader())

    assert discovery.proposed is not None
    assert discovery.proposed.network_interface == "eno1"
    assert discovery.proposed.ansible_host == "192.168.200.125"
    assert discovery.proposed.subnet == 24
    assert discovery.proposed.gateway_addr == "192.168.200.1"


def test_the_ptp_interface_is_never_guessed() -> None:
    discovery = discover(FakeHostReader())

    # Which NIC receives sampled values is a cabling fact the machine cannot
    # observe, and a NIC that is up is not necessarily that one.
    assert discovery.proposed.ptp_interface is None
    assert [item.name for item in discovery.interfaces if item.ptp_capable]


def test_the_administration_account_is_read_from_the_machine() -> None:
    # `configure_seapath_distro` removes the account holding UID 1000 when
    # `admin_user` names another one, so the proposal reports the account the
    # installer made instead of assuming the name.
    assert discover(FakeHostReader()).proposed.admin_user == "admin"


def test_an_unreadable_administration_account_falls_back_and_says_so() -> None:
    reader = FakeHostReader()
    identity = reader.node_identity()
    identity.admin_account = None
    reader.node_identity = lambda: identity

    discovery = discover(reader)

    assert discovery.proposed.admin_user == "admin"
    assert any("administration account" in warning for warning in discovery.warnings)


def test_the_seed_names_an_administration_account(tmp_path: Path) -> None:
    # Without it the prerequisites run stops on its first task, on the
    # conditional of "Remove old admin user from sudoers file".
    service = InventoryService(
        InventoryRepository(tmp_path / "inventory"), FakeHostReader()
    )
    service.ensure_seed()

    inventory = service.state().inventory
    assert inventory.hosts["seapath-machine"].admin_user == "admin"
    assert "admin_user: admin" in service.raw()


def test_the_loopback_is_not_offered_as_a_candidate() -> None:
    assert "lo" not in {item.name for item in discover(FakeHostReader()).interfaces}


def test_the_isolated_set_is_observed_when_the_machine_already_has_one() -> None:
    discovery = discover(FakeHostReader())

    assert discovery.proposed.isolcpus == "4-7"
    assert discovery.isolated_now == [4, 5, 6, 7]


def test_the_isolated_set_is_proposed_from_the_topology_on_a_fresh_machine() -> None:
    reader = FakeHostReader()
    reading = reader.cpu()
    reader.cpu = lambda: reading.model_copy(  # type: ignore[method-assign]
        update={"isolated": [], "online": 16}
    )

    # A machine installed from the ISO has had no isolation applied yet, so the
    # proposal comes from the topology and the operator confirms it.
    assert discover(reader).proposed.isolcpus == "4-15"


def test_a_machine_with_too_few_cpus_gets_no_isolation_proposal() -> None:
    reader = FakeHostReader()
    reading = reader.cpu()
    reader.cpu = lambda: reading.model_copy(  # type: ignore[method-assign]
        update={"isolated": [], "online": 4}
    )

    assert discover(reader).proposed.isolcpus is None


def test_the_free_disk_is_offered_with_the_name_ceph_needs() -> None:
    disks = {disk.path: disk for disk in discover(FakeHostReader()).disks}

    assert disks["/dev/sdb"].claimed is False
    assert disks["/dev/sdb"].by_path.startswith("/dev/disk/by-path/")
    assert disks["/dev/sda"].claimed is True


def test_a_machine_that_cannot_describe_itself_proposes_nothing(
    tmp_path: Path,
) -> None:
    reader = FakeHostReader()
    reader.network = lambda: NetworkReading()  # type: ignore[method-assign]

    discovery = discover(reader)

    # Better than a file full of placeholders that look like decisions.
    assert discovery.proposed is None
    assert seed_inventory(discovery) is None
    assert any("default route" in warning for warning in discovery.warnings)


def test_the_seed_inventory_is_written_once_at_first_boot(tmp_path: Path) -> None:
    service = InventoryService(
        InventoryRepository(tmp_path / "inventory"), FakeHostReader()
    )

    assert service.ensure_seed() is True
    first = service.state()

    # Called at every start, and a second call must not touch a desired state
    # the operator has since edited.
    assert service.ensure_seed() is False
    assert service.state().commit == first.commit

    assert first.inventory is not None
    assert list(first.inventory.hosts) == ["seapath-machine"]
    assert first.inventory.hosts["seapath-machine"].ansible_host == "192.168.200.125"


def test_the_seed_is_a_starting_point_that_still_needs_the_operator(
    tmp_path: Path,
) -> None:
    service = InventoryService(
        InventoryRepository(tmp_path / "inventory"), FakeHostReader()
    )
    service.ensure_seed()

    state = service.state()

    # Discovery proposes. What it cannot know is exactly what the operator has
    # to fill in, and the warnings are what tells them where to look.
    assert state.validation.valid
    assert {finding.rule for finding in state.validation.findings} >= {
        "hypervisor_has_ptp",
        "ntp_fallback_exists",
    }


def test_a_seed_commit_is_authored_by_the_service_not_by_an_operator(
    tmp_path: Path,
) -> None:
    repository = InventoryRepository(tmp_path / "inventory")
    InventoryService(repository, FakeHostReader()).ensure_seed()

    commit = repository.history()[0]

    assert commit.author == "seapath-webui"
    assert "discovery" in commit.message


def test_the_proposal_is_offered_on_demand_and_written_by_nobody(
    tmp_path: Path,
) -> None:
    service = InventoryService(
        InventoryRepository(tmp_path / "inventory"), FakeHostReader()
    )

    document = service.proposed_document()

    assert document is not None
    assert "seapath-machine" in document
    assert "standalone_machine" in document
    # Asking for it is not writing it. The editor shows it and the operator
    # decides, which is the difference between a proposal and a decision.
    assert service.state().seeded is False


def test_a_machine_that_cannot_describe_itself_proposes_no_document(
    tmp_path: Path,
) -> None:
    reader = FakeHostReader()
    reader.network = lambda: NetworkReading()  # type: ignore[method-assign]
    service = InventoryService(InventoryRepository(tmp_path / "inventory"), reader)

    assert service.proposed_document() is None


def _seeded_image(reader: FakeHostReader) -> str | None:
    inventory = seed_inventory(discover(reader))
    assert inventory is not None
    return inventory.hosts[reader.hostname].extra.get("seapath_webui_image")


def test_the_seed_pins_the_version_answering_when_the_machine_boots_on_latest(
    tmp_path: Path,
) -> None:
    # What the ISO installs. Seeding `latest` as it stands would write a
    # variable that names no version, and the whole point of the variable is
    # that the inventory says which code a machine is meant to run. The version
    # answering is the one that tag resolved to, and it is published as a tag of
    # its own, so the pin names an image that exists.
    assert (
        _seeded_image(FakeHostReader())
        == f"docker.io/insatomcat/seapath-webui:{__version__}"
    )


@pytest.mark.parametrize(
    "reference",
    [
        "docker.io/insatomcat/seapath-webui:0.1.0",
        "registry.example.org:5000/seapath/webui:0.1.0",
        "docker.io/insatomcat/seapath-webui@sha256:" + "0" * 64,
    ],
)
def test_a_reference_somebody_decided_is_seeded_as_it_stands(reference: str) -> None:
    # An older version, a site registry with a port, a digest. Each one is a
    # decision, and the seed records the machine rather than correcting it.
    reader = FakeHostReader()
    reader.service_image = reference

    assert _seeded_image(reader) == reference


def test_a_machine_whose_unit_file_could_not_be_read_pins_nothing() -> None:
    # A variable naming a repository this service invented would be worse than
    # the silence: the System page says the inventory names no image, and that
    # is true.
    reader = FakeHostReader()
    reader.service_image = None

    assert _seeded_image(reader) is None


def test_the_seeded_pin_reaches_the_file(tmp_path: Path) -> None:
    # Through the renderer, because a variable the schema carries and the file
    # does not would pin nothing at all: Ansible reads the file.
    repository = InventoryRepository(tmp_path / "inventory")
    InventoryService(repository, FakeHostReader()).ensure_seed()

    assert (
        f"seapath_webui_image: docker.io/insatomcat/seapath-webui:{__version__}"
        in repository.read()
    )
