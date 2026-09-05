# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading a collection this service was never written against.

The fixture is a small collection laid out the way `ansible-galaxy` lays one
out, carrying the shapes the real one uses: a playbook that imports another,
a role that dispatches through a subdirectory, a command whose output a later
task reads, a reboot behind a skip switch, and a play whose `hosts:` is a
variable. Each test is one thing the UI has to know before it may offer a
button.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.runs import analysis, catalogue

PLAYBOOKS: dict[str, str] = {
    # The shape of seapath_setup_main: a chain of imports, a reboot at the end
    # that a variable holds back, and an imported playbook that reboots behind
    # a switch of its own.
    "site_main.yaml": """---
- name: Import the prerequisites
  import_playbook: site_prerequisites.yaml

- name: Import the network
  import_playbook: site_network.yaml

- name: Restart everything
  hosts:
    - cluster_machines
    - standalone_machine
  become: true
  tasks:
    - name: Restart
      ansible.builtin.reboot:
      when:
        - skip_reboot_setup is not defined or not skip_reboot_setup
""",
    "site_prerequisites.yaml": """---
- name: Prepare the machines
  hosts:
    - cluster_machines
    - standalone_machine
  become: true
  roles:
    - write_files
""",
    # The shape of seapath_setup_network: the reboot is in a block, and the
    # switch that declines it is on the block rather than on the task.
    "site_network.yaml": """---
- name: Restart machine if needed
  hosts:
    - cluster_machines
    - standalone_machine
  become: true
  tasks:
    - name: Reboot system to apply network configuration
      when:
        - need_reboot is defined and need_reboot
        - skip_reboot_setup_network is not defined or not skip_reboot_setup_network
      block:
        - name: Restart
          ansible.builtin.reboot:
        - name: Wait for host to be online
          ansible.builtin.wait_for_connection:
""",
    # A reboot nothing holds back, beside one that a switch does. The switch
    # covers half the playbook, which is worth nothing to an operator who
    # ticked a box.
    "site_two_reboots.yaml": """---
- name: Restart twice
  hosts: standalone_machine
  tasks:
    - name: Restart
      ansible.builtin.reboot:
      when:
        - skip_reboot_setup is not defined or not skip_reboot_setup
    - name: Restart again
      ansible.builtin.reboot:
""",
    # Command driven from end to end: nothing here writes through a module, so
    # check mode has nothing at all to report.
    "site_commands.yaml": """---
- name: Evict a machine
  hosts: cluster_machines
  tasks:
    - name: Evict it
      ansible.builtin.command: crm_node -R {{ machine_to_remove }}
      changed_when: true
""",
    # The playbook asks for a variable, the way this collection asks: a `fail`
    # guarded by `is undefined`.
    "site_needs_variable.yaml": """---
- name: Sanity check
  hosts: localhost
  pre_tasks:
    - name: Exit playbook, if no machine was given
      ansible.builtin.fail:
        msg: "machine_to_remove must be declared"
      when: machine_to_remove is undefined

- name: Remove it
  hosts: cluster_machines
  roles:
    - write_files
""",
    # A play targeting a machine the operator has to name.
    "site_templated_target.yaml": """---
- name: Update one machine
  hosts: "{{ machine_to_update }}"
  roles:
    - write_files
""",
    # And one targeting a machine Ansible works out on its own, which asks the
    # operator for nothing.
    "site_first_member.yaml": """---
- name: Deploy from the first member
  hosts: "{{ groups['cluster_machines'][0] }}"
  tasks:
    - name: Deploy
      ansible.builtin.include_role:
        name: write_files
""",
    # Standalone and cluster both, so it needs no cluster.
    "site_both.yaml": """---
- name: Configure both
  hosts:
    - cluster_machines
    - standalone_machine
  roles:
    - read_then_write
""",
    "site_dispatch.yaml": """---
- name: Configure through a dispatching role
  hosts: standalone_machine
  roles:
    - dispatching
""",
    "ci_reinstall.yaml": """---
- name: Reinstall from the ISO
  hosts: all
  tasks:
    - name: Wipe it
      ansible.builtin.command: dd if=/dev/zero of=/dev/sda
      changed_when: true
""",
    "test_run_checks.yaml": """---
- name: Run the checks
  hosts: all
  tasks:
    - name: Check
      ansible.builtin.command: cukinia
      changed_when: false
""",
}

