# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The root password a guest is created with, and why it lives only here.

A guest reached at its console needs an account with a password, which is what
[D52](../../docs/decisions.md) asks the add form for. Everything else that form
collects becomes an inventory variable, because the inventory is the desired
state and git is the audit trail. A password hash cannot follow that path. It
would be committed, replicated to every machine the file declares, and readable
there by everybody the repository is, for as long as the repository lives, and
an attacker holding it gets to try offline and without a rate limit. Taking it
out afterwards changes none of that: git keeps what it was given.

So the hash goes where it is read and nowhere else. The deployment run already
copies the inventory folder into its own directory, frozen (`app.runs.staging`),
and that copy is what Ansible reads. This splices the `users` member into that
copy, after the staging and before the play, and wipes it when the run ends.
The repository is never written, committed or otherwise.

What is left behind:

- The guest's entry, in git, with its seed and no password in it.
- The run's copy, on this node, carrying the hash while the play runs and the
  original seed afterwards. The window is one run long.

The password itself is held nowhere at any point: it arrives on the request
that launches the run, is hashed on the way in, and the plain text is gone when
that call returns. It reaches no log, no run record and no `extravars`, which
is the file `ansible-runner` writes into the run directory and the command the
run view shows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.inventory import cloudinit
from app.inventory.editor import (
    UneditableInventory,
    edit,
    guest_entries,
    guest_entry,
)

logger = logging.getLogger(__name__)

SEED = "cloud_init"
USERS = "users"


class SeedRefused(ValueError):
    """The password cannot be spliced into the run's copy of the inventory."""


def carrying_passwords(document: str, passwords: dict[str, str]) -> str:
    """The inventory, with a root password in the seed of each guest named.

    One `users` member per guest, beside the account the entry already names
    where it names one. The guest has to be declared: a name the file does not
    carry is a password about to be written into nothing, and the run would
    create the guest without it and say nothing about why the console refuses.

    The document is edited line by line, like every other write to this file,
    so the copy the run reads stays the copy an operator can diff against the
    repository afterwards.
    """
    changes: dict[str, dict[str, Any]] = {}
    for guest, password in sorted(passwords.items()):
        entry = guest_entry(document, guest)
        if entry is None:
            raise SeedRefused(
                f"{guest} is not declared in this inventory, so there is no "
                "seed to give a root password to."
            )
        seed = dict(entry.get(SEED) or {})
        users = [dict(user) for user in seed.get(USERS) or []]
        # Replaced rather than added to: a seed already naming root is one
        # this ran against once, and two members for one account is a seed
        # cloud-init applies in whichever order it read them.
        users = [user for user in users if user.get("name") != "root"]
        users.append(cloudinit.root_user(password))
        seed[USERS] = users
        changes[guest] = {SEED: seed}

    if not changes:
        return document
    try:
        return edit(document, changes)
    except UneditableInventory as error:
        raise SeedRefused(
            f"The root password could not be written into the run's copy of "
            f"the inventory: {error}"
        ) from error


def without_passwords(document: str) -> str:
    """The same inventory with every hashed password taken back out.

    Written over the run's copy when the run ends, whatever the run's state:
    the play has read the seed by then, and what is left is a file on this node
    holding a hash nothing reads again. A member is judged by its
    `hashed_passwd` rather than by its name, so a seed the site wrote by hand
    with a password on another account is wiped too, and the account named by
    key alone is left exactly as it was.

    A seed whose `users` list is emptied by this loses the list, which is what
    the entry carried before the splice.
    """
    changes: dict[str, dict[str, Any]] = {}
    for guest, entry in _entries(document).items():
        seed = dict(entry.get(SEED) or {})
        users = [dict(user) for user in seed.get(USERS) or []]
        kept = [user for user in users if "hashed_passwd" not in user]
        if len(kept) == len(users):
            continue
        if kept:
            seed[USERS] = kept
        else:
            seed.pop(USERS, None)
        changes[guest] = {SEED: seed}

    if not changes:
        return document
    return edit(document, changes)


def wipe(inventory_file: Path) -> bool:
    """Take the hashes out of a run's copy of the inventory, on disk.

    True when something was written. Never raises: this runs at the end of a
    run, where the run's own outcome is what the operator is waiting for, and a
    copy that could not be rewritten is worth a log line rather than an error
    on a deployment that worked. The file is one this service wrote, in a
    directory it owns.
    """
    try:
        document = inventory_file.read_text()
        cleaned = without_passwords(document)
    except (OSError, UneditableInventory, ValueError):
        logger.exception("Could not read %s to wipe its hashes", inventory_file)
        return False
    if cleaned == document:
        return False
    try:
        inventory_file.write_text(cleaned)
    except OSError:
        logger.exception("Could not wipe the hashes out of %s", inventory_file)
        return False
    logger.info("Wiped the seed passwords out of %s", inventory_file)
    return True


def _entries(document: str) -> dict[str, dict[str, Any]]:
    try:
        return guest_entries(document)
    except UneditableInventory:
        return {}
