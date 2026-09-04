# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What the installed collection holds, read from the collection.

`catalogue.py` is the reviewed half: entries a human wrote sentences for. This
is the derived half. It opens every playbook the collection ships, follows its
`import_playbook` chain and its roles, and answers the questions the UI has to
ask before it may offer a button:

- **which machines a run reaches**, read off the `hosts:` lines of the plays;
- **what check mode is worth**, read off the modules the tasks use;
- **whether it reboots**, and which variables hold every reboot back;
- **which variables the playbook refuses to start without**, read off the
  `fail` tasks guarded by `... is undefined` and off a `hosts:` line built from
  a variable, which are the two ways this collection asks for an input.

One question was asked here and withdrawn: whether a preview would crash on a
task reading the output of a command check mode skipped. It is answerable, and
the answer is useless. Nearly every playbook imports `detect_seapath_distro`,
which registers a `grep` and reads its `rc` two tasks later inside a block
guarded by a condition that is almost never true, so the check fired on twenty
of the collection's twenty-six playbooks. Telling a rarely taken guarded path
apart from a real dependency needs the run, and a warning that appears on three
quarters of a list is not a warning. `cluster_setup_libvirt` and
`cluster_setup_users` carry `none` in the reviewed catalogue for exactly this
reason, written by someone who read the roles.

None of that is a substitute for a reviewed entry. Static analysis reads what a
playbook *does*; it cannot write the sentence an operator needs before
restarting a substation hypervisor, and it under reports whenever a role
dispatches through a path computed at run time. So the two are merged rather
than swapped: where a reviewed entry exists its prose and its judgement win,
and every other playbook of the collection is offered with what analysis found,
marked as not reviewed.

The parsing is deliberately shallow. It never imports Ansible, never evaluates
a template and never runs anything: it is YAML read from disk, so a collection
built from a branch nobody here has seen can produce a poor description and
nothing worse.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class _Loader(yaml.SafeLoader):
    """A loader that shrugs at a tag it does not know.

    `!vault` and `!unsafe` are ordinary in an Ansible tree, and one of them must
    not stop the analysis of the playbook that carries it.
    """


def _unknown(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_Loader.add_multi_constructor("", _unknown)


# Keys of a task that are not the module it runs. Whatever else a task carries
# is the module, which is how Ansible itself reads a task.
_TASK_KEYWORDS = frozenset(
    {
        "action",
        "always",
        "any_errors_fatal",
        "args",
        "async",
        "become",
        "become_flags",
        "become_method",
        "become_user",
        "block",
        "changed_when",
        "check_mode",
        "collections",
        "connection",
        "delay",
        "delegate_facts",
        "delegate_to",
        "diff",
        "environment",
        "failed_when",
        "ignore_errors",
        "ignore_unreachable",
        "listen",
        "loop",
        "loop_control",
        "module_defaults",
        "name",
        "no_log",
        "notify",
        "poll",
        "register",
        "remote_user",
        "rescue",
        "retries",
        "run_once",
        "tags",
        "throttle",
        "timeout",
        "until",
        "vars",
        "when",
    }
)
_TASK_KEYWORD_PREFIXES = ("with_",)

# The modules check mode cannot report on. A task running one of these is
# skipped by a preview, and whatever it would have changed stays invisible.
_COMMAND_MODULES = frozenset({"command", "shell", "raw", "script"})

# Modules that change nothing on a machine. A playbook whose only other tasks
# are commands has nothing at all to show in check mode.
_INERT_MODULES = frozenset(
    {
        "add_host",
        "assert",
        "debug",
        "fail",
        "find",
        "gather_facts",
        "group_by",
        "import_role",
        "import_tasks",
        "include",
        "include_role",
        "include_tasks",
        "include_vars",
        "meta",
        "pause",
        "reboot",
        "set_fact",
        "setup",
        "slurp",
        "stat",
        "wait_for",
        "wait_for_connection",
    }
)

# `machine_to_remove is undefined`, which is how a playbook of this collection
# says an input is mandatory, next to the `fail` that carries the message.
_UNDEFINED = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+is\s+(?:not\s+defined|undefined)\b"
)
_IDENTIFIER = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")
# `skip_reboot_setup is not defined or not skip_reboot_setup`, which is how
# this collection lets an operator decline what a task would do.
_DECLINE_SWITCH = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*) is not defined or not \1")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_TEMPLATE = re.compile(r"{{(.*?)}}", re.DOTALL)

