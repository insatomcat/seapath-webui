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
this node actually runs, read for what it says about its own variables. It
answers the question the assistant has to settle before it tells an operator
that nothing reads their variable, "does anything here know this name", and it
carries what the roles say about the names it knows.

Three products, because the jobs they serve pull in different directions.

`mentioned` is every identifier in every file of the tree, templates included:
`interfaces_to_wait_for` appears only in a `.j2`, and so does `ptp_vlanid`.
Reading it loosely is deliberate. A word in a comment marking a variable as
known costs silence about one name; a real variable missing from the set costs
a warning about working configuration, which is what teaches an operator to
ignore every warning this service prints.

`declared` is narrow on purpose: names a role wrote down as variables, which is
the keys of its `defaults` and `vars` files and the rows of its documented
variable table, plus the curated table. It is what a suggestion is drawn from,
where a wrong answer given confidently is worse than no answer.

`declarations` is `declared` with what the collection says about each name
beside it, which is what lets a completion offer a variable the curated table
has never heard of. Two sources, both written by whoever wrote the role:

- the `defaults` and `vars` keys again, for the role that declares the name and
  the value it falls back to;
- the variable table of `roles/<role>/README.md`, for the prose. The collection
  documents its variables in a markdown table whose first column is headed
  `Variable`, and that table is the only place a variable a role *requires*
  appears at all: a role that requires a variable does not default it, so the
  names a site actually writes are absent from every `defaults` file. Only that
  header is accepted, which is what keeps the tables listing metrics, paths and
  allocation strategies out of a list of variable names.

The README is read for its variable tables and for nothing else: its prose
stays out of `mentioned`, where every English word of it would answer "yes,
something here knows this name" for half the typos an operator can make.

Read once per collection and cached against the fingerprint `catalogue` already
computes, so a reinstall is picked up and a page is not a tree walk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
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

# A collection that carries a collection tree of its own is read for its own
# roles and for nothing else. The image builds the SEAPATH collection from a
# working tree where `prepare.sh` has already installed the dependencies under
# `collections/`, and `build_ignore` does not drop that directory, so the
# artefact ships `community.general`, `ansible.posix`, `containers.podman` and
# `openstack` inside itself. Reading them costs twice: a completion offers
# variables of roles no SEAPATH playbook runs, and `mentioned` grows four
# collections of identifiers, which is the set that decides whether to warn
# that nothing reads a name. A site installing its own collection under
# `site_collections_dir` is not built here at all, so the guard belongs in the
# walk rather than in the packaging.
_NESTED = "ansible_collections"

# A markdown table is a header row, a rule, then the rows. The rule is what
# says the row above it was a header.
_TABLE_RULE = re.compile(r"^\|[\s:|-]+\|$")
# The one header that means the rows below are variables. `Metric`, `Path`,
# `Strategy` and `Criterion` head tables in the same READMEs, and `Member
# variable` heads the keys of one entry of a list rather than a variable of an
# inventory.
_VARIABLE_HEADERS = frozenset({"variable", "variables"})
_SUMMARY_HEADERS = ("comments", "description", "purpose")
_DEFAULT_HEADERS = ("default", "defaults")
_TYPE_HEADERS = ("type",)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
# Long enough for the sentence that says what the variable does, short enough
# that one row of a completion list stays one row.
_SUMMARY_LIMIT = 200


@dataclass(frozen=True)
class Declaration:
    """One variable, as the collection itself declares it.

    Every field is copied from the collection and none is interpreted here.
    `kind` is the word the README's Type column uses, which is prose rather
    than a schema: mapping it onto something a completion can punctuate is the
    vocabulary's job.
    """

    name: str
    role: str = ""
    kind: str = ""
    default: str = ""
    summary: str = ""


class Lexicon:
    """What one installed collection knows, as the sets above."""

    def __init__(
        self,
        mentioned: frozenset[str],
        declared: frozenset[str],
        declarations: dict[str, Declaration] | None = None,
    ) -> None:
        self.mentioned = mentioned
        self.declared = declared
        self.declarations = declarations or {}

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
    documented: dict[str, Declaration] = {}
    defaulted: dict[str, Declaration] = {}
    found = False
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if _NESTED in path.relative_to(directory).parts:
            continue
        if path.name == "README.md":
            text = _text(path)
            if text is not None:
                _keep(documented, _documented(text, _role_of(path, directory)))
            continue
        if path.suffix not in _SUFFIXES:
            continue
        text = _text(path)
        if text is None:
            continue
        found = True
        mentioned.update(_IDENTIFIER.findall(text))
        if path.parent.name in ("defaults", "vars") and path.suffix in (
            ".yml",
            ".yaml",
        ):
            _keep(defaulted, _defaulted(text, _role_of(path, directory)))

    if not found or len(mentioned) < _MINIMUM_IDENTIFIERS:
        # A directory that exists and holds no usable collection is the same
        # answer as no directory: too little was read to claim that a name is
        # read by nothing.
        return None

    declarations = _merged(documented, defaulted)
    declared = set(BY_NAME) | set(declarations)
    # A name a README documents is a name this collection knows, even where no
    # task file happens to spell it out. The prose around it stays out: the
    # question `mentioned` answers is asked of variable names, and answering it
    # with the English of a README would answer yes to half the typos.
    mentioned |= set(declarations)
    lexicon = Lexicon(frozenset(mentioned), frozenset(declared), declarations)
    _cache[key] = lexicon
    return lexicon


