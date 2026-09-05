# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What the application factory wires by default.

The development switch replaces both the host adapter and the authentication,
which is convenient and dangerous in equal measure. These tests pin which one
a service gets, because the answer must never depend on a stray environment
variable that happens to be set on a machine.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.cluster.fake import FakeMetricsClient
from app.core.auth import (
    DevAuthenticator,
    DevRoleDirectory,
    PamAuthenticator,
    UnixGroupDirectory,
)
from app.core.settings import Settings
from app.hosts.fake import FakeHostReader
from app.hosts.local import LocalHostReader
from app.main import create_app
from app.runs.adapter import AnsibleRunnerAdapter
from app.runs.fake import FakeRunAdapter
from tests.fakes import write_fake_collection


def test_a_default_service_reads_the_real_machine_and_uses_pam(
    settings: Settings,
) -> None:
    application = create_app(settings=settings, session_secret=b"test-secret")

    assert isinstance(application.state.reader, LocalHostReader)
    assert isinstance(application.state.authenticator, PamAuthenticator)
    assert isinstance(application.state.role_directory, UnixGroupDirectory)
    assert isinstance(application.state.run_service._adapter, AnsibleRunnerAdapter)


def test_the_development_switch_replaces_both_adapters_and_the_password_check(
    settings: Settings,
) -> None:
    settings = settings.model_copy(update={"use_fakes": True})

    application = create_app(settings=settings, session_secret=b"test-secret")

    assert isinstance(application.state.reader, FakeHostReader)
    assert isinstance(application.state.authenticator, DevAuthenticator)
    assert isinstance(application.state.role_directory, DevRoleDirectory)
    # The run adapter too. A service serving invented readings that
    # nonetheless launched a real convergence would be the worst of both.
    assert isinstance(application.state.run_service._adapter, FakeRunAdapter)


def test_a_node_with_no_collection_says_so_in_the_journal(
    tmp_path: Path, caplog
) -> None:
    # The symptom is an Apply section with no buttons, which reads as a broken
    # page rather than as a missing directory. The answer belongs in the log
    # before anyone opens the page.
    from app.core.bootstrap import check_collection
    from app.core.settings import Settings

    settings = Settings(collections_path=tmp_path / "nowhere")

    with caplog.at_level(logging.WARNING):
        assert check_collection(settings) is False

    assert "No SEAPATH playbook was found" in caplog.text
    assert "SEAPATH_WEBUI_COLLECTIONS_PATH" in caplog.text


def test_a_node_with_the_collection_says_nothing(tmp_path: Path, caplog) -> None:
    from app.core.bootstrap import check_collection
    from app.core.settings import Settings

    settings = Settings(collections_path=write_fake_collection(tmp_path / "c"))

    with caplog.at_level(logging.WARNING):
        assert check_collection(settings) is True

    assert "collection" not in caplog.text


def test_the_site_collection_wins_over_the_one_the_image_ships(
    tmp_path: Path, caplog
) -> None:
    # The point of D23: a corrected playbook reaches a node as a file in the
    # state volume, with no image build and no registry to reach.
    from app.core.bootstrap import check_collection, collections_root
    from app.core.settings import Settings

    image = write_fake_collection(tmp_path / "image", contents="---\n# image\n")
    site = write_fake_collection(tmp_path / "site", contents="---\n# site\n")
    settings = Settings(collections_path=image, site_collections_dir=site)

    assert collections_root(settings) == site

    # And the boot that starts applying it says so, because that code arrived
    # outside an image release.
    with caplog.at_level(logging.INFO):
        assert check_collection(settings) is True
    assert str(site) in caplog.text


def test_an_empty_site_directory_does_not_shadow_the_image_collection(
    tmp_path: Path,
) -> None:
    # The quadlet creates the state volume, so an empty `collections/` in it is
    # the ordinary shape of a node nobody has updated. Choosing on the
    # directory existing would leave every one of those nodes with no playbook
    # at all.
    from app.core.bootstrap import collections_root
    from app.core.settings import Settings

    image = write_fake_collection(tmp_path / "image")
    site = tmp_path / "site"
    site.mkdir()
    (site / "ansible_collections").mkdir()

    settings = Settings(collections_path=image, site_collections_dir=site)

    assert collections_root(settings) == image


def test_the_run_service_and_the_inventory_read_the_same_collection(
    settings: Settings, tmp_path: Path
) -> None:
    # One root for the whole service. The inventory reads it to tell a file the
    # site owes a run from one the collection already ships, and a run executes
    # it: the two disagreeing would be a file resolved against a collection
    # nothing runs.
    site = write_fake_collection(tmp_path / "site", contents="---\n# site\n")
    settings = settings.model_copy(update={"site_collections_dir": site})

    application = create_app(settings=settings, session_secret=b"test-secret")

    assert application.state.collections_root() == site
    assert application.state.run_service._paths.collections_path == site
    assert application.state.inventory_service._collections_path() == site


def test_the_suite_reaches_no_exporter_over_the_network(
    settings, reader, authenticator, directory, run_adapter, console_adapter
) -> None:
    """No test may touch the network, and three readings now leave this machine.

    The CPU pool, the Pacemaker cluster and Ceph are each read from an exporter
    over HTTP, so a service built without a client would try to reach
    192.168.200.125 on every request that renders one. A suite that touches the
    network is one that fails on a train, and slowly.

    Asserted per service rather than on the factory, because the failure this
    catches is a new reader wired up with the real client while the ones beside
    it were injected properly.
    """
    from app.cluster.exporters import UrllibMetricsClient
    from app.main import create_app

    application = create_app(
        settings=settings,
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=console_adapter,
        metrics_client=FakeMetricsClient(),
    )

    clients = [
        application.state.realtime_service._pool._client,
        application.state.cluster_service._client,
        application.state.storage_service._client,
    ]
    assert not any(isinstance(client, UrllibMetricsClient) for client in clients)


def test_each_reading_asks_the_port_its_own_exporter_listens_on(
    settings, reader, authenticator, directory, run_adapter, console_adapter
) -> None:
    """One machine serves three exporters, and they say different things.

    A service pointed at the wrong port gets an answer it can parse into
    nothing, which renders as a cluster that is not there rather than as a
    mistake.
    """
    from app.main import create_app

    application = create_app(
        settings=settings,
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=console_adapter,
        metrics_client=FakeMetricsClient(),
    )

    assert application.state.realtime_service._pool._port == 9100
    assert application.state.cluster_service._port == 9664
    assert application.state.storage_service._port == 9283
