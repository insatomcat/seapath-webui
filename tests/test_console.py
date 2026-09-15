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
import shlex
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.console.adapter import ConsoleRequest, ssh_command
from app.console.fake import FakeConsoleAdapter
from app.console.service import (
    ConsoleService,
    ConsoleUnavailable,
    clamp_window,
    serial_command,
)
from app.core.auth import Role
from app.core.settings import Settings
from app.main import create_app
from app.trust import known_hosts
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
    assert command.count("-i") == 1
    assert command[command.index("-l") + 1] == "ansible"
    assert command[-1] == "127.0.0.1"


def test_another_machine_is_offered_the_keys_a_run_offers_in_its_order(
    tmp_path: Path,
) -> None:
    command = ssh_command(
        ConsoleRequest(
            address="10.132.159.61",
            user="ansible",
            private_key_file=tmp_path / "id_ed25519_self",
            known_hosts_file=tmp_path / "known_hosts",
            extra_key_files=(tmp_path / "id_site",),
        )
    )

    identities = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    assert identities == [str(tmp_path / "id_ed25519_self"), str(tmp_path / "id_site")]
    assert command[-1] == "10.132.159.61"


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
    # The console reaches the `ansible` account, and `sudo sh` there is root.
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
            "host": "seapath-machine",
            "kind": "this_machine",
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


# The console on the other machines and the guests. The cluster is the real
# inventory the fidelity tests use, with two guests added: one a run reaches at
# an address, and one nothing reaches from outside.
CLUSTER = Path(__file__).parent / "golden" / "adopted-cluster.yaml"
GUESTS = """
VMs:
  hosts:
    guest-addressed:
      ansible_host: 192.168.55.20
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest.qcow2"
    guest-dark:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest.qcow2"
"""
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAteCrWVAzKoCWvV6"


def _declare_cluster(client: TestClient) -> None:
    response = client.post(
        "/api/v1/inventory/import",
        json={"document": CLUSTER.read_text() + GUESTS},
    )
    assert response.status_code == 200, response.text


def _accept(settings: Settings, *addresses: str) -> None:
    known_hosts.accept_peers(
        settings.known_hosts_file, {address: [HOST_KEY] for address in addresses}
    )


def test_every_machine_and_addressed_guest_is_offered(
    signed_in: TestClient, settings: Settings
) -> None:
    _declare_cluster(signed_in)
    _accept(settings, "10.132.159.61", "192.168.55.20")

    targets = signed_in.get("/api/v1/node/console").json()["targets"]

    # This machine first, by the name it is known under, then the inventory in
    # its own order. The guest nobody can reach from outside is not offered.
    offered = [
        (t["name"], t["kind"], t["address"], t["host_key_known"]) for t in targets
    ]
    assert offered == [
        ("seapath-machine", "this_machine", "127.0.0.1", True),
        ("node1", "machine", "10.132.159.60", False),
        ("node2", "machine", "10.132.159.61", True),
        ("node3", "machine", "10.132.159.62", False),
        ("guest-addressed", "guest", "192.168.55.20", True),
    ]


def test_a_console_on_another_machine_goes_to_its_ansible_host_with_the_site_key(
    signed_in: TestClient, settings: Settings, console_adapter: FakeConsoleAdapter
) -> None:
    _declare_cluster(signed_in)
    _accept(settings, "10.132.159.61")
    settings.site_private_key_file.write_text("not a real key\n")

    with connect(signed_in, "?host=node2") as socket:
        assert socket.receive_json() == {
            "type": "ready",
            "host": "node2",
            "kind": "machine",
            "target": "ansible@10.132.159.61",
        }

    request = console_adapter.opened[0]
    assert request.address == "10.132.159.61"
    assert request.user == "ansible"
    assert request.private_key_file == settings.self_private_key_file
    assert request.extra_key_files == (settings.site_private_key_file,)
    assert request.known_hosts_file == settings.known_hosts_file


def test_this_machine_is_reached_over_its_self_relation_alone(
    signed_in: TestClient, settings: Settings, console_adapter: FakeConsoleAdapter
) -> None:
    # The self relation is the one that carries `pty`, and the loopback is in
    # its `from=` clause. Offering the site key there as well would change
    # nothing that works and make a refused self key look like a site problem.
    settings.site_private_key_file.write_text("not a real key\n")

    with connect(signed_in) as socket:
        socket.receive_json()

    assert console_adapter.opened[0].address == "127.0.0.1"
    assert console_adapter.opened[0].extra_key_files == ()