ROLES: dict[str, str] = {
    # Every task writes through a module check mode understands.
    "write_files/tasks/main.yml": """---
- name: Write the configuration
  ansible.builtin.template:
    src: thing.conf.j2
    dest: /etc/thing.conf

- name: Restart the daemon
  ansible.builtin.systemd:
    name: thing
    state: restarted
""",
    # A command whose output the next task reads. Check mode skips the command
    # and the reader dies on an attribute that is not there.
    "read_then_write/tasks/main.yml": """---
- name: Read what is there
  ansible.builtin.shell: virsh secret-list
  register: existing
  changed_when: false

- name: Write it
  ansible.builtin.copy:
    content: "{{ existing.stdout }}"
    dest: /etc/secret
""",
    # A role that dispatches to a file under a subdirectory, through a path
    # built at run time. Reading the whole directory is what finds it.
    "dispatching/tasks/main.yml": """---
- name: Dispatch
  ansible.builtin.include_tasks: "{{ ansible_distribution }}/setup.yml"
""",
    "dispatching/tasks/Debian/setup.yml": """---
- name: Write the Debian file
  ansible.builtin.copy:
    content: hello
    dest: /etc/debian-thing
""",
}


@pytest.fixture
def collection(tmp_path: Path) -> Path:
    root = tmp_path / "ansible_collections/seapath/ansible"
    for name, body in PLAYBOOKS.items():
        path = root / "playbooks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    for name, body in ROLES.items():
        path = root / "roles" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def test_the_ci_and_test_playbooks_are_not_offered(collection: Path) -> None:
    # They reinstall an ISO, restore a snapshot and reboot on a USB drive. They
    # exist to build a machine from nothing, and no reading of a YAML file
    # makes them safe to offer as a button next to the network configuration.
    ids = analysis.playbook_ids(collection)

    assert "site_main" in ids
    assert "ci_reinstall" not in ids
    assert "test_run_checks" not in ids


def test_the_machines_a_run_reaches_are_read_off_the_plays(collection: Path) -> None:
    facts = analysis.read(collection, "site_main")

    # Through the import, since that is where the plays of a chained playbook
    # are, and localhost is never one of them: it is where a playbook checks
    # its own inputs.
    assert facts.targets == ["cluster_machines", "standalone_machine"]
    assert facts.imports == ["site_prerequisites", "site_network"]


def test_a_reboot_behind_a_skip_switch_is_gated(collection: Path) -> None:
    facts = analysis.read(collection, "site_network")

    assert facts.reboots
    assert facts.reboot_variables == ["skip_reboot_setup_network"]
    assert facts.reboot_state == "gated"


def test_the_switch_is_found_where_it_is_written_on_the_block(
    collection: Path,
) -> None:
    """The reboot is in the block, the switch is on it, and Ansible ands both.

    Read task by task, the reboot carries no condition at all, and the run view
    said the playbook reboots and cannot be told not to. The checkbox that
    declines it was greyed out on the one playbook most likely to cut the
    connection under the run.
    """
    facts = analysis.read(collection, "site_network")

    assert not facts.ungated_reboot
    assert facts.reboot_variables == ["skip_reboot_setup_network"]


def test_a_playbook_reboots_behind_every_switch_of_its_chain(
    collection: Path,
) -> None:
    # The shape of seapath_setup_main: its own last play, and the network
    # playbook it imports. Declining means setting both, and an entry that
    # named one alone rebooted the machine after the operator said no.
    facts = analysis.read(collection, "site_main")

    assert facts.reboot_state == "gated"
    assert sorted(facts.reboot_variables) == [
        "skip_reboot_setup",
        "skip_reboot_setup_network",
    ]


