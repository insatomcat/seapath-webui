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
from functools import partial

from fastapi import FastAPI

from app import __version__
from app.api import v1
from app.cluster.exporters import MetricsClient
from app.cluster.fake import FakeMetricsClient
from app.cluster.pool import PoolReader
from app.console.adapter import ConsoleAdapter, SshConsoleAdapter
from app.console.service import ConsoleService
from app.core.auth import (
    Authenticator,
    DevAuthenticator,
    DevRoleDirectory,
    PamAuthenticator,
    Role,
    RoleDirectory,
    UnixGroupDirectory,
)
from app.core.bootstrap import collections_root, run_startup_tasks
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.security import CsrfMiddleware, derive_cookie_names
from app.core.sessions import SessionStore
from app.core.settings import Settings, get_settings
from app.core.tls import ensure_session_secret
from app.hosts.fake import FakeHostReader
from app.hosts.local import LocalHostReader, read_hostname
from app.hosts.reader import HostReader
from app.inventory.artefacts import ArtefactStore
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService
from app.runs.adapter import AnsibleRunnerAdapter, RunAdapter
from app.runs.install import CollectionInstaller
from app.runs.service import RunPaths, RunService
from app.runs.store import RunStore
from app.services.cluster import ClusterService
from app.services.node import NodeService
from app.services.realtime import RealtimeService
from app.services.storage import StorageService
from app.services.update import UpdateService
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


def _default_console_adapter(settings: Settings) -> ConsoleAdapter:
    if settings.use_fakes:
        from app.console.fake import FakeConsoleAdapter

        return FakeConsoleAdapter()
    return SshConsoleAdapter()


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
    console_adapter: ConsoleAdapter | None = None,
    metrics_client: MetricsClient | None = None,
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
            else LocalHostReader(
                root=settings.host_root, etc_root=settings.host_etc_root
            )
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
    secret = session_secret or ensure_session_secret(settings)
    app.state.sessions = SessionStore(
        secret=secret,
        ttl_seconds=settings.session_ttl_seconds,
    )
    # Named after this node rather than after the service, because an operator
    # holding an ssh tunnel to each of two clusters reaches both on localhost
    # and the browser keeps one cookie jar for the pair. See `CookieNames`.
    app.state.cookie_names = derive_cookie_names(settings, secret)
    app.state.node_service = NodeService(reader, settings.collection_version)
    app.state.console_service = ConsoleService(
        console_adapter or _default_console_adapter(settings),
        target=settings.console_target,
        user=settings.ansible_user,
        # The key and the record the self trust provisions at every start. A
        # console is the same connection a run makes, which is what keeps this
        # from being a second way into the machine.
        private_key_file=settings.self_private_key_file,
        known_hosts_file=settings.known_hosts_file,
        enabled=settings.console_enabled,
        required_role=Role(settings.console_min_role),
        max_sessions=settings.console_max_sessions,
        idle_timeout_seconds=settings.console_idle_timeout_seconds,
    )

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
    # The site's collection where one is installed, the image's otherwise.
    # Resolved at each access rather than once here, because an administrator
    # can install one while the service is up, and the answer has to be the
    # tree the next run will execute. See D23.
    resolve_collections = partial(collections_root, settings)
    app.state.collections_root = resolve_collections

    app.state.inventory_service = InventoryService(
        InventoryRepository(settings.inventory_dir),
        reader,
        # The two stores a run overlays: the versioned folder, and the large
        # files git has no business carrying.
        artefacts=ArtefactStore(settings.artefacts_dir),
        # Read to tell a file the site owes the run from one the collection
        # already ships, such as the syslog template a role defaults to.
        collections_path=resolve_collections,
        max_file_bytes=settings.max_inventory_file_bytes,
    )
    # One store, because the installer and the runs share its lock: a
    # collection is never swapped under a convergence that is already going.
    run_store = RunStore(settings.runs_dir)
    app.state.collection_installer = CollectionInstaller(
        site_dir=settings.site_collections_dir,
        image_dir=settings.collections_path,
        store=run_store,
    )
    # What the inventory asks this service to be, next to what it is. Read
    # only: replacing it is an Ansible run like any other.
    app.state.update_service = UpdateService(app.state.inventory_service)

    app.state.run_service = RunService(
        store=run_store,
        adapter=run_adapter or _default_run_adapter(settings),
        inventory=app.state.inventory_service,
        trust=app.state.trust_service,
        paths=RunPaths(
            # Looked up at each access, so a collection installed on the node
            # is what the next run executes, with no restart.
            collections_root=resolve_collections,
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
        # Read through the adapter on each call, so the fake answers in the
        # tests and a machine reinstalled under a running service is read
        # again rather than remembered.
        node_distribution=lambda: reader.node_identity().seapath_distro,
    )

    # What this service is allowed to reach over the network, in one place.
    # Injected like every other adapter, so the suite reaches no network;
    # `use_fakes` covers the development switch, where nobody passes one in.
    exporters = metrics_client or (FakeMetricsClient() if settings.use_fakes else None)

    # The real time page, which reads both halves of the same question: the
    # tuning this machine came out with, and the latency a cyclictest run
    # measured on it. The run half is the run service, filtered, so there is
    # one history and one lock rather than a second way to load a machine.
    app.state.realtime_service = RealtimeService(
        reader=reader,
        inventory=app.state.inventory_service,
        runs=app.state.run_service,
        hostname=hostname,
        # The one reading that leaves this machine: each node's exporter, for
        # the CPU pool seapath-alloc computes and this container cannot.
        pool=PoolReader(client=exporters, port=settings.node_exporter_port),
    )

    # The cluster and the storage views: Pacemaker and Corosync from each
    # node's ha_cluster_exporter, Ceph from whichever machine holds the active
    # manager. Both are one GET per machine and neither writes anything, which
    # is what lets them exist here at all.
    app.state.cluster_service = ClusterService(
        inventory=app.state.inventory_service,
        client=exporters,
        port=settings.ha_cluster_exporter_port,
    )
    app.state.storage_service = StorageService(
        inventory=app.state.inventory_service,
        client=exporters,
        port=settings.ceph_exporter_port,
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
