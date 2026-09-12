# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The browser facing pages.

The pages are thin. Everything they display comes from `/api/v1`, which is the
same surface an automation client uses, so a screen can never show something
the API cannot answer.

The top bar is the one exception, and a deliberate one: its three strings are
the same on every page between two runs, so the document carries them instead
of fetching them. They are the values `/auth/me` and `/node` answer, read from
the same session and the same service, so the rule above still holds: nothing
is on a screen that the API could not have said.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.types import Scope

from app import __version__
from app.core.security import current_session
from app.core.sessions import Session
from app.hosts.models import NodeMode

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).parent
_STATIC = _UI_DIR / "static"
templates = Jinja2Templates(directory=str(_UI_DIR / "templates"))


def styles(name: str = "style.css") -> Markup:
    """A stylesheet of this service, for the head of a page.

    Linked, and stamped with the version that serves it exactly like a script,
    which is what lets a browser keep it: a stamped URL can only ever answer
    with one release's bytes, so `_StampedStatics` hands it over as immutable
    and is never asked about it again.

    The whole stylesheet used to be carried inside every document. The assets
    answered `no-cache`, a linked one therefore put a conditional request
    between the navigation and the first paint, and the page rendered unstyled
    while it was in flight: serif text, browser blue links, no layout, on every
    hop. Sixty seven kilobytes in every document bought that away. A cacheable
    link buys it back, and the sixty seven kilobytes with it: after the first
    page of a release, a navigation fetches the document and nothing else.
    """
    return Markup('<link rel="stylesheet" href="static/{}?v={}">').format(
        name, __version__
    )


def script(name: str) -> Markup:
    """A script tag of this service, stamped with the version that serves it.

    The version in the URL is what keeps a browser from running two releases
    at once. The assets are served `no-cache`, so an edit is always picked up,
    and that check answers a different question: it compares the copy a
    browser holds against the file the same service has on disk. A browser
    holding `deployment.js` from one version, asking another version for the
    page it belongs to, is told its copy is current, because for the service
    it took it from it was.

    The two halves then disagree about the elements they name, the page script
    dies on the first one that is missing, and what an operator sees is a page
    that renders and then does nothing, because a script that dies before its
    first statement has nothing to report. Stamping the URL makes the two
    halves two resources, so a browser has to fetch the one the page it is
    looking at was written against.
    """
    return Markup('<script src="static/{}?v={}"></script>').format(name, __version__)


templates.env.globals["styles"] = styles
templates.env.globals["script"] = script


def csp_nonce(request: Request) -> str:
    """The nonce the inline scripts of this page have to carry.

    Set per response by `SecurityHeadersMiddleware`, which is the only place
    that knows what the policy allows. Empty when a template is rendered with
    no middleware above it, which makes the attribute inert rather than wrong.
    """
    return getattr(request.state, "csp_nonce", "")


