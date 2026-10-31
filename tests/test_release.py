# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""tools/release, run for real against a throwaway repository.

The repository starts from this checkout's own version files, so a change to
their format that would stop the tool fails here rather than on a release day.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import __version__

_ROOT = Path(__file__).resolve().parent.parent
_TOOL = _ROOT / "tools" / "release"
_FILES = (
    "CHANGELOG.md",
    "app/__init__.py",
    "buildpush.sh",
    "docs/deployment.md",
    "seapath-webui.container",
)
_MAJOR, _MINOR, _PATCH = (int(part) for part in __version__.split("."))
_NEXT = f"{_MAJOR}.{_MINOR}.{_PATCH + 1}"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _commit(repo: Path, subject: str) -> None:
    _git(repo, "commit", "--allow-empty", "-m", subject)


def _release(
    repo: Path, *args: str, collection: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("SEAPATH_ANSIBLE_REF", None)
    env.pop("SEAPATH_ANSIBLE_REPOSITORY", None)
    if collection is not None:
        env["SEAPATH_ANSIBLE_REPOSITORY"] = str(collection)
    return subprocess.run(
        [sys.executable, str(_TOOL), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    checkout = tmp_path / "checkout"
    _git(tmp_path, "init", "--quiet", "-b", "main", str(checkout))
    _git(checkout, "config", "user.name", "Release Tester")
    _git(checkout, "config", "user.email", "tester@example.com")
    _git(checkout, "remote", "add", "origin", str(origin))
    for name in _FILES:
        target = checkout / name
        target.parent.mkdir(exist_ok=True)
        shutil.copy(_ROOT / name, target)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "feat: already released")
    _git(checkout, "commit", "--allow-empty", "-m", f"release: {__version__}")
    _git(checkout, "push", "--quiet", "origin", "main")
    return checkout


@pytest.fixture
def collection(tmp_path: Path) -> tuple[Path, str]:
    """A collection repository, with the head of the branch images are built from."""
    work = tmp_path / "collection"
    _git(tmp_path, "init", "--quiet", "-b", "seapathalloc", str(work))
    _git(work, "config", "user.name", "Release Tester")
    _git(work, "config", "user.email", "tester@example.com")
    _commit(work, "the collection")
    return work, _git(work, "rev-parse", "HEAD")


def test_the_commits_since_the_last_release_make_the_next_bugfix(repo: Path) -> None:
    _commit(repo, "feat: the first change")
    _commit(repo, "fix: the second change")

    done = _release(repo)

    assert done.returncode == 0, done.stderr
    changelog = (repo / "CHANGELOG.md").read_text()
    assert (
        f"\n\n## {_NEXT} - {datetime.date.today()}\n\n"
        "- feat: the first change\n"
        "- fix: the second change\n"
        f"\n## {__version__} - " in changelog
    )
    assert "already released" not in changelog
    assert f'__version__ = "{_NEXT}"' in (repo / "app/__init__.py").read_text()
    for name in ("seapath-webui.container", "docs/deployment.md"):
        text = (repo / name).read_text()
        assert f"seapath-webui:{_NEXT}" in text
        assert f"seapath-webui:{__version__}" not in text


def test_the_release_is_one_signed_commit_of_the_version_files(repo: Path) -> None:
    _commit(repo, "fix: a change")

    _release(repo)

    assert _git(repo, "log", "-1", "--format=%B") == (
        f"release: {_NEXT}\n\n" "Signed-off-by: Release Tester <tester@example.com>"
    )
    assert _git(repo, "show", "--format=", "--name-only").splitlines() == [
        "CHANGELOG.md",
        "app/__init__.py",
        "docs/deployment.md",
        "seapath-webui.container",
    ]
    assert _git(repo, "status", "--porcelain") == ""


def test_the_release_stays_local(repo: Path) -> None:
    _commit(repo, "fix: a change")

    _release(repo)

    assert _git(repo, "rev-list", "--count", "origin/main..HEAD") == "2"


def test_a_given_version_is_the_one_released(repo: Path) -> None:
    _commit(repo, "feat: a change")
    wanted = f"{_MAJOR}.{_MINOR + 1}.0"

    done = _release(repo, wanted)

    assert done.returncode == 0, done.stderr
    assert f"## {wanted} - " in (repo / "CHANGELOG.md").read_text()
    assert _git(repo, "log", "-1", "--format=%s") == f"release: {wanted}"


def test_no_commit_since_the_last_release_is_refused(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")

    done = _release(repo)

    assert done.returncode == 1
    assert "no commit since the last release" in done.stderr
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""


def test_force_with_no_commit_records_the_collection_head(
    repo: Path, collection: tuple[Path, str]
) -> None:
    url, head = collection

    done = _release(repo, "--force", collection=url)

    assert done.returncode == 0, done.stderr
    assert (
        f"## {_NEXT} - {datetime.date.today()}\n\n"
        f"- collection: seapathalloc {head[:8]}\n"
    ) in (repo / "CHANGELOG.md").read_text()


def test_force_with_an_unreachable_collection_changes_nothing(
    repo: Path, tmp_path: Path
) -> None:
    done = _release(repo, "--force", collection=tmp_path / "nowhere")

    assert done.returncode == 1
    assert _git(repo, "status", "--porcelain") == ""


def test_force_with_commits_records_the_commits(repo: Path) -> None:
    _commit(repo, "fix: a change")

    done = _release(repo, "--force")

    assert done.returncode == 0, done.stderr
    changelog = (repo / "CHANGELOG.md").read_text()
    assert "- fix: a change\n" in changelog
    assert "- collection: seapathalloc" not in changelog.split(f"## {__version__}")[0]


@pytest.mark.parametrize(
    "version",
    [
        __version__,
        f"{_MAJOR}.{_MINOR}.{_PATCH - 1}",
        f"{_MAJOR}.{_MINOR}",
        "v1.2.3",
        "1.2.3-rc1",
    ],
)
def test_a_version_that_is_not_above_the_current_one_is_refused(
    repo: Path, version: str
) -> None:
    _commit(repo, "fix: a change")

    done = _release(repo, version)

    assert done.returncode == 1
    assert _git(repo, "status", "--porcelain") == ""


def test_a_version_is_compared_by_number_rather_than_as_text(repo: Path) -> None:
    _commit(repo, "fix: a change")

    done = _release(repo, f"{_MAJOR}.{_MINOR}.{_PATCH * 10}")

    assert done.returncode == 0, done.stderr


def test_uncommitted_changes_are_refused(repo: Path) -> None:
    _commit(repo, "fix: a change")
    (repo / "CHANGELOG.md").write_text("edited")

    done = _release(repo)

    assert done.returncode == 1
    assert "uncommitted" in done.stderr
    assert (repo / "CHANGELOG.md").read_text() == "edited"


def test_another_branch_is_refused(repo: Path) -> None:
    _git(repo, "switch", "--quiet", "-c", "topic")
    _commit(repo, "fix: a change")

    done = _release(repo)

    assert done.returncode == 1
    assert "this is topic" in done.stderr


def test_a_remote_ahead_of_the_checkout_is_refused(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _git(tmp_path, "clone", "--quiet", str(tmp_path / "origin.git"), str(other))
    _git(other, "config", "user.name", "Release Tester")
    _git(other, "config", "user.email", "tester@example.com")
    _commit(other, "fix: landed elsewhere")
    _git(other, "push", "--quiet", "origin", "main")
    _commit(repo, "fix: a change")

    done = _release(repo)

    assert done.returncode == 1
    assert "origin/main" in done.stderr
