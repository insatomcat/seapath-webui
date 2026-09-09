# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The vocabulary against the four authorities it was read off.

A curated table rots the moment the thing it describes moves, and it rots
silently: nothing fails, the completion just starts naming a variable no role
reads any more. So almost nothing here asserts a value typed into the table by
hand. Each test takes an authority the repository already holds, or the
`seapath-ansible` checkout when it is there, and asserts the table agrees with
it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.inventory import references, renderer, vocabulary
from app.inventory.lexicon import Declaration, Lexicon
from app.inventory.model import WEBUI_IMAGE_VARIABLE, Guest, NodeConfig
from app.inventory.vocabulary import Scope, Written
from tests.fakes import write_fake_collection

REFERENCE = Path.home() / "dev/seapath-ansible/inventories/examples"


def _names() -> set[str]:
    return {term.name for term in vocabulary.TERMS}


# 1. The table against itself.


def test_a_variable_is_described_once() -> None:
    # Two entries for one name is two answers to one question, and the second
    # is unreachable through `BY_NAME`.
    names = [term.name for term in vocabulary.TERMS]

    assert len(names) == len(set(names))
    assert set(vocabulary.BY_NAME) == set(names)


def test_every_term_says_what_setting_it_does() -> None:
    # The summary is the whole point of the table. An entry without one is a
    # name a completion could have offered on its own.
    for term in vocabulary.TERMS:
        assert term.summary, term.name
        assert term.summary.endswith("."), term.name


# 2. Against what this service already writes.


def test_the_form_written_variables_are_exactly_the_ones_a_form_holds() -> None:
    """`FORM` is a claim about this service, so the model settles it.

    Marked on a variable no field writes, it sends an operator looking for a
    form that does not exist. Missing from one a field does write, it invites a
    hand edit the next form submission overwrites.
    """
    modelled = {
        name for name in NodeConfig.model_fields if name not in ("role", "extra")
    }
    modelled |= {
        name for name in Guest.model_fields if name not in ("deployment", "extra")
    }

    assert {
        term.name for term in vocabulary.TERMS if term.written is Written.FORM
    } == modelled


def test_the_fixed_variables_are_exactly_the_ones_the_renderer_writes() -> None:
    # These are what makes a generated inventory equivalent to a hand written
    # one, so the renderer is the authority on the list.
    written = set(renderer.FIXED_HOST_VARS) | set(renderer.PTP_DOMAIN_ALIASES)

    assert {
        term.name for term in vocabulary.TERMS if term.written is Written.FIXED
    } == written


def test_every_path_variable_the_service_checks_is_described() -> None:
    # `references.KNOWN` is the list the folder page resolves against, so a
    # path variable it knows and this table does not is one an operator is
    # told about in one place and cannot look up in the other.
    assert {variable.name for variable in references.KNOWN} <= _names()


def test_the_variable_naming_this_services_own_image_is_described() -> None:
    assert WEBUI_IMAGE_VARIABLE in _names()


def test_every_variable_a_validation_finding_names_is_described() -> None:
    # A finding carries the field it is about, and the page shows both. A field
    # the table cannot explain is a rule an operator cannot act on.
    from app.inventory.validation import validate

    inventory = _inventory_breaking_every_rule_it_can()
    fields = {
        finding.field
        for finding in validate(inventory).findings
        if finding.field is not None
    }

    assert fields
    assert fields <= _names()


def _inventory_breaking_every_rule_it_can():
    """One machine wrong in as many ways as a single machine can be.

    The point is the set of `field` values the rules produce, so this is built
    to trip as many of them at once as possible rather than to be plausible.
    """
    from app.inventory.model import Inventory, Mode

    return Inventory(
        mode=Mode.STANDALONE,
        hosts={
            "node1": NodeConfig(
                ansible_host="not-an-address",
                network_interface="",
                gateway_addr="10.0.0.1",
                dns_servers=["nonsense"],
                ptp_interface=None,
                ntp_servers=[],
                admin_user=None,
                grub_password="seapath",
                isolcpus=None,
            )
        },
    )


# 3. Against the inventories a site actually copies.


