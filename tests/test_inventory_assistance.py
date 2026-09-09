# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The assistant, and the silence it has to keep on a correct file.

The rule that decides whether this is worth having: a file that came straight
from upstream produces no remark at all. One false positive on a reference
inventory teaches an operator to ignore the whole thing, and the remarks that
matter go with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.inventory.assistance import assist
from app.inventory.lexicon import Lexicon, forget, read

REFERENCE = Path.home() / "dev/seapath-ansible/inventories/examples"

CLUSTER = """
all:
  hosts:
    node1:
      ansible_host: 192.168.200.121
      network_interface: eno1
  vars:
    admin_user: admin
    subnet: 24
hypervisors:
  hosts:
    node1:
  vars:
    isolcpus: 4-7
VMs:
  hosts:
    rtvm:
      vm_disk: ../files/guest.qcow2
"""


# What the roles of a collection read, as the scan would return it. Written out
# rather than derived, so these tests say which names they are assuming.
LEXICON = Lexicon(
    mentioned=frozenset(
        {
            "cephadm_network",
            "isolcpus",
            "vm_disk",
            "admin_user",
            "subnet",
            "network_interface",
            "apt_repo",
            "nics_affinity",
            "interfaces_to_wait_for",
            "custom_network",
        }
    ),
    declared=frozenset({"cephadm_network", "isolcpus", "nics_affinity", "apt_repo"}),
)


COLLECTION = Path.home() / ".ansible/collections/ansible_collections/seapath/ansible"


def _real_lexicon() -> Lexicon:
    """The collection installed on this machine, when there is one."""
    found = read(COLLECTION, "tests")
    assert found is not None
    return found


def _names(assistance, kind: str) -> set[str]:
    return {remark.name for remark in assistance.remarks if remark.kind == kind}


# 1. Silence on what is correct.


def test_a_correct_file_produces_no_remark() -> None:
    assert assist(CLUSTER, LEXICON).remarks == []


def test_an_empty_file_produces_no_remark() -> None:
    # The page opens on a node nobody has seeded, and an empty editor is not a
    # place to start listing what is missing.
    assert assist("", LEXICON).remarks == []
    assert assist("", LEXICON).known == 0


