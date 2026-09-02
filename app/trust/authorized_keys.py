# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Editing an `authorized_keys` file that belongs to someone else.

This is the single most destructive thing the service can do to a host, and it
is destructive by omission rather than by action: the file arrives from the ISO
seeded with the site key, which is how a conventional Ansible control machine
reaches the node. Rewriting it would lock that machine out on the first boot of
this service, and nobody would notice until they needed it.

So: whole lines, added and removed by their comment, everything else preserved
byte for byte. The module is deliberately small and deliberately paranoid.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Every line this service owns ends with a comment starting with this prefix.
# It is how our lines are told apart from the site key in a file we share.
COMMENT_PREFIX = "seapath-webui:"


@dataclass(frozen=True)
class AuthorizedKey:
    """One line: the options, the key, and the comment that identifies it."""

    comment: str
    public_key: str
    from_addresses: tuple[str, ...] = ()
    # A terminal, which `restrict` forbids by default. Set on the relation a
    # node has with itself, because that is the one the console connects over.
    # A run needs no terminal: the ISO sets `Defaults:ansible !requiretty` in
    # sudoers, so `become` never asks for one.
    allow_pty: bool = False

    def render(self) -> str:
        """The line exactly as it is written to the file.

        `restrict` disables forwarding, agent, X11 and tunnelling, and `pty`
        after it puts back exactly one of the things it turned off. Without it
        `sshd` answers "PTY allocation request failed on channel 0" and the
        console closes on the spot, since a terminal is the whole point of one.
        Re-enabling options must come after `restrict`, which is why the order
        below is the order it is.

        What `pty` grants is worth stating: nothing this key could not already
        do. It carries no `command=` restriction, so it can run
        `python3 -c "import pty; pty.spawn(...)"` and have a terminal anyway.

        There is no `command=` restriction, and that is honest rather than
        lazy. The sudoers rule the ISO ships grants `NOPASSWD:EXEC:SETENV:
        /bin/sh`, which is arbitrary root by construction, because that is how
        Ansible works. A command restriction here would look like a limit and
        be none.
        """
        options = []
        if self.from_addresses:
            options.append('from="' + ",".join(self.from_addresses) + '"')
        options.append("restrict")
        if self.allow_pty:
            options.append("pty")
        return f"{','.join(options)} {self.public_key} {self.comment}"


class MissingAccount(Exception):
    """The `ansible` account, or its `.ssh` directory, is not on this machine.

    The service does not create the account. A machine where it is missing is a
    machine that was not installed from the SEAPATH ISO, and inventing a user
    with privileges nobody reviewed is not a recovery, it is a second problem.
    """


def install(path: Path, key: AuthorizedKey) -> bool:
    """Add or update our line. Returns whether the file changed.

    An unchanged file is not rewritten at all, so the mtime of a file the site
    may be watching does not move for nothing.
    """
    lines = _read(path)
    kept: list[str] = []
    for line in lines:
        comment = _comment_of(line)
        if comment is None:
            # Not ours. Never touched, never parsed further.
            kept.append(line)
            continue
        if comment == key.comment:
            # The relation being installed, about to be rewritten.
            continue
        if _key_blob_of(line) == key.public_key:
            # One of ours carrying this very key under another name, which is
            # what a renamed node leaves behind. Authorising nothing and
            # explaining nothing, so it goes.
            continue
        kept.append(line)

    kept.append(key.render())
    if kept == lines:
        return False
    _write(path, kept)
    return True


def remove(path: Path, comment: str) -> bool:
    """Drop our line, identified by its comment. Returns whether it was there."""
    lines = _read(path)
    updated = [line for line in lines if _comment_of(line) != comment]
    if updated == lines:
        return False
    _write(path, updated)
    return True


def installed(path: Path) -> list[str]:
    """The comments of the lines this service owns, in file order."""
    return [
        comment
        for comment in (_comment_of(line) for line in _read(path))
        if comment and comment.startswith(COMMENT_PREFIX)
    ]


def _comment_of(line: str) -> str | None:
    """The trailing comment of a line, when it is one of ours.

    Only lines carrying our prefix are ever matched. A site key whose comment
    happens to be the last field of the line is never a candidate for removal,
    which is the property this whole module exists to guarantee.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    fields = stripped.split()
    candidate = fields[-1]
    return candidate if candidate.startswith(COMMENT_PREFIX) else None


def _key_blob_of(line: str) -> str | None:
    """The `ssh-ed25519 AAAA...` part of one of our lines.

    Only meaningful for lines this service wrote, which always carry options
    before the key and a comment after it.
    """
    fields = line.strip().split()
    if len(fields) < 4:
        return None
    return f"{fields[-3]} {fields[-2]}"


def _read(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except FileNotFoundError:
        if not path.parent.is_dir():
            raise MissingAccount(
                f"{path.parent} does not exist. The `ansible` account this "
                "service drives is missing, and it does not create accounts."
            ) from None
        return []
    except OSError as error:
        raise MissingAccount(f"{path} cannot be read: {error}") from error


def _write(path: Path, lines: list[str]) -> None:
    """Replace the file atomically, keeping its owner and its mode.

    Written next to the target and renamed, so a crash between the two leaves
    the old file intact rather than a truncated one: a half written
    `authorized_keys` is a machine nobody can reach.

    The owner is preserved explicitly. The file belongs to the `ansible`
    account and this service runs as root, so a fresh file would land
    root owned. `sshd` accepts that, but a file whose owner silently changed is
    a surprise waiting for whoever debugs the next problem.
    """
    payload = "\n".join(lines) + ("\n" if lines else "")
    original = path.stat() if path.exists() else None
    temporary = path.with_name(f".{path.name}.seapath-webui")

    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if original is not None:
            os.chmod(temporary, original.st_mode & 0o7777)
            _restore_owner(temporary, original.st_uid, original.st_gid)
        else:
            os.chmod(temporary, 0o600)
            parent = path.parent.stat()
            _restore_owner(temporary, parent.st_uid, parent.st_gid)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _restore_owner(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except PermissionError:
        # Only root may give a file away. Outside a container, in the tests,
        # the file already belongs to the right user.
        pass