def test_one_reboot_nothing_holds_back_makes_the_whole_playbook_reboot(
    collection: Path,
) -> None:
    # Half the reboots gated is a playbook that reboots. The confirmation names
    # the worse of the two outcomes, and offers no switch that cannot deliver.
    facts = analysis.read(collection, "site_two_reboots")

    assert facts.ungated_reboot
    assert facts.reboot_variables == ["skip_reboot_setup"]
    assert facts.reboot_state == "yes"
    assert catalogue.derive(facts).reboot_variables == []


def test_a_playbook_that_only_runs_commands_has_no_preview(collection: Path) -> None:
    facts = analysis.read(collection, "site_commands")

    # Check mode skips every task, the run reaches the end and reports an empty
    # convergence, which is worse than no preview at all.
    assert facts.writing_tasks == 0
    assert facts.preview == "none"


def test_a_playbook_that_writes_through_modules_previews_fully(
    collection: Path,
) -> None:
    facts = analysis.read(collection, "site_prerequisites")

    assert facts.command_tasks == 0
    assert facts.preview == "full"
    assert facts.roles == ["write_files"]


def test_a_role_mixing_a_command_and_a_write_previews_partially(
    collection: Path,
) -> None:
    facts = analysis.read(collection, "site_both")

    # One command among the writes. Check mode skips it and reports the rest,
    # which is what `partial` promises and all it promises.
    assert facts.command_tasks == 1
    assert facts.writing_tasks == 1
    assert facts.preview == "partial"


def test_the_variable_a_playbook_refuses_to_start_without_is_found(
    collection: Path,
) -> None:
    facts = analysis.read(collection, "site_needs_variable")

    assert facts.required_variables == ["machine_to_remove"]


def test_a_machine_named_by_a_variable_is_a_variable_to_supply(
    collection: Path,
) -> None:
    facts = analysis.read(collection, "site_templated_target")

    assert facts.required_variables == ["machine_to_update"]


def test_a_target_ansible_works_out_alone_asks_for_nothing(
    collection: Path,
) -> None:
    facts = analysis.read(collection, "site_first_member")

    # `groups` is there on every run. And the role the play includes is read,
    # so the playbook is not described as one that does nothing.
    assert facts.required_variables == []
    assert facts.roles == ["write_files"]
    assert facts.writing_tasks == 2


def test_a_role_dispatching_through_a_path_is_still_read(collection: Path) -> None:
    # The path is built from a fact, so nothing static resolves it. Reading the
    # whole task directory over reports rather than under reports, which is the
    # right direction for a promise about a preview.
    facts = analysis.read(collection, "site_dispatch")

    assert facts.roles == ["dispatching"]
    assert facts.writing_tasks == 1
    assert facts.preview == "full"


def test_a_playbook_playing_only_cluster_machines_needs_a_cluster(
    collection: Path,
) -> None:
    assert analysis.read(collection, "site_commands").needs_cluster
    assert not analysis.read(collection, "site_main").needs_cluster


def test_the_derived_entry_carries_what_was_counted(collection: Path) -> None:
    entry = catalogue.derive(analysis.read(collection, "site_main"))

    assert entry.reviewed is False
    assert entry.id == "site_main"
    assert entry.playbook == "seapath.ansible.site_main"
    assert "not reviewed by anyone here" in entry.disruption
    assert entry.derivation is not None
    assert entry.derivation.plays == 3
    assert entry.reboots.value == "gated"
    # Both reboot switches are variables the page can offer, since the shape of
    # the condition each is written in says which way it points.
    assert [(v.name, v.type.value) for v in entry.variables] == [
        ("skip_reboot_setup_network", "boolean"),
        ("skip_reboot_setup", "boolean"),
    ]


def test_a_variable_with_no_field_is_declared_unknown(collection: Path) -> None:
    entry = catalogue.derive(analysis.read(collection, "site_templated_target"))

    # Found, named, and never guessed at: a free text field wired to an Ansible
    # run is the extra vars box this service refuses to have.
    assert [(v.name, v.type.value, v.required) for v in entry.variables] == [
        ("machine_to_update", "unknown", True)
    ]


