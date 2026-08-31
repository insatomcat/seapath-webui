# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Application factory.

Everything the request handlers need is built here and hung on `app.state`,
which keeps the wiring visible in one place and makes the test suite a matter
of building an application with the fakes instead of the real adapters.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import v1
from app.core.auth import (
    Authenticator,
    DevAuthenticator,
    DevRoleDirectory,
    PamAuthenticator,
    RoleDirectory,
    UnixGroupDirectory,
)
from app.core.bootstrap import run_startup_tasks
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.security import CsrfMiddleware
from app.core.sessions import SessionStore
from app.core.settings import Settings, get_settings
from app.core.tls import ensure_session_secret
from app.hosts.fake import FakeHostReader
from app.hosts.local import LocalHostReader, read_hostname
from app.hosts.reader import HostReader
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService
from app.runs.adapter import AnsibleRunnerAdapter, RunAdapter
from app.runs.service import RunPaths, RunService
from app.runs.store import RunStore
from app.services.node import NodeService
from app.trust.service import TrustService
from app.ui import routes as ui_routes

logger = logging.getLogger(__name__)

_DESCRIPTION = """
Node local management API for a SEAPATH machine.

This service does not configure machines. It edits the inventory and runs the
upstream SEAPATH playbooks. Anything that changes a host is an Ansible run.
"""


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "seapath-webui %s starting, collection %s",
        __version__,
        settings.collection_version,
    )
    # Trust, host keys, the seed inventory and the recovery of runs that were
    # going when the service stopped. Each is idempotent and none may prevent
    # the service from answering: an operator whose node cannot converge needs
    # the UI in order to find out why.
    run_startup_tasks(
        hostname=app.state.node_hostname,
        reader=app.state.reader,
        trust=app.state.trust_service,
        inventory=app.state.inventory_service,
        runs=app.state.run_service,
        settings=settings,
    )
    yield
    logger.info("seapath-webui stopping")


def _default_run_adapter(settings: Settings) -> RunAdapter:
    if settings.use_fakes:
        # The development switch has to cover the run adapter too. A service
        # serving invented readings that nonetheless launched a real
        # convergence would be the worst of both.
        from app.runs.fake import FakeRunAdapter

        return FakeRunAdapter()
    return AnsibleRunnerAdapter()


def create_app(
    settings: Settings | None = None,
    reader: HostReader | None = None,
    authenticator: Authenticator | None = None,
    role_directory: RoleDirectory | None = None,
    session_secret: bytes | None = None,
    run_adapter: RunAdapter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    if settings.use_fakes:
        logger.warning(
            "SEAPATH_WEBUI_USE_FAKES is set: this service is serving invented "
            "readings and accepting any password. It must not be used on a "
            "real machine."
        )

    if reader is None:
        reader = (
            FakeHostReader()
            if settings.use_fakes
            else LocalHostReader(root=settings.host_root)
        )
    if authenticator is None:
        authenticator = (
            DevAuthenticator()
            if settings.use_fakes
            else PamAuthenticator(settings.pam_service)
        )
    if role_directory is None:
        role_directory = (
            DevRoleDirectory() if settings.use_fakes else UnixGroupDirectory(settings)
        )

    app = FastAPI(
        title="seapath-webui",
        version=__version__,
        description=_DESCRIPTION,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
        lifespan=_lifespan,
    )

    app.state.settings = settings
    app.state.reader = reader
    app.state.authenticator = authenticator
    app.state.role_directory = role_directory
    app.state.sessions = SessionStore(
        secret=session_secret or ensure_session_secret(settings),
        ttl_seconds=settings.session_ttl_seconds,
    )
    app.state.node_service = NodeService(reader, settings.collection_version)

    # The node's own name, from the mounted /etc/hostname rather than from this
    # container's UTS namespace. It is the inventory host key, the name in the
    # trust relation, and what an operator recognises.
    hostname = read_hostname(settings.host_root)
    app.state.node_hostname = hostname

    app.state.trust_service = TrustService(
        ssh_dir=settings.ssh_dir,
        authorized_keys_file=settings.authorized_keys_file,
        ansible_user=settings.ansible_user,
    )
    app.state.inventory_service = InventoryService(
        InventoryRepository(settings.inventory_dir), reader
    )
    app.state.run_service = RunService(
        store=RunStore(settings.runs_dir),
        adapter=run_adapter or _default_run_adapter(settings),
        inventory=app.state.inventory_service,
        trust=app.state.trust_service,
        paths=RunPaths(
            collections_path=settings.collections_path,
            private_key_file=settings.self_private_key_file,
            known_hosts_file=settings.known_hosts_file,
            ssh_config_file=settings.client_ssh_config_file,
            # Looked up at each launch, so adding or removing the site key
            # takes effect on the next run rather than on the next restart.
            extra_key_files=lambda: (
                (settings.site_private_key_file,)
                if settings.site_private_key_file.exists()
                else ()
            ),
        ),
        hostname=hostname,
        collection_version=settings.collection_version,
    )

    install_error_handlers(app)
    app.add_middleware(CsrfMiddleware)
    app.include_router(v1.router)
    ui_routes.install(app)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        # Unauthenticated on purpose: it says the process answers, and nothing
        # about the machine.
        return {"status": "ok", "version": __version__}

    return app
