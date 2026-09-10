# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What the installed collection says about its own variables, as read.

The curated table in `vocabulary.py` was written against four authorities and
a release of this service. The collection outlives both: a site runs the one it
installed, and a role that grows a variable grows it there. So everything the
completion offers past the curated half is read off the collection at the
moment it is asked for, and these are the tests of that reading.

Two failures are worth more than the rest. Reading too little means an operator
types a variable the role has documented and is offered nothing, which is the
state this replaced. Reading too much means a completion list offering
`Metric`, `spreading` and `Purpose` as inventory variables, which is worse:
a list is judged on its worst entry, and one bad row teaches an operator to
stop opening it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.inventory.lexicon import forget, read

# What one role of the collection looks like on disk. The variable table is
# copied from the shape `seapath-ansible` actually uses, headers included,
# down to the tables that sit beside it and document something else.
README = """
# deploy_seapath_alloc

Install the allocator.

## Variables

| Variable | Required | Type | Default | Comments |
|----------|----------|------|---------|----------|
| `seapath_alloc_strategy` | No | String | `spreading` | Allocation strategy \
written to `/etc/seapath/alloc.yaml`. Valid values: `spreading`, `packing`, \
`repacking`. |
| `cephadm_network` | Yes | String | | Ceph network (e.g. "192.168.55.0/24") |
| `nics_affinity` | No | Dict list | | IRQ affinity, one entry per NIC.<br>See \
[the section below](#irq-affinity) for the syntax. |

## What it publishes

| Metric | Type | Description |
|--------|------|-------------|
| `seapath_alloc_free_logical_cpus` | gauge | How many are left |

## Allocation strategies

| Strategy | Behaviour |
|----------|-----------|
| `spreading` | one thread per physical core |
| `repacking` | fill a core before opening another |
"""

DEFAULTS = """---
# Controls how isolated cores are distributed.
seapath_alloc_strategy: spreading
deploy_seapath_alloc_binary_path: /usr/bin/seapath-alloc
deploy_seapath_alloc_enabled: true
deploy_seapath_alloc_interval: 15
deploy_seapath_alloc_fallbacks:
  - soft
  - hard
deploy_seapath_alloc_declared_and_empty:
"""


def _collection(tmp_path: Path, files: dict[str, str]) -> Path:
    """A collection tree holding exactly what a test is about, plus a floor.

    Below five hundred identifiers the reader answers `None`, because a tree
    that small cannot be used to claim that nothing reads a name. Every test
    here is about a tree that clears it, so the filler goes into a playbook:
    it is neither a `defaults` file nor a README, so it declares nothing and
    only makes the tree big enough to be answered from.
    """
    root = tmp_path / "ansible_collections/seapath/ansible"
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    filler = root / "playbooks/filler.yaml"
    filler.parent.mkdir(parents=True, exist_ok=True)
    filler.write_text("\n".join(f"filler_{index}: true" for index in range(600)))
    return root


@pytest.fixture(autouse=True)
def _forget() -> None:
    forget()


@pytest.fixture
def lexicon(tmp_path: Path):
    root = _collection(
        tmp_path,
        {
            "roles/deploy_seapath_alloc/README.md": README,
            "roles/deploy_seapath_alloc/defaults/main.yml": DEFAULTS,
        },
    )
    found = read(root, "test")
    assert found is not None
    return found


# 1. The two sources, and what each one is the authority on.


def test_a_variable_is_read_with_what_its_readme_says_about_it(lexicon) -> None:
    found = lexicon.declarations["seapath_alloc_strategy"]

    assert found.role == "deploy_seapath_alloc"
    assert found.kind == "string"
    assert found.default == "spreading"
    assert found.summary.startswith("Allocation strategy written to")
    assert "Valid values: spreading, packing, repacking." in found.summary


def test_a_variable_no_role_defaults_is_read_from_the_readme_alone(lexicon) -> None:
    """The half of the collection a `defaults` scan cannot see.

    A role that requires a variable does not default it, so the variables a
    site actually writes are in no `defaults` file at all. `cephadm_network` is
    one of them, and reading the tables is the only way it is ever offered.
    """
    found = lexicon.declarations["cephadm_network"]

    assert found.role == "deploy_seapath_alloc"
    assert found.default == ""
    assert found.summary == 'Ceph network (e.g. "192.168.55.0/24")'


def test_a_variable_no_readme_documents_is_read_from_the_defaults_file(
    lexicon,
) -> None:
    found = lexicon.declarations["deploy_seapath_alloc_binary_path"]

    assert found.role == "deploy_seapath_alloc"
    assert found.kind == "string"
    assert found.default == "/usr/bin/seapath-alloc"
    # Nothing invented. The role wrote a comment above the key and this reads
    # tables, so the entry carries a name, a role and a value, and no prose.
    assert found.summary == ""


