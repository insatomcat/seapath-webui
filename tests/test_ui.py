# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The browser facing pages, which are thin wrappers over the same API."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_the_root_sends_an_anonymous_visitor_to_the_login_page(
    client: TestClient,
) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "login"


def test_the_login_page_explains_where_the_roles_come_from(
    client: TestClient,
) -> None:
    body = client.get("/login").text

    assert "seapath-admin" in body


def test_a_signed_in_operator_gets_the_node_view(signed_in: TestClient) -> None:
    response = signed_in.get("/")

    assert response.status_code == 200
    assert "node.js" in response.text


def test_the_login_page_redirects_an_operator_who_is_already_signed_in(
    signed_in: TestClient,
) -> None:
    response = signed_in.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "./"


def test_health_answers_without_a_session_and_says_nothing_about_the_machine(
    client: TestClient,
) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "version"}
