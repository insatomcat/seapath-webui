# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Application factory.

Everything the request handlers need is built here and hung on `app.state`,
which keeps the wiring visible in one place and makes the test suite a matter
of building an application with the fakes instead of the real adapters.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

from app import __version__
from app.api import v1
from app.cluster.exporters import (
    CachingMetricsClient,
    MetricsClient,
    ScrapeCache,
    UrllibMetricsClient,
)
from app.cluster.fake import FakeMetricsClient, FakeRbdClient
from app.cluster.pool import PoolReader
from app.cluster.rbd import CommandRbdClient, RbdClient
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
from app.core.headers import SecurityHeadersMiddleware
from app.core.logging import configure_logging
from app.core.security import CsrfMiddleware, derive_cookie_names
from app.core.sessions import SessionStore
from app.core.settings import Settings, get_settings
from app.core.tls import ensure_session_secret
from app.hosts.fake import FakeHostReader
from app.hosts.local import LocalHostReader, read_hostname
from app.hosts.reader import HostReader
from app.inventory.artefacts import ArtefactStore
from app.inventory.replication import ReplicationService, SshTransport, Transport
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService
from app.runs.adapter import AnsibleRunnerAdapter, RunAdapter
from app.runs.install import CollectionInstaller
from app.runs.service import RunPaths, RunService
from app.runs.store import RunStore
from app.services.cluster import ClusterService
from app.services.containers import ContainerService
from app.services.metadata import MetadataService
from app.services.node import NodeService
from app.services.realtime import RealtimeService
from app.services.registry import FakeTagSource, RegistryTagSource, TagSource
from app.services.storage import StorageService
from app.services.update import UpdateService
from app.services.vms import VmService
from app.trust.service import TrustService
from app.ui import routes as ui_routes

logger = logging.getLogger(__name__)