# Words of a `when:` that are the language rather than a variable.
_CONDITION_WORDS = frozenset(
    {"is", "not", "or", "and", "in", "defined", "undefined", "true", "false", "none"}
)
# What a `hosts:` line may name without asking anything of the operator.
_MAGIC_VARIABLES = frozenset(
    {
        "groups",
        "hostvars",
        "inventory_hostname",
        "ansible_play_hosts",
        "ansible_play_batch",
        "play_hosts",
        "item",
    }
)


@dataclass
class PlaybookFacts:
    """What reading a playbook off the disk says about it."""

    id: str
    targets: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    play_count: int = 0
    task_count: int = 0
    command_tasks: int = 0
    writing_tasks: int = 0
    reboots: bool = False
    reboot_variables: list[str] = field(default_factory=list)
    ungated_reboot: bool = False
    """A reboot no variable of this playbook holds back."""
    required_variables: list[str] = field(default_factory=list)
    parsed: bool = True

    @property
    def preview(self) -> str:
        """What a check run of this playbook would be worth.

        `none` is kept for the case that has no answer at all: nothing here
        writes through a module, so check mode reports an empty run. A playbook
        that does write is `partial` as soon as one task is command driven.
        """
        if not self.writing_tasks:
            return "none"
        return "partial" if self.command_tasks else "full"

    @property
    def reboot_state(self) -> str:
        if not self.reboots:
            return "no"
        # Gated only when every reboot in the chain can be declined.
        # `seapath_setup_main` reboots in two places, its own last play and the
        # network playbook it imports, behind two different switches. One of
        # them left out is a machine that restarts after the operator was told
        # it would not.
        if self.ungated_reboot or not self.reboot_variables:
            return "yes"
        return "gated"

    @property
    def needs_cluster(self) -> bool:
        """Plays cluster machines and nothing a standalone machine has."""
        joined = " ".join(self.targets)
        return "cluster_machines" in joined and "standalone_machine" not in joined


def _documents(path: Path) -> tuple[list[Any], bool]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    try:
        return [
            doc for doc in yaml.load_all(text, Loader=_Loader) if doc is not None
        ], True
    except yaml.YAMLError:
        # A playbook this service cannot parse is described as poorly as it can
        # be, and never hidden: the operator has to see that it is there.
        logger.warning("Could not parse %s, its description will be thin", path)
        return [], False


def _clauses(when: Any) -> tuple[str, ...]:
    """A `when:` as the list of conditions Ansible ands together."""
    if isinstance(when, list):
        return tuple(_text(item) for item in when if _text(item))
    text = _text(when)
    return (text,) if text else ()


def _flatten(tasks: Any, inherited: tuple[str, ...] = ()) -> Iterator[dict]:
    """Every task of a list, `block`, `rescue` and `always` included.

    A nested task is yielded carrying the conditions of the blocks around it as
    well as its own, which is how Ansible runs it. `seapath_setup_network.yaml`
    is why: its reboot sits in a block, and the switch an operator declines it
    with sits on the block. Dropping that switch described a reboot nobody can
    hold back, and the confirmation offered no way to decline it.
    """
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        conditions = inherited + _clauses(task.get("when"))
        yield task if not inherited else {**task, "when": list(conditions)}
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section), conditions)


def _module(task: dict) -> tuple[str, str]:
    """The module a task runs: its short name, and the key it was written as.

    The key matters as much as the name. A task says
    `ansible.builtin.include_role:` and the arguments are under that exact
    string, so a reader that looks for `include_role` finds nothing.
    """
    for key in task:
        name = str(key)
        if name in _TASK_KEYWORDS or name.startswith(_TASK_KEYWORD_PREFIXES):
            continue
        return name.rsplit(".", 1)[-1], name
    return "", ""