@pytest.mark.skipif(
    not REFERENCE.is_dir(),
    reason="the seapath-ansible checkout is not next to this one",
)
@pytest.mark.parametrize(
    "name",
    [
        "seapath-cluster.yaml",
        "seapath-standalone.yaml",
        "seapath-ovs.yaml",
        "seapath-vm-deployement.yaml",
    ],
)
def test_every_variable_the_reference_inventories_write_is_described(
    name: str,
) -> None:
    """The test that decides whether this table is worth having.

    A site starts from these four files. A variable one of them writes and this
    table cannot name is a variable the service would flag as unknown on an
    inventory that came straight from upstream, which would teach an operator
    to ignore the warning.
    """
    unknown = vocabulary.unknown(_written_by(REFERENCE / name))

    assert unknown == set()


def _written_by(path: Path) -> set[str]:
    """Every variable name the file sets, groups and hosts alike.

    Walks the group structure rather than resolving it: a variable written on a
    group nobody is a member of is still a variable the file names, and this is
    about the vocabulary rather than about what a machine ends up with.
    """
    found: set[str] = set()

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "vars" and isinstance(value, dict):
                found.update(value)
            elif key in ("hosts", "children") and isinstance(value, dict):
                for entry in value.values():
                    if isinstance(entry, dict) and _is_group(entry):
                        walk(entry)
                    elif isinstance(entry, dict):
                        found.update(entry)
            elif isinstance(value, dict):
                walk(value)

    document = yaml.safe_load(path.read_text()) or {}
    walk(document)
    return found


def _is_group(entry: dict) -> bool:
    return bool({"hosts", "children", "vars"} & set(entry))


# 4. Scope, which decides what a completion may offer where.


def test_a_guest_variable_is_never_offered_on_a_machine() -> None:
    # A guest is a libvirt domain and never a machine. `vm_disk` written on a
    # hypervisor is read by nothing, and `isolcpus` on a guest entry is read by
    # nothing either.
    for scope in (Scope.HOST, Scope.GROUP):
        offered = vocabulary.vocabulary(scope).terms
        assert all(term.scope is not Scope.GUEST for term in offered)

    guest = vocabulary.vocabulary(Scope.GUEST).terms
    assert guest
    assert all(term.scope in (Scope.GUEST, Scope.CONNECTION) for term in guest), [
        term.name for term in guest if term.scope not in (Scope.GUEST, Scope.CONNECTION)
    ]


def test_a_connection_variable_is_offered_wherever_a_host_is() -> None:
    """Ansible's own describe how it reaches a host rather than what it is.

    The reference VM inventory writes `ansible_host` and `ansible_user` on its
    guest entries, for the play that waits for the guest to answer over SSH
    once it is created. Reading them as a machine's made the assistant report
    a file upstream ships.
    """
    connection = {
        term.name for term in vocabulary.TERMS if term.scope is Scope.CONNECTION
    }

    assert "ansible_host" in connection
    # `ip_addr`, `hostname` and `apply_network_config` stay a machine's: the
    # roles that read them play machines.
    assert "hostname" not in connection
    for scope in (Scope.HOST, Scope.GROUP, Scope.GUEST):
        offered = {term.name for term in vocabulary.vocabulary(scope).terms}
        assert connection <= offered, scope


def test_a_variable_written_anywhere_is_offered_on_a_machine_and_on_a_group() -> None:
    # `ANY` means a site may write it on `all` or on a machine and both are
    # correct, so narrowing to either place has to keep it.
    anywhere = {term.name for term in vocabulary.TERMS if term.scope is Scope.ANY}

    for scope in (Scope.HOST, Scope.GROUP):
        assert anywhere <= {term.name for term in vocabulary.vocabulary(scope).terms}


def test_the_whole_table_is_answered_when_no_scope_is_asked_for() -> None:
    answer = vocabulary.vocabulary()

    assert len(answer.terms) == len(vocabulary.TERMS)
    assert answer.reviewed == len(vocabulary.TERMS)


# 5. What `unknown` is for.


