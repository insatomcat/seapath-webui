# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Where a run finds the files the inventory names.

The last test of this file is the one that matters. Everything above it asserts
the shape of the tree a run is given, which is a claim about this service. The
last one hands that tree to a real `ansible-playbook`, with a real `copy` task
whose `src` is written the way the inventories in the field write it, and looks
at whether the file arrived. Ansible is the only authority on how Ansible
resolves a path, and this is where it is asked.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from app.runs import staging
from app.runs.adapter import AnsibleRunnerAdapter, RunRequest, prepare
from tests.fakes import write_fake_collection

# The site inventory, written the way a control machine's is: the file
# references sit beside the inventory rather than under it, because on a
# checkout of seapath-ansible the playbooks are one directory down.
_INVENTORY = """\
all:
  hosts:
    testhost:
      ansible_connection: local
      ansible_python_interpreter: "{{ ansible_playbook_python }}"
      upload_extra_files_upload_files:
        - src: '../inventories_private/quadlet-macvlan.network'
          dest: /etc/containers/systemd/quadlet-macvlan.network
"""

_PROBE = """\
---
- name: Push a file the inventory names
  hosts: all
  gather_facts: false
  tasks:
    - name: Copy it the way upload_extra_files does
      ansible.builtin.copy:
        src: "{{ item.src }}"
        dest: "{{ probe_dest }}"
        mode: "0644"
      loop: "{{ upload_extra_files_upload_files }}"
"""


@pytest.fixture
def inventory_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "inventory"
    (folder / "inventories_private").mkdir(parents=True)
    (folder / "inventory.yaml").write_text(_INVENTORY)
    (folder / "inventories_private/quadlet-macvlan.network").write_text(
        "[Network]\nNetworkName=macvlan\n"
    )
    return folder


@pytest.fixture
def artefacts_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "artefacts"
    (folder / "files").mkdir(parents=True)
    (folder / "files/guest.qcow2").write_bytes(b"\x00" * 32)
    return folder


def test_the_mirror_puts_the_site_where_the_playbooks_can_see_it(
    tmp_path: Path, inventory_dir: Path, collections_path: Path
) -> None:
    staged = staging.stage(
        directory=tmp_path / "run",
        inventory_dir=inventory_dir,
        collections_path=collections_path,
    )

    playbooks = staged.site_root / "playbooks"
    # A real directory, never a symlink: `..` is resolved by the kernel after
    # a symlink, so a symlinked playbooks/ would send every relative src back
    # into the installed collection.
    assert playbooks.is_dir() and not playbooks.is_symlink()
    assert (playbooks / "seapath_setup_main.yaml").exists()
    # And this is the property the whole module exists for, asserted the way
    # the kernel answers it rather than after a resolve().
    assert os.path.exists(
        str(playbooks / ".." / "inventories_private" / "quadlet-macvlan.network")
    )


def test_the_inventory_folder_is_frozen_into_the_run(
    tmp_path: Path, inventory_dir: Path, collections_path: Path
) -> None:
    staged = staging.stage(
        directory=tmp_path / "run",
        inventory_dir=inventory_dir,
        collections_path=collections_path,
    )
    quadlet = inventory_dir / "inventories_private/quadlet-macvlan.network"
    quadlet.write_text("changed after the launch\n")

    # A trace that changed with the repository afterwards is not a trace.
    assert staged.inventory_file.read_text() == _INVENTORY
    assert (
        tmp_path / "run/site/inventories_private/quadlet-macvlan.network"
    ).read_text() == "[Network]\nNetworkName=macvlan\n"
    assert [file.path for file in staged.files] == [
        "inventory.yaml",
        "inventories_private/quadlet-macvlan.network",
    ]


