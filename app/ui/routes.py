# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The browser facing pages.

The pages are thin. Everything they display comes from `/api/v1`, which is the
same surface an automation client uses, so a screen can never show something
the API cannot answer.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.types import Scope

from app import __version__
from app.core.security import current_session

_UI_DIR = Path(__file__).parent
_STATIC = _UI_DIR / "static"
templates = Jinja2Templates(directory=str(_UI_DIR / "templates"))


@lru_cache(maxsize=4)
def _read_stylesheet(path: Path, mtime: float) -> Markup:
    return Markup(path.read_text(encoding="utf-8"))


def stylesheet(name: str = "style.css") -> Markup:
    """A stylesheet of this service, for the head of a page.

    Carried in the document rather than linked. A linked stylesheet is a
    network round trip standing between the navigation and the first paint,
    and these assets are served `no-cache`, so every page waited on a
    conditional request and painted itself unstyled while it was in flight:
    serif text, browser blue links, no layout, on every hop. Twenty three
    kilobytes in a document that is only fetched on a navigation buys that
    away.

    Read per render so an edit shows up on the next reload, and cached on the
    file's timestamp so the ordinary case is one `stat`.
    """
    path = _STATIC.joinpath(name)
    return _read_stylesheet(path, path.stat().st_mtime)


templates.env.globals["stylesheet"] = stylesheet


class _RevalidatedStatics(StaticFiles):
    """Static assets a browser must ask about before reusing.

    `no-cache` means revalidate, and it costs one conditional request that
    almost always answers 304: the file is still transferred only when it
    changed. Without it a browser holds an old script for as long as it likes,
    which on a node upgraded in place means a page half from this version and
    half from the last one. That was diagnosed once as a bug in the new code,
    which is an hour nobody gets back.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def install(app: FastAPI) -> None:
    app.mount(
        "/static",
        _RevalidatedStatics(directory=str(_STATIC)),
        name="static",
    )

    def _page(request: Request, template: str, page: str):
        if current_session(request) is None:
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
            },
        )