def test_a_variable_no_role_reads_is_reported_as_unknown() -> None:
    # The point of the table: a typo is a name the file accepts and nothing
    # reads, and today it is found three minutes into a convergence.
    assert vocabulary.unknown({"cephadm_netwrok"}) == {"cephadm_netwrok"}
    assert vocabulary.unknown({"cephadm_network"}) == set()


# 6. The endpoint.


def test_the_vocabulary_is_readable_by_a_viewer(signed_in: TestClient) -> None:
    response = signed_in.get("/api/v1/inventory/vocabulary")

    assert response.status_code == 200
    body = response.json()
    assert body["reviewed"] == len(vocabulary.TERMS)
    # Each entry carries its prose. A completion that says `isolcpus` alone
    # has told an operator what the file in front of them already said.
    isolcpus = next(term for term in body["terms"] if term["name"] == "isolcpus")
    assert isolcpus["role"] == "configure_hypervisor"
    assert isolcpus["summary"]
    assert isolcpus["caution"]


def test_the_vocabulary_narrows_to_the_place_it_is_asked_about(
    signed_in: TestClient,
) -> None:
    guests = signed_in.get("/api/v1/inventory/vocabulary?scope=guest").json()

    names = {term["name"] for term in guests["terms"]}
    assert "vm_disk" in names
    assert "isolcpus" not in names


def test_the_vocabulary_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/v1/inventory/vocabulary").status_code == 401


# 7. The derived tail, which is what keeps this table from being a second place
# to edit. A collection that grows a variable grows it here on the next scan.


def _lexicon(**declared: Declaration) -> Lexicon:
    """A collection that declares exactly these, as the reader would return it."""
    return Lexicon(
        mentioned=frozenset(declared),
        declared=frozenset(declared),
        declarations=dict(declared),
    )


ALLOC = Declaration(
    name="seapath_alloc_strategy",
    role="deploy_seapath_alloc",
    kind="String",
    default="spreading",
    summary="Allocation strategy written to /etc/seapath/alloc.yaml.",
)


def test_a_variable_the_collection_declares_is_offered_without_being_curated() -> None:
    """The case this was built for.

    `seapath_alloc_strategy` is written by a real site, read by a real role and
    absent from the four reference inventories, so nobody ever typed it into
    the table above. A completion that cannot offer it is a completion an
    operator has to work around, and adding it by hand would make this table a
    second place to edit every time the collection moves.
    """
    answer = vocabulary.vocabulary(None, _lexicon(alloc=ALLOC))

    offered = {term.name: term for term in answer.terms}
    assert "seapath_alloc_strategy" in offered
    entry = offered["seapath_alloc_strategy"]
    assert entry.reviewed is False
    assert entry.role == "deploy_seapath_alloc"
    assert entry.default == "spreading"
    assert entry.summary == "Allocation strategy written to /etc/seapath/alloc.yaml."
    assert entry.kind is vocabulary.Kind.STRING


def test_the_curated_half_is_answered_first_and_counted() -> None:
    # A derived entry is ranked behind every reviewed one, whatever it holds,
    # and `reviewed` says where the boundary is.
    answer = vocabulary.vocabulary(None, _lexicon(alloc=ALLOC))

    assert answer.reviewed == len(vocabulary.TERMS)
    assert [term.reviewed for term in answer.terms[: answer.reviewed]] == [True] * (
        answer.reviewed
    )
    assert all(term.reviewed is False for term in answer.terms[answer.reviewed :])


def test_a_reviewed_entry_is_never_replaced_by_what_the_collection_says() -> None:
    """The prose above was written knowing what the role says.

    `isolcpus` carries a caution about a machine that reboots into a state
    where the housekeeping CPUs have nothing left, which no README says. A
    README sentence overwriting it would be a regression dressed as freshness.
    """
    collection = _lexicon(
        isolcpus=Declaration(
            name="isolcpus",
            role="somewhere_else",
            summary="CPU cores isolate.",
        )
    )

    answer = vocabulary.vocabulary(None, collection)

    entries = [term for term in answer.terms if term.name == "isolcpus"]
    assert len(entries) == 1
    assert entries[0].reviewed is True
    assert entries[0].caution