def test_the_shape_of_a_default_is_read_from_the_value_the_role_wrote(
    lexicon,
) -> None:
    kinds = {
        name: lexicon.declarations[name].kind
        for name in (
            "deploy_seapath_alloc_enabled",
            "deploy_seapath_alloc_interval",
            "deploy_seapath_alloc_fallbacks",
            "deploy_seapath_alloc_declared_and_empty",
        )
    }

    assert kinds == {
        "deploy_seapath_alloc_enabled": "boolean",
        "deploy_seapath_alloc_interval": "integer",
        "deploy_seapath_alloc_fallbacks": "list",
        # A key written with no value has said the name exists and nothing else.
        "deploy_seapath_alloc_declared_and_empty": "",
    }


# 2. What is deliberately not read.


def test_a_table_that_is_not_about_variables_is_not_read_as_one(lexicon) -> None:
    """The rule that keeps the list worth opening.

    The same README documents metrics, strategies and paths in tables of the
    same shape. Only the one headed `Variable` names variables, and taking the
    others would offer `spreading` and `gauge` as things to write in an
    inventory.
    """
    names = set(lexicon.declarations)

    assert "seapath_alloc_free_logical_cpus" not in names
    assert "spreading" not in names
    assert "repacking" not in names


def test_the_header_of_a_table_is_never_taken_for_a_variable(lexicon) -> None:
    names = set(lexicon.declarations)

    assert "Variable" not in names
    assert "Metric" not in names
    assert "Strategy" not in names


def test_the_prose_of_a_readme_stays_out_of_what_the_collection_knows(
    lexicon,
) -> None:
    """`mentioned` decides whether a name is reported as read by nothing.

    Feeding it the English of a README would answer "something here knows it"
    for half the typos an operator can make, and the warning that catches a
    misspelled variable three minutes before a convergence would go with it.
    """
    assert not lexicon.knows("Install")
    assert not lexicon.knows("Behaviour")
    # The names in the tables are another matter: a documented variable is one
    # the collection knows, whether or not a task file spells it out.
    assert lexicon.knows("cephadm_network")


# 3. A cell of markdown, read as the one line it will be shown as.


def test_a_description_is_flattened_to_something_a_list_can_show(lexicon) -> None:
    found = lexicon.declarations["nics_affinity"]

    assert found.kind == "dict list"
    assert found.summary == (
        "IRQ affinity, one entry per NIC. See the section below for the syntax."
    )


def test_a_long_description_is_cut_on_a_word(tmp_path: Path) -> None:
    sentence = "word " * 100
    root = _collection(
        tmp_path,
        {
            "roles/long/README.md": (
                "| Variable | Comments |\n|---|---|\n"
                f"| `long_variable` | {sentence.strip()} |\n"
            )
        },
    )

    found = read(root, "test")

    assert found is not None
    summary = found.declarations["long_variable"].summary
    assert len(summary) <= 200
    assert summary.endswith("word")


# 4. Where a name comes from, which is a directory and never a guess.


def test_a_variable_outside_a_role_is_declared_by_no_role(tmp_path: Path) -> None:
    # The distribution `vars` at the top of the collection declare real
    # variables and belong to no role. Naming one that does not exist would be
    # worse than naming none.
    root = _collection(tmp_path, {"vars/Debian_paths.yml": "hosts_path: /etc/hosts\n"})

    found = read(root, "test")

    assert found is not None
    assert found.declarations["hosts_path"].role == ""
    assert found.declarations["hosts_path"].default == "/etc/hosts"


def test_a_tree_that_is_not_a_collection_is_not_answered_from(tmp_path: Path) -> None:
    root = tmp_path / "ansible_collections/seapath/ansible"
    (root / "roles/small").mkdir(parents=True)
    (root / "roles/small/README.md").write_text(
        "| Variable | Comments |\n|---|---|\n| `small_variable` | A variable |\n"
    )

    # A README and nothing else: too little was read to say what this
    # collection does and does not know.
    assert read(root, "test") is None


def test_a_collection_shipped_inside_the_collection_is_not_read(
    tmp_path: Path,
) -> None:
    """The image ships four dependencies twice, and only one copy is resolved.

    `prepare.sh` installs the dependencies into the working tree the artefact
    is then built from, so the SEAPATH collection carries `community.general`
    and three others inside itself. Reading them offers an operator variables
    of roles no SEAPATH playbook runs, and it grows `mentioned` by four
    collections, which is the set that decides whether to say that nothing
    reads a name.
    """
    nested = "collections/ansible_collections/community/general"
    root = _collection(
        tmp_path,
        {
            "roles/deploy_seapath_alloc/README.md": README,
            f"{nested}/roles/keycloak/README.md": (
                "| Variable | Comments |\n|---|---|\n"
                "| `keycloak_realm` | The realm |\n"
            ),
            f"{nested}/roles/keycloak/defaults/main.yml": "keycloak_port: 8080\n",
            f"{nested}/plugins/modules/bad_beers.py.yml": "bad_beers: true\n",
        },
    )

    found = read(root, "test")

    assert found is not None
    assert "seapath_alloc_strategy" in found.declarations
    assert "keycloak_realm" not in found.declarations
    assert "keycloak_port" not in found.declarations
    assert not found.knows("bad_beers")