def test_a_reviewed_entry_is_not_overwritten_by_the_reader(tmp_path: Path) -> None:
    from tests.fakes import write_fake_collection

    collections_path = write_fake_collection(tmp_path / "collections")
    entries = {entry.id: entry for entry in catalogue.resolve(collections_path)}

    reviewed = entries["seapath_setup_snmp"]
    assert reviewed.reviewed is True
    assert reviewed.title == "Apply the SNMP configuration"
    assert reviewed.disruption == "Restarts snmpd."


def test_every_playbook_of_the_collection_is_in_the_list(collection: Path) -> None:
    entries = catalogue.resolve(collection.parents[2], version="fixture")
    ids = [entry.id for entry in entries]

    # The reviewed catalogue first, whether or not this collection carries it,
    # and then everything else the collection ships.
    assert ids[: len(catalogue.CATALOGUE)] == [e.id for e in catalogue.CATALOGUE]
    assert "site_main" in ids
    assert "ci_reinstall" not in ids


PREREQUISITES = (
    "seapath_setup_prerequisitesdebian",
    "seapath_setup_prerequisitescentos",
    "seapath_setup_prerequisitesoraclelinux",
    "seapath_setup_prerequisitessles",
    "seapath_setup_prerequisitesyocto",
)


def test_every_prerequisites_entry_says_it_checks_no_distribution() -> None:
    # `seapath_setup_main` picks between the five after `detect_seapath_distro`,
    # and that choice is the only thing standing between a machine and the
    # wrong one. Launched on its own, the Debian playbook runs
    # `configure_seapath_distro` with `update-grub` wherever it is sent.
    for playbook_id in PREREQUISITES:
        entry = catalogue.BY_ID[playbook_id]

        assert entry.reviewed is True
        assert "does not check the distribution it lands on" in entry.notes


def test_every_reboot_switch_an_entry_names_is_a_variable_it_accepts() -> None:
    """The checkbox sets them, and `_accepted_variables` refuses the rest.

    `seapath_setup_network` named `skip_reboot_setup_network` as its switch and
    declared no variables at all, so the box the operator ticked produced
    "Apply the network configuration accepts no variables, not
    skip_reboot_setup_network" and no run.
    """
    undeclared = {}
    for entry in catalogue.CATALOGUE:
        declared = {spec.name for spec in entry.variables}
        missing = [name for name in entry.reboot_variables if name not in declared]
        if missing:
            undeclared[entry.id] = missing

    assert undeclared == {}


def test_the_yocto_prerequisites_are_declared_as_rebooting() -> None:
    entry = catalogue.BY_ID["seapath_setup_prerequisitesyocto"]

    # It reboots only when the kernel parameters actually changed and
    # `kernel_parameters_restart` is set. Declared as a plain reboot, because
    # the confirmation has to name the worse of the two outcomes.
    assert entry.reboots.value == "yes"
    assert entry.reboot_variables == []


def test_the_prerequisites_that_configures_no_hypervisor_says_so() -> None:
    # OracleLinux is the one with no hypervisor play, so a machine prepared
    # with it has had no tuned profile applied. That is invisible until the
    # latency is measured.
    entry = catalogue.BY_ID["seapath_setup_prerequisitesoraclelinux"]

    assert "hypervisors" not in entry.targets
    assert "no tuned" in entry.notes


REAL_COLLECTION = (
    Path.home() / ".ansible/collections/ansible_collections/seapath/ansible"
)

# The reviewed entries were written by reading the playbooks, and the playbooks
# move. This test is the second opinion: it runs only where a real collection
# is installed, so the suite still passes on a laptop with none, and it fails
# on the machine that has one when an entry drifts from what it describes.
real_collection = pytest.mark.skipif(
    not (REAL_COLLECTION / "playbooks").is_dir(),
    reason="no seapath.ansible collection installed",
)


