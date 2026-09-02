# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Entry point.

The TLS material has to exist before uvicorn opens its socket, and its
fingerprint has to reach the console before the operator is asked to trust it.
That ordering is why the service starts uvicorn itself instead of being handed
to a `uvicorn app.main:app` command line.
"""

import logging

import uvicorn

from app.core.logging import configure_logging
from app.core.settings import Settings, get_settings
from app.core.tls import ensure_tls_material, print_console_banner
from app.hosts.local import read_admin_address, read_hostname
from app.main import create_app

logger = logging.getLogger(__name__)


def _resolve_bind_address(settings: Settings) -> str:
    """The address uvicorn opens its socket on.

    A configured address is taken as given: the Ansible role writes the one the
    inventory holds, and an operator who names an address means it. `auto`, the
    default a fresh ISO boots with, resolves to the administration address of
    this machine so the UI answers on that network only.

    A machine with no default route has no administration network to resolve,
    and refusing to start there would leave it with no way in at all, so the
    wildcard is the fallback and the console says so.
    """
    if settings.bind_address != "auto":
        return settings.bind_address
    address = read_admin_address(settings.host_root)
    if address:
        return address
    logger.warning(
        "No interface carries the default route, so the administration address "
        "could not be resolved. Listening on every address of this machine. "
        "Set SEAPATH_WEBUI_BIND_ADDRESS to the administration address."
    )
    return "0.0.0.0"


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    # Resolved before the certificate is generated, because the address the
    # operator types is a name the certificate has to carry.
    settings = settings.model_copy(
        update={"bind_address": _resolve_bind_address(settings)}
    )

    # The node's name, not this container's: the certificate names the machine
    # an operator is about to trust.
    tls = ensure_tls_material(settings, hostname=read_hostname(settings.host_root))
    host = settings.bind_address if settings.bind_address != "0.0.0.0" else None
    print_console_banner(
        f"https://{host or '<node address>'}:{settings.port}/", tls.fingerprint
    )

    uvicorn.run(
        create_app(settings),
        host=settings.bind_address,
        port=settings.port,
        ssl_certfile=str(tls.cert_file),
        ssl_keyfile=str(tls.key_file),
        log_config=None,
        # Real time safety: the service shares the housekeeping CPUs with
        # everything else the node has to do, so it stays single process.
        workers=None,
    )


if __name__ == "__main__":
    main()
