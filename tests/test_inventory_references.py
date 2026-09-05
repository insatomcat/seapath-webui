# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Which files an inventory names, and where a run would find them.

The variables here are read off the roles of the collection one by one, so the
tests are written against the shapes those roles actually accept rather than
against a convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.inventory.references import Roots, Where, check

_INVENTORY = """\
all:
  vars:
    iptables_rules_path: "../inventories_private/iptables.rules"
  children:
    hypervisors:
      hosts:
        node1:
          vm_disk: "../files/guest.qcow2"
          upload_extra_files_upload_files:
            - src: '../inventories_private/quadlet.network'
              dest: /etc/containers/systemd/quadlet.network
          cloud_init:
            user_data_file: "../inventories_private/user-data.yaml"
        node2:
          additional_disk:
            - "../files/data.qcow2"
"""

# A guest that brings its own libvirt XML rather than a template, which is how
# a VM already running is adopted into the inventory.
_GUEST = """
VMs:
  hosts:
    guest1:
      xml_path: "../files/guest1.xml"
"""


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    inventory = tmp_path / "inventory"
    artefacts = tmp_path / "artefacts"
    collection = tmp_path / "collection"
    (inventory / "inventories_private").mkdir(parents=True)
    (artefacts / "files").mkdir(parents=True)
    (collection / "roles/syslog_ng_client/templates").mkdir(parents=True)
    return Roots(inventory=inventory, artefacts=artefacts, collection=collection)


def _by_variable(references) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for reference in references:
        grouped.setdefault(reference.variable, []).append(reference)
    return grouped


def test_a_group_variable_is_a_reference_on_every_host(roots: Roots) -> None:
    grouped = _by_variable(check(_INVENTORY, roots))

    # `iptables_rules_path` is set once on `all`, and it names a file every
    # machine of the inventory will be given.
    assert [reference.host for reference in grouped["iptables_rules_path"]] == [
        "node1",
        "node2",
    ]
    assert grouped["upload_extra_files_upload_files"][0].host == "node1"
    assert grouped["cloud_init"][0].value == "../inventories_private/user-data.yaml"
    assert grouped["additional_disk"][0].value == "../files/data.qcow2"


def test_the_libvirt_xml_a_guest_names_is_a_reference_like_any_other(
    roots: Roots,
) -> None:
    # A guest that brings its own XML rather than a template names it in
    # `xml_path`, and `deploy_vms_cluster` reads it with `lookup('file')`.
    # Left out of the list, it was the one path a guest names that the page
    # said nothing about, which is worse than not looking at all: an operator
    # reads "one file missing" as "one file missing".
    grouped = _by_variable(check(_GUEST, roots))

    assert grouped["xml_path"][0].host == "guest1"
    assert grouped["xml_path"][0].expected == "files/guest1.xml"


def test_a_file_is_found_in_whichever_store_holds_it(roots: Roots) -> None:
    (roots.inventory / "inventories_private/iptables.rules").write_text("-A INPUT\n")
    (roots.artefacts / "files/guest.qcow2").write_bytes(b"\x00")

    grouped = _by_variable(check(_INVENTORY, roots))

    assert grouped["iptables_rules_path"][0].where is Where.INVENTORY
    # The image is found in the store git does not carry, under the same root.
    assert grouped["vm_disk"][0].where is Where.ARTEFACTS
    assert grouped["additional_disk"][0].found is False


def test_a_file_the_collection_already_ships_is_not_reported_missing(
    roots: Roots,
) -> None:
    # `syslog_conf_template` defaults to a name that lives in the role's own
    # templates directory. Reporting that as a file the site owes the run would
    # be wrong and loud.
    templates = roots.collection / "roles/syslog_ng_client/templates"
    (templates / "syslog-ng.conf.j2").write_text("@version: 4\n")
    document = (
        "all:\n  hosts:\n    node1:\n"
        '      syslog_conf_template: "syslog-ng.conf.j2"\n'
    )

    reference = check(document, roots)[0]

    assert reference.found is True
    assert reference.where is Where.COLLECTION


def test_an_absolute_path_is_looked_for_on_this_machine(
    roots: Roots, tmp_path: Path
) -> None:
    present = tmp_path / "hosts"
    present.write_text("127.0.0.1 localhost\n")
    document = (
        "all:\n  hosts:\n    node1:\n"
        f'      hosts_path: "{present}"\n'
        '      cephadm_spec_path: "/etc/seapath/absent.yaml"\n'
    )

    grouped = _by_variable(check(document, roots))

    assert grouped["hosts_path"][0].where is Where.NODE
    assert grouped["cephadm_spec_path"][0].found is False
    # An absolute path names a place rather than a file to upload, so there is
    # no name to offer.
    assert grouped["cephadm_spec_path"][0].expected is None


def test_a_templated_path_is_left_to_ansible(roots: Roots) -> None:
    document = (
        "all:\n  hosts:\n    node1:\n"
        '      vm_disk: "../files/{{ inventory_hostname }}.qcow2"\n'
    )

    reference = check(document, roots)[0]

    # Guessing at the value would produce a confident wrong answer, and the
    # page would tell an operator to upload a file named after a variable.
    assert reference.found is True
    assert reference.where is None


def test_a_path_above_the_folder_says_no_run_can_reach_it(roots: Roots) -> None:
    document = (
        "all:\n  hosts:\n    node1:\n"
        '      iptables_rules_path: "../../elsewhere/iptables.rules"\n'
    )

    reference = check(document, roots)[0]

    assert reference.found is False
    assert reference.expected is None
