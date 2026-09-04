# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What happens at every start, and what happened only at the first one.

None of this is guarded by an "is this the first boot" flag. Each step is
idempotent and is run every time, which is what makes them repairs rather than
one shot initialisations: re-provisioning the self trust is how the relation
survives an administration address change, and re-reading the host keys is how
`known_hosts` survives a host key rotation.

Nothing here is allowed to stop the service from starting. A node that cannot
provision its trust must still serve its UI, because the operator's next move
is to look at the UI and find out why.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.hosts.reader import HostReader
from app.inventory.service import InventoryService
from app.runs import catalogue
from app.runs.service import RunService
from app.trust import known_hosts
from app.trust.authorized_keys import MissingAccount
from app.trust.service import TrustService

logger = logging.getLogger(__name__)


def node_addresses(reader: HostReader) -> list[str]:
    """The addresses a connection from this node would arrive from.

    Used for the `from=` restriction on the installed key. Only the interface
    carrying the default route is taken: adding every address on the machine
    would turn a restriction into a formality.
    """
    network = reader.network()
    return [
        address.address
        for interface in network.interfaces
        if interface.name == network.default_route_interface
        for address in interface.addresses
        if address.family == "inet"
    ]


# The image symlinks these into /run/host/etc, which the quadlet mounts from the
# host. They are what PAM authenticates against and what the role of an account
# is read from.
_ACCOUNT_FILES = ("/etc/passwd", "/etc/group", "/etc/shadow")


def check_account_files() -> list[str]:
    """Report the account files that cannot be read, and say so loudly.

    A symlink that leads nowhere means the host's `/etc` was not mounted. The
    only other symptom is every password being refused, which looks exactly
    like a machine whose operators have all forgotten theirs, so it is worth a
    line in the journal naming the cause.

    A file that is not a symlink at all is a real one, which is the shape these
    paths have outside the image, and there is nothing to complain about.
    """
    missing = [
        path
        for path in _ACCOUNT_FILES
        if Path(path).is_symlink() and not Path(path).exists()
    ]
    if missing:
        logger.error(
            "%s cannot be read, so no account can be authenticated. The host's "
            "/etc must be mounted read only at /run/host/etc, which the quadlet "
            "does.",
            ", ".join(missing),
        )
    return missing


def collections_root(settings) -> Path:
    """The collection root this service runs playbooks from.

    Resolved once at start rather than at every run: a collection installed in
    the volume while a convergence is going must not change what that run is
    halfway through executing, since a run is identified by the code it ran.
    """
    return catalogue.select_root(
        settings.site_collections_dir, settings.collections_path
    )


def check_collection(settings) -> bool:
    """Say in the journal when there is no collection to run playbooks from.

    The image carries one and a source checkout does not, and the symptom is an
    Apply section with no buttons, which reads as a broken page rather than as
    a missing directory. Reported here so the answer is in the log before
    anyone opens the page.

    Which of the two roots was chosen is said here too. A node running the
    site's collection rather than the image's is running code that arrived
    outside an image release, and that belongs in the log of the boot it
    started applying with.
    """
    collections_path = collections_root(settings)
    if collections_path == settings.site_collections_dir:
        logger.info(
            "Running the collection installed under %s (%s) rather than the "
            "one the image ships.",
            collections_path,
            catalogue.identity(collections_path),
        )

    derived = [
        entry for entry in catalogue.resolve(collections_path) if not entry.reviewed
    ]
    if derived:
        # The collection moved past the catalogue, which is the ordinary state
        # of affairs. Named in the journal so "why is there a button for that"
        # has an answer that does not need the source.
        logger.info(
            "%d playbooks of the collection under %s have no reviewed entry "
            "and are offered as read from the collection: %s",
            len(derived),
            collections_path,
            ", ".join(entry.id for entry in derived),
        )

    missing = catalogue.missing_from(collections_path)
    if not missing:
        return True
    if len(missing) == len(catalogue.CATALOGUE):
        logger.warning(
            "No SEAPATH playbook was found under %s, so nothing can be applied "
            "from this node. The image installs the collection there; a "
            "service started from a source checkout has to be pointed at one "
            "with SEAPATH_WEBUI_COLLECTIONS_PATH.",
            collections_path,
        )
    else:
        logger.warning(
            "%d of %d catalogue entries are missing from the collection under "
            "%s, and are offered as unavailable.",
            len(missing),
            len(catalogue.CATALOGUE),
            collections_path,
        )
    return False


def run_startup_tasks(
    hostname: str,
    reader: HostReader,
    trust: TrustService,
    inventory: InventoryService,
    runs: RunService,
    settings,
) -> None:
    check_account_files()

    addresses = node_addresses(reader)

    try:
        _, changed = trust.ensure_self_trust(hostname, addresses)
        if changed:
            logger.info("The self trust relation was provisioned or repaired")
    except MissingAccount as error:
        # The service does not create the account. A machine where it is
        # missing was not installed from the SEAPATH ISO, and inventing a user
        # with privileges nobody reviewed is a second problem, not a recovery.
        logger.error(
            "Could not provision the self trust, so this node cannot converge "
            "anything: %s",
            error,
        )
    except OSError as error:
        logger.error("Could not provision the self trust: %s", error)

    try:
        known_hosts.ensure_local(
            settings.known_hosts_file,
            settings.ssh_config_dir,
            [hostname, *addresses, "127.0.0.1", "localhost"],
        )
    except OSError as error:
        logger.error("Could not record the local host keys: %s", error)

    try:
        inventory.ensure_seed()
    except Exception as error:  # pragma: no cover - defensive
        logger.error("Could not write the seed inventory: %s", error)

    check_collection(settings)

    # A record left saying `running` is a run whose process is gone, because
    # the machine rebooted or the container restarted. Leaving it would hold
    # the run lock forever and block every future convergence.
    for record in runs.reconcile():
        logger.warning(
            "Run %s was going when the service stopped, and is relaunchable",
            record.id,
        )
