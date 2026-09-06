# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Replicating the inventory to the machines the inventory declares.

Against real git repositories on disk, over a transport that is a directory
rather than an ssh connection. What is being tested is what git does with a
push, which is the whole safety of D32, so faking git itself would test
nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.inventory.model import Guest, Inventory, NodeConfig
from app.inventory.replication import (
    ReplicationService,
    SshTransport,
    Status,
    Target,
    build_ssh_command,
    remote_helper,
)
from app.inventory.repository import InventoryRepository


class DirectoryTransport:
    """A peer is a directory, so a push is a real push with no network.

    The address of a machine is the path of its repository, which is what
    `git push` takes for a local transport. Nothing is started through `sudo`
    or `ssh`, so the helpers are the defaults.
    """

    def url(self, target: Target) -> str:
        return target.address

    def ssh_command(self) -> str | None:
        return None

    def upload_pack(self) -> str | None:
        return None

    def receive_pack(self) -> str | None:
        return None


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> InventoryRepository:
    repository = InventoryRepository(tmp_path / "source")
    repository.initialise()
    repository.commit(
        content="all:\n  hosts:\n    node1: {}\n",
        message="inventory: the first commit",
        author="alice",
    )
    return repository


def _peer(source: InventoryRepository, path: Path) -> InventoryRepository:
    """A machine that took its copy from this one, as a join would leave it."""
    subprocess.run(
        ["git", "clone", "--quiet", str(source.path), str(path)],
        check=True,
        capture_output=True,
    )
    peer = InventoryRepository(path)
    peer.accept_replication()
    return peer


def _inventory(*hosts: str, guests: tuple[str, ...] = ()) -> Inventory:
    return Inventory(
        hosts={
            name: NodeConfig(ansible_host=address, network_interface="eth0")
            for name, address in (host.split("=", 1) for host in hosts)
        },
        guests={name: Guest() for name in guests},
    )


def _service(source: InventoryRepository) -> ReplicationService:
    return ReplicationService(source, DirectoryTransport())


# What reaches the network, in one reviewable place


def test_the_invocation_is_the_connection_a_run_makes(tmp_path: Path) -> None:
    command = build_ssh_command(
        tmp_path / "id_ed25519_self",
        tmp_path / "known_hosts",
        (tmp_path / "id_site",),
    )

    assert command == (
        "ssh -F /dev/null "
        f"-o UserKnownHostsFile={tmp_path}/known_hosts "
        "-o StrictHostKeyChecking=yes "
        "-o IdentitiesOnly=yes "
        "-o BatchMode=yes "
        "-o ControlMaster=no "
        "-o ControlPath=none "
        "-o ConnectTimeout=10 "
        f"-i {tmp_path}/id_ed25519_self "
        f"-i {tmp_path}/id_site"
    )


def test_the_remote_helper_goes_through_the_sudo_rule_the_iso_grants() -> None:
    """`NOPASSWD:EXEC:SETENV: /bin/sh`, which is what Ansible's become uses.

    A bare `sudo git-receive-pack` would be refused by that rule, and the
    repository on the far side is root owned.
    """
    assert remote_helper("git-receive-pack") == (
        "sudo -n /bin/sh -c 'exec git-receive-pack \"$0\"'"
    )


def test_the_url_names_the_ansible_account_and_the_path_a_node_holds() -> None:
    transport = SshTransport(
        user="ansible",
        private_key_file=Path("/etc/seapath/webui/ssh/id_ed25519_self"),
        known_hosts_file=Path("/etc/seapath/webui/ssh/known_hosts"),
        remote_path=Path("/etc/seapath/inventory"),
    )

    assert transport.url(Target(host="node2", address="10.0.0.2")) == (
        "ansible@10.0.0.2:/etc/seapath/inventory"
    )


# Who receives


def test_the_guests_and_this_machine_receive_nothing(
    source: InventoryRepository,
) -> None:
    """A member of `VMs` is a libvirt domain, and holds no inventory."""
    inventory = _inventory(
        "node1=10.0.0.1", "node2=10.0.0.2", "node3=10.0.0.3", guests=("guest1",)
    )

    targets = _service(source).targets(inventory, this_host="node1")

    assert [target.host for target in targets] == ["node2", "node3"]


# What a push does


