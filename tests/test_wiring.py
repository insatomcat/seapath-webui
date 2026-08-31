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
