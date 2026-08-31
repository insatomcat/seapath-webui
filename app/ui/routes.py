# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The browser facing pages.

The pages are thin. Everything they display comes from `/api/v1`, which is the
same surface an automation client uses, so a screen can never show something
the API cannot answer.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Scope

from app import __version__
from app.core.security import current_session

_UI_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_UI_DIR / "templates"))


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
        _RevalidatedStatics(directory=str(_UI_DIR / "static")),
        name="static",
    )

    def _page(request: Request, template: str, page: str):
        if current_session(request) is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            template,
            {"version": __version__, "page": page, "nav": True},
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request):
        return _page(request, "node.html", "node")

    @app.get("/inventory", response_class=HTMLResponse, include_in_schema=False)
    def inventory(request: Request):
        return _page(request, "inventory.html", "inventory")

    @app.get("/system", response_class=HTMLResponse, include_in_schema=False)
    def system(request: Request):
        return _page(request, "system.html", "system")

    @app.get("/setup", include_in_schema=False)
    def setup(request: Request):
        # The page that used to do both jobs. Kept as a redirect because it is
        # in people's history and in the first deployment's notes.
        return RedirectResponse("/inventory", status_code=308)

    @app.get("/runs", response_class=HTMLResponse, include_in_schema=False)
    def runs(request: Request):
        return _page(request, "runs.html", "runs")

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login(request: Request):
        if current_session(request) is not None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"version": __version__, "nav": False}
        )