def _text(value: Any) -> str:
    """Everything a value says, flattened, for the regexes to read."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return str(value)


def _hosts(play: dict) -> list[str]:
    value = play.get("hosts")
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _role_names(play: dict) -> list[str]:
    names = []
    for entry in play.get("roles") or []:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and entry.get("role"):
            names.append(str(entry["role"]))
    return names


def _template_variables(text: str) -> list[str]:
    """What a `{{ }}` asks for, beyond what Ansible always provides.

    `{{ groups['cluster_machines'][0] }}` asks for nothing: `groups` is there
    on every run. `{{ machine_to_update }}` asks the operator for a machine,
    and a page with no field for it cannot launch that playbook.
    """
    names = []
    for expression in _TEMPLATE.findall(text):
        stripped = _QUOTED.sub(" ", expression)
        for name in _IDENTIFIER.findall(stripped):
            if (
                name not in _MAGIC_VARIABLES
                and name.lower() not in _CONDITION_WORDS
                and name not in names
            ):
                names.append(name)
    return names


def _decline_switches(when: Any) -> list[str]:
    """The variables a task can be declined with, if it can be declined.

    Only a whole clause of the shape `X is not defined or not X` counts, since
    the clauses of a `when:` are anded: setting X there drops the task whatever
    the other clauses say, so the polarity is proven rather than inferred from
    the name. A condition that mentions the same variable in any
    other shape is left alone: guessing wrong here means a checkbox reading
    "converge without rebooting" that reboots a substation hypervisor.
    """
    found: list[str] = []
    for clause in _clauses(when):
        match = _DECLINE_SWITCH.fullmatch(" ".join(clause.split()))
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def _role_task_files(roles_dir: Path, role: str) -> list[Path]:
    """Every task file of a role, rather than `main.yml` alone.

    A role dispatches to `tasks/centos/network.yml` with an `include_tasks`
    whose path is built from a fact, and a reader that follows only what it can
    resolve statically misses exactly the tasks that do the work. Reading the
    whole directory over reports rather than under reports, which is the right
    direction for a promise about a preview.
    """
    root = roles_dir / role
    return sorted(
        path
        for directory in ("tasks", "handlers")
        for path in (root / directory).rglob("*")
        if path.is_file() and path.suffix in (".yml", ".yaml")
    )


class _Reader:
    def __init__(self, collection: Path) -> None:
        self.playbooks_dir = collection / "playbooks"
        self.roles_dir = collection / "roles"

    def facts(self, playbook_id: str) -> PlaybookFacts:
        facts = PlaybookFacts(id=playbook_id)
        self._read_playbook(playbook_id, facts, set(), set())
        return facts

    def _read_playbook(
        self,
        playbook_id: str,
        facts: PlaybookFacts,
        seen_playbooks: set[str],
        seen_roles: set[str],
    ) -> None:
        if playbook_id in seen_playbooks:
            return
        seen_playbooks.add(playbook_id)

        path = self._playbook_file(playbook_id)
        if path is None:
            return
        documents, parsed = _documents(path)
        facts.parsed = facts.parsed and parsed

        for document in documents:
            if not isinstance(document, list):
                continue
            for play in document:
                if not isinstance(play, dict):
                    continue
                imported = play.get("import_playbook") or play.get("include_playbook")
                if imported:
                    name = Path(str(imported)).stem
                    if name not in facts.imports:
                        facts.imports.append(name)
                    self._read_playbook(name, facts, seen_playbooks, seen_roles)
                    continue
                self._read_play(play, facts, seen_roles)

    def _read_play(
        self, play: dict, facts: PlaybookFacts, seen_roles: set[str]
    ) -> None:
        facts.play_count += 1
        for host in _hosts(play):
            # localhost is where a playbook checks its own inputs, not a
            # machine an operator has to be warned about.
            if host in ("localhost", "127.0.0.1") or host in facts.targets:
                continue
            facts.targets.append(host)
            # A play whose `hosts:` is built from a variable is a play that
            # cannot start until someone supplies it.
            for name in _template_variables(host):
                self._require(facts, name)

        for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
            tasks = list(_flatten(play.get(section)))
            self._read_tasks(tasks, facts, own=True)
            self._follow_includes(tasks, facts, seen_roles)

        for role in _role_names(play):
            self._read_role(role, facts, seen_roles)

    def _read_role(self, role: str, facts: PlaybookFacts, seen_roles: set[str]) -> None:
        if role in seen_roles:
            return
        seen_roles.add(role)
        facts.roles.append(role)

        for path in _role_task_files(self.roles_dir, role):
            documents, parsed = _documents(path)
            facts.parsed = facts.parsed and parsed
            for document in documents:
                tasks = list(_flatten(document))
                self._read_tasks(tasks, facts, own=False)
                self._follow_includes(tasks, facts, seen_roles)

    def _read_tasks(
        self,
        tasks: Iterable[dict],
        facts: PlaybookFacts,
        own: bool,
    ) -> None:
        """`own` is the playbook's own tasks rather than a role's.

        The distinction is what keeps a role's internal sanity check out of the
        list of things to ask the operator: `detect_seapath_distro` fails when
        it cannot work out the distribution, and that is the role talking to
        its author, not the playbook asking for an input.
        """
        for task in tasks:
            short, _ = _module(task)
            if not short:
                continue
            facts.task_count += 1

            when = _text(task.get("when", ""))

            if short in _COMMAND_MODULES:
                facts.command_tasks += 1
            elif short not in _INERT_MODULES:
                facts.writing_tasks += 1

            if short == "reboot":
                facts.reboots = True
                switches = _decline_switches(task.get("when"))
                for switch in switches:
                    if switch not in facts.reboot_variables:
                        facts.reboot_variables.append(switch)
                # A reboot behind a condition this reader does not understand
                # is reported as a reboot that happens.
                if not switches:
                    facts.ungated_reboot = True

            if own and short == "fail":
                for name in _UNDEFINED.findall(when):
                    self._require(facts, name)

    def _follow_includes(
        self, tasks: Iterable[dict], facts: PlaybookFacts, seen_roles: set[str]
    ) -> None:
        """A role a task pulls in, which is how a play with no `roles:` works.

        `deploy_vms_cluster.yaml` is one task, an `include_role` in a loop, and
        a reader that only expands the `roles:` list of a play describes it as
        a playbook that does nothing at all.
        """
        for task in tasks:
            short, key = _module(task)
            if short not in ("include_role", "import_role"):
                continue
            value = task.get(key)
            name = value.get("name") if isinstance(value, dict) else None
            if name and "{{" not in str(name):
                self._read_role(str(name), facts, seen_roles)

    @staticmethod
    def _require(facts: PlaybookFacts, name: str) -> None:
        if name not in facts.required_variables:
            facts.required_variables.append(name)

    def _playbook_file(self, playbook_id: str) -> Path | None:
        for suffix in (".yaml", ".yml"):
            path = self.playbooks_dir / f"{playbook_id}{suffix}"
            if path.is_file():
                return path
        return None


def playbook_ids(collection: Path) -> list[str]:
    """Every playbook of the collection, CI and test helpers aside.

    `ci_*` and `test_*` drive the upstream CI: they reinstall an ISO, restore a
    snapshot, reboot on a USB drive. They exist to build a machine from
    nothing, not to converge one that is running virtual machines in a
    substation, and no reading of a YAML file makes them safe to offer here.
    """
    directory = Path(collection) / "playbooks"
    if not directory.is_dir():
        return []
    return sorted(
        path.stem
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix in (".yaml", ".yml")
        and not path.stem.startswith(("ci_", "test_"))
    )


def read(collection: Path, playbook_id: str) -> PlaybookFacts:
    """Analyse one playbook of a collection tree."""
    return _Reader(Path(collection)).facts(playbook_id)


@lru_cache(maxsize=8)
def _cached(collection: str, identity: str) -> tuple[PlaybookFacts, ...]:
    reader = _Reader(Path(collection))
    return tuple(reader.facts(name) for name in playbook_ids(Path(collection)))


def read_all(collection: Path, identity: str) -> tuple[PlaybookFacts, ...]:
    """Every playbook of the collection, analysed once per installed version.

    `identity` is the collection's fingerprint, so reinstalling it reads the
    tree again while a page reload does not: this walks a few hundred YAML
    files, which is cheap once and silly on every request.
    """
    return _cached(str(collection), identity)
