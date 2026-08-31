# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The generated inventory, against the hand written reference.

The product claim is that an inventory this service produces is equivalent to
one an engineer wrote by hand: export it, run the same playbooks from a
conventional control machine, and observe no change. These tests are what keep
that claim from quietly becoming false.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from app.inventory.model import Inventory, Mode, NodeConfig
from app.inventory.parser import InvalidInventory, parse
from app.inventory.renderer import render
from tests.conftest import ANSIBLE_INVENTORY

GOLDEN = Path(__file__).parent / "golden" / "standalone.yaml"


@pytest.fixture
def standalone() -> Inventory:
    return Inventory(
        mode=Mode.STANDALONE,
        hosts={
            "seapath-machine": NodeConfig(
                ansible_host="192.168.200.125",
                network_interface="eno1",
                subnet=24,
                gateway_addr="192.168.200.1",
                dns_servers=["192.168.200.1"],
                ptp_interface="eno12419",
                ptp_domain_number=0,
                ntp_servers=["185.254.101.25"],
                admin_user="admin",
                grub_password="grub.pbkdf2.sha512.65536.AAAA.BBBB",
                isolcpus="4-7",
            )
        },
    )


def test_the_rendered_inventory_matches_the_golden_file(standalone: Inventory) -> None:
    assert render(standalone) == GOLDEN.read_text()


def test_rendering_is_deterministic(standalone: Inventory) -> None:
    # A file that reorders itself between two renders would produce a commit
    # with no change in it, and an audit trail full of noise.
    assert render(standalone) == render(standalone)


def test_the_groups_match_the_reference_inventory(standalone: Inventory) -> None:
    document = yaml.safe_load(render(standalone))

    assert set(document) == {
        "all",
        "standalone_machine",
        "hypervisors",
        "cluster_machines",
        "observers",
    }
    assert list(document["standalone_machine"]["hosts"]) == ["seapath-machine"]
    assert list(document["hypervisors"]["hosts"]) == ["seapath-machine"]
    # Empty groups, present to prevent the warnings the reference inventory
    # prevents the same way.
    assert document["cluster_machines"] is None
    assert document["observers"] is None
    # isolcpus is a group variable in the reference, not a host one.
    assert document["hypervisors"]["vars"]["isolcpus"] == "4-7"


def test_the_fixed_variables_are_written_exactly_as_the_reference_has_them(
    standalone: Inventory,
) -> None:
    host = yaml.safe_load(render(standalone))["all"]["hosts"]["seapath-machine"]

    assert host["ansible_connection"] == "ssh"
    assert host["ansible_user"] == "ansible"
    assert host["ansible_python_interpreter"] == "/usr/bin/python3"
    assert host["ansible_remote_tmp"] == "/tmp/.ansible/tmp"
    assert host["ip_addr"] == "{{ ansible_host }}"
    assert host["hostname"] == "{{ inventory_hostname }}"
    # seapath_setup_network.yaml defaults this to false, so an inventory that
    # leaves it out configures no network at all.
    assert host["apply_network_config"] is True


def test_the_ptp_domain_propagates_to_the_variables_the_roles_read(
    standalone: Inventory,
) -> None:
    host = yaml.safe_load(render(standalone))["all"]["hosts"]["seapath-machine"]

    assert host["ptp_domain_number"] == 0
    assert host["timemaster_ptp_domain_number"] == "{{ ptp_domain_number }}"
    assert host["ptp_status_vsock_domain_number"] == "{{ ptp_domain_number }}"


def test_an_observer_gets_no_ptp_interface(standalone: Inventory) -> None:
    # Exactly what the cluster example says to remove when converting a
    # hypervisor into an observer.
    node = standalone.hosts["seapath-machine"].model_copy(
        update={"ptp_interface": None}
    )
    document = render(Inventory(mode=Mode.STANDALONE, hosts={"seapath-machine": node}))

    assert (
        "ptp_interface"
        not in yaml.safe_load(document)["all"]["hosts"]["seapath-machine"]
    )


def test_the_model_survives_a_round_trip(standalone: Inventory) -> None:
    assert parse(render(standalone)) == standalone


def test_a_variable_this_service_does_not_model_is_preserved(
    standalone: Inventory,
) -> None:
    # A site that added something by hand keeps it. Dropping it would be a
    # configuration change nobody asked for and nobody would see.
    document = render(standalone).replace(
        "  ansible_host: 192.168.200.125",
        "  ansible_host: 192.168.200.125\n      custom_site_variable: keep me",
    )

    reparsed = parse(document)
    assert reparsed.hosts["seapath-machine"].extra == {
        "custom_site_variable": "keep me"
    }
    assert "custom_site_variable: keep me" in render(reparsed)


def test_a_broken_file_is_reported_rather_than_guessed_at() -> None:
    with pytest.raises(InvalidInventory, match="not valid YAML"):
        parse("all: {hosts: {node1: {")

    with pytest.raises(InvalidInventory, match="no host"):
        parse("all:\n  hosts:\n")


@pytest.mark.skipif(
    ANSIBLE_INVENTORY is None,
    reason="ansible-core is not installed in this environment",
)
def test_ansible_itself_parses_the_result(
    standalone: Inventory, tmp_path: Path
) -> None:
    # The rule that catches what a schema cannot: Ansible's own opinion.
    path = tmp_path / "inventory.yaml"
    path.write_text(render(standalone))

    completed = subprocess.run(
        [str(ANSIBLE_INVENTORY), "--list", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Unable to parse" not in completed.stderr
    listed = yaml.safe_load(completed.stdout)
    assert "seapath-machine" in listed["_meta"]["hostvars"]
    assert listed["standalone_machine"]["hosts"] == ["seapath-machine"]
    assert listed["hypervisors"]["hosts"] == ["seapath-machine"]
    # isolcpus is set on the hypervisors group and Ansible resolves it onto the
    # host, which is what the reference inventory relies on.
    variables = listed["_meta"]["hostvars"]["seapath-machine"]
    assert variables["isolcpus"] == "4-7"
    # The templated values stay templates here: `ansible-inventory` reports
    # host variables raw, and Jinja is resolved at play time. That is exactly
    # what the reference inventory contains too.
    assert variables["ip_addr"] == "{{ ansible_host }}"
