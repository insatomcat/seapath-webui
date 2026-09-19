# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A guest's graphic console: the relay, the route, the wire, the declaration.

The relay is the one program this service sends to a hypervisor for it, so it
is run here for real, against a `virsh` that prints what libvirt prints and a
TCP server standing in for QEMU's display. Everything else runs against the
fake display, which speaks enough of the VNC protocol for noVNC. See D62.
"""

from __future__ import annotations

import os
import shlex
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.v1.console import operator_input
from app.cluster.fake import FakeRbdClient
from app.cluster.rbd import RbdUnavailable
from app.console.adapter import ConsoleRequest, ssh_command
from app.console.fake import FakeConsoleAdapter
from app.console.service import RELAY, graphic_command
from app.core.settings import Settings
from app.services.vms import has_vnc_display
from app.trust import known_hosts
from tests.conftest import cookie_names
from tests.test_console import CLUSTER, HOST_KEY, STANDALONE

WS = "/api/v1/node/console/graphic"

GUESTS = """
VMs:
  hosts:
    guest-windows:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/windows.qcow2"
      vm_features: ["graphic-console"]
    guest-linux:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest.qcow2"
"""


def connect(client: TestClient, guest: str, **kwargs):
    """The socket noVNC opens, with the session the client holds."""
    headers = dict(kwargs.pop("headers", {}))
    name = cookie_names(client).session
    session = client.cookies.get(name)
    if session is not None:
        headers.setdefault("cookie", f"{name}={session}")
    return client.websocket_connect(f"{WS}?guest={guest}", headers=headers, **kwargs)


def _declare_cluster(client: TestClient, located: str | None) -> None:
    response = client.post(
        "/api/v1/inventory/import",
        json={"document": CLUSTER.read_text() + GUESTS},
    )
    assert response.status_code == 200, response.text
    client.app.state.console_service._locate = lambda guest: located


def _accept(settings: Settings, *addresses: str) -> None:
    known_hosts.accept_peers(
        settings.known_hosts_file, {address: [HOST_KEY] for address in addresses}
    )


# The command, and the relay it carries.


def test_the_command_is_the_relay_as_root_and_nothing_else() -> None:
    # The rule the ISO grants, `-n` so a missing rule is an error rather than a
    # prompt, and a name quoted for both shells it crosses.
    words = shlex.split(graphic_command("a'b; reboot"))
    assert words[:4] == ["sudo", "-n", "/bin/sh", "-c"]
    assert shlex.split(words[4]) == [
        "exec",
        "python3",
        "-I",
        "-c",
        RELAY,
        "a'b; reboot",
    ]


def test_a_stream_is_an_ssh_with_no_terminal(tmp_path: Path) -> None:
    # A pseudo terminal would translate line endings in the VNC protocol and
    # echo it back.
    command = ssh_command(
        ConsoleRequest(
            address="10.0.0.2",
            user="ansible",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
            command="true",
            terminal=False,
        )
    )
    assert "-T" in command
    assert "-tt" not in command
    assert "StrictHostKeyChecking=yes" in command
    assert command[-2:] == ["10.0.0.2", "true"]


class _Display:
    """A TCP server upper casing what it is sent, standing in for QEMU's."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        connection, _ = self.server.accept()
        with connection, self.server:
            while data := connection.recv(1024):
                connection.sendall(data.upper())


def _virsh(tmp_path: Path, stdout: str, stderr: str = "", code: int = 0) -> dict:
    """A `virsh` printing what libvirt would, and recording how it was asked."""
    tool = tmp_path / "bin" / "virsh"
    tool.parent.mkdir(exist_ok=True)
    tool.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(tmp_path / 'asked')!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({code})\n"
    )
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{tool.parent}{os.pathsep}{os.environ['PATH']}"}