def test_a_console_on_a_guest_goes_to_the_address_its_entry_gives(
    signed_in: TestClient, settings: Settings, console_adapter: FakeConsoleAdapter
) -> None:
    _declare_cluster(signed_in)
    _accept(settings, "192.168.55.20")

    with connect(signed_in, "?host=guest-addressed") as socket:
        assert socket.receive_json()["kind"] == "guest"

    assert console_adapter.opened[0].address == "192.168.55.20"


@pytest.mark.parametrize(
    "host",
    [
        # Not in the inventory at all.
        "elsewhere",
        # A guest with no address, which a run does not reach from outside.
        "guest-dark",
        # An address rather than a name. Accepting one would make the console
        # an ssh relay, carrying the site key, to anything the network routes.
        "10.132.159.61",
    ],
)
def test_a_console_goes_only_to_a_name_the_inventory_gives_an_address(
    signed_in: TestClient,
    settings: Settings,
    console_adapter: FakeConsoleAdapter,
    host: str,
) -> None:
    _declare_cluster(signed_in)
    _accept(settings, "10.132.159.61")

    with connect(signed_in, f"?host={host}") as socket:
        assert socket.receive_json()["code"] == "unknown_host"
        assert socket.receive()["code"] == 4404

    assert console_adapter.opened == []