class _StampedStatics(StaticFiles):
    """Static assets, kept by the browser for as long as their URL names one release.

    `script()` and `styles()` stamp every URL they emit with the version that
    served the page, so a release's copy of a file is a resource of its own.
    That is what keeps a browser from pairing a script from one version with a
    page from another, and it also makes the file one a browser never has to
    ask about twice: a stamped request is answered immutable, and a navigation
    inside a release then fetches the document and nothing else. It used to
    revalidate the stylesheet and all seven scripts on every hop, which is
    seven round trips to paint a page whose assets had not moved.

    Everything else keeps `no-cache`, which means revalidate: an unstamped URL,
    typed or held from before this was in place, and a stamp from another
    version, are all answered by a service that may have been upgraded under
    them. Without it a browser holds an old script for as long as it likes,
    which on a node upgraded in place means a page half from this version and
    half from the last one. That was diagnosed once as a bug in the new code,
    which is an hour nobody gets back.
    """

    def __init__(self, *args, version: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # The whole query string, and not a substring of it: the only URLs this
        # promise is made about are the ones this service emits.
        self._stamp = f"v={version}".encode()

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        stamped = scope.get("query_string") == self._stamp
        if stamped and response.status_code < 400:
            # A year is the longest a cache is ever asked to keep anything, and
            # `immutable` is what spares even the conditional request. These
            # are the UI's own assets and carry nothing of the operator's, so a
            # cache in front of this service may hold them too.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def _topbar(request: Request, session: Session) -> dict[str, str]:
    """The three strings of the top bar, said in the document that carries it.

    They are the same three on every page between two runs: who is signed in
    changes when somebody signs in, the name and the mode when a machine is
    renamed or joins a cluster. Asking for them cost two requests standing in
    front of every page, and blinked the header through its placeholders on the
    way back to the values it had a second ago. The server holds all three, so
    it says them once, here.

    The identity is formed in this one place, which is why it is handed over
    rendered as well as in halves: the role stops being a parenthesis on the day
    two places build that string. The halves are there because a page that gates
    an action on the role compares it.

    Reading the machine is the same local reading `/node` does, and it is
    guarded: no screen of this service may fail to render because a file under
    /proc or /etc could not be read. The name resolved at start up answers for
    the header then, and the badge says what the Node page says of a machine
    whose /etc/corosync is not mounted.
    """
    who = {
        "username": session.username,
        "role": session.role.value,
        "identity": f"{session.username} ({session.role.value})",
    }
    try:
        node = request.app.state.node_service.summary()
    except Exception as error:
        logger.warning("The top bar could not read this node: %s", error)
        return {
            **who,
            "node_name": request.app.state.node_hostname,
            "node_mode": NodeMode.UNKNOWN.value,
        }
    return {**who, "node_name": node.hostname, "node_mode": node.mode.value}


def install(app: FastAPI) -> None:
    app.mount(
        "/static",
        _StampedStatics(directory=str(_STATIC), version=__version__),
        name="static",
    )

    def _page(request: Request, template: str, page: str):
        session = current_session(request)
        if session is None:
            # Relative, like every URL the pages themselves carry, so a
            # reverse proxy serving this service under a prefix keeps the
            # operator inside it. RFC 9110 allows it, and unlike
            # `request.url_for` it needs no knowledge of the prefix.
            return RedirectResponse("login", status_code=303)
        return templates.TemplateResponse(
            request,
            template,
            {
                "version": __version__,
                "page": page,
                "nav": True,
                "csrf_cookie": request.app.state.cookie_names.csrf,
                "csp_nonce": csp_nonce(request),
                **_topbar(request, session),
            },
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request):
        return _page(request, "node.html", "node")

    @app.get("/inventory", response_class=HTMLResponse, include_in_schema=False)
    def inventory(request: Request):
        return _page(request, "inventory.html", "inventory")

    @app.get("/deployment", response_class=HTMLResponse, include_in_schema=False)
    def deployment(request: Request):
        return _page(request, "deployment.html", "deployment")

    @app.get("/system", include_in_schema=False)
    def system(request: Request):
        # What the page was called until it was named after what it does.
        # Kept as a redirect for the same reason as `/setup` below: it is in
        # people's history and in the notes of the deployments already made.
        return RedirectResponse("deployment", status_code=308)

    @app.get("/setup", include_in_schema=False)
    def setup(request: Request):
        # The page that used to do both jobs. Kept as a redirect because it is
        # in people's history and in the first deployment's notes.
        return RedirectResponse("inventory", status_code=308)

    @app.get("/vms", response_class=HTMLResponse, include_in_schema=False)
    def vms(request: Request):
        return _page(request, "vms.html", "vms")

    @app.get("/containers", response_class=HTMLResponse, include_in_schema=False)
    def containers(request: Request):
        return _page(request, "containers.html", "containers")

    @app.get("/cluster", response_class=HTMLResponse, include_in_schema=False)
    def cluster(request: Request):
        return _page(request, "cluster.html", "cluster")

    @app.get("/realtime", response_class=HTMLResponse, include_in_schema=False)
    def realtime(request: Request):
        return _page(request, "realtime.html", "realtime")

    @app.get("/runs", response_class=HTMLResponse, include_in_schema=False)
    def runs(request: Request):
        return _page(request, "runs.html", "runs")

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login(request: Request):
        if current_session(request) is not None:
            return RedirectResponse("./", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "version": __version__,
                "nav": False,
                "csrf_cookie": request.app.state.cookie_names.csrf,
                "csp_nonce": csp_nonce(request),
            },
        )
