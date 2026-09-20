# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Trusting the certificate a node's collector answers with.

`deploy_otel_collector` puts one OpenTelemetry collector on each node, in
front of the exporters, and moves those exporters to the loopback: the node
then serves everything this service reads on a single TLS port. The role
signs that certificate on the node itself unless a site hands it one, so
there is no authority here to verify it against, and accepting whatever a
machine presents would hand the whole reading to anyone who can answer on
that address.

What is verified instead is the same thing a run verifies: this service
already reaches every machine of the inventory over SSH, with the key the
trust provisioned and the `known_hosts` the startup wrote. The certificate is
read over that connection and kept, and every later scrape is verified
against the copy. It is the model the SSH host keys themselves use, and the
authority is the one this service already has.

A site with a PKI sets `collector_ca_file` and none of this runs: every node's
certificate is verified against that CA, and nothing is fetched or kept.

Two consequences worth stating, because both are operational rather than
theoretical. A certificate that stops verifying is re-read over SSH and
re-pinned once, since the role replaces a certificate a month before it
expires and a site can install its own at any time; a stale copy would
otherwise take the page down with a message about an expiry nobody caused.
And a machine that cannot be reached over SSH has no certificate to pin, so
its panel says that rather than falling back to an unverified connection.
"""

from __future__ import annotations

import logging
import shlex
import ssl
import threading
from pathlib import Path
from typing import Protocol

from app.hosts.remote import RemoteRefused, RemoteRequest, RemoteRunner
from app.runs.service import RunPaths

logger = logging.getLogger(__name__)

# Where `deploy_otel_collector` installs what it serves. The directory is
# root's, which is why reading the certificate takes the same `sudo -n /bin/sh`
# rule the journal reading takes: the `ansible` account holds arbitrary root
# already, because Ansible needs it.
CERTIFICATE_PATH = "/etc/otelcol/cert.d/servercert.pem"

_PEM_HEADER = "-----BEGIN CERTIFICATE-----"


class TrustRefused(Exception):
    """No verified certificate for this node, and the message says why."""


class CertificateFetcher(Protocol):
    """Reads one node's collector certificate, or explains why it could not."""

    def fetch(self, address: str) -> str: ...


class SshCertificateFetcher:
    """The certificate, over the connection a run already makes.

    The command is built here and never by a caller, and the address is the one
    the inventory holds. `BatchMode` and the connect timeout come from the
    remote runner, which is the one place that opens an SSH connection for a
    reading.
    """

    def __init__(
        self,
        remote: RemoteRunner,
        keys: RunPaths,
        ansible_user: str,
        timeout: float = 15.0,
        connect_timeout: int = 5,
    ) -> None:
        self._remote = remote
        self._keys = keys
        self._ansible_user = ansible_user
        self._timeout = timeout
        # Shorter than a reading an operator waits on alone: this one happens
        # inside a page that is already fanning out to every machine.
        self._connect_timeout = connect_timeout

    def fetch(self, address: str) -> str:
        command = f"sudo -n /bin/sh -c {shlex.quote(f'cat {CERTIFICATE_PATH}')}"
        try:
            answer = self._remote.run(
                RemoteRequest(
                    address=address,
                    user=self._ansible_user,
                    command=command,
                    private_key_file=self._keys.private_key_file,
                    known_hosts_file=self._keys.known_hosts_file,
                    extra_key_files=self._keys.extra_key_files(),
                    timeout=self._timeout,
                    connect_timeout=self._connect_timeout,
                )
            )
        except RemoteRefused as error:
            raise TrustRefused(
                f"its certificate could not be read over SSH: {error}"
            ) from error
        if _PEM_HEADER not in answer:
            raise TrustRefused(
                "the machine answered something that is not a certificate"
            )
        return answer


class CollectorTrust:
    """One verified TLS context per machine, and where it came from.

    Contexts are kept because building one parses a certificate and every panel
    of a page asks for the same machine. The lock is the fan out's: the pages
    read every machine of the inventory in parallel.
    """

    def __init__(
        self,
        store_dir: Path,
        ca_file: Path | None = None,
        fetcher: CertificateFetcher | None = None,
    ) -> None:
        self._store_dir = store_dir
        self._ca_file = ca_file
        self._fetcher = fetcher
        self._contexts: dict[str, ssl.SSLContext] = {}
        self._lock = threading.Lock()

    def pinned_path(self, address: str) -> Path:
        """Where this machine's certificate is kept.

        The address names the file, and a name that is not one is refused
        rather than sanitised: the addresses come from the inventory, and
        anything with a separator in it is a bug upstream of here.
        """
        if not address or "/" in address or address in {".", ".."}:
            raise TrustRefused(f"{address!r} is not an address")
        return self._store_dir / f"{address}.pem"

    def context_for(self, address: str) -> ssl.SSLContext:
        """The context that verifies this machine, pinning its certificate if needed."""
        with self._lock:
            kept = self._contexts.get(address)
        if kept is not None:
            return kept
        context = self._build(address)
        with self._lock:
            self._contexts[address] = context
        return context

    def renew(self, address: str) -> ssl.SSLContext:
        """Read this machine's certificate again, when the kept one stops verifying.

        The role replaces a certificate before it expires and a site can
        install one of its own, so a copy that stops verifying is first of all
        out of date. Re-reading it over SSH is safe for the same reason pinning
        it was: the SSH connection is the authority here.
        """
        if self._ca_file is not None:
            # Nothing was pinned, so there is nothing to renew: a certificate
            # that fails against the site CA is a certificate to look at.
            return self.context_for(address)
        with self._lock:
            self._contexts.pop(address, None)
        path = self.pinned_path(address)
        path.unlink(missing_ok=True)
        return self.context_for(address)

    def _build(self, address: str) -> ssl.SSLContext:
        if self._ca_file is not None:
            return self._verifying(self._ca_file)
        path = self.pinned_path(address)
        if not path.exists():
            self._pin(address, path)
        return self._verifying(path)

    def _pin(self, address: str, path: Path) -> None:
        if self._fetcher is None:
            raise TrustRefused(
                "no certificate is pinned for this machine and none can be read"
            )
        certificate = self._fetcher.fetch(address)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(certificate, encoding="utf-8")
        # The certificate is public material, the key it goes with never leaves
        # the node, and an operator comparing a fingerprint reads this file.
        path.chmod(0o644)
        logger.info("Pinned the collector certificate of %s", address)

    def _verifying(self, ca_file: Path) -> ssl.SSLContext:
        try:
            context = ssl.create_default_context(cafile=str(ca_file))
        except (ssl.SSLError, OSError) as error:
            raise TrustRefused(f"its certificate could not be read: {error}") from error
        # Both left at their default, said out loud because this is the whole
        # of what makes the reading a verified one.
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context
