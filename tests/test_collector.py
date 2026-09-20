# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading a machine that serves its exporters behind one collector.

Two things are under test here and they fail differently. The rewrite, which
decides that a machine is read through its collector and turns the four URLs a
page asks it for into one request. And the trust, which is what makes that one
request a verified one: the certificate is read over the SSH connection a run
already makes, kept, and checked on every scrape afterwards.

The TLS half runs against a real server holding a real certificate, because
what is being asserted is that Python verifies it, and a fake socket would
assert that this file believes it does.
"""

from __future__ import annotations

import datetime
import ipaddress
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.cluster.exporters import (
    CachingMetricsClient,
    CollectorClient,
    ScrapeCache,
    UrllibMetricsClient,
)
from app.cluster.trust import (
    CERTIFICATE_PATH,
    CollectorTrust,
    SshCertificateFetcher,
    TrustRefused,
)
from app.hosts.remote import FakeRemoteRunner
from app.runs.service import RunPaths

EXPOSITION = "node_boot_time_seconds 1\n"


class RecordingClient:
    """Every URL it was handed, and one answer for all of them."""

    def __init__(self, answer: str = EXPOSITION) -> None:
        self.urls: list[str] = []
        self._answer = answer

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        self.urls.append(url)
        return self._answer, ""


class FakeFetcher:
    """The certificate a machine would have answered with over SSH."""

    def __init__(self, pem: str, refusal: str | None = None) -> None:
        self.pem = pem
        self.refusal = refusal
        self.addresses: list[str] = []

    def fetch(self, address: str) -> str:
        self.addresses.append(address)
        if self.refusal is not None:
            raise TrustRefused(self.refusal)
        return self.pem


def _self_signed(directory: Path, name: str) -> tuple[Path, Path, str]:
    """A certificate for 127.0.0.1, shaped like the one the role signs."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file = directory / f"{name}.crt"
    key_file = directory / f"{name}.key"
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    cert_file.write_text(pem)
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_file, key_file, pem


