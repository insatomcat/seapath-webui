# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The console: the invocation, the wire, and every way it is refused.

No ssh is spawned anywhere here. What is asserted about the real client is the
command line it would be given, which is the same thing the run tests assert
about `ansible-runner`: it is the one place where a wrong option is a machine
reached differently from how this service says it reaches machines.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.console.adapter import ConsoleRequest, ssh_command
from app.console.fake import FakeConsoleAdapter
from app.console.service import ConsoleService, ConsoleUnavailable, clamp_window
from app.core.auth import Role
from app.core.settings import Settings
from app.main import create_app
from tests.conftest import BASE_URL, cookie_names

WS = "/api/v1/node/console/ws"


def connect(client: TestClient, query: str = "", **kwargs):
    """Open the console socket with the session the client holds.

    The cookie is handed over by hand because the test client builds a `ws://`
    URL, and `http.cookiejar` withholds a `Secure` cookie from anything that is
    not `https`. A browser sends it over `wss://`, so leaving it out would test
    a situation that does not exist rather than the authentication path.
    """
    headers = dict(kwargs.pop("headers", {}))
    name = cookie_names(client).session
    session = client.cookies.get(name)
    if session is not None:
        headers.setdefault("cookie", f"{name}={session}")
    return client.websocket_connect(WS + query, headers=headers, **kwargs)


def test_command_line_is_the_connection_a_run_makes(tmp_path: Path) -> None:
    command = ssh_command(
        ConsoleRequest(
            address="127.0.0.1",
            user="ansible",
            private_key_file=tmp_path / "id_ed25519_self",
            known_hosts_file=tmp_path / "known_hosts",
        )
    )

    assert command[0] == "ssh"
    assert "-tt" in command
    # The client configuration a run writes for rsync must not decide what a
    # console connects to.
    assert command[command.index("-F") + 1] == "/dev/null"
    assert f"UserKnownHostsFile={tmp_path / 'known_hosts'}" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "IdentitiesOnly=yes" in command
    # Without it a refused key ends in a password prompt on an account whose
    # password authentication the hardening role has disabled.
    assert "BatchMode=yes" in command
    # A console must not ride, or leave behind, the multiplexed connection a
    # run holds open.
    assert "ControlMaster=no" in command
    assert "ControlPath=none" in command
    assert command[command.index("-i") + 1] == str(tmp_path / "id_ed25519_self")
    assert command[command.index("-l") + 1] == "ansible"
    assert command[-1] == "127.0.0.1"


def test_window_size_from_a_browser_is_bounded() -> None:
    assert clamp_window(80, 24) == (80, 24)
    assert clamp_window(0, 0) == (20, 5)
    assert clamp_window(100000, 100000) == (500, 200)


def test_description_is_open_to_a_viewer(signed_in_viewer: TestClient) -> None:
    # Reading what the console is stays open to everyone, so the button can say
    # why it is not there. Opening one is a different question, below.
    info = signed_in_viewer.get("/api/v1/node/console").json()

    assert info["enabled"] is True
    assert info["user"] == "ansible"
    assert info["target"] == "127.0.0.1"
    assert info["required_role"] == "admin"
    assert info["active_sessions"] == 0


def test_a_viewer_is_refused_a_shell_by_default(signed_in_viewer: TestClient) -> None:
    # The console reaches the `ansible` account, which has passwordless sudo.
    # A viewer's whole surface is GET requests, so serving one here would raise
    # a read only account to root on a live hypervisor. `seapath-viewer` is a
    # supplementary group added to an ordinary Unix account: being in it says
    # nothing about holding sudo, and the console must not be what grants it.
    with connect(signed_in_viewer) as socket:
        assert socket.receive_json()["code"] == "permission_denied"
        assert socket.receive()["code"] == 4403


def test_an_operator_is_refused_a_shell_by_default(
    settings: Settings, reader, authenticator, directory, run_adapter
) -> None:
    # An operator adds exactly one thing to a viewer, cancelling a run. The
    # distance from there to root is the same distance.
    application = create_app(
        settings=settings,
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=FakeConsoleAdapter(),
    )
    with TestClient(application, base_url=BASE_URL) as client:
        client.post(
            "/api/v1/auth/login", json={"username": "operator", "password": "secret"}
        )
        with connect(client) as socket:
            assert socket.receive_json()["code"] == "permission_denied"
            assert socket.receive()["code"] == 4403


