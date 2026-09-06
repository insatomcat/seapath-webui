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
    repository on the far side is root owned. The subcommand rather than the
    dashed binary, which a machine with git may not have on `PATH`: node3
    answered "git-receive-pack: not found" with git installed.

    The peer's own container is the fallback, because a SEAPATH observer can
    be a Yocto machine with no git at all, and its `seapath-webui` holds the
    same repository at the same path through the same bind mount. No single
    quote anywhere in the script, which is what keeps the outer one whole.
    """
    assert remote_helper("receive-pack") == (
        "sudo -n /bin/sh -c '"
        "if command -v git >/dev/null 2>&1; then "
        'exec git receive-pack "$0"; '
        "else "
        'exec podman exec -i seapath-webui git receive-pack "$0"; '
        "fi'"
    )
    assert "upload-pack" in remote_helper("upload-pack")
    assert "'" not in remote_helper("upload-pack")[len("sudo -n /bin/sh -c '") : -1]


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


# The repository a machine actually serves, which is not always the one it holds


def _repository_on(path: Path, branch: str) -> InventoryRepository:
    """A repository as an earlier version of this service left it.

    `git init` without `--initial-branch` produced `master`, and the only
    thing set was `receive.denyCurrentBranch`. There are no hooks and no
    `advertisePushOptions`, so this is also what a machine that has not been
    updated looks like to a replication.
    """
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", f"--initial-branch={branch}", str(path)],
        check=True,
        capture_output=True,
    )
    _git(path, "config", "receive.denyCurrentBranch", "updateInstead")
    return InventoryRepository(path)


def _updated_repository_on(path: Path, branch: str) -> InventoryRepository:
    """The same repository, on a node running this version.

    `accept_replication` is what a start runs, so this is `_repository_on`
    plus everything that start repairs: the hooks, the push options, and the
    branch moved to `main`.
    """
    repository = _repository_on(path, branch)
    repository.accept_replication()
    return repository


def test_a_machine_holding_the_commit_without_serving_it_is_not_up_to_date(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """The failure this was found by, on a real cluster.

    A repository created on `master` takes a push into `main` and leaves its
    worktree exactly as it was. Reading `refs/heads/main` back then answers
    this node's own commit, so three machines reported themselves up to date
    while their inventories were empty. What the machine serves is `HEAD`, and
    that is what is asked.
    """
    peer = _repository_on(tmp_path / "node2", "master")
    inventory = _inventory("node1=ignored", f"node2={peer.path}")

    pushed = _service(source).replicate(inventory, this_host="node1")

    assert pushed[0].status is Status.NOT_SERVED
    assert "another branch" in pushed[0].detail
    assert peer.read() == ""
    # And the page says so afterwards rather than showing the commit it sent.
    survey = _service(source).survey(inventory, this_host="node1")
    assert survey[0].status is Status.BEHIND
    assert survey[0].commit is None


def test_a_machine_serving_master_receives_it_in_its_files(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """The push targets the ref that machine serves, whatever it is called."""
    peer = _peer(source, tmp_path / "node2")
    _git(peer.path, "branch", "--move", "master")
    source.commit(
        content="all:\n  hosts:\n    node1:\n      gateway_addr: 10.0.0.254\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )

    results = _service(source).replicate(
        _inventory("node1=ignored", f"node2={peer.path}"), this_host="node1"
    )

    assert results[0].status is Status.UPDATED
    assert "gateway_addr" in peer.read()
    assert _git(peer.path, "symbolic-ref", "HEAD") == "refs/heads/master"


def test_a_file_nobody_committed_there_is_named_rather_than_overwritten(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """An ordinary replication overwrites nothing nobody committed.

    Git refuses even when the two files are byte for byte the same, and the
    refusal says the three things an operator can do about it.
    """
    peer = _updated_repository_on(tmp_path / "node2", "main")
    (peer.path / "inventory.yaml").write_text(source.read())

    results = _service(source).replicate(
        _inventory("node1=ignored", f"node2={peer.path}"), this_host="node1"
    )

    assert results[0].status is Status.REFUSED
    assert "inventory.yaml" in results[0].detail
    assert "Remove it on node2" in results[0].detail
    assert "tick Force" in results[0].detail


def test_a_repository_left_on_master_is_moved_to_the_branch_a_push_lands_on(
    tmp_path: Path,
) -> None:
    """Every node serves `main`, so a repository found elsewhere is moved."""
    unborn = _repository_on(tmp_path / "unborn", "master")
    started = _repository_on(tmp_path / "started", "master")
    started.commit(content="all: {}\n", message="inventory: one", author="alice")
    before = started.head()

    unborn.accept_replication()
    started.accept_replication()

    assert _git(unborn.path, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _git(started.path, "symbolic-ref", "HEAD") == "refs/heads/main"
    # The move is a rename, so the history is the same history.
    assert started.head() == before


def test_a_machine_that_can_run_neither_git_nor_this_service_says_which(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """Both halves of the helper failed, and which one says what to do.

    The message git carries here is the shell's, and on its own it is
    "exec: git: not found" against a path, which names no machine and no fix.
    """

    class Broken:
        """A transport whose far side has nothing to serve the repository."""

        def __init__(self, message: str) -> None:
            self.message = message

        def url(self, target: Target) -> str:
            return target.address

        def ssh_command(self) -> str | None:
            return None

        def upload_pack(self) -> str | None:
            # `git ls-remote` runs this, and a helper that exits saying so is
            # what a machine with neither binary produces.
            return f"""/bin/sh -c 'echo "{self.message}" >&2; exit 127'"""

        def receive_pack(self) -> str | None:
            return self.upload_pack()

    inventory = _inventory("node1=ignored", "node3=/anywhere")

    no_podman = ReplicationService(source, Broken("sh: podman: not found")).survey(
        inventory, this_host="node1"
    )
    stopped = ReplicationService(
        source, Broken("Error: no such container seapath-webui")
    ).survey(inventory, this_host="node1")

    assert no_podman[0].status is Status.UNREACHABLE
    assert "neither git nor podman" in no_podman[0].detail
    assert "Install git on it" in no_podman[0].detail

    assert "no seapath-webui container is running" in stopped[0].detail
    assert "Start the service on that machine" in stopped[0].detail


# Forcing, which is the one act here that can destroy a commit


def test_forcing_moves_a_diverged_machine_to_this_commit(
    source: InventoryRepository, tmp_path: Path
) -> None:
    peer = _peer(source, tmp_path / "node2")
    peer.commit(
        content="all:\n  hosts:\n    node1:\n      isolcpus: 2-7\n",
        message="tuning: isolate the real time cores",
        author="bob",
    )
    source.commit(
        content="all:\n  hosts:\n    node1:\n      gateway_addr: 10.0.0.254\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )
    inventory = _inventory("node1=ignored", f"node2={peer.path}")

    refused = _service(source).replicate(inventory, this_host="node1")
    forced = _service(source).replicate(inventory, this_host="node1", force=True)

    assert refused[0].status is Status.REFUSED
    assert forced[0].status is Status.UPDATED
    assert peer.head() == source.head()
    # The files followed the branch, and what that machine held is gone from
    # everything but its reflog.
    assert "gateway_addr" in peer.read()
    assert "isolcpus" not in peer.read()


def test_forcing_overwrites_a_file_nobody_committed_there(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """What ticking Force has to mean to be worth ticking.

    The case a real cluster hit: a peer holding an `inventories/` directory
    nobody had committed, which stopped every replication. Force reaches the
    files as well as the branch.
    """
    peer = _updated_repository_on(tmp_path / "node2", "master")
    (peer.path / "inventory.yaml").write_text("theirs: by hand\n")
    inventory = _inventory("node1=ignored", f"node2={peer.path}")

    refused = _service(source).replicate(inventory, this_host="node1")
    forced = _service(source).replicate(inventory, this_host="node1", force=True)

    assert refused[0].status is Status.REFUSED
    assert forced[0].status is Status.UPDATED
    assert peer.read() == source.read()
    assert peer.head() == source.head()


def test_a_machine_that_cannot_be_forced_says_so_rather_than_doing_less(
    source: InventoryRepository, tmp_path: Path
) -> None:
    """Forcing is carried by a push option the receiving hooks read.

    A node that has not been updated advertises none, and sending the push
    without it would quietly do the ordinary thing under a button that says
    Force.
    """
    peer = _repository_on(tmp_path / "node2", "main")
    (peer.path / "inventory.yaml").write_text("theirs: by hand\n")

    results = _service(source).replicate(
        _inventory("node1=ignored", f"node2={peer.path}"),
        this_host="node1",
        force=True,
    )

    assert results[0].status is Status.REFUSED
    assert "cannot be forced" in results[0].detail
    assert "Nothing was sent" in results[0].detail
    assert (peer.path / "inventory.yaml").read_text() == "theirs: by hand\n"


def test_the_hooks_are_written_where_git_runs_them(tmp_path: Path) -> None:
    """A repository made by an earlier version gains them at the next start."""
    repository = _repository_on(tmp_path / "node", "master")

    repository.accept_replication()

    hooks = repository.path / ".git" / "hooks"
    for name in ("pre-receive", "push-to-checkout"):
        hook = hooks / name
        assert hook.read_text().startswith("#!/bin/sh")
        assert hook.stat().st_mode & 0o111, f"{name} is not executable"
    assert _git(repository.path, "config", "receive.advertisePushOptions") == "true"


def test_forcing_is_asked_for_by_name(signed_in) -> None:
    """A replication that did not ask for it keeps every refusal."""
    document = CLUSTER.read_text()
    signed_in.post("/api/v1/inventory/import", json={"document": document})
    signed_in.get("/api/v1/inventory/replicas")

    default = signed_in.post("/api/v1/inventory/replicate")
    asked = signed_in.post("/api/v1/inventory/replicate", json={"force": True})

    assert default.status_code == 200
    assert asked.status_code == 200
    assert [replica["status"] for replica in asked.json()["replicas"]] == [
        "up_to_date"
    ] * 3


def test_a_node_whose_own_repository_is_on_master_can_still_send_it(
    tmp_path: Path,
) -> None:
    """The source is `HEAD`, so the name this node uses does not matter.

    A repository adopted from a site arrives on whatever branch that site
    committed on, and it is this node's own until it is restarted.
    """
    source = _repository_on(tmp_path / "source", "master")
    source.commit(content="all: {}\n", message="inventory: one", author="alice")
    peer = _peer(source, tmp_path / "node2")
    source.commit(
        content="all:\n  hosts:\n    node1:\n      gateway_addr: 10.0.0.254\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )

    results = _service(source).replicate(
        _inventory("node1=ignored", f"node2={peer.path}"), this_host="node1"
    )

    assert results[0].status is Status.UPDATED
    assert "gateway_addr" in peer.read()
