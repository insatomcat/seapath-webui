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

from pathlib import Path

import pytest

from app.runs import analysis, catalogue

PLAYBOOKS: dict[str, str] = {
    # The shape of seapath_setup_main: a chain of imports, and a reboot at the
    # end that a variable holds back.
    "site_main.yaml": """---
- name: Import the prerequisites
  import_playbook: site_prerequisites.yaml

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
    assert facts.imports == ["site_prerequisites"]


def test_a_reboot_behind_a_skip_switch_is_gated(collection: Path) -> None:
    facts = analysis.read(collection, "site_main")

    assert facts.reboots
    assert facts.reboot_variable == "skip_reboot_setup"
    assert facts.reboot_state == "gated"


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
    assert entry.derivation.plays == 2
    assert entry.reboots.value == "gated"
    # The reboot switch is a variable the page can offer, since its name says
    # which way it points.
    assert [(v.name, v.type.value) for v in entry.variables] == [
        ("skip_reboot_setup", "boolean")
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
