# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.logging import audit_event
from app.core.security import (
    clear_session_cookies,
    current_session,
    require_user,
    set_session_cookies,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class Identity(BaseModel):
    username: str
    role: Role
    node: str
    mode: str
    csrf_token: str | None = None


@router.post("/login", response_model=Identity)
def login(payload: LoginRequest, request: Request, response: Response) -> Identity:
    settings = request.app.state.settings
    authenticator = request.app.state.authenticator
    directory = request.app.state.role_directory

    if not authenticator.authenticate(payload.username, payload.password):
        # The same answer whether the account does not exist or the password is
        # wrong, so the endpoint does not enumerate the machine's accounts.
        audit_event("login.failed", user=payload.username)
        raise ApiError("invalid_credentials", "Wrong user name or password.", 401)

    role = directory.role_for(payload.username)
    if role is None:
        audit_event("login.denied", user=payload.username, reason="no_role")
        raise ApiError(
            "no_role",
            (
                "This account is not a member of any SEAPATH group. "
                "Add it to seapath-admin, seapath-operator or seapath-viewer."
            ),
            403,
        )

    sessions = request.app.state.sessions
    session = sessions.create(User(username=payload.username, role=role))
    set_session_cookies(
        response, session, sessions, settings, request.app.state.cookie_names
    )
    audit_event("login.succeeded", user=payload.username, role=role.value)

    identity = _identity(request, session.user)
    identity.csrf_token = session.csrf_token
    return identity


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    session = current_session(request)
    if session is not None:
        request.app.state.sessions.delete(session.id)
        audit_event("logout", user=session.username)
    clear_session_cookies(response, request.app.state.cookie_names)
    response.status_code = 204
    return response


@router.get("/me", response_model=Identity)
def me(request: Request) -> Identity:
    user = require_user(request)
    return _identity(request, user)


def _identity(request: Request, user: User) -> Identity:
    identity = request.app.state.reader.node_identity()
    return Identity(
        username=user.username,
        role=user.role,
        node=identity.hostname,
        mode=identity.mode.value,
    )