def test_a_machine_whose_host_key_nobody_accepted_is_refused_before_ssh(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    # The console checks host keys strictly and never learns one. Saying so
    # beats "Host key verification failed" inside a terminal.
    _declare_cluster(signed_in)

    with connect(signed_in, "?host=node1") as socket:
        message = socket.receive_json()
        assert message["code"] == "host_key_unknown"
        assert "10.132.159.60" in message["message"]
        assert socket.receive()["code"] == 4409

    assert console_adapter.opened == []


def test_another_machine_needs_the_same_role(signed_in_viewer: TestClient) -> None:
    with connect(signed_in_viewer, "?host=node2") as socket:
        assert socket.receive_json()["code"] == "permission_denied"


def test_the_names_a_known_hosts_file_holds_keys_for(tmp_path: Path) -> None:
    record = tmp_path / "known_hosts"
    record.write_text(
        "# a comment\n"
        f"10.0.0.1 {HOST_KEY}\n"
        f"ccv1,10.0.0.2 {HOST_KEY}\n"
        f"[10.0.0.3]:22 {HOST_KEY}\n"
        f"[10.0.0.4]:2222 {HOST_KEY}\n"
        f"|1|c2FsdA==|aGFzaA== {HOST_KEY}\n"
        f"@revoked 10.0.0.5 {HOST_KEY}\n"
        "10.0.0.6\n"
    )

    assert known_hosts.recorded_names(record) == {
        "10.0.0.1",
        "ccv1",
        "10.0.0.2",
        "10.0.0.3",
        "10.0.0.5",
    }
    assert known_hosts.recorded_names(tmp_path / "missing") == set()


# A guest's serial console: `vm-mgr console` at the end of the same connection.
STANDALONE = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      network_interface: eno1
      admin_user: admin
  children:
    standalone_machine:
      hosts:
        seapath-machine:
    hypervisors:
      hosts:
        seapath-machine:
    VMs:
      hosts:
        ABBICT:
"""


def test_the_serial_console_command_is_vm_mgr_as_root_and_nothing_else() -> None:
    # The whole of the ISO's rule is `/bin/sh`, `-n` keeps a missing rule from
    # becoming a prompt, and `exec` leaves no shell on the hypervisor once
    # `virsh` is gone. A name is quoted for both shells it crosses.
    assert serial_command("vm-guest1") == (
        "sudo -n /bin/sh -c 'exec vm-mgr console vm-guest1'"
    )
    assert shlex.split(shlex.split(serial_command("a'b; reboot"))[4]) == [
        "exec",
        "vm-mgr",
        "console",
        "a'b; reboot",
    ]


def test_a_cluster_guests_serial_console_runs_vm_mgr_on_a_reachable_hypervisor(
    signed_in: TestClient, settings: Settings, console_adapter: FakeConsoleAdapter
) -> None:
    # This node is not in that cluster, so the first hypervisor whose host key
    # is accepted drives: `vm-mgr` asks Pacemaker where the guest runs and goes
    # there itself, as libvirtadmin.
    _declare_cluster(signed_in)
    _accept(settings, "10.132.159.61", "10.132.159.62")
    settings.site_private_key_file.write_text("not a real key\n")

    with connect(signed_in, "?serial=guest-dark") as socket:
        assert socket.receive_json() == {
            "type": "ready",
            "host": "node2",
            "kind": "machine",
            "target": "ansible@10.132.159.61",
            "serial": "guest-dark",
        }

    request = console_adapter.opened[0]
    assert request.address == "10.132.159.61"
    assert request.command == "sudo -n /bin/sh -c 'exec vm-mgr console guest-dark'"
    assert request.extra_key_files == (settings.site_private_key_file,)
    assert ssh_command(request)[-2:] == ["10.132.159.61", request.command]


def test_a_standalone_guests_serial_console_runs_on_this_machine(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    response = signed_in.post("/api/v1/inventory/import", json={"document": STANDALONE})
    assert response.status_code == 200, response.text

    with connect(signed_in, "?serial=ABBICT") as socket:
        ready = socket.receive_json()
        assert ready["kind"] == "this_machine"
        assert ready["serial"] == "ABBICT"

    request = console_adapter.opened[0]
    assert request.address == "127.0.0.1"
    assert request.extra_key_files == ()
    assert request.command.endswith("'exec vm-mgr console ABBICT'")


def test_a_shell_carries_no_command(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    with connect(signed_in) as socket:
        assert "serial" not in socket.receive_json()

    assert console_adapter.opened[0].command == ""
    assert ssh_command(console_adapter.opened[0])[-1] == "127.0.0.1"


def test_a_serial_console_goes_only_to_a_guest_of_the_inventory(
    signed_in: TestClient, settings: Settings, console_adapter: FakeConsoleAdapter
) -> None:
    # A machine is not a guest, and a name nobody declared reaches nothing.
    _declare_cluster(signed_in)
    _accept(settings, "10.132.159.61")

    for name in ("node2", "elsewhere"):
        with connect(signed_in, f"?serial={name}") as socket:
            assert socket.receive_json()["code"] == "unknown_guest"
            assert socket.receive()["code"] == 4404

    assert console_adapter.opened == []


def test_a_serial_console_with_no_reachable_hypervisor_says_which_were_tried(
    signed_in: TestClient, console_adapter: FakeConsoleAdapter
) -> None:
    _declare_cluster(signed_in)

    with connect(signed_in, "?serial=guest-dark") as socket:
        message = socket.receive_json()
        assert message["code"] == "no_hypervisor"
        assert "node1, node2, node3" in message["message"]
        assert socket.receive()["code"] == 4409

    assert console_adapter.opened == []


def test_a_serial_console_needs_the_same_role(signed_in_viewer: TestClient) -> None:
    with connect(signed_in_viewer, "?serial=guest-dark") as socket:
        assert socket.receive_json()["code"] == "permission_denied"


def _standalone_service(tmp_path: Path, document: str, located: str | None):
    from app.inventory.parser import parse
    from app.inventory.service import InventoryState

    (tmp_path / "key").write_text("")
    record = tmp_path / "known_hosts"
    known_hosts.accept_peers(record, {"10.0.0.2": [HOST_KEY], "10.0.0.3": [HOST_KEY]})
    return ConsoleService(
        FakeConsoleAdapter(),
        target="127.0.0.1",
        user="ansible",
        private_key_file=tmp_path / "key",
        known_hosts_file=record,
        hostname="box1",
        inventory=lambda: InventoryState(
            inventory=parse(document), seeded=True, this_host="box1"
        ),
        locate=lambda guest: located,
    )


TWO_STANDALONE = """
all:
  hosts:
    box1:
      ansible_host: 10.0.0.1
    box2:
      ansible_host: 10.0.0.2
    box3:
      ansible_host: 10.0.0.3
  children:
    standalone_machine:
      hosts:
        box1:
        box2:
        box3:
    hypervisors:
      hosts:
        box1:
        box2:
        box3:
    VMs:
      hosts:
        guest:
"""


def test_a_standalone_guest_is_reached_where_libvirt_reports_it(
    tmp_path: Path,
) -> None:
    service = _standalone_service(tmp_path, TWO_STANDALONE, located="box3")

    assert service.serial_route("guest").name == "box3"


def test_an_unlocated_guest_among_several_machines_is_tried_on_this_one(
    tmp_path: Path,
) -> None:
    # On this machine rather than an arbitrary other: a guest that is not here
    # makes `virsh` say so, while a guest on a machine picked at random could
    # be a different guest of the same name.
    service = _standalone_service(tmp_path, TWO_STANDALONE, located=None)

    assert service.serial_route("guest").name == "box1"
