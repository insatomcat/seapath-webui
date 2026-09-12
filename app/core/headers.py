# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What every response tells the browser to enforce, and what it leaves alone.

An authenticated session here is root on every machine of the cluster, through
a console button and through an apply. So the headers below are worth their
weight: a content policy that gives an injected string nowhere to run, and the
four small ones that close framing, sniffing and referrer leaks.

One header is deliberately absent. `Strict-Transport-Security` would be a
promise this service cannot keep: the certificate a node generates at first
boot is self signed, and HSTS turns the browser's certificate warning from
something an operator can accept into a wall with no way through. A site that
has installed its own material can add the header at a reverse proxy, where the
promise is true. Until then, locking an operator out of the machine that is in
front of them buys nothing.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Swagger UI, whose page is a special case below.
_DOCS_PATH = "/api/v1/docs"

# Nothing here depends on the request, and nothing here is contentious.
_FIXED_HEADERS = {
    # The pages and the API both answer JSON and HTML from the same origin,
    # and a browser guessing which is which is how a stored string becomes a
    # document.
    "X-Content-Type-Options": "nosniff",
    # `frame-ancestors` below says the same thing to a browser that reads the
    # policy. This one is for the ones that do not.
    "X-Frame-Options": "DENY",
    # A URL here carries a machine name and sometimes a run identifier, and no
    # request this service makes has any use for the page it came from.
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _content_security_policy(request: Request, nonce: str) -> str:
    """The policy for one response, which is where the nonce comes from.

    A nonce rather than a hash because three of the inline scripts have to be
    inline: the palette and the two switches of the bar are resolved before the
    first paint, the controls are marked from that as the bar itself is drawn,
    and the listener that reports a script which never loaded has to be
    registered ahead of the scripts it watches. All three are in `base.html`,
    all three carry this nonce, and anything else that ends up in the document
    has nowhere to run.
    """
    # The console's terminal opens a WebSocket on this same origin. From CSP
    # level 3 `'self'` covers a ws: or wss: URL of the page's own origin, and
    # naming the origin as well keeps the terminal working on a browser that
    # reads level 2.
    scheme = "wss" if request.url.scheme == "https" else "ws"
    socket = f"{scheme}://{request.url.netloc}"
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            # The stylesheets are this node's own, which `'self'` covers.
            # `'unsafe-inline'` is for xterm, which builds a style element of
            # its own at run time: it cannot be given a nonce, and a nonce in
            # this directive would turn `'unsafe-inline'` off. Styles are not
            # where the risk is.
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self'",
            f"connect-src 'self' {socket}",
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


# Swagger UI fetches its script and its stylesheet from a CDN and boots from an
# inline script, so the policy above leaves the page blank. A node in a
# substation has no route to that CDN, and the page is a development tool: this
# keeps it working on a laptop, and it is the only path served this way. The
# API it documents answers under the policy above, like everything else.
_DOCS_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "img-src 'self' data: https://fastapi.tiangolo.com",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Sets the headers, and hands the page's nonce to whoever renders it.

    Outermost of the middlewares, so a response this service refuses to make
    carries the same headers as one it makes: an error page is a document too.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        nonce = secrets.token_urlsafe(16)
        # Read back by `app/ui/routes.py` for the inline scripts of the page it
        # is about to render. A response with no inline script ignores it.
        request.state.csp_nonce = nonce

        response = await call_next(request)

        for name, value in _FIXED_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                _DOCS_POLICY
                if request.url.path == _DOCS_PATH
                else _content_security_policy(request, nonce)
            ),
        )
        return response
