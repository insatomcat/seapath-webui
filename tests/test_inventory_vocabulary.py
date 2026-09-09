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
from app.inventory.model import WEBUI_IMAGE_VARIABLE, Guest, NodeConfig
from app.inventory.vocabulary import Scope, Written

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