def _text(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _role_of(path: Path, root: Path) -> str:
    """The role a file belongs to, which is a directory name and nothing more.

    Empty for everything the collection keeps outside `roles/`, the
    distribution `vars` at the top of the tree among them. A variable declared
    there is still declared; naming a role that does not exist would be worse
    than naming none.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return ""
    return parts[1] if len(parts) > 2 and parts[0] == "roles" else ""


def _keep(found: dict[str, Declaration], new: dict[str, Declaration]) -> None:
    """First declaration wins, which with a sorted walk is the first role.

    Three names are declared by two roles apiece, all of them hardening paths
    with the same value on both sides. Answering with one of them is right;
    answering with whichever the walk happened to finish on is not.
    """
    for name, declaration in new.items():
        found.setdefault(name, declaration)


def _merged(
    documented: dict[str, Declaration], defaulted: dict[str, Declaration]
) -> dict[str, Declaration]:
    """The two sources joined, each keeping what it is the authority on.

    The README has the prose and the type. The `defaults` file has the value
    the role actually falls back to, where the README often has a sentence
    about it instead, and it has the role that declares the name rather than
    the one that documents it.
    """
    names = sorted(set(documented) | set(defaulted))
    joined: dict[str, Declaration] = {}
    for name in names:
        prose = documented.get(name)
        value = defaulted.get(name)
        if prose is None:
            joined[name] = value  # type: ignore[assignment]
        elif value is None:
            joined[name] = prose
        else:
            joined[name] = replace(
                prose,
                role=value.role or prose.role,
                kind=prose.kind or value.kind,
                default=value.default or prose.default,
            )
    return joined


def _defaulted(text: str, role: str) -> dict[str, Declaration]:
    """What one `defaults` or `vars` file declares, with the value it holds."""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        name: Declaration(
            name=name,
            role=role,
            kind=_kind_of(value),
            default=_literal(value),
        )
        for name, value in loaded.items()
        if isinstance(name, str) and _NAME.fullmatch(name)
    }


def _documented(text: str, role: str) -> dict[str, Declaration]:
    """The variable tables of one README, and none of its other tables."""
    found: dict[str, Declaration] = {}
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if index == 0 or not _TABLE_RULE.match(line):
            continue
        header = _cells(lines[index - 1])
        if not header or _word(header[0]) not in _VARIABLE_HEADERS:
            continue
        columns = {_word(cell): at for at, cell in enumerate(header)}
        for row in lines[index + 1 :]:
            if not row.startswith("|"):
                break
            cells = _cells(row)
            if not cells:
                break
            name = _bare(cells[0])
            if not _NAME.fullmatch(name):
                continue
            found.setdefault(
                name,
                Declaration(
                    name=name,
                    role=role,
                    kind=_bare(_column(cells, columns, _TYPE_HEADERS)).lower(),
                    default=_prose(_column(cells, columns, _DEFAULT_HEADERS), 80),
                    summary=_prose(
                        _column(cells, columns, _SUMMARY_HEADERS), _SUMMARY_LIMIT
                    ),
                ),
            )
    return found


def _cells(row: str) -> list[str]:
    """One markdown row, split on the pipes that are not escaped."""
    if not row.startswith("|"):
        return []
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row.strip("|"))]


def _word(cell: str) -> str:
    """A header cell, as a word to compare against."""
    return _bare(cell).lower()


def _bare(cell: str) -> str:
    return cell.strip().strip("`*_ ").strip()


def _column(cells: list[str], columns: dict[str, int], wanted: tuple[str, ...]) -> str:
    for header in wanted:
        at = columns.get(header)
        if at is not None and at < len(cells):
            return cells[at]
    return ""


def _prose(cell: str, limit: int) -> str:
    """A markdown cell as one line of text, which is how it will be read."""
    flattened = _BREAK.sub(" ", cell)
    flattened = _LINK.sub(r"\1", flattened)
    flattened = flattened.replace("`", "").replace("*", "").replace("\\|", "|")
    flattened = " ".join(flattened.split())
    if len(flattened) > limit:
        # Cut on a word, and never in the middle of one.
        flattened = flattened[:limit].rsplit(" ", 1)[0]
    return flattened


def _kind_of(value: object) -> str:
    """The YAML shape of a default, which is a fair guess at the variable's.

    Nothing for a key declared with no value: a role writing `name:` and
    stopping has said the name exists and nothing else.
    """
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            return "entries"
        return "list"
    if isinstance(value, dict):
        return "mapping"
    if isinstance(value, str):
        return "string"
    return ""


def _literal(value: object) -> str:
    """A default as the role writes it, on one line.

    A jinja expression is left exactly as it is. `{{ cephadm_installbinary |
    default(false) }}` is what the role falls back to, and rendering it as
    anything else would be this service inventing a value.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | str):
        rendered = " ".join(str(value).split())
    else:
        rendered = yaml.safe_dump(value, default_flow_style=True, width=10**6).strip()
    return rendered if len(rendered) <= 80 else rendered[:80].rsplit(" ", 1)[0]


def forget() -> None:
    """Drop what was read, for a test that installs a collection twice."""
    _cache.clear()
