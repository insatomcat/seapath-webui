# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Whether something already answers at the address a new guest is given.

The inventory refuses two of its own hosts on one address, guests included.
What it cannot see is everything it does not declare: a machine on the same
network, a guest another site deployed, a forgotten appliance. The VMs page
asks this before the entry is written, because a guest brought up on an
address somebody else holds is found out later, by whichever of the two stops
answering.

One echo request, sent from this node, and nothing else. It reads the network
the way `ping` would and changes nothing on any machine.

The echo goes out through an ICMP datagram socket, the kind the kernel offers
without `CAP_NET_RAW`, so the quadlet asks for no capability and the image
carries no `ping` binary. The kernel lets a group open one when
`net.ipv4.ping_group_range` includes it, which is the Debian default. A node
that narrowed it answers `unknown` with that sentence rather than a guess.

Silence is weaker evidence than an answer, and the answer says so: a host that
drops ICMP is invisible here, and so is one on a network this node has no
route to. An echo that comes back is the only conclusive result.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import select
import socket
import struct
import time
from enum import Enum
from typing import Protocol

from pydantic import BaseModel

# Three echoes at most, one second each, stopping at the first reply. A guest
# that is up answers the first; the other two cover a lost packet and the ARP
# resolution that the first echo to a quiet neighbour waits on.
ATTEMPTS = 3
TIMEOUT_SECONDS = 1.0

_ECHO_REQUEST = {socket.AF_INET: 8, socket.AF_INET6: 128}
_ECHO_REPLY = {socket.AF_INET: 0, socket.AF_INET6: 129}
_PROTOCOL = {
    socket.AF_INET: socket.IPPROTO_ICMP,
    socket.AF_INET6: socket.IPPROTO_ICMPV6,
}


class InvalidAddress(ValueError):
    """An address no echo request should be sent to."""


class PingState(str, Enum):
    ANSWERED = "answered"
    SILENT = "silent"
    UNKNOWN = "unknown"


class PingAnswer(BaseModel):
    address: str
    state: PingState
    round_trip_ms: float | None = None
    detail: str


class Pinger(Protocol):
    """Sends echo requests to one address and says what came back.

    Injected like the other adapters, so the suite sends nothing on the wire.
    """

    def ping(self, address: str) -> PingAnswer: ...


def target(value: str) -> str:
    """The address an echo is sent to, from what the form field holds.

    The field carries a prefix length, `10.0.0.42/24`, so the address is taken
    from the interface. Refused where an echo could only mislead: an address
    that names a network, a broadcast, a group, or this machine itself.
    """
    try:
        interface = ipaddress.ip_interface(value.strip())
    except ValueError as error:
        raise InvalidAddress(f"{value!r} is not an IP address.") from error
    address = interface.ip
    network = interface.network
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or address.is_link_local
    ):
        raise InvalidAddress(f"{address} cannot be given to a guest.")
    if isinstance(address, ipaddress.IPv4Address) and network.prefixlen < 31:
        if address in (network.network_address, network.broadcast_address):
            raise InvalidAddress(
                f"{address} is the network or broadcast address of {network}."
            )
    return str(address)


