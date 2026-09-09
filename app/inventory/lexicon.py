# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Every name the installed collection uses, read off the collection itself.

`vocabulary.py` is 81 variables a human read and wrote prose for. That table
was built from the four reference inventories, and a real site inventory is far
richer than those four files: the first one this ran against reported
`apt_repo`, `nics_affinity`, `admin_ssh_keys`, `interfaces_to_wait_for` and
seven more as names no role reads, and every one of them is read by a role.
The reference inventories exercise some forty five variables, so passing
against them proved much less than it looked.

This is the other half, and it is derived rather than curated: the collection
this node actually runs, scanned for the identifiers it contains. It carries no
prose and no scope, and it is used for one question only, "does anything here
know this name", which is the question the assistant has to answer before it
tells an operator that nothing reads their variable.

Two sets, because two jobs pull in opposite directions.

`mentioned` is every identifier in every file of the tree, templates included:
`interfaces_to_wait_for` appears only in a `.j2`, and so does `ptp_vlanid`.
Reading it loosely is deliberate. A word in a comment marking a variable as
known costs silence about one name; a real variable missing from the set costs
a warning about working configuration, which is what teaches an operator to
ignore every warning this service prints.

`declared` is narrow on purpose: the keys of `defaults/main.yml` and
`vars/main.yml`, which are real variable names a role wrote down, plus the
curated table. It is what a suggestion is drawn from, where a wrong answer
given confidently is worse than no answer.

Read once per collection and cached against the fingerprint `catalogue` already
computes, so a reinstall is picked up and a page is not a tree walk.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.inventory.vocabulary import BY_NAME

# The files worth reading. A template is included because a role reads
# variables there and nowhere else.
_SUFFIXES = frozenset({".yml", ".yaml", ".j2", ".cfg", ".conf"})
# A guard rather than a limit that matters: the collection is a few hundred
# kilobytes of YAML, and a file larger than this is not one a role reads a
# variable out of.
_MAX_FILE_BYTES = 2 * 1024 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Below this, the tree is not a collection worth answering from. The real one
# holds some four thousand identifiers across four hundred files; a partial
# install, a clone whose submodules never came down, or a directory holding
# playbook files and nothing else lands far under it. The floor matters
# because of what the answer is used for: a lexicon that knows almost nothing
# reports almost every variable of a real inventory as read by no role, which
# is the failure this module exists to end rather than one to reintroduce
# from the other side.
_MINIMUM_IDENTIFIERS = 500


class Lexicon:
    """What one installed collection knows, as the two sets above."""

    def __init__(self, mentioned: frozenset[str], declared: frozenset[str]) -> None:
        self.mentioned = mentioned
        self.declared = declared

    def knows(self, name: str) -> bool:
        return name in self.mentioned


_cache: dict[tuple[str, str], Lexicon] = {}


def read(root: Path | None, fingerprint: str | None = None) -> Lexicon | None:
    """The lexicon of the collection at `root`, or `None` when there is none.

    `None` is the answer that matters. Without the collection this service
    cannot say that no role reads a name, so it says nothing at all rather than
    guessing from the curated table alone: on a laptop with no collection
    installed, that would report most of a real inventory.
    """
    if root is None:
        return None
    directory = Path(root)
    if not directory.is_dir():
        return None

    key = (str(directory), fingerprint or "")
    cached = _cache.get(key)
    if cached is not None:
        return cached

    mentioned: set[str] = set()
    declared: set[str] = set(BY_NAME)
    found = False
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix not in _SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        found = True
        mentioned.update(_IDENTIFIER.findall(text))
        if path.parent.name in ("defaults", "vars") and path.suffix in (
            ".yml",
            ".yaml",
        ):
            declared.update(_keys(text))

    if not found or len(mentioned) < _MINIMUM_IDENTIFIERS:
        # A directory that exists and holds no usable collection is the same
        # answer as no directory: too little was read to claim that a name is
        # read by nothing.
        return None

    lexicon = Lexicon(frozenset(mentioned), frozenset(declared))
    _cache[key] = lexicon
    return lexicon


def _keys(text: str) -> set[str]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return set()
    if not isinstance(loaded, dict):
        return set()
    return {key for key in loaded if isinstance(key, str)}


def forget() -> None:
    """Drop what was read, for a test that installs a collection twice."""
    _cache.clear()