def test_a_derived_entry_says_who_declares_it_and_invents_nothing_else() -> None:
    # Half the variables a `defaults` file declares carry no prose anywhere.
    # The entry says where it comes from and stops: a sentence guessed here
    # would be indistinguishable from the ones that were read off a role.
    answer = vocabulary.vocabulary(
        None,
        _lexicon(plumbing=Declaration(name="configure_ha_tmpdir", role="configure_ha")),
    )

    entry = next(term for term in answer.terms if term.name == "configure_ha_tmpdir")
    assert entry.summary == "Declared by configure_ha."


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("String", vocabulary.Kind.STRING),
        ("Boolean", vocabulary.Kind.BOOLEAN),
        ("bool", vocabulary.Kind.BOOLEAN),
        ("Integer", vocabulary.Kind.INTEGER),
        ("int", vocabulary.Kind.INTEGER),
        ("String list", vocabulary.Kind.LIST),
        ("list of strings", vocabulary.Kind.LIST),
        ("Dict", vocabulary.Kind.MAPPING),
        ("Dict list", vocabulary.Kind.ENTRIES),
        ("list of dict", vocabulary.Kind.ENTRIES),
        # The Type column is prose, and a role is free to write a sentence in
        # it. A scalar is what the file will hold, and the punctuation a
        # completion writes after the name is the same either way.
        ("RSTP or HSR", vocabulary.Kind.STRING),
        ("", vocabulary.Kind.STRING),
    ],
)
def test_the_type_a_readme_writes_is_mapped_onto_a_shape(
    written: str, expected: vocabulary.Kind
) -> None:
    derived = vocabulary.derive(Declaration(name="whatever", kind=written))

    assert derived.kind is expected


def test_a_derived_entry_is_never_offered_inside_a_guest() -> None:
    """What a guest entry may carry is reviewed above, and only there.

    A role default is read wherever Ansible resolves it for a machine, which is
    a group or a host entry and never a libvirt domain. Offering
    `cephadm_network` inside a VM entry would put the whole tail in the one
    place the file is hardest to get right.
    """
    collection = _lexicon(alloc=ALLOC)

    guest = vocabulary.vocabulary(Scope.GUEST, collection)
    assert all(term.reviewed for term in guest.terms)

    for scope in (Scope.HOST, Scope.GROUP):
        offered = vocabulary.vocabulary(scope, collection).terms
        assert "seapath_alloc_strategy" in {term.name for term in offered}


def test_a_node_with_no_collection_answers_with_the_curated_table_alone() -> None:
    # A laptop, or an image whose collection failed to install. The table it
    # was released with is what it can honestly say.
    answer = vocabulary.vocabulary(None, None)

    assert len(answer.terms) == len(vocabulary.TERMS)
    assert all(term.reviewed for term in answer.terms)


def test_the_endpoint_answers_from_the_collection_this_node_runs(
    signed_in_with, tmp_path: Path
) -> None:
    """The property the whole tail exists for.

    The service is released, then the collection moves. What the page offers
    has to follow the collection installed on the machine, without a version of
    this service that knows the name.
    """
    collections = write_fake_collection(tmp_path / "collections")
    role = (
        collections / "ansible_collections/seapath/ansible/roles/deploy_seapath_alloc"
    )
    role.mkdir(parents=True)
    (role / "README.md").write_text(
        "| Variable | Type | Comments |\n|---|---|---|\n"
        "| `seapath_alloc_strategy` | String | Allocation strategy. |\n"
    )
    # The reader answers nothing at all for a tree too small to be a
    # collection, which the fake one is until it holds a collection's worth of
    # names. See `tests/test_inventory_lexicon.py`.
    playbooks = collections / "ansible_collections/seapath/ansible/playbooks"
    (playbooks / "filler.yaml").write_text(
        "\n".join(f"filler_{index}: true" for index in range(600))
    )

    body = signed_in_with(collections).get("/api/v1/inventory/vocabulary").json()

    offered = {term["name"]: term for term in body["terms"]}
    assert body["reviewed"] == len(vocabulary.TERMS)
    assert offered["seapath_alloc_strategy"]["reviewed"] is False
    assert offered["seapath_alloc_strategy"]["summary"] == "Allocation strategy."
    assert offered["isolcpus"]["reviewed"] is True