class IcmpPinger:
    """The real echo, over an unprivileged ICMP datagram socket."""

    def __init__(
        self, attempts: int = ATTEMPTS, timeout: float = TIMEOUT_SECONDS
    ) -> None:
        self._attempts = attempts
        self._timeout = timeout

    def ping(self, address: str) -> PingAnswer:
        family = (
            socket.AF_INET6
            if isinstance(ipaddress.ip_address(address), ipaddress.IPv6Address)
            else socket.AF_INET
        )
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM, _PROTOCOL[family])
        except OSError as error:
            return PingAnswer(
                address=address,
                state=PingState.UNKNOWN,
                detail=(
                    "This node may not send an echo request "
                    f"({os.strerror(error.errno or errno.EPERM)}). The kernel "
                    "opens an unprivileged ICMP socket only for a group "
                    "net.ipv4.ping_group_range includes."
                ),
            )
        with sock:
            for sequence in range(1, self._attempts + 1):
                answer = self._echo(sock, family, address, sequence)
                if answer is not None:
                    return answer
        return PingAnswer(
            address=address,
            state=PingState.SILENT,
            detail=(
                f"No answer to {self._attempts} echo requests. Nothing this "
                "node can reach holds the address, unless it drops ICMP."
            ),
        )

    def _echo(
        self, sock: socket.socket, family: int, address: str, sequence: int
    ) -> PingAnswer | None:
        # Linux rewrites the identifier and the checksum on a datagram socket,
        # and delivers only the replies to this socket. The checksum is still
        # computed here, because other kernels send the packet as it is given.
        # ICMPv6 carries a pseudo header only the kernel knows, and computes it
        # everywhere.
        payload = b"seapath-webui"
        header = struct.pack("!BBHHH", _ECHO_REQUEST[family], 0, 0, 0, sequence)
        if family == socket.AF_INET:
            header = struct.pack(
                "!BBHHH",
                _ECHO_REQUEST[family],
                0,
                _checksum(header + payload),
                0,
                sequence,
            )
        packet = header + payload
        started = time.monotonic()
        try:
            sock.sendto(packet, (address, 0))
        except OSError as error:
            return _unreachable(address, error)
        deadline = started + self._timeout
        while (remaining := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                break
            try:
                data = sock.recv(1024)
            except OSError as error:
                return _unreachable(address, error)
            if _is_reply(data, family):
                return PingAnswer(
                    address=address,
                    state=PingState.ANSWERED,
                    round_trip_ms=round((time.monotonic() - started) * 1000, 2),
                    detail=f"{address} answered. Something already holds it.",
                )
        return None


def _checksum(data: bytes) -> int:
    """The Internet checksum of RFC 1071."""
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _is_reply(data: bytes, family: int) -> bool:
    # Any echo reply, whatever its sequence: the kernel delivers to this socket
    # only the replies carrying its identifier, so a late answer to an earlier
    # echo is still the address answering.
    #
    # Linux hands a datagram ICMP socket the ICMP message alone. Other kernels
    # prepend the IPv4 header, which is skipped by its own length.
    if family == socket.AF_INET and data and data[0] >> 4 == 4:
        data = data[(data[0] & 0x0F) * 4 :]
    return len(data) >= 8 and data[0] == _ECHO_REPLY[family]


def _unreachable(address: str, error: OSError) -> PingAnswer:
    # A neighbour that never answered ARP is reported as host unreachable, and
    # on a network this node is attached to that is as good as silence.
    if error.errno == errno.EHOSTUNREACH:
        return PingAnswer(
            address=address,
            state=PingState.SILENT,
            detail=(
                f"{address} is unreachable: nothing on the network answered " "for it."
            ),
        )
    return PingAnswer(
        address=address,
        state=PingState.UNKNOWN,
        detail=(
            f"This node cannot reach {address} ({os.strerror(error.errno or 0)}), "
            "so it cannot tell whether the address is taken."
        ),
    )


class FakePinger:
    """Answers for the addresses it is given, and is silent for the rest."""

    def __init__(self, answering: set[str] | None = None) -> None:
        self.answering = set(answering or ())
        self.asked: list[str] = []

    def ping(self, address: str) -> PingAnswer:
        self.asked.append(address)
        if address in self.answering:
            return PingAnswer(
                address=address,
                state=PingState.ANSWERED,
                round_trip_ms=0.4,
                detail=f"{address} answered. Something already holds it.",
            )
        return PingAnswer(
            address=address,
            state=PingState.SILENT,
            detail=(
                f"No answer to {ATTEMPTS} echo requests. Nothing this node can "
                "reach holds the address, unless it drops ICMP."
            ),
        )
