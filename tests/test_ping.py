# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Whether an address a new guest is given already answers.

The endpoint runs against the fake, so the suite sends nothing on the wire.
The real pinger is held to the two things that can be checked without a
network: what it counts as a reply, and the sentence it gives when the kernel
refuses the socket.
"""

from __future__ import annotations

import errno
import socket

import pytest
from fastapi.testclient import TestClient

from app.services import ping
from app.services.ping import FakePinger, IcmpPinger, InvalidAddress, PingState
from tests.conftest import sign_in


def test_an_address_that_answers_is_reported_taken(
    signed_in: TestClient, pinger: FakePinger
) -> None:
    response = signed_in.get("/api/v1/vms/ping", params={"address": "192.168.200.1/24"})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "answered"
    assert body["address"] == "192.168.200.1"
    # The prefix the form field carries is dropped before anything is sent.
    assert pinger.asked == ["192.168.200.1"]


def test_an_address_nothing_answers_is_reported_silent(signed_in: TestClient) -> None:
    body = signed_in.get(
        "/api/v1/vms/ping", params={"address": "192.168.200.42"}
    ).json()

    assert body["state"] == "silent"
    assert "unless it drops ICMP" in body["detail"]


@pytest.mark.parametrize(
    "address",
    [
        "not-an-address",
        "0.0.0.0",
        "127.0.0.1",
        "224.0.0.1",
        "10.0.0.0/24",
        "10.0.0.255/24",
    ],
)
def test_an_address_no_guest_can_hold_is_refused_before_anything_is_sent(
    signed_in: TestClient, pinger: FakePinger, address: str
) -> None:
    response = signed_in.get("/api/v1/vms/ping", params={"address": address})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_address"
    assert pinger.asked == []


def test_asking_is_the_role_that_declares_the_guest(
    client: TestClient, pinger: FakePinger
) -> None:
    sign_in(client, "operator")

    response = client.get("/api/v1/vms/ping", params={"address": "192.168.200.1"})

    assert response.status_code == 403
    assert pinger.asked == []


def test_a_point_to_point_prefix_has_no_broadcast_to_refuse() -> None:
    assert ping.target("10.0.0.0/31") == "10.0.0.0"
    assert ping.target("fd00::1/64") == "fd00::1"
    with pytest.raises(InvalidAddress):
        ping.target("fe80::1")


def test_only_an_echo_reply_counts_with_or_without_the_ip_header() -> None:
    reply = bytes([0, 0, 0, 0, 0, 1, 0, 1])
    request = bytes([8, 0, 0, 0, 0, 1, 0, 1])
    ipv4_header = bytes([0x45]) + bytes(19)

    assert ping._is_reply(reply, socket.AF_INET)
    assert ping._is_reply(ipv4_header + reply, socket.AF_INET)
    assert not ping._is_reply(request, socket.AF_INET)
    assert not ping._is_reply(reply[:4], socket.AF_INET)
    assert ping._is_reply(bytes([129]) + bytes(7), socket.AF_INET6)


def test_a_kernel_that_refuses_the_socket_answers_unknown_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_: object) -> socket.socket:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(ping.socket, "socket", refuse)

    answer = IcmpPinger().ping("10.0.0.42")

    assert answer.state is PingState.UNKNOWN
    assert "ping_group_range" in answer.detail


def test_a_neighbour_that_never_resolved_is_silence() -> None:
    answer = ping._unreachable("10.0.0.42", OSError(errno.EHOSTUNREACH, "x"))
    assert answer.state is PingState.SILENT

    answer = ping._unreachable("10.0.0.42", OSError(errno.ENETUNREACH, "x"))
    assert answer.state is PingState.UNKNOWN