@pytest.mark.skipif(
    not REFERENCE.is_dir() or not COLLECTION.is_dir(),
    reason="the seapath-ansible checkout or the collection is not installed here",
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
def test_the_reference_inventories_draw_no_remark(name: str) -> None:
    """The test that decides whether the assistant is worth switching on.

    `seapath-vm-deployement.yaml` is the one that caught the first version of
    this: it writes `ansible_host` and `ansible_user` on its guest entries, for
    the play that waits for the guest to answer over SSH, and calling those
    misplaced was a remark about a file upstream ships.
    """
    assistance = assist((REFERENCE / name).read_text(), _real_lexicon())

    assert assistance.remarks == []
    assert assistance.known > 0


# 2. What it does catch.


def test_a_misspelling_is_named_with_what_was_probably_meant() -> None:
    assistance = assist(
        "all:\n  hosts:\n    node1:\n  vars:\n    cephadm_netwrok: 192.168.55.0/24\n",
        LEXICON,
    )

    assert _names(assistance, "unknown") == {"cephadm_netwrok"}
    assert assistance.remarks[0].suggestion == "cephadm_network"


def test_a_variable_of_a_sites_own_is_reported_without_a_guess() -> None:
    # Legitimate, and indistinguishable from a misspelling without reading the
    # roles. The remark says so rather than inventing a correction.
    assistance = assist(
        "all:\n  hosts:\n    node1:\n  vars:\n    site_own_thing: 1\n", LEXICON
    )

    assert _names(assistance, "unknown") == {"site_own_thing"}
    assert assistance.remarks[0].suggestion is None


def test_two_variables_that_merely_look_alike_are_not_offered_for_each_other() -> None:
    # `cluster_next_ip_addr` and `cluster_ip_addr` are two addresses on two
    # machines, and a suggestion swapping one for the other would be a wrong
    # answer written with confidence. The cutoff is what keeps the pair apart.
    pool = Lexicon(
        mentioned=frozenset({"cluster_ip_addr", "cluster_next_ip_addr"}),
        declared=frozenset({"cluster_ip_addr", "cluster_next_ip_addr"}),
    )
    assistance = assist(
        "all:\n  hosts:\n    node1:\n  vars:\n    cluster_nxt_ip_addr: 1.2.3.4\n",
        pool,
    )

    suggestion = assistance.remarks[0].suggestion
    assert suggestion in (None, "cluster_next_ip_addr")


def test_a_misspelling_written_once_is_reported_once() -> None:
    # Written on `all`, it reaches three machines. Reporting it per machine
    # would read as three mistakes, and there is one.
    document = (
        "all:\n"
        "  hosts:\n    node1:\n    node2:\n    node3:\n"
        "  vars:\n    cephadm_netwrok: 192.168.55.0/24\n"
    )
    assistance = assist(document, LEXICON)

    assert len(assistance.remarks) == 1
    assert assistance.remarks[0].where == ["the whole inventory"]


def test_a_guest_variable_on_a_machine_is_read_by_nothing() -> None:
    document = CLUSTER.replace(
        "      network_interface: eno1\n",
        "      network_interface: eno1\n      vm_disk: ../files/wrong.qcow2\n",
    )

    assistance = assist(document, LEXICON)

    assert _names(assistance, "misplaced") == {"vm_disk"}
    assert assistance.remarks[0].where == ["node1"]


def test_a_machine_variable_on_a_guest_is_read_by_nothing() -> None:
    document = CLUSTER.replace(
        "      vm_disk: ../files/guest.qcow2\n",
        "      vm_disk: ../files/guest.qcow2\n      isolcpus: 4-7\n",
    )

    assert _names(assist(document, LEXICON), "misplaced") == {"isolcpus"}


# 3. Where the guest boundary is, and where it is not.


def test_a_connection_variable_is_at_home_on_a_guest_and_on_a_machine() -> None:
    """Ansible's own variables describe how it reaches a host, not what it is.

    The reference VM inventory writes `ansible_host` on a guest entry so the
    play can wait for the guest over SSH once it is created.
    """
    document = CLUSTER.replace(
        "      vm_disk: ../files/guest.qcow2\n",
        "      vm_disk: ../files/guest.qcow2\n      ansible_host: 10.132.170.8\n",
    )

    assert assist(document, LEXICON).remarks == []


def test_a_guest_variable_on_all_reaches_the_guests_and_is_left_alone() -> None:
    # Untidy, and it does reach them. Reaching a reader is the test, rather
    # than reaching only readers.
    document = CLUSTER.replace(
        "    admin_user: admin\n", "    admin_user: admin\n    vm_disk: ../files/a\n"
    )

    assert _names(assist(document, LEXICON), "misplaced") == set()


def test_a_guest_group_declared_through_children_is_still_guests() -> None:
    # `cluster_VMs` and `standalone_VMs` are how a file that holds both says
    # which playbook creates a guest, and the boundary has to follow them.
    document = """
all:
  hosts:
    node1:
      ansible_host: 192.168.200.121
      network_interface: eno1
VMs:
  children:
    cluster_VMs:
      hosts:
        rtvm:
          isolcpus: 4-7
"""

    assert _names(assist(document, LEXICON), "misplaced") == {"isolcpus"}


def test_an_empty_group_is_judged_by_nothing() -> None:
    # The reference inventories declare empty groups to keep Ansible from
    # warning, and a variable on one reaches no host at all.
    document = CLUSTER + "\nstandalone_machine:\n  vars:\n    isolcpus: 4-7\n"

    assert _names(assist(document, LEXICON), "misplaced") == set()


def test_a_host_variable_on_a_group_is_a_way_of_writing_an_inventory() -> None:
    # `isolcpus` on `hypervisors` is what the reference cluster does. Judging
    # anything but the guest boundary would report the upstream file.
    assert _names(assist(CLUSTER, LEXICON), "misplaced") == set()


# 4. The endpoint, and the switch that decides whether it is asked.


def test_the_assistant_answers_a_viewer(signed_in: TestClient) -> None:
    response = signed_in.post(
        "/api/v1/inventory/raw/assist", json={"document": CLUSTER}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["remarks"] == []
    assert body["known"] == 6
    # No collection is installed under a test's state directory, so the
    # unknown half was not attempted and the answer says so.
    assert body["roles_read"] is False


def test_the_assistant_reports_rather_than_refuses(signed_in: TestClient) -> None:
    """No remark ever reaches `validate()`, so none of them blocks a commit.

    A variable of a site's own is a legitimate name, and a service that refused
    the commit would be a service a site has to work around.
    """
    document = CLUSTER.replace(
        "    admin_user: admin\n", "    admin_user: admin\n    site_own_thing: 1\n"
    )

    check = signed_in.post("/api/v1/inventory/raw/check", json={"document": document})

    assert len(assist(document, LEXICON).remarks) == 1
    # The rules never mention it, at any level. The assistant and the
    # validation are two answers about one file, and only one of them decides
    # whether it may be committed.
    assert all(
        "site_own_thing" not in finding["message"]
        for finding in check.json()["findings"]
    ), check.json()


def test_the_assistant_needs_a_session(client: TestClient) -> None:
    response = client.post("/api/v1/inventory/raw/assist", json={"document": ""})

    assert response.status_code == 401


def test_a_file_that_is_not_yaml_is_refused_rather_than_guessed_at(
    signed_in: TestClient,
) -> None:
    response = signed_in.post(
        "/api/v1/inventory/raw/assist", json={"document": "all:\n  hosts:\n   - ["}
    )

    assert response.status_code == 400


# 5. The collection is what says whether a name is read by nothing.


def test_a_variable_the_collection_reads_is_never_reported() -> None:
    """The regression this half exists for.

    The curated table is 81 names. The first real site inventory this ran
    against reported `apt_repo`, `nics_affinity`, `admin_ssh_keys`,
    `interfaces_to_wait_for` and seven more as read by no role, and every one
    of them is read by a role. Four reference inventories exercise some forty
    five variables, so passing against them proved much less than it looked.
    """
    document = (
        "all:\n  hosts:\n    node1:\n"
        "  vars:\n    apt_repo: x\n    nics_affinity: y\n"
        "    interfaces_to_wait_for: z\n"
    )

    assert _names(assist(document, LEXICON), "unknown") == set()


def test_ansibles_own_namespace_is_never_reported() -> None:
    # `ansible_ssh_common_args`, `ansible_become` and the rest are read by
    # Ansible rather than by a role, so no collection mentions them and no
    # table here will ever list them all. The prefix is the rule.
    document = (
        "all:\n  hosts:\n    node1:\n"
        "  vars:\n    ansible_ssh_common_args: -o X=y\n    ansible_become: true\n"
    )

    assert _names(assist(document, LEXICON), "unknown") == set()


def test_without_a_collection_nothing_is_said_about_an_unknown_name() -> None:
    """Saying no role reads a name is a claim about the roles.

    On a node with no collection installed the claim cannot be made, so it is
    not made. The placement half still works, since it reads the curated table
    alone.
    """
    document = CLUSTER.replace(
        "    admin_user: admin\n", "    admin_user: admin\n    site_own_thing: 1\n"
    ).replace(
        "      network_interface: eno1\n",
        "      network_interface: eno1\n      vm_disk: ../files/wrong.qcow2\n",
    )

    answer = assist(document, None)

    assert answer.roles_read is False
    assert _names(answer, "unknown") == set()
    assert _names(answer, "misplaced") == {"vm_disk"}


def test_a_tree_that_is_not_a_collection_is_read_as_no_collection(
    tmp_path: Path,
) -> None:
    # A partial install, or a clone whose submodules never came down, holds far
    # too little to say that a name is read by nothing. Answering from it would
    # report almost every variable of a real inventory.
    forget()
    empty = tmp_path / "collections"
    (empty / "playbooks").mkdir(parents=True)
    (empty / "playbooks" / "seapath_setup_main.yaml").write_text("---\n")

    assert read(empty, "empty") is None
    assert read(tmp_path / "absent", "none") is None
    assert read(None) is None


@pytest.mark.skipif(
    not COLLECTION.is_dir(), reason="no seapath collection is installed here"
)
def test_the_installed_collection_is_read_off_its_templates_too() -> None:
    # `interfaces_to_wait_for` appears in a `.j2` and nowhere else, and so does
    # `ptp_vlanid`. A scan over the task files alone reports both.
    lexicon = _real_lexicon()

    for name in ("interfaces_to_wait_for", "ptp_vlanid", "apt_repo", "nics_affinity"):
        assert lexicon.knows(name), name
    for typo in ("cephadm_netwrok", "network_interfce", "isolcpu"):
        assert not lexicon.knows(typo), typo


# 6. The inventory reads its own variables, and that is a third reader.


SELF_REFERENCE = """
all:
  hosts:
    node1:
      ansible_host: 192.168.200.121
      network_interface: eno1
    node2:
      ansible_host: 192.168.200.122
      network_interface: eno1
      sec_ip_address: 10.0.1.2/24
cluster_machines:
  hosts:
    node1:
    node2:
  vars:
    custom_network:
      eno1:
        Network:
          - Address: "{{ sec_ip_address | default(omit) }}"
"""


def test_a_variable_the_inventory_itself_reads_is_not_reported() -> None:
    """A site's intermediate variable, read by a `{{ }}` in the same file.

    `custom_network` is handed whole to `network_systemdnetworkd`, and the
    addresses inside it come from variables the site sets per machine. No role
    will ever mention `sec_ip_address`, and it is read on every run. The file
    is the third reader, beside the collection and Ansible itself.
    """
    assert _names(assist(SELF_REFERENCE, LEXICON), "unknown") == set()


def test_a_misspelling_in_the_definition_leaves_it_unread_and_reported() -> None:
    # The `{{ }}` asks for `sec_ip_address` and the machine sets
    # `sec_ip_addres`, so the value reaches nothing and the run silently gets
    # the `default(omit)` branch.
    document = SELF_REFERENCE.replace(
        "      sec_ip_address: 10.0.1.2/24", "      sec_ip_addres: 10.0.1.2/24"
    )

    assert _names(assist(document, LEXICON), "unknown") == {"sec_ip_addres"}


def test_a_misspelling_in_the_reference_leaves_the_definition_reported() -> None:
    # The other half of the same mistake, and the useful direction: the
    # variable is set, the template asks for a name nothing defines, and the
    # machine is configured without the address it was given.
    document = SELF_REFERENCE.replace("{{ sec_ip_address |", "{{ sec_ip_addres |")

    assert _names(assist(document, LEXICON), "unknown") == {"sec_ip_address"}


def test_a_template_in_a_block_scalar_counts_as_a_reader() -> None:
    # `extra_crm_cmd_to_run` is written as a block, and a site templates the
    # machine names into it. Reading the loaded structure rather than the text
    # would miss every reference inside one.
    document = """
all:
  hosts:
    node1:
      ansible_host: 192.168.200.121
      network_interface: eno1
      site_vip: 10.0.0.9
cluster_machines:
  hosts:
    node1:
  vars:
    extra_crm_cmd_to_run: |
      primitive vip IPaddr2 params ip={{ site_vip }}
"""

    assert _names(assist(document, LEXICON), "unknown") == set()