@contextmanager
def _serving(cert_file: Path, key_file: Path):
    """An HTTPS server on the loopback, answering one exposition."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
            body = EXPOSITION.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _paths(tmp_path: Path) -> RunPaths:
    return RunPaths(
        collections_root=tmp_path,
        private_key_file=tmp_path / "id_ed25519",
        known_hosts_file=tmp_path / "known_hosts",
        ssh_config_file=tmp_path / "config",
    )


def test_a_collected_machine_is_read_through_its_collector() -> None:
    inner = RecordingClient()
    client = CollectorClient(inner, collected=lambda _: True, port=9464)

    client.fetch("http://10.0.0.1:9100/metrics")

    assert inner.urls == ["https://10.0.0.1:9464/metrics"]


def test_a_machine_without_a_collector_is_read_as_before() -> None:
    inner = RecordingClient()
    client = CollectorClient(inner, collected=lambda _: False, port=9464)

    client.fetch("http://10.0.0.1:9100/metrics")

    assert inner.urls == ["http://10.0.0.1:9100/metrics"]


def test_a_cluster_is_migrated_one_machine_at_a_time() -> None:
    inner = RecordingClient()
    client = CollectorClient(
        inner, collected=lambda address: address == "10.0.0.1", port=9464
    )

    client.fetch("http://10.0.0.1:9100/metrics")
    client.fetch("http://10.0.0.2:9100/metrics")

    assert inner.urls == [
        "https://10.0.0.1:9464/metrics",
        "http://10.0.0.2:9100/metrics",
    ]


def test_the_four_ports_of_one_machine_become_one_request() -> None:
    """The rewrite sits in front of the scrape window, which is what makes it one."""
    inner = RecordingClient()
    client = CollectorClient(
        CachingMetricsClient(inner, ScrapeCache(window_seconds=60)),
        collected=lambda _: True,
        port=9464,
    )

    for port in (9100, 9177, 9664, 9283):
        client.fetch(f"http://10.0.0.1:{port}/metrics")

    assert inner.urls == ["https://10.0.0.1:9464/metrics"]


def test_the_certificate_is_read_over_the_connection_a_run_makes(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteRunner({CERTIFICATE_PATH: "-----BEGIN CERTIFICATE-----\nx\n"})
    fetcher = SshCertificateFetcher(
        remote=remote, keys=_paths(tmp_path), ansible_user="ansible"
    )

    fetcher.fetch("10.0.0.1")

    request = remote.requests[0]
    assert request.address == "10.0.0.1"
    assert request.user == "ansible"
    assert request.command == (
        "sudo -n /bin/sh -c 'cat /etc/otelcol/cert.d/servercert.pem'"
    )
    assert request.private_key_file == tmp_path / "id_ed25519"
    assert request.known_hosts_file == tmp_path / "known_hosts"


def test_a_machine_that_answers_something_else_is_refused(tmp_path: Path) -> None:
    remote = FakeRemoteRunner({CERTIFICATE_PATH: "sudo: a password is required\n"})
    fetcher = SshCertificateFetcher(
        remote=remote, keys=_paths(tmp_path), ansible_user="ansible"
    )

    with pytest.raises(TrustRefused, match="not a certificate"):
        fetcher.fetch("10.0.0.1")


def test_a_machine_that_cannot_be_reached_is_refused(tmp_path: Path) -> None:
    remote = FakeRemoteRunner()
    remote.refusal = "Host key verification failed"
    fetcher = SshCertificateFetcher(
        remote=remote, keys=_paths(tmp_path), ansible_user="ansible"
    )

    with pytest.raises(TrustRefused, match="over SSH"):
        fetcher.fetch("10.0.0.1")


def test_the_certificate_is_pinned_once_and_kept(tmp_path: Path) -> None:
    _, _, pem = _self_signed(tmp_path, "node")
    fetcher = FakeFetcher(pem)
    trust = CollectorTrust(store_dir=tmp_path / "pins", fetcher=fetcher)

    trust.context_for("10.0.0.1")
    CollectorTrust(store_dir=tmp_path / "pins", fetcher=fetcher).context_for("10.0.0.1")

    pinned = tmp_path / "pins" / "10.0.0.1.pem"
    assert pinned.read_text() == pem
    assert pinned.stat().st_mode & 0o777 == 0o644
    assert fetcher.addresses == ["10.0.0.1"]


def test_a_site_ca_pins_nothing(tmp_path: Path) -> None:
    ca_file, _, _ = _self_signed(tmp_path, "site")
    fetcher = FakeFetcher("unused")
    trust = CollectorTrust(
        store_dir=tmp_path / "pins", ca_file=ca_file, fetcher=fetcher
    )

    trust.context_for("10.0.0.1")

    assert fetcher.addresses == []
    assert not (tmp_path / "pins").exists()


def test_a_machine_with_no_certificate_to_read_is_a_result(tmp_path: Path) -> None:
    trust = CollectorTrust(
        store_dir=tmp_path / "pins",
        fetcher=FakeFetcher("", refusal="its certificate could not be read over SSH"),
    )
    client = UrllibMetricsClient(trust)

    text, error = client.fetch("https://10.0.0.1:9464/metrics")

    assert text is None
    assert "could not be read over SSH" in error


def test_an_address_that_is_not_one_is_refused(tmp_path: Path) -> None:
    trust = CollectorTrust(store_dir=tmp_path / "pins", fetcher=FakeFetcher("x"))

    with pytest.raises(TrustRefused):
        trust.pinned_path("../../etc/shadow")


def test_a_pinned_certificate_is_what_the_scrape_verifies(tmp_path: Path) -> None:
    cert_file, key_file, pem = _self_signed(tmp_path, "node")
    trust = CollectorTrust(store_dir=tmp_path / "pins", fetcher=FakeFetcher(pem))
    client = UrllibMetricsClient(trust)

    with _serving(cert_file, key_file) as port:
        text, error = client.fetch(f"https://127.0.0.1:{port}/metrics")

    assert text == EXPOSITION
    assert error == ""


def test_a_replaced_certificate_is_read_again_and_pinned(tmp_path: Path) -> None:
    """The role replaces one before it expires, and a stale copy takes the page down."""
    _, _, stale = _self_signed(tmp_path, "stale")
    cert_file, key_file, current = _self_signed(tmp_path, "current")
    pins = tmp_path / "pins"
    pins.mkdir()
    (pins / "127.0.0.1.pem").write_text(stale)
    fetcher = FakeFetcher(current)
    client = UrllibMetricsClient(CollectorTrust(store_dir=pins, fetcher=fetcher))

    with _serving(cert_file, key_file) as port:
        text, error = client.fetch(f"https://127.0.0.1:{port}/metrics")

    assert text == EXPOSITION
    assert error == ""
    assert fetcher.addresses == ["127.0.0.1"]
    assert (pins / "127.0.0.1.pem").read_text() == current


def test_a_certificate_that_still_does_not_verify_is_a_result(tmp_path: Path) -> None:
    _, _, other = _self_signed(tmp_path, "other")
    cert_file, key_file, _ = _self_signed(tmp_path, "served")
    fetcher = FakeFetcher(other)
    client = UrllibMetricsClient(
        CollectorTrust(store_dir=tmp_path / "pins", fetcher=fetcher)
    )

    with _serving(cert_file, key_file) as port:
        text, error = client.fetch(f"https://127.0.0.1:{port}/metrics")

    assert text is None
    assert "certificate" in error
