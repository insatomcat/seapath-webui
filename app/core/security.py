# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Request scoped security: who is calling, may they, and is this a forgery."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection

from app.core.auth import Role, User
from app.core.errors import AuthenticationRequired, PermissionDenied
from app.core.sessions import Session, SessionStore
from app.core.settings import Settings

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def get_settings_from(connection: HTTPConnection) -> Settings:
    return connection.app.state.settings


def get_sessions_from(connection: HTTPConnection) -> SessionStore:
    return connection.app.state.sessions


# An `HTTPConnection` rather than a `Request` because the console is a
# websocket, and a websocket handshake carries the same cookies. Everything
# these three need, the application state and the cookies, is on the base
# class both share.
def current_session(connection: HTTPConnection) -> Session | None:
    settings = get_settings_from(connection)
    cookie = connection.cookies.get(settings.session_cookie_name)
    return get_sessions_from(connection).resolve(cookie)


def require_user(request: Request) -> User:
    session = current_session(request)
    if session is None:
        raise AuthenticationRequired()
    return session.user


def require_role(required: Role) -> Callable[[Request], User]:
    """Dependency factory: refuse anyone below `required`."""

    def dependency(request: Request) -> User:
        user = require_user(request)
        if not user.role.can(required):
            raise PermissionDenied(
                f"This action requires the {required.value} role.",
                {"required": required.value, "role": user.role.value},
            )
        return user

    return dependency


class CsrfMiddleware(BaseHTTPMiddleware):
    """Reject unsafe requests that ride on a session cookie without a token.

    The check only applies when a session cookie is present, which is what
    makes the login request itself possible. A bearer authenticated call from
    M1 on carries no ambient authority and is not affected.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings: Settings = request.app.state.settings
        if request.method not in _SAFE_METHODS and request.cookies.get(
            settings.session_cookie_name
        ):
            session = current_session(request)
            supplied = request.headers.get(settings.csrf_header_name, "")
            if session is not None and not hmac.compare_digest(
                supplied, session.csrf_token
            ):
                return _csrf_failure()
        return await call_next(request)


def _csrf_failure() -> JSONResponse:
    # Built here rather than raised: an exception from a middleware bypasses
    # the application's exception handlers and would leave the envelope behind.
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "code": "csrf_failed",
                "message": (
                    "The request is missing a valid CSRF token. "
                    "Reload the page and retry."
                ),
                "detail": {},
            }
        },
    )


def set_session_cookies(
    response: Response, session: Session, sessions: SessionStore, settings: Settings
) -> None:
    max_age = settings.session_ttl_seconds
    # `secure` is unconditional: the service listens on HTTPS only, so a cookie
    # that would travel in clear is a cookie that would never be sent at all.
    response.set_cookie(
        settings.session_cookie_name,
        sessions.sign(session.id),
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    # Readable by the front end, which echoes it back in the header. It is not
    # a secret on its own: it is only useful together with the session cookie a
    # foreign origin cannot read.
    response.set_cookie(
        settings.csrf_cookie_name,
        session.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
