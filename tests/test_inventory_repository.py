# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The inventory repository, which is the configuration audit trail."""

from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from app.inventory.repository import InventoryRepository, StaleWrite


@pytest.fixture
def repository(tmp_path: Path) -> InventoryRepository:
    repository = InventoryRepository(tmp_path / "inventory")
    repository.initialise()
    return repository


def test_initialising_twice_is_harmless(tmp_path: Path) -> None:
    repository = InventoryRepository(tmp_path / "inventory")
    repository.initialise()
    repository.initialise()

    assert repository.exists()
    assert repository.head() is None


def test_a_commit_records_the_operator_who_made_it(
    repository: InventoryRepository,
) -> None:
    commit = repository.commit(
        content="all: {}\n",
        message="network: set gateway_addr on node1",
        author="alice",
    )

    assert commit is not None
    # `git log` answers "who changed the desired state" without anyone having
    # to trust a separate audit log.
    assert commit.author == "alice"
    assert commit.message == "network: set gateway_addr on node1"
    assert repository.head() == commit.hash


def test_committing_the_same_content_twice_creates_no_commit(
    repository: InventoryRepository,
) -> None:
    repository.commit(content="all: {}\n", message="first", author="alice")

    # An empty commit would be noise in the audit trail.
    assert (
        repository.commit(content="all: {}\n", message="again", author="alice") is None
    )


def test_a_write_from_a_stale_read_is_refused(
    repository: InventoryRepository,
) -> None:
    first = repository.commit(content="one\n", message="first", author="alice")
    assert first is not None
    repository.commit(content="two\n", message="second", author="bob")

    # Two operators on two browsers. Refusing beats merging: a silently merged
    # desired state is one nobody reviewed.
    with pytest.raises(StaleWrite, match="changed since you read it"):
        repository.commit(
            content="three\n",
            message="third",
            author="alice",
            expected_head=first.hash,
        )


def test_the_history_is_most_recent_first(repository: InventoryRepository) -> None:
    repository.commit(content="one\n", message="first", author="alice")
    repository.commit(content="two\n", message="second", author="bob")

    history = repository.history()

    assert [commit.message for commit in history] == ["second", "first"]
    assert [commit.author for commit in history] == ["bob", "alice"]


def test_reverting_produces_a_commit_and_does_not_apply_anything(
    repository: InventoryRepository,
) -> None:
    first = repository.commit(content="one\n", message="first", author="alice")
    assert first is not None
    repository.commit(content="two\n", message="second", author="bob")

    reverted = repository.revert(repository.head(), author="alice")

    assert repository.read() == "one\n"
    assert reverted.author == "alice"
    # Rollback is a new commit, never a rewritten history.
    assert len(repository.history()) == 3


def test_reading_an_older_version(repository: InventoryRepository) -> None:
    first = repository.commit(content="one\n", message="first", author="alice")
    assert first is not None
    repository.commit(content="two\n", message="second", author="bob")

    assert repository.read_at(first.hash) == "one\n"


def test_a_candidate_can_be_diffed_without_touching_the_working_tree(
    repository: InventoryRepository,
) -> None:
    repository.commit(content="one\n", message="first", author="alice")

    diff = repository.diff_against("two\n")

    assert "-one" in diff
    assert "+two" in diff
    # The preview is a question, and a question must not change the answer.
    assert repository.read() == "one\n"
    assert not list(repository.path.glob(".*candidate"))


def test_the_export_carries_the_history_a_control_machine_would_want(
    repository: InventoryRepository,
) -> None:
    repository.commit(content="one\n", message="first", author="alice")

    archive = tarfile.open(fileobj=BytesIO(repository.export()), mode="r:gz")
    names = archive.getnames()

    assert "seapath-inventory/inventory.yaml" in names
    # The git directory too: a site taking the inventory to a conventional
    # control machine wants the audit trail, not just the current file.
    assert any(name.startswith("seapath-inventory/.git/") for name in names)


def test_the_commit_is_read_without_running_git(
    repository: InventoryRepository,
) -> None:
    """The most asked question in the service, answered by a file read.

    Every panel of every page carries the commit it was drawn at, so drawing one
    page asked this eight times and paid eight forks of `git rev-parse` for it,
    on housekeeping CPUs beside real time guests. See D45.
    """
    made = repository.commit(
        content="all: {}\n", message="inventory: describe", author="alice"
    )
    assert made is not None

    def refuse(*arguments: str) -> str:
        raise AssertionError(f"git was run: {arguments}")

    repository._git = refuse  # type: ignore[method-assign]

    assert repository.head() == made.hash


def test_a_branch_git_has_packed_is_still_read(
    repository: InventoryRepository,
) -> None:
    """`git gc` moves a branch out of its own file, and every repository gets there."""
    made = repository.commit(
        content="all: {}\n", message="inventory: describe", author="alice"
    )
    assert made is not None
    git_dir = repository.path / ".git"
    reference = (git_dir / "HEAD").read_text().strip().removeprefix("ref: ")
    (git_dir / reference).unlink()
    (git_dir / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted \n"
        f"{made.hash} {reference}\n"
        f"^{made.hash}\n"
    )

    assert repository.head() == made.hash


def test_a_repository_this_cannot_read_still_answers(
    repository: InventoryRepository,
) -> None:
    """git itself is the authority, and the fallback is one fork slower.

    A layout this does not recognise must never cost a page its commit: what it
    costs is the process the direct read exists to avoid.
    """
    made = repository.commit(
        content="all: {}\n", message="inventory: describe", author="alice"
    )
    assert made is not None
    # A HEAD naming something this cannot resolve on its own.
    (repository.path / ".git" / "HEAD").write_text("ref: refs/heads/nowhere\n")

    assert repository.head() is None

    (repository.path / ".git" / "HEAD").write_text("something else entirely\n")

    assert repository.head() is None


def test_the_commit_a_write_is_refused_against_is_the_one_on_disk(
    repository: InventoryRepository,
) -> None:
    """A browser saving against a commit another has moved past is told."""
    repository.commit(
        content="all: {}\n", message="inventory: describe", author="alice"
    )
    second = repository.commit(
        content="all: {hosts: {}}\n",
        message="cluster: add node2",
        author="alice",
        expected_head=repository.head(),
    )

    assert second is not None
    assert repository.head() == second.hash
    with pytest.raises(StaleWrite):
        repository.commit(
            content="all: {hosts: {node3: {}}}\n",
            message="cluster: add node3",
            author="bob",
            expected_head="0" * 40,
        )
