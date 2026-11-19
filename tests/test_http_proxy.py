# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The HTTP proxy a site gives this service, and the one request it serves.

`seapath_webui_http_proxy` puts `http_proxy`, `https_proxy` and `no_proxy` in
the container (D80). Two clients here speak HTTP, and the variable has to reach
one of them only: the registry is asked through the proxy, and the exporters
of the cluster are read directly, whatever `no_proxy` lists.

Both run against real sockets on the loopback, because what is asserted is
where `urllib` sends a request once the environment holds a proxy, and a
patched `urlopen` would skip exactly that decision.
"""

from __future__ import annotations

import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.cluster.exporters import UrllibMetricsClient
from app.services.registry import RegistryTagSource, RegistryUnreachable

EXPOSITION = "node_load1 0.5\n"


@contextmanager
def _serving(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return


@contextmanager
def _exporter() -> Iterator[int]:
    class Handler(_Quiet):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
            body = EXPOSITION.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with _serving(Handler) as port:
        yield port


@contextmanager
def _proxy(asked: list[str]) -> Iterator[int]:
    """A proxy that writes down every request and forwards none of them."""

    class Handler(_Quiet):
        def _refuse(self) -> None:
            asked.append(f"{self.command} {self.path}")
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _refuse  # noqa: N815 - the stdlib's spelling
        do_CONNECT = _refuse  # noqa: N815 - the stdlib's spelling

    with _serving(Handler) as port:
        yield port


@pytest.fixture
def proxied(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The environment the quadlet gives the container behind a proxy.

    No `no_proxy`, which is the case where everything would go through it.

    `urlopen` reads the environment once, when it builds the opener it keeps
    for the process. Dropping that opener on both sides makes this test see the
    proxy it set, and keeps the next test in the worker from inheriting one
    that is closed.
    """
    asked: list[str] = []
    with _proxy(asked) as port:
        for name in ("no_proxy", "NO_PROXY", "HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{port}")
        urllib.request.install_opener(None)
        try:
            yield asked
        finally:
            urllib.request.install_opener(None)


def test_an_exporter_is_read_directly_with_a_proxy_in_the_environment(
    proxied: list[str],
) -> None:
    with _exporter() as port:
        text, error = UrllibMetricsClient().fetch(f"http://127.0.0.1:{port}/metrics")

    assert (text, error) == (EXPOSITION, "")
    assert proxied == []


def test_the_registry_is_asked_through_the_proxy(proxied: list[str]) -> None:
    # The name resolves nowhere, which is the point: behind a proxy the
    # machine never resolves it, the proxy does.
    with pytest.raises(RegistryUnreachable, match="registry.invalid could not"):
        RegistryTagSource(timeout=2).tags("registry.invalid/site/seapath-webui")

    assert proxied == ["CONNECT registry.invalid:443"]
