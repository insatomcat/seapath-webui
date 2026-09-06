# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The inventory repository.

A git repository per node, holding the inventory and the files it references.
The commit hash is the version of the desired state, `git log` is the
configuration audit trail, and `git revert` is the rollback. There is no
database anywhere in this service, and this is why one is not needed.

The repository is exportable and clonable as is. A site that decides it wants a
conventional Ansible control machine clones this and loses nothing, which is
the escape hatch that makes the whole design safe to adopt.

**The inventory is rarely alone.** `upload_extra_files`, `iptables`,
`syslog_ng_client`, `cephadm` and the VM roles all take a path to a file the
control machine holds, written in the inventory as an ordinary variable. A
repository holding `inventory.yaml` and nothing else could carry the desired
state of a machine that no playbook could actually converge, so this holds a
whole tree and versions all of it.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.inventory import files as tree

logger = logging.getLogger(__name__)

INVENTORY_FILENAME = "inventory.yaml"

# The one branch this service writes, and the one a replication moves.
DEFAULT_BRANCH = "main"

# How a replication says it is a forced one, and why it takes two hooks to hear
# it. Git hands the push options to `pre-receive` and does not hand them to
# `push-to-checkout`, which is the hook that owns the working tree. So the
# first records the request for the commit it arrives with, and the second
# consumes it. Both run in the same `git-receive-pack`, in that order.
#
# `push-to-checkout` replaces git's own `updateInstead` behaviour entirely,
# which is why its ordinary path reproduces it exactly, refusal on an untracked
# file included. Only the forced path differs, and `read-tree --reset` is what
# makes it differ: it writes the incoming tree over whatever is there.
_FORCE_OPTION = "seapath-webui:force"
_FORCE_MARKER = "seapath-webui-force"

_PRE_RECEIVE_HOOK = rf"""#!/bin/sh
# Written by seapath-webui. Records a forced replication for push-to-checkout,
# which git does not hand the push options to.
dir="$(git rev-parse --git-dir)/{_FORCE_MARKER}"
rm -rf "$dir"
i=0
while [ "$i" -lt "${{GIT_PUSH_OPTION_COUNT:-0}}" ]; do
    eval "value=\$GIT_PUSH_OPTION_$i"
    if [ "$value" = "{_FORCE_OPTION}" ]; then
        mkdir -p "$dir"
        while read -r _old new _ref; do
            [ -n "$new" ] && : > "$dir/$new"
        done
    fi
    i=$((i + 1))
done
exit 0
"""

_PUSH_TO_CHECKOUT_HOOK = rf"""#!/bin/sh
# Written by seapath-webui. The checkout a replication performs on this
# machine, replacing git's own updateInstead behaviour.
commit="$1"
dir="$(git rev-parse --git-dir)/{_FORCE_MARKER}"
forced=0
[ -e "$dir/$commit" ] && forced=1
rm -rf "$dir"

if [ "$forced" -eq 1 ]; then
    # The operator asked for this node's copy to win, so the incoming tree is
    # written over whatever is here, a file nobody committed included.
    exec git read-tree -u --reset "$commit"
fi

# Git's own behaviour, reproduced: a file nobody committed is never overwritten
# by an ordinary replication.
if head=$(git rev-parse --verify --quiet HEAD); then
    exec git read-tree -u -m "$head" "$commit"
fi
exec git read-tree -u -m "$(git hash-object -t tree /dev/null)" "$commit"
"""

# A git command that waits on another machine. Long enough for a slow link,
# short enough that a page listing three nodes still renders when one is down.
_NETWORK_TIMEOUT_SECONDS = 30.0

_COMMITTER_NAME = "seapath-webui"
_LOG_FORMAT = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s"


class RepositoryError(Exception):
    """A git operation failed. The message carries git's own words."""