_DESCRIPTION = """
Node local management API for a SEAPATH machine and the cluster it belongs to.

The configuration of a host is edited here as an inventory and applied by a run
of the upstream SEAPATH playbooks. What this service writes itself is that
inventory, its own trust material, and the Pacemaker metadata a guest carries
on its disk image, which is D31 in the decisions.
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


def _site_keys(settings: Settings) -> Callable[[], tuple[Path, ...]]:
    """The keys this node offers a machine that is not itself.

    Resolved at each act rather than held, because an operator can upload or
    remove the site key while the service is up. The runs and the replication
    share it: they make the same connection, to the same account.
    """

    def resolve() -> tuple[Path, ...]:
        if settings.site_private_key_file.exists():
            return (settings.site_private_key_file,)
        return ()

    return resolve


def _replication_transport(settings: Settings, repository: Path) -> Transport:
    """Where the other machines of the inventory are.

    Every node of a SEAPATH deployment runs this image, so a peer holds its
    repository where this one holds its own.
    """
    if settings.use_fakes:
        from app.inventory.fake import FakePeerTransport

        return FakePeerTransport(settings.state_dir / "fake-peers", repository)
    return SshTransport(
        user=settings.ansible_user,
        private_key_file=settings.self_private_key_file,
        known_hosts_file=settings.known_hosts_file,
        remote_path=settings.inventory_dir,
        extra_key_files=_site_keys(settings),
    )


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


def _fake_tags() -> list[str]:
    """A tag list a registry could plausibly answer with, for the fake mode.

    The version answering, and the next patch above it, so the Deployment page
    of a laptop shows the update path rather than an empty one. `latest` is
    there because a real repository has it and it must be ignored: it names no
    version, and pinning a machine to a moving tag is what the seed already
    refuses to do.
    """
    major, _, rest = __version__.partition(".")
    minor, _, patch = rest.partition(".")
    ahead = f"{major}.{minor}.{int(patch) + 1}" if patch.isdigit() else __version__
    return ["latest", __version__, ahead]


def create_app(
    settings: Settings | None = None,
    reader: HostReader | None = None,
    authenticator: Authenticator | None = None,
    role_directory: RoleDirectory | None = None,
    session_secret: bytes | None = None,
    run_adapter: RunAdapter | None = None,
    console_adapter: ConsoleAdapter | None = None,
    metrics_client: MetricsClient | None = None,
    rbd_client: RbdClient | None = None,
    tag_source: TagSource | None = None,
    replication_transport: Transport | None = None,
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
        # Served below rather than by FastAPI, which writes the absolute path
        # of the specification into the page.
        docs_url=None,
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

    inventory_repository = InventoryRepository(settings.inventory_dir)
    app.state.inventory_service = InventoryService(
        inventory_repository,
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
    # What the inventory asks this service to be, next to what it is, and the
    # registry that says which versions exist. Choosing one is a commit here;
    # replacing the container is an Ansible run like any other.
    if tag_source is None:
        tag_source = (
            FakeTagSource(_fake_tags())
            if settings.use_fakes
            else RegistryTagSource(timeout=settings.registry_timeout)
        )
    app.state.update_service = UpdateService(
        app.state.inventory_service, tags=tag_source
    )

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
            extra_key_files=_site_keys(settings),
        ),
        hostname=hostname,
        collection_version=settings.collection_version,
        # Read through the adapter on each call, so the fake answers in the
        # tests and a machine reinstalled under a running service is read
        # again rather than remembered.
        node_distribution=lambda: reader.node_identity().seapath_distro,
    )

    # The inventory pushed to the other machines the inventory declares, over
    # the connection a run makes, when an operator asks. The repository is the
    # one the inventory service holds: a replication moves the branch that
    # every commit from the editor lands on. See D32.
    app.state.replication_service = ReplicationService(
        inventory_repository,
        replication_transport
        or _replication_transport(settings, inventory_repository.path),
    )

    # What this service is allowed to reach over the network, in one place.
    # Injected like every other adapter, so the suite reaches no network;
    # `use_fakes` covers the development switch, where nobody passes one in.
    #
    # One scrape of an exporter answers every panel of the page that asked for
    # it. The window is here because every service below shares one client, and
    # it wraps the injected client too: the suite runs against the fakes, and a
    # window nothing exercises is a window nobody knows the shape of. A reading
    # an operator asked for empties it first, which is what `fresh` does on
    # those endpoints. `scrape_window_seconds: 0` turns it off. See D45.
    app.state.scrapes = ScrapeCache(settings.scrape_window_seconds)
    exporters = CachingMetricsClient(
        metrics_client
        or (FakeMetricsClient() if settings.use_fakes else UrllibMetricsClient()),
        app.state.scrapes,
    )

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
    # The guests: their definition from the inventory, their files from the two
    # stores, and their Pacemaker resource from the cluster service above.
    # A guest's Pacemaker configuration lives as metadata on its RBD image, and
    # the quadlet has mounted /etc/ceph from the start for exactly this. Read
    # over Ceph's own client rather than through a machine asked to read it on
    # this service's behalf. See D31.
    if rbd_client is None:
        rbd_client = FakeRbdClient() if settings.use_fakes else CommandRbdClient()
    app.state.metadata_service = MetadataService(rbd_client)
    app.state.vm_service = VmService(
        inventory=app.state.inventory_service,
        cluster=app.state.cluster_service,
        # What libvirt says about the domains of each machine, which is the
        # only reading a guest on a standalone machine has. Same exporter fan
        # out as the cluster and storage views.
        client=exporters,
        libvirt_port=settings.libvirt_exporter_port,
        # The RBD groups, which tell a guest taken out of the cluster from one
        # never deployed: Pacemaker has no resource for either.
        rbd=rbd_client,
    )
    # The containers: the quadlets the inventory uploads, the systemd units
    # they become on each machine, and the Pacemaker resources holding some of
    # them. The unit half is read from the exposition the CPU pool already
    # fetches, on the same port, so this adds no scrape of its own. See D33.
    app.state.container_service = ContainerService(
        inventory=app.state.inventory_service,
        cluster=app.state.cluster_service,
        client=exporters,
        port=settings.node_exporter_port,
        distribution=lambda: reader.node_identity().seapath_distro,
    )

    install_error_handlers(app)
    app.add_middleware(CsrfMiddleware)
    # Added last, which puts it outermost: a request the CSRF check refuses is
    # still a document a browser renders, and it gets the same headers.
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(v1.router)
    ui_routes.install(app)

    @app.get("/api/v1/docs", include_in_schema=False)
    def docs() -> HTMLResponse:
        """Swagger UI, pointed at the specification by a relative URL.

        FastAPI's own docs route writes `openapi_url` into the page as it was
        given, and it is given as a path from the root. Behind a reverse proxy
        serving this service under a prefix, that page asks for
        `/api/v1/openapi.json` while the specification is at
        `/<prefix>/api/v1/openapi.json`, and Swagger UI renders "Failed to load
        API definition" over a 404.

        `openapi.json` resolves against this page's own directory instead,
        which is the rule the rest of this service already follows: every URL
        the front end builds is relative, so a proxy can mount the application
        under a prefix without the application being told what the prefix is.
        It holds here for the same reason it holds there, because this page
        sits beside the specification it asks for.
        """
        return get_swagger_ui_html(
            openapi_url="openapi.json",
            title=f"{app.title} - Swagger UI",
            # This service authenticates with a session cookie and a PAM
            # login, so there is no OAuth2 flow to hand a redirect back to.
            oauth2_redirect_url=None,
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        # Unauthenticated on purpose: it says the process answers, and nothing
        # about the machine.
        return {"status": "ok", "version": __version__}

    return app