def test_a_session_carries_bytes_both_ways(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    with connect(signed_in, "?columns=120&lines=40") as socket:
        assert socket.receive_json() == {
            "type": "ready",
            "target": "ansible@127.0.0.1",
        }
        # The terminal stream is binary: a UTF-8 sequence split across two
        # reads must stay split rather than become a replacement character.
        assert b"fake console" in socket.receive_bytes()

        socket.send_json({"type": "input", "data": "hostname\r"})
        echoed = socket.receive_bytes()
        assert echoed == b"hostname\r\n"

    assert console_adapter.opened[0].columns == 120
    assert console_adapter.opened[0].lines == 40


def test_the_shell_exiting_ends_and_releases_the_session(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    with connect(signed_in) as socket:
        socket.receive_json()
        socket.receive_bytes()

        # End of transmission, which is how a shell is left.
        socket.send_json({"type": "input", "data": "\x04"})
        assert socket.receive_bytes() == b"\r\nlogout\r\n"

        # The node closes the socket itself, and does it after releasing the
        # terminal, so what follows is not a race with the cleanup.
        closed = socket.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 1000

    assert console_adapter.processes[0].closed is True
    assert signed_in.get("/api/v1/node/console").json()["active_sessions"] == 0


def test_a_resize_reaches_the_terminal(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    with connect(signed_in) as socket:
        socket.receive_json()
        socket.receive_bytes()
        socket.send_json({"type": "resize", "columns": 132, "lines": 43})
        # A round trip through the terminal, so the resize has been handled by
        # the time the echo comes back.
        socket.send_json({"type": "input", "data": "x"})
        assert socket.receive_bytes() == b"x"

    assert console_adapter.processes[0].resizes == [(132, 43)]


def test_a_console_is_counted_while_it_is_open(signed_in: TestClient) -> None:
    with connect(signed_in) as socket:
        socket.receive_json()
        assert signed_in.get("/api/v1/node/console").json()["active_sessions"] == 1


def test_an_unauthenticated_socket_is_refused(client: TestClient) -> None:
    with connect(client) as socket:
        assert socket.receive_json() == {
            "type": "error",
            "code": "authentication_required",
            "message": "This session has expired. Sign in again.",
        }
        assert socket.receive()["code"] == 4401


def test_a_socket_from_another_origin_is_refused(signed_in: TestClient) -> None:
    # A websocket handshake is not subject to the same origin policy and
    # carries the session cookie whatever page opened it, so this is the check
    # the CSRF middleware performs for every other unsafe request.
    with pytest.raises(WebSocketDisconnect) as refusal:
        # Refused during the handshake: the socket is never accepted at all.
        with connect(signed_in, headers={"origin": "https://elsewhere.example"}):
            pass

    assert refusal.value.code == 1008


def test_the_pages_own_origin_is_accepted(signed_in: TestClient) -> None:
    with connect(signed_in, headers={"origin": BASE_URL}) as socket:
        assert socket.receive_json()["type"] == "ready"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"console_enabled": False}, "console_disabled"),
        ({"console_max_sessions": 0}, "console_busy"),
    ],
)
def test_a_refusal_says_why(
    settings: Settings,
    reader,
    authenticator,
    directory,
    run_adapter,
    overrides: dict[str, object],
    code: str,
) -> None:
    application = create_app(
        settings=settings.model_copy(update=overrides),
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=FakeConsoleAdapter(),
    )
    with TestClient(application, base_url=BASE_URL) as client:
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "secret"}
        )
        with connect(client) as socket:
            message = socket.receive_json()
            assert message["type"] == "error"
            assert message["code"] == code
            assert socket.receive()["code"] == 4409


def test_a_site_can_lower_the_bar_and_gets_what_it_asked_for(
    settings: Settings, reader, authenticator, directory, run_adapter
) -> None:
    # The setting goes both ways, and a site that deliberately opens the
    # console to every account gets exactly that. The default refuses; this is
    # the site overriding it, which is the whole point of it being a setting.
    application = create_app(
        settings=settings.model_copy(update={"console_min_role": "viewer"}),
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=FakeConsoleAdapter(),
    )
    with TestClient(application, base_url=BASE_URL) as client:
        client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "secret"}
        )
        assert client.get("/api/v1/node/console").json()["required_role"] == "viewer"
        with connect(client) as socket:
            # The refusal is what a default install answers here. This one gets
            # a terminal, because the site asked for it.
            assert socket.receive() != {"type": "websocket.close", "code": 4403}


def test_a_node_without_its_own_key_says_so_before_opening_a_terminal(
    tmp_path: Path,
) -> None:
    # "Permission denied (publickey)" inside a terminal is a worse answer than
    # naming the trust that was never provisioned.
    service = ConsoleService(
        FakeConsoleAdapter(),
        target="127.0.0.1",
        user="ansible",
        private_key_file=tmp_path / "missing",
        known_hosts_file=tmp_path / "known_hosts",
        required_role=Role.VIEWER,
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        asyncio.run(service.open("admin", 80, 24))

    assert failure.value.code == "trust_missing"