class StaleWrite(Exception):
    """HEAD moved since the caller read it.

    Two operators editing the same inventory from two browsers is the ordinary
    case this prevents, and it prevents it by refusing rather than by merging:
    a silently merged desired state is one nobody reviewed.
    """


@dataclass(frozen=True)
class Commit:
    hash: str
    author: str
    email: str
    timestamp: datetime
    message: str


class InventoryRepository:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def inventory_file(self) -> Path:
        return self._path / INVENTORY_FILENAME

    # Lifecycle

    def exists(self) -> bool:
        return (self._path / ".git").is_dir()

    def initialise(self) -> None:
        """Create the repository if this is the first boot. Idempotent."""
        if self.exists():
            return
        self._path.mkdir(parents=True, exist_ok=True)
        self._git("init", "--initial-branch=main")
        self.accept_replication()
        logger.info("Initialised the inventory repository at %s", self._path)

    def accept_replication(self) -> None:
        """Let a peer's push land in this checkout. Idempotent, and a repair.

        Two things have to be true, and a repository made by an earlier version
        of this service has neither, so both are applied at every start rather
        than at creation only.

        **The setting.** This repository has a worktree, and git refuses by
        default to push into the branch a worktree has checked out.
        `updateInstead` is the setting for exactly this shape: the files move
        with the push, and the push is refused when the worktree carries
        changes nobody committed. That refusal is the safety, so it is
        configured rather than worked around.

        **The branch.** A push lands in the worktree only when it targets the
        branch that worktree has checked out. A repository sitting on `master`
        therefore takes the commit into a branch nobody serves and leaves its
        files exactly as they were, which is a replication that reports success
        and changes nothing. Every node serves `main`, so a repository found on
        another branch is moved to it.

        **The hooks.** They are what makes a forced replication reach the files
        of this machine, and `_FORCE_OPTION` says why they are a pair.
        """
        for name, value in (
            ("receive.denyCurrentBranch", "updateInstead"),
            # What lets a peer say that its replication is a forced one. Git
            # carries a push option to `pre-receive` and nowhere else, which is
            # the whole reason there are two hooks below.
            ("receive.advertisePushOptions", "true"),
        ):
            try:
                self._git("config", name, value)
            except RepositoryError as error:  # pragma: no cover - defensive
                logger.warning("Could not set %s on the repository: %s", name, error)
        self._install_hooks()
        self._serve_default_branch()

    def _install_hooks(self) -> None:
        """Write the two hooks a replication lands through.

        This service owns this repository outright, so the hooks are written
        rather than merged, and rewritten at every start so a node that was
        updated gains them.
        """
        directory = self._path / ".git" / "hooks"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for name, script in (
                ("pre-receive", _PRE_RECEIVE_HOOK),
                ("push-to-checkout", _PUSH_TO_CHECKOUT_HOOK),
            ):
                target = directory / name
                target.write_text(script)
                target.chmod(0o755)
        except OSError as error:  # pragma: no cover - defensive
            logger.warning("Could not install the replication hooks: %s", error)

    def _serve_default_branch(self) -> None:
        try:
            current = self._git("symbolic-ref", "--quiet", "HEAD").strip()
        except RepositoryError:  # pragma: no cover - a detached HEAD
            logger.warning("This repository has no branch checked out")
            return
        if current == f"refs/heads/{DEFAULT_BRANCH}":
            return
        try:
            if self.head() is None:
                # Nothing committed yet, so the branch is a name and moving it
                # loses nothing.
                self._git("symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}")
            else:
                self._git("branch", "--move", DEFAULT_BRANCH)
        except RepositoryError as error:
            logger.warning(
                "This repository serves %s rather than %s, and could not be "
                "moved, so a replication would not reach its files: %s",
                current,
                DEFAULT_BRANCH,
                error,
            )
            return
        logger.info("Moved the inventory repository from %s to main", current)

    # Reading

    def head(self) -> str | None:
        """The current commit, or None on a repository with no commit yet."""
        try:
            return self._git("rev-parse", "HEAD").strip()
        except RepositoryError:
            return None

    def read(self) -> str:
        try:
            return self.inventory_file.read_text()
        except FileNotFoundError:
            return ""

    def read_at(self, commit: str) -> str:
        return self._git("show", f"{commit}:{INVENTORY_FILENAME}")

    def history(self, limit: int = 50) -> list[Commit]:
        try:
            output = self._git(
                "log", f"--max-count={limit}", f"--pretty=format:{_LOG_FORMAT}"
            )
        except RepositoryError:
            return []
        return [_parse_commit(line) for line in output.splitlines() if line]

    def diff(self, from_ref: str | None = None, to_ref: str | None = None) -> str:
        """A unified diff of the whole folder, defaulting to HEAD.

        The whole folder rather than the inventory alone: a commit that
        replaced the quadlet a machine runs is a change to what the next
        convergence pushes, and a history that showed only `inventory.yaml`
        would not say so.
        """
        if from_ref and to_ref:
            return self._git("diff", from_ref, to_ref)
        if from_ref:
            return self._git("diff", from_ref)
        return self._git("diff", "HEAD")

    def diff_against(self, candidate: str) -> str:
        """What committing this content would change, without writing it.

        The preview an operator sees before an apply. Produced with
        `git diff --no-index` against a temporary file, so the working tree is
        never touched by a question.
        """
        temporary = self._path / f".{INVENTORY_FILENAME}.candidate"
        temporary.write_text(candidate)
        try:
            # --no-index exits 1 when the files differ, which is not an error.
            result = self._run(
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--",
                    INVENTORY_FILENAME if self.inventory_file.exists() else "/dev/null",
                    temporary.name,
                ],
                allowed_returncodes=(0, 1),
            )
            return result.replace(temporary.name, INVENTORY_FILENAME)
        finally:
            temporary.unlink(missing_ok=True)

    # The files beside the inventory

    def files(self) -> list[tree.StoredFile]:
        """Every file the folder holds, the inventory included."""
        return [tree.describe(self._path, path) for path in tree.walk(self._path)]

    def file_path(self, path: str) -> Path:
        """Where a named file sits, refusing anything outside the folder."""
        return tree.resolve_within(self._path, path)

    def read_file(self, path: str) -> bytes:
        return self.file_path(path).read_bytes()

    def write_file(
        self,
        path: str,
        content: bytes,
        message: str,
        author: str,
    ) -> Commit | None:
        """Write one file and commit it. Returns None when nothing changed."""
        target = self.file_path(path)
        self.initialise()
        if target.exists() and target.read_bytes() == content:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return self._commit_path(target, message, author)

    def delete_file(self, path: str, message: str, author: str) -> Commit | None:
        target = self.file_path(path)
        if not target.exists():
            return None
        target.unlink()
        commit = self._commit_path(target, message, author)
        # An empty directory is not a thing git records, so leaving one behind
        # would make the folder on disk and the folder in a fresh clone differ.
        parent = target.parent
        while parent != self._path and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        return commit

    # Writing

    def commit(
        self,
        content: str,
        message: str,
        author: str,
        expected_head: str | None = None,
    ) -> Commit | None:
        """Write the inventory and commit it. Returns None when nothing changed.

        `expected_head` is the `If-Match` of the API: a caller that read the
        inventory at one commit and writes back at another is told rather than
        allowed to overwrite a change it never saw.
        """
        self.initialise()
        current = self.head()
        if expected_head is not None and current != expected_head:
            raise StaleWrite(
                f"The inventory changed since you read it. It is now at "
                f"{current or 'no commit'}, you sent {expected_head}."
            )

        if self.inventory_file.exists() and self.inventory_file.read_text() == content:
            return None

        self.inventory_file.write_text(content)
        self._git("add", INVENTORY_FILENAME)
        # The authenticated operator is the author, the service is the
        # committer. `git log` then answers "who changed the desired state"
        # without anyone having to trust a separate audit log.
        self._git(
            "commit",
            "--message",
            message,
            "--author",
            f"{author} <{author}@{_COMMITTER_NAME}>",
        )
        commit = self.history(limit=1)[0]
        logger.info("Inventory commit %s by %s: %s", commit.hash[:12], author, message)
        return commit

    def _commit_path(self, target: Path, message: str, author: str) -> Commit | None:
        relative = target.relative_to(self._path).as_posix()
        self._git("add", "--all", "--", relative)
        # `git add` on a file whose content is already what HEAD holds stages
        # nothing, and committing then would be an empty commit in the audit
        # trail.
        if not self._git("diff", "--cached", "--name-only").strip():
            return None
        self._git(
            "commit",
            "--message",
            message,
            "--author",
            f"{author} <{author}@{_COMMITTER_NAME}>",
        )
        commit = self.history(limit=1)[0]
        logger.info(
            "Inventory commit %s by %s: %s", commit.hash[:12], author, commit.message
        )
        return commit

    def record(self, message: str, author: str) -> Commit:
        """Write an event that changed no file into the audit trail.

        A commit with no diff, which is exactly what it describes: the desired
        state did not move, and something else did. Installing a collection is
        the case it exists for. That changes the code the next apply runs
        without touching a single variable, and the repository is where this
        service already answers "who changed what, and when", so it is where an
        operator will look for it.
        """
        self.initialise()
        self._git(
            "commit",
            "--allow-empty",
            "--message",
            message,
            "--author",
            f"{author} <{author}@{_COMMITTER_NAME}>",
        )
        commit = self.history(limit=1)[0]
        logger.info("Inventory event %s by %s: %s", commit.hash[:12], author, message)
        return commit

    def revert(self, commit: str, author: str) -> Commit:
        """Create a revert commit. It is not applied: that is a separate act."""
        self._git(
            "-c",
            f"user.name={author}",
            "-c",
            f"user.email={author}@{_COMMITTER_NAME}",
            "revert",
            "--no-edit",
            commit,
        )
        return self.history(limit=1)[0]

    def export(self) -> bytes:
        """The whole repository as a tarball, git directory included.

        Git directory included on purpose: what a site wants when it takes the
        inventory to a conventional control machine is the history, not just
        the current file.
        """
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            archive.add(self._path, arcname="seapath-inventory")
        return buffer.getvalue()

    # Replication

    def contains(self, commit: str) -> bool:
        """Whether this repository already holds that commit.

        What tells "the peer is behind us" from "the peer carries work we have
        never seen", which is the difference between a push that will land and
        one git is about to refuse.
        """
        try:
            self._git("cat-file", "-e", f"{commit}^{{commit}}")
        except RepositoryError:
            return False
        return True

    def remote_state(
        self,
        url: str,
        *,
        ssh_command: str | None = None,
        upload_pack: str | None = None,
        timeout: float = _NETWORK_TIMEOUT_SECONDS,
    ) -> RemoteState:
        """What that machine serves: the branch its worktree is on, and where.

        `HEAD` rather than a branch name of our choosing, because what the
        machine's own service reads is its worktree, and the worktree is
        whatever `HEAD` points at. Asking for `refs/heads/main` instead would
        answer for a branch that machine may hold without serving, which reads
        as a copy that is up to date while its files are something else.

        Both halves come back empty on a repository with no commit, since a
        `HEAD` pointing at a branch nobody created is advertised by nothing. A
        machine that cannot be reached, or that holds no repository at all,
        raises with git's own words.
        """
        argv = ["git", "ls-remote", "--symref"]
        if upload_pack is not None:
            argv.append(f"--upload-pack={upload_pack}")
        argv += [url, "HEAD"]
        output = self._run(argv, environment=_with_ssh(ssh_command), timeout=timeout)
        return _parse_remote_state(output)

    def push(
        self,
        url: str,
        *,
        ssh_command: str | None = None,
        receive_pack: str | None = None,
        target: str = f"refs/heads/{DEFAULT_BRANCH}",
        force: bool = False,
        timeout: float = _NETWORK_TIMEOUT_SECONDS,
    ) -> None:
        """Send this branch to that machine, or raise with git's refusal.

        The target is the ref that machine serves rather than a name assumed
        here: a push into any other branch updates a ref nobody reads and
        leaves the machine's files untouched.

        Git accepts a fast forward only, so a machine carrying commits this one
        has never seen ends the push with an error naming it, and its history
        is still there afterwards. `force` is the operator overriding exactly
        that: the machine's branch is moved to this node's commit whatever it
        held, and what it held is reachable there only through its reflog. It
        carries a push option the receiving hooks read, so a forced replication
        writes that machine's files over whatever is in them.
        """
        argv = ["git", "push"]
        if force:
            argv += ["--force", f"--push-option={_FORCE_OPTION}"]
        if receive_pack is not None:
            argv.append(f"--receive-pack={receive_pack}")
        # `HEAD` as the source rather than a branch name: this node commits to
        # whatever it has checked out, and a repository adopted from a site may
        # be on any branch. What it is called here has no bearing on what the
        # machine receiving it serves.
        argv += [url, f"HEAD:{target}"]
        self._run(argv, environment=_with_ssh(ssh_command), timeout=timeout)

    # Plumbing

    def _git(self, *arguments: str) -> str:
        return self._run(["git", *arguments])

    def _run(
        self,
        argv: list[str],
        allowed_returncodes: tuple[int, ...] = (0,),
        environment: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, never a shell
                argv,
                cwd=self._path,
                capture_output=True,
                text=True,
                check=False,
                env=self._environment(environment),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            # Only the replication passes a timeout, and it is the one git
            # command that waits on another machine.
            raise RepositoryError(
                f"no answer after {error.timeout:.0f} seconds"
            ) from error
        if completed.returncode not in allowed_returncodes:
            raise RepositoryError(
                (completed.stderr or completed.stdout).strip()
                or f"{' '.join(argv)} failed with {completed.returncode}"
            )
        return completed.stdout

    def _environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_COMMITTER_NAME": _COMMITTER_NAME,
                "GIT_COMMITTER_EMAIL": f"{_COMMITTER_NAME}@localhost",
                # The repository is root owned inside the container and the
                # process is root, but a bind mount can still look dubious to
                # git. This is a repository the service owns outright.
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(self._path),
            }
        )
        environment.update(extra or {})
        return environment


@dataclass(frozen=True)
class RemoteState:
    """What one machine answers about the repository it serves."""

    branch: str | None = None
    """The ref its worktree is on, as `refs/heads/main`. None when unborn."""

    commit: str | None = None
    """The commit that ref points at, which is what its files are."""


def _parse_remote_state(output: str) -> RemoteState:
    branch = None
    commit = None
    for line in output.splitlines():
        value, _, name = line.partition("\t")
        if name.strip() != "HEAD":
            continue
        if value.startswith("ref: "):
            branch = value[len("ref: ") :].strip()
        else:
            commit = value.strip()
    return RemoteState(branch=branch, commit=commit)


def _with_ssh(ssh_command: str | None) -> dict[str, str]:
    """How git is told which ssh to run, and with which key.

    The connection credentials are a fact about this control machine rather
    than about the desired state, which is why they travel in the environment
    of one command and are written nowhere.
    """
    return {} if ssh_command is None else {"GIT_SSH_COMMAND": ssh_command}


def _parse_commit(line: str) -> Commit:
    commit_hash, author, email, timestamp, message = line.split("\x1f", 4)
    return Commit(
        hash=commit_hash,
        author=author,
        email=email,
        timestamp=datetime.fromisoformat(timestamp),
        message=message,
    )