def test_a_machine_that_is_behind_is_brought_to_this_commit(
    source: InventoryRepository, tmp_path: Path
) -> None:
    peer = _peer(source, tmp_path / "node2")
    source.commit(
        content="all:\n  hosts:\n    node1:\n      gateway_addr: 10.0.0.254\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )

    results = _service(source).replicate(
        _inventory("node1=ignored", f"node2={peer.path}"), this_host="node1"
    )

    assert [(r.host, r.status) for r in results] == [("node2", Status.UPDATED)]
    assert peer.head() == source.head()
    # The push moved the files, not only the branch: the peer serves its
    # working tree, and a copy whose files lag what it committed would be read
    # by a run from that machine.
    assert "gateway_addr" in peer.read()


def test_a_second_replication_has_nothing_to_send(
    source: InventoryRepository, tmp_path: Path
) -> None:
    peer = _peer(source, tmp_path / "node2")
    inventory = _inventory("node1=ignored", f"node2={peer.path}")

    results = _service(source).replicate(inventory, this_host="node1")

    assert [r.status for r in results] == [Status.UP_TO_DATE]


def test_a_machine_carrying_its_own_commits_is_refused_and_keeps_them(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """The whole reason this pushes a branch rather than copying a folder."""
    peer = _peer(source, tmp_path / "node2")
    peer.commit(
        content="all:\n  hosts:\n    node1:\n      isolcpus: 2-7\n",
        message="tuning: isolate the real time cores",
        author="bob",
    )
    theirs = peer.head()
    source.commit(
        content="all:\n  hosts:\n    node1:\n      gateway_addr: 10.0.0.254\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )

    results = _service(source).replicate(
        _inventory("node1=ignored", f"node2={peer.path}"), this_host="node1"
    )

    assert [r.status for r in results] == [Status.REFUSED]
    assert "node2" in results[0].detail
    assert peer.head() == theirs
    assert "isolcpus" in peer.read()


def test_a_machine_that_cannot_be_reached_is_one_line_of_the_result(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """A partial success is the ordinary outcome of a node being down."""
    peer = _peer(source, tmp_path / "node2")
    source.commit(
        content="all:\n  hosts:\n    node1:\n      gateway_addr: 10.0.0.254\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )

    results = _service(source).replicate(
        _inventory(
            "node1=ignored",
            f"node2={peer.path}",
            f"node3={tmp_path / 'never-installed'}",
        ),
        this_host="node1",
    )

    assert [(r.host, r.status) for r in results] == [
        ("node2", Status.UPDATED),
        ("node3", Status.UNREACHABLE),
    ]
    assert results[1].detail
    assert peer.head() == source.head()


def test_a_peer_that_never_accepted_a_push_refuses_it(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """`receive.denyCurrentBranch=updateInstead` is load bearing.

    Without it git refuses to push into the branch a worktree has checked out,
    which is every copy of this repository. The setting is written at first
    boot and repaired at every start, so this asserts what it buys.
    """
    peer = _peer(source, tmp_path / "node2")
    _git(peer.path, "config", "--unset", "receive.denyCurrentBranch")
    source.commit(
        content="all:\n  hosts:\n    node1:\n      gateway_addr: 10.0.0.254\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )

    results = _service(source).replicate(
        _inventory("node1=ignored", f"node2={peer.path}"), this_host="node1"
    )

    # The machine answered and said no, which is a refusal rather than a
    # machine that could not be reached, and it says so in git's own words.
    assert results[0].status is Status.REFUSED
    assert "checked out" in results[0].detail
    assert peer.head() != source.head()


# What the page shows before anyone pushes


def test_the_survey_says_which_machine_is_behind(
    source: InventoryRepository, tmp_path: Path
) -> None:
    behind = _peer(source, tmp_path / "node2")
    current = _peer(source, tmp_path / "node3")
    source.commit(
        content="all:\n  hosts:\n    node1:\n      gateway_addr: 10.0.0.254\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )
    _git(current.path, "pull", "--quiet", "origin", "main")

    results = _service(source).survey(
        _inventory("node1=ignored", f"node2={behind.path}", f"node3={current.path}"),
        this_host="node1",
    )

    assert [(r.host, r.status) for r in results] == [
        ("node2", Status.BEHIND),
        ("node3", Status.UP_TO_DATE),
    ]
    assert results[0].commit == behind.head()


def test_a_copy_carrying_unknown_work_is_diverged_rather_than_behind(
    source: InventoryRepository, tmp_path: Path
) -> None:
    peer = _peer(source, tmp_path / "node2")
    peer.commit(
        content="all:\n  hosts:\n    node1:\n      isolcpus: 2-7\n",
        message="tuning: isolate the real time cores",
        author="bob",
    )

    results = _service(source).survey(
        _inventory("node1=ignored", f"node2={peer.path}"), this_host="node1"
    )

    assert results[0].status is Status.DIVERGED


# Through the API, which is where an operator meets it


CLUSTER = Path(__file__).parent / "golden" / "adopted-cluster.yaml"


def test_a_standalone_node_has_nowhere_to_replicate_to(signed_in) -> None:
    """The seeded inventory describes this machine and no other."""
    survey = signed_in.get("/api/v1/inventory/replicas").json()
    assert survey["commit"]
    assert survey["replicas"] == []

    refusal = signed_in.post("/api/v1/inventory/replicate")

    assert refusal.status_code == 409
    assert refusal.json()["error"]["code"] == "no_replicas"


def test_the_page_lists_what_every_machine_of_the_inventory_holds(
    signed_in,
) -> None:
    signed_in.post("/api/v1/inventory/import", json={"document": CLUSTER.read_text()})

    survey = signed_in.get("/api/v1/inventory/replicas").json()

    assert [replica["host"] for replica in survey["replicas"]] == [
        "node1",
        "node2",
        "node3",
    ]
    assert [replica["commit"] for replica in survey["replicas"]] == [
        survey["commit"]
    ] * 3


def test_replicating_brings_every_machine_to_this_commit(signed_in) -> None:
    document = CLUSTER.read_text()
    signed_in.post("/api/v1/inventory/import", json={"document": document})
    # The machines are contacted once, so their copies exist before the change.
    signed_in.get("/api/v1/inventory/replicas")
    signed_in.post(
        "/api/v1/inventory/import",
        json={"document": document + "\n# One more variable, committed here.\n"},
    )

    behind = signed_in.get("/api/v1/inventory/replicas").json()
    assert [replica["status"] for replica in behind["replicas"]] == ["behind"] * 3

    response = signed_in.post("/api/v1/inventory/replicate")

    assert response.status_code == 200
    body = response.json()
    assert [replica["status"] for replica in body["replicas"]] == ["updated"] * 3
    assert [replica["commit"] for replica in body["replicas"]] == [body["commit"]] * 3
    assert (
        signed_in.get("/api/v1/inventory/replicas").json()["replicas"][0]["status"]
        == "up_to_date"
    )


def test_a_viewer_reads_the_copies_and_never_moves_them(signed_in_viewer) -> None:
    """A commit landing on three hypervisors is an administrator's act."""
    assert signed_in_viewer.get("/api/v1/inventory/replicas").status_code == 200

    assert signed_in_viewer.post("/api/v1/inventory/replicate").status_code == 403
