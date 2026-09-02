# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""First boot TLS material.

A self signed certificate generated once, on the node, and never regenerated.
Its fingerprint is printed on the console because that fingerprint is the whole
security story of the first contact: it is what the operator compares in the
browser, and from M3 it is what a joining node pins before it will talk to
anyone. Regenerating it would silently invalidate every pin already taken, so
existing material is always kept, even if it is close to expiry.
"""

import datetime as dt
import ipaddress
import logging
import os
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core.settings import Settings

logger = logging.getLogger(__name__)

# Long lived on purpose: a substation node has no renewal machinery and no
# certificate authority above it at first boot. Rotation is an operator action,
# and it is a fingerprint change the peers must be told about.
_VALIDITY_DAYS = 3650

_SECRET_BYTES = 32


@dataclass(frozen=True)
class TlsMaterial:
    cert_file: Path
    key_file: Path
    fingerprint: str


def certificate_fingerprint(certificate: x509.Certificate) -> str:
    """SHA256 fingerprint in the form the join blob and the console use."""
    digest = certificate.fingerprint(hashes.SHA256())
    return "SHA256:" + ":".join(f"{byte:02x}" for byte in digest)


def _subject_alt_names(settings: Settings, hostname: str) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            names.append(x509.DNSName(value))

    add(hostname)
    add("localhost")
    add("127.0.0.1")
    add("::1")
    # A wildcard bind address names no host, so it is not a usable name, and
    # neither does `auto` before the entry point has resolved it.
    if settings.bind_address not in ("0.0.0.0", "::", "", "auto"):
        add(settings.bind_address)
    for extra in settings.tls_additional_sans.split(","):
        add(extra)
    return names


def _generate(settings: Settings, hostname: str) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SEAPATH"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "seapath-webui"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(_subject_alt_names(settings, hostname)),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    settings.pki_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.pki_dir, 0o700)
    _write_private(
        settings.tls_key_file,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    settings.tls_cert_file.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
    )
    os.chmod(settings.tls_cert_file, 0o644)


def _write_private(path: Path, payload: bytes) -> None:
    """Create the file with its final mode, never wider for a moment."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    os.chmod(path, 0o600)


def ensure_tls_material(settings: Settings, hostname: str | None = None) -> TlsMaterial:
    """Generate the certificate if this is the first boot, then report it."""
    hostname = hostname or socket.gethostname()
    if not (settings.tls_cert_file.exists() and settings.tls_key_file.exists()):
        _generate(settings, hostname)
        logger.info("Generated a self signed certificate for %s", hostname)

    certificate = x509.load_pem_x509_certificate(settings.tls_cert_file.read_bytes())
    return TlsMaterial(
        cert_file=settings.tls_cert_file,
        key_file=settings.tls_key_file,
        fingerprint=certificate_fingerprint(certificate),
    )


def ensure_session_secret(settings: Settings) -> bytes:
    """The key the session cookies are signed with.

    Persisted so that restarting the service, which `Restart=always` does after
    every crash, does not log every operator out.
    """
    path = settings.session_secret_file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        _write_private(path, secrets.token_hex(_SECRET_BYTES).encode("ascii"))
        logger.info("Generated the session secret")
    return path.read_bytes().strip()


def print_console_banner(url: str, fingerprint: str) -> None:
    """Tell the operator where to browse and what to verify.

    The fingerprint is not decoration. A self signed certificate is only worth
    anything if someone compares it out of band, and the console is that band.
    """
    line = "=" * 72
    logger.info(
        "\n%s\nSEAPATH management UI: %s\nCertificate fingerprint: %s\n%s",
        line,
        url,
        fingerprint,
        line,
    )