@real_collection
def test_no_reviewed_entry_understates_the_machines_it_plays() -> None:
    # Understating is the dangerous direction. The scope line is what an
    # operator reads before confirming an apply, and a group missing from it is
    # a set of machines they did not know they were about to converge. This
    # found `hypervisors` missing from `seapath_setup_network`, which plays the
    # SR-IOV pools and the NIC IRQ affinity there.
    missing = {}
    for entry in catalogue.CATALOGUE:
        facts = analysis.read(REAL_COLLECTION, entry.id)
        if not facts.play_count:
            continue  # not in this collection, which is its own precondition
        unlisted = [
            target
            for target in facts.targets
            # An intersection is covered by the group it narrows, and the
            # upstream `standalone` in seapath_setup_vmmgrapi.yaml is a typo
            # for `standalone_machine` that matches no group of any inventory.
            if ":" not in target
            and target != "standalone"
            and not _names(target, entry.targets)
        ]
        if unlisted:
            missing[entry.id] = unlisted

    assert missing == {}


def _names(target: str, listed: list[str]) -> bool:
    """Whether the entry's scope line covers a target the reader found.

    Usually a plain group name, matched as one. A `hosts:` built from a
    template is reported verbatim by the reader, and comparing
    `{{ groups['cluster_machines'][0] }}` against a line written for an
    operator would only ever fail: what is checked there is that every group
    the template names is one the entry names too, so `cluster_machines[0]` is
    accepted and a scope line that forgot the group is not.
    """
    if "{{" not in target:
        return target in listed
    return all(
        any(group in item for item in listed)
        for group in re.findall(r"groups\[['\"]([^'\"]+)['\"]\]", target)
    )


@real_collection
def test_every_switch_a_reviewed_entry_offers_is_one_the_playbooks_carry() -> None:
    # The third dangerous direction, and the one the network entry was wrong
    # in: a checkbox that reads "converge without rebooting" and sets a
    # variable no reboot of the chain is behind. The operator declines, the run
    # accepts, the machine restarts.
    wrong = {}
    for entry in catalogue.CATALOGUE:
        facts = analysis.read(REAL_COLLECTION, entry.id)
        if not facts.play_count:
            continue  # not in this collection, which is its own precondition
        unknown = [
            name
            for name in entry.reboot_variables
            if name not in facts.reboot_variables
        ]
        if unknown:
            wrong[entry.id] = unknown

    assert wrong == {}


@real_collection
def test_no_reviewed_entry_refuses_a_reboot_the_playbook_accepts() -> None:
    # The other way an entry can be wrong about a reboot. Declaring `yes` where
    # every reboot of the chain is behind a switch greys out the checkbox and
    # tells the operator the reboot cannot be declined, which sends them to
    # relaunch from another machine for nothing.
    greyed = [
        entry.id
        for entry in catalogue.CATALOGUE
        if analysis.read(REAL_COLLECTION, entry.id).reboot_state == "gated"
        and entry.reboots.value == "yes"
    ]

    assert greyed == []


@real_collection
def test_no_reviewed_entry_understates_a_reboot() -> None:
    # The other dangerous direction. An entry saying a playbook does not
    # reboot, on a machine running virtual machines in a substation, is the
    # worst sentence this catalogue can carry.
    silent = [
        entry.id
        for entry in catalogue.CATALOGUE
        if analysis.read(REAL_COLLECTION, entry.id).reboots
        and entry.reboots.value == "no"
    ]

    assert silent == []


def test_a_reviewed_test_playbook_is_read_and_an_unreviewed_one_is_not(
    tmp_path: Path,
) -> None:
    """The `test_*` rule stops derivation, and reviewing is what lifts it.

    `ci_*` and `test_*` build a machine from nothing, and offering one next to
    the network configuration on the strength of a YAML read is what the rule
    forbids. `test_run_cyclictest` is in the catalogue because a human read it
    and wrote the sentence an operator needs, so it is read for its facts. The
    ones nobody reviewed stay unread, which is what keeps them off the page.
    """
    playbooks = tmp_path / "playbooks"
    playbooks.mkdir(parents=True)
    for name in (
        "test_run_cyclictest",
        "test_run_cukinia",
        "ci_test",
        "seapath_setup_snmp",
    ):
        (playbooks / f"{name}.yaml").write_text("---\n- hosts: all\n")

    with_review = analysis.playbook_ids(tmp_path, frozenset({"test_run_cyclictest"}))
    without = analysis.playbook_ids(tmp_path)

    assert with_review == ["seapath_setup_snmp", "test_run_cyclictest"]
    assert without == ["seapath_setup_snmp"]
