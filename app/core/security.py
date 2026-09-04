# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Request scoped security: who is calling, may they, and is this a forgery."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection

from app.core.auth import Role, User
from app.core.errors import AuthenticationRequired, PermissionDenied
from app.core.sessions import Session, SessionStore
from app.core.settings import Settings

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Domain separation, so what a cookie name publishes shares nothing with any
# other use the session secret is put to.
_COOKIE_NAME_LABEL = b"seapath-webui/cookie-name"


@dataclass(frozen=True)
class CookieNames:
    """The names this node gives its two cookies.

    A cookie is scoped by host and path, and never by port: RFC 6265 section
    8.5 says so in as many words. An operator reaching two nodes through two
    ssh tunnels sees both as `localhost`, so one cookie jar serves both, and a
    fixed name means signing in to the second one overwrites the first one's
    cookie and signs it out. The suffix gives every node its own name in that
    shared jar. It comes from the session secret, which is persisted, so it
    survives a restart, and which is generated per machine, so no two nodes
    collide.
    """

    session: str
    csrf: str


def derive_cookie_names(settings: Settings, secret: bytes) -> CookieNames:
    suffix = hmac.new(secret, _COOKIE_NAME_LABEL, sha256).hexdigest()[:8]
    return CookieNames(
        session=f"{settings.session_cookie_name}_{suffix}",
        csrf=f"{settings.csrf_cookie_name}_{suffix}",
    )


def get_settings_from(connection: HTTPConnection) -> Settings:
    return connection.app.state.settings


def get_sessions_from(connection: HTTPConnection) -> SessionStore:
    return connection.app.state.sessions


def get_cookie_names_from(connection: HTTPConnection) -> CookieNames:
    return connection.app.state.cookie_names


# An `HTTPConnection` rather than a `Request` because the console is a
# websocket, and a websocket handshake carries the same cookies. Everything
# these three need, the application state and the cookies, is on the base
# class both share.
def current_session(connection: HTTPConnection) -> Session | None:
    cookie = connection.cookies.get(get_cookie_names_from(connection).session)
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
            get_cookie_names_from(request).session
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
    response: Response,
    session: Session,
    sessions: SessionStore,
    settings: Settings,
    names: CookieNames,
) -> None:
    max_age = settings.session_ttl_seconds
    # `secure` is unconditional: the service listens on HTTPS only, so a cookie
    # that would travel in clear is a cookie that would never be sent at all.
    response.set_cookie(
        names.session,
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
        names.csrf,
        session.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response, names: CookieNames) -> None:
    response.delete_cookie(names.session, path="/")
    response.delete_cookie(names.csrf, path="/")