def test_the_artefacts_are_overlaid_under_the_same_root(
    tmp_path: Path, inventory_dir: Path, artefacts_dir: Path, collections_path: Path
) -> None:
    staged = staging.stage(
        directory=tmp_path / "run",
        inventory_dir=inventory_dir,
        collections_path=collections_path,
        artefacts_dir=artefacts_dir,
    )

    # `vm_disk: ../files/guest.qcow2` means the same thing here as it does on
    # a control machine, without the image ever entering git.
    assert os.path.exists(str(staged.site_root / "playbooks/../files/guest.qcow2"))
    assert [file.source for file in staged.files] == [
        "inventory",
        "inventory",
        "artefacts",
    ]


def test_a_name_both_stores_hold_is_merged_with_the_versioned_one_first(
    tmp_path: Path, inventory_dir: Path, artefacts_dir: Path, collections_path: Path
) -> None:
    (inventory_dir / "files").mkdir()
    (inventory_dir / "files/iptables.rules").write_text("-A INPUT -j DROP\n")

    staged = staging.stage(
        directory=tmp_path / "run",
        inventory_dir=inventory_dir,
        collections_path=collections_path,
        artefacts_dir=artefacts_dir,
    )

    # Both stores have a `files/`, so the mirror holds the merge rather than
    # whichever one it saw last.
    merged = staged.site_root / "files"
    assert merged.is_dir() and not merged.is_symlink()
    assert (merged / "iptables.rules").exists()
    assert (merged / "guest.qcow2").exists()


def test_the_collection_is_searched_after_the_mirror(
    tmp_path: Path, inventory_dir: Path, collections_path: Path
) -> None:
    staged = staging.stage(
        directory=tmp_path / "run",
        inventory_dir=inventory_dir,
        collections_path=collections_path,
    )

    request = RunRequest(
        run_id="staging",
        playbook="seapath.ansible.seapath_setup_main",
        inventory_file=staged.inventory_file,
        private_data_dir=tmp_path / "run",
        collections_path=collections_path,
        site_collections_path=staged.collections_paths[0],
        private_key_file=tmp_path / "key",
        known_hosts_file=tmp_path / "known_hosts",
    )
    preparation = prepare(request)

    search = preparation.environment["ANSIBLE_COLLECTIONS_PATH"].split(os.pathsep)
    # The mirror first, so `seapath.ansible` resolves to it. The image's own
    # root second, because `ansible.posix` and `community.general` live there
    # and the roles use them.
    assert search == [str(tmp_path / "run/collections"), str(collections_path)]
    assert f"collections_path = {os.pathsep.join(search)}" in (
        preparation.config_file.read_text()
    )


@pytest.mark.skipif(
    shutil.which(
        "ansible-playbook",
        path=os.pathsep.join(
            [str(Path(sys.executable).parent), os.environ.get("PATH", "")]
        ),
    )
    is None,
    reason="ansible-playbook is not installed",
)
def test_a_real_playbook_finds_the_file_the_inventory_names(
    tmp_path: Path, inventory_dir: Path
) -> None:
    pytest.importorskip("ansible_runner")

    collections_path = write_fake_collection(tmp_path / "collections")
    playbooks = collections_path / "ansible_collections/seapath/ansible/playbooks"
    (playbooks / "probe.yaml").write_text(_PROBE)

    directory = tmp_path / "run"
    staged = staging.stage(
        directory=directory,
        inventory_dir=inventory_dir,
        collections_path=collections_path,
    )
    pushed = tmp_path / "pushed.network"

    outcome = AnsibleRunnerAdapter().execute(
        RunRequest(
            run_id="staging",
            playbook="seapath.ansible.probe",
            inventory_file=staged.inventory_file,
            private_data_dir=directory,
            collections_path=collections_path,
            site_collections_path=staged.collections_paths[0],
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
            extra_vars={"probe_dest": str(pushed)},
        ),
        on_event=lambda event: None,
        on_output=lambda text: None,
        should_cancel=lambda: False,
    )

    # `src: '../inventories_private/quadlet-macvlan.network'`, written against
    # a checkout of seapath-ansible, resolved against the inventory folder of
    # this node, with the playbook still addressed by its collection name.
    assert outcome.return_code == 0
    assert pushed.read_text() == "[Network]\nNetworkName=macvlan\n"