def _relay(env: dict, stdin: bytes) -> subprocess.CompletedProcess:
    """The relay run to its end, with everything it is sent at once."""
    return subprocess.run(
        [sys.executable, "-I", "-c", RELAY, "guest-windows"],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=10,
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_the_relay_copies_bytes_to_the_display_virsh_names(
    tmp_path: Path, host: str
) -> None:
    # `virsh` gives the display number, and a server listening everywhere is
    # named `localhost`: both are reached on the loopback.
    display = _Display()
    env = _virsh(tmp_path, f"vnc://{host}:{display.port - 5900}\n")
    relay = subprocess.Popen(
        [sys.executable, "-I", "-c", RELAY, "guest-windows"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    relay.stdin.write(b"rfb 003.008\n")
    relay.stdin.flush()
    assert relay.stdout.read(12) == b"RFB 003.008\n"
    # The ssh going away is the end of stdin, and ends the relay.
    relay.stdin.close()
    assert relay.wait(timeout=10) == 0, relay.stderr.read()
    relay.stdout.close()
    relay.stderr.close()
    assert (tmp_path / "asked").read_text() == "domdisplay --type vnc guest-windows"


def test_the_relay_says_why_there_is_no_display(tmp_path: Path) -> None:
    env = _virsh(
        tmp_path,
        "",
        "error: Requested operation is not valid: domain is not running\n",
        code=1,
    )

    done = _relay(env, b"")

    assert done.returncode == 3
    assert done.stdout == b""
    assert b"domain is not running" in done.stderr


def test_a_domain_with_no_vnc_display_is_said_so(tmp_path: Path) -> None:
    # A running domain with no `<graphics>`: virsh prints nothing and succeeds.
    done = _relay(_virsh(tmp_path, ""), b"")

    assert done.returncode == 3
    assert b"no VNC display" in done.stderr


# Where it opens.


def test_it_opens_on_the_machine_running_the_guest(
    signed_in: TestClient, settings: Settings, console_adapter: FakeConsoleAdapter
) -> None:
    _declare_cluster(signed_in, located="node3")
    _accept(settings, "10.132.159.61", "10.132.159.62")
    settings.site_private_key_file.write_text("not a real key\n")

    with connect(signed_in, "guest-windows") as socket:
        assert socket.receive_bytes() == b"RFB 003.008\n"

    request = console_adapter.opened[0]
    assert request.address == "10.132.159.62"
    assert request.terminal is False
    assert request.command == graphic_command("guest-windows")
    assert request.extra_key_files == (settings.site_private_key_file,)


def test_an_unlocated_cluster_guest_is_refused(
    signed_in: TestClient, settings: Settings, console_adapter: FakeConsoleAdapter
) -> None:
    # Any member but the one running it would only answer that it has no such
    # domain, so none is tried.
    _declare_cluster(signed_in, located=None)
    _accept(settings, "10.132.159.61")

    with connect(signed_in, "guest-windows") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_bytes()

    assert closed.value.code == 4409
    assert "Nothing reports guest-windows running" in closed.value.reason
    assert console_adapter.opened == []


def test_the_machine_running_it_needs_an_accepted_host_key(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    _declare_cluster(signed_in, located="node1")

    with connect(signed_in, "guest-windows") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_bytes()

    assert closed.value.code == 4409
    assert "runs on node1" in closed.value.reason
    assert console_adapter.opened == []


def test_a_standalone_guest_opens_on_this_machine(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    response = signed_in.post("/api/v1/inventory/import", json={"document": STANDALONE})
    assert response.status_code == 200, response.text

    with connect(signed_in, "ABBICT") as socket:
        socket.receive_bytes()

    assert console_adapter.opened[0].address == "127.0.0.1"
    assert console_adapter.opened[0].extra_key_files == ()


@pytest.mark.parametrize("guest", ["node2", "elsewhere", ""])
def test_it_goes_only_to_a_guest_of_the_inventory(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter, guest: str
) -> None:
    _declare_cluster(signed_in, located="node2")

    with connect(signed_in, guest) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_bytes()

    assert closed.value.code == 4404
    assert console_adapter.opened == []


def test_it_needs_the_consoles_role_and_says_so_in_the_close_alone(
    signed_in_viewer: TestClient,
) -> None:
    # No JSON frame: noVNC would read one as a broken server.
    with connect(signed_in_viewer, "guest-windows") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_bytes()

    assert closed.value.code == 4403
    assert "admin" in closed.value.reason


def test_a_socket_from_another_origin_is_refused(signed_in: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with connect(
            signed_in, "ABBICT", headers={"origin": "https://elsewhere.example"}
        ) as socket:
            socket.receive_bytes()

    assert closed.value.code == 1008


# The wire.


def _handshake(socket) -> bytes:
    """What noVNC does before the first picture, and the ServerInit it gets."""
    assert socket.receive_bytes() == b"RFB 003.008\n"
    socket.send_bytes(b"RFB 003.008\n")
    assert socket.receive_bytes() == b"\x01\x01"
    socket.send_bytes(b"\x01")
    assert socket.receive_bytes() == b"\x00\x00\x00\x00"
    socket.send_bytes(b"\x01")
    return socket.receive_bytes()


def test_the_vnc_protocol_crosses_untouched_both_ways(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    response = signed_in.post("/api/v1/inventory/import", json={"document": STANDALONE})
    assert response.status_code == 200, response.text

    with connect(signed_in, "ABBICT") as socket:
        init = _handshake(socket)
        assert init[:4] == b"\x04\x00\x03\x00"
        assert init.endswith(b"fake display of ABBICT")
        assert signed_in.get("/api/v1/node/console").json()["active_sessions"] == 1

        # A whole screen, asked for and drawn.
        request = b"\x03\x00\x00\x00\x00\x00\x04\x00\x03\x00"
        socket.send_bytes(request)
        update = socket.receive_bytes()
        assert update[:4] == b"\x00\x00\x00\x01"

    assert bytes(console_adapter.displays[0].received).endswith(request)


class _Silent(FakeConsoleAdapter):
    """A relay that ended before the display said anything, and why."""

    async def open_stream(self, request: ConsoleRequest):
        display = await super().open_stream(request)
        display.read = self._nothing
        display.diagnostic = lambda: (
            "error: failed to get domain 'ABBICT'\n"
            "error: Requested operation is not valid: domain is not running"
        )
        return display

    async def _nothing(self) -> bytes:
        return b""


# Shadows the fixture of the same name, which is what `signed_in` is built on.
@pytest.mark.parametrize("console_adapter", [_Silent()])
def test_a_relay_ending_at_once_closes_with_its_last_line(
    signed_in: TestClient,
) -> None:
    response = signed_in.post("/api/v1/inventory/import", json={"document": STANDALONE})
    assert response.status_code == 200, response.text

    with connect(signed_in, "ABBICT") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_bytes()

    assert closed.value.code == 4409
    assert closed.value.reason == (
        "error: Requested operation is not valid: domain is not running"
    )


@pytest.mark.parametrize(
    ("frame", "counted"),
    [
        # noVNC asking for the screen, alone or several at once.
        (b"\x03\x01\x00\x00\x00\x00\x04\x00\x03\x00", False),
        (b"\x03\x01" + bytes(8) + b"\x03\x01" + bytes(8), False),
        # A key, the pointer, and a key sent with a request.
        (b"\x04\x01\x00\x00\x00\x00\xff\x0d", True),
        (b"\x05\x00\x00\x10\x00\x20", True),
        (b"\x03\x01" + bytes(8) + b"\x04\x01\x00\x00\x00\x00\xff\x0d", True),
    ],
)
def test_the_idle_timeout_counts_the_operator_and_not_novnc(
    frame: bytes, counted: bool
) -> None:
    assert operator_input(frame) is counted


# Which guests have a screen: the XML Ceph holds for each cluster guest.

WINDOWS_XML = """<domain type="kvm"><name>guest-windows</name><devices>
<graphics type="spice" autoport="yes"><listen type="address"/></graphics>
<graphics type='vnc' port='-1' autoport='yes'/>
<video><model type="qxl"/></video></devices></domain>"""

LINUX_XML = """<domain type="kvm"><name>guest-linux</name><devices>
<graphics type="spice" autoport="yes"/><video><model type="virtio"/></video>
</devices></domain>"""


@pytest.mark.parametrize(
    ("xml", "shown"),
    [
        (WINDOWS_XML, True),
        # SPICE is a protocol noVNC does not speak, and a video card alone
        # shows nothing outside the guest.
        (LINUX_XML, False),
        ("<domain><devices><video/></devices></domain>", False),
        ('<domain><devices><graphics  autoport="yes" type="vnc"/></devices>', True),
    ],
)
def test_a_vnc_graphics_element_is_what_gives_a_screen(xml: str, shown: bool) -> None:
    assert has_vnc_display(xml) is shown


class _Ceph(FakeRbdClient):
    """Holds the two guests' XML, and fails for one image."""

    def __init__(self) -> None:
        super().__init__(
            {
                "system_guest-windows": {"xml": WINDOWS_XML, "_priority": "10"},
                "system_guest-linux": {"xml": LINUX_XML},
            }
        )

    def list_metadata(self, image: str) -> dict[str, str]:
        if image == "system_guest-lost":
            raise RbdUnavailable("the monitors did not answer")
        return super().list_metadata(image)


LOST = """    guest-lost:
      vm_template: "../templates/vm/guest.xml.j2"
    guest-new:
      vm_template: "../templates/vm/guest.xml.j2"
"""


@pytest.mark.parametrize("rbd_client", [_Ceph()])
def test_the_page_reads_the_screens_from_the_xml_ceph_holds(
    signed_in: TestClient,
) -> None:
    # Whatever created the guest: the inventory keeps no XML once it exists.
    # A guest with no image yet has nothing to show and is left out, and one
    # Ceph did not answer for is said as unknown.
    response = signed_in.post(
        "/api/v1/inventory/import",
        json={"document": CLUSTER.read_text() + GUESTS + LOST},
    )
    assert response.status_code == 200, response.text

    answer = signed_in.get("/api/v1/vms/displays")

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "guests": {"guest-windows": True, "guest-linux": False, "guest-lost": None}
    }


def test_a_standalone_guest_is_not_read_from_ceph(signed_in: TestClient) -> None:
    # Its definition is its machine's libvirt, and the console asks it there.
    response = signed_in.post("/api/v1/inventory/import", json={"document": STANDALONE})
    assert response.status_code == 200, response.text

    assert signed_in.get("/api/v1/vms/displays").json() == {"guests": {}}


def test_the_vms_page_loads_the_panel_and_novnc_is_served(
    signed_in: TestClient,
) -> None:
    from app.ui.routes import stamp

    body = signed_in.get("/vms").text

    assert f'src="static/graphic.js?v={stamp("graphic.js")}"' in body
    rfb = "vendor/novnc/core/rfb.js"
    assert f'data-rfb="static/{rfb}?v={stamp(rfb)}"' in body
    module = signed_in.get(f"/static/{rfb}")
    assert module.status_code == 200
    # A module is refused by the browser under any other type.
    assert "javascript" in module.headers["content-type"]
