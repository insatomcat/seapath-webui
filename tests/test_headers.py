# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The headers a browser is told to enforce.

An authenticated session here is root on every machine of the cluster, so the
content policy is the last thing standing between an injected string and a
console. These tests pin what it says, and one of them pins what it must never
say.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

_PAGES = [
    "/",
    "/inventory",
    "/deployment",
    "/vms",
    "/containers",
    "/cluster",
    "/realtime",
    "/runs",
]


def _directives(response) -> dict[str, str]:
    policy = response.headers["content-security-policy"]
    return {
        part.split(" ", 1)[0]: part.split(" ", 1)[1] if " " in part else ""
        for part in (piece.strip() for piece in policy.split(";"))
    }


@pytest.mark.parametrize("path", _PAGES)
def test_a_page_allows_only_this_service_and_its_own_nonce(
    signed_in: TestClient, path: str
) -> None:
    response = signed_in.get(path)
    directives = _directives(response)

    assert directives["default-src"] == "'self'"
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["form-action"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["img-src"] == "'self'"
    assert re.fullmatch(r"'self' 'nonce-[\w-]+'", directives["script-src"])


@pytest.mark.parametrize("path", _PAGES)
def test_every_inline_script_of_a_page_carries_the_nonce_the_header_allows(
    signed_in: TestClient, path: str
) -> None:
    """The pairing the whole policy rests on.

    A page whose inline scripts do not carry the nonce is a page that renders
    and then does nothing: the theme flashes, and the banner that reports a
    dead script is itself dead. So this asserts the header and the document
    agree, on every page, rather than that each one is well formed on its own.
    """
    response = signed_in.get(path)
    allowed = re.search(
        r"'nonce-([\w-]+)'", response.headers["content-security-policy"]
    )
    assert allowed is not None

    inline = re.findall(r"<script(?![^>]*\ssrc=)([^>]*)>", response.text)

    assert inline, "base.html carries two inline scripts"
    for attributes in inline:
        assert f'nonce="{allowed.group(1)}"' in attributes


def test_the_nonce_is_new_on_every_response(signed_in: TestClient) -> None:
    first, second = (
        re.search(
            r"'nonce-([\w-]+)'", signed_in.get("/").headers["content-security-policy"]
        )
        for _ in range(2)
    )

    assert first is not None and second is not None
    assert first.group(1) != second.group(1)


def test_the_console_socket_is_allowed_on_this_origin_only(
    signed_in: TestClient,
) -> None:
    """The terminal opens a WebSocket, and level 2 browsers need it named."""
    directives = _directives(signed_in.get("/"))

    assert directives["connect-src"] == "'self' wss://testserver"


def test_the_style_of_the_document_and_of_xterm_stay_allowed(
    signed_in: TestClient,
) -> None:
    """`unsafe-inline` for styles is a decision, so it is asserted rather than
    left to be noticed when the terminal renders as a heap of characters."""
    directives = _directives(signed_in.get("/"))

    assert directives["style-src"] == "'self' 'unsafe-inline'"


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-opener-policy", "same-origin"),
    ],
)
def test_the_small_headers_are_on_a_page(
    signed_in: TestClient, header: str, value: str
) -> None:
    assert signed_in.get("/").headers[header] == value


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/login",
        "/api/v1/node",
        "/api/v1/openapi.json",
        "/healthz",
        "/static/api.js",
    ],
)
def test_nothing_this_service_serves_goes_out_bare(
    client: TestClient, path: str
) -> None:
    """Signed out, refused or static, the answer carries the policy.

    The middleware is the outermost one for this reason: the redirect to the
    login page, the 401 from the API and a static file are all documents a
    browser does something with.
    """
    response = client.get(path, follow_redirects=False)

    assert "content-security-policy" in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_static_asset_keeps_the_cache_check_it_had(client: TestClient) -> None:
    response = client.get("/static/api.js")

    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_refused_write_carries_the_headers(signed_in: TestClient) -> None:
    """A response the CSRF check refuses never reaches a route."""
    response = signed_in.post("/api/v1/runs", json={})

    assert response.status_code in (400, 403, 422)
    assert "content-security-policy" in response.headers


@pytest.mark.parametrize("path", ["/", "/login", "/api/v1/node", "/healthz"])
def test_no_node_ever_promises_https_to_a_browser(
    client: TestClient, path: str
) -> None:
    """HSTS is deliberately absent, and this is the test that keeps it absent.

    The certificate a node generates at first boot is self signed. HSTS turns
    the browser's warning about it into a wall with no way through, which locks
    an operator out of the machine in front of them. A site that installed its
    own material adds the header at a reverse proxy, where the promise holds.
    See D35.
    """
    response = client.get(path, follow_redirects=False)

    assert "strict-transport-security" not in response.headers


def test_the_docs_page_is_the_one_exception_and_it_is_named(
    signed_in: TestClient,
) -> None:
    """Swagger UI boots from an inline script and a CDN, so its page gets a
    policy of its own. Every other path keeps the strict one, and the API the
    page documents answers under it."""
    docs = _directives(signed_in.get("/api/v1/docs"))
    api = _directives(signed_in.get("/api/v1/openapi.json"))

    assert "https://cdn.jsdelivr.net" in docs["script-src"]
    assert "cdn.jsdelivr.net" not in api.get("script-src", "")
