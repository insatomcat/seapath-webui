# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""One test per rule, accepting and refusing.

An invalid desired state must never reach the repository, because a broken
inventory that is committed and then applied is how a cluster dies.
"""

from __future__ import annotations

import pytest

from app.inventory.grub import hash_password, verify
from app.inventory.model import Inventory, Mode, NodeConfig, Role
from app.inventory.validation import Level, validate


def node(**overrides) -> NodeConfig:
    values = {
        "ansible_host": "192.168.200.125",
        "network_interface": "eno1",
        "subnet": 24,
        "gateway_addr": "192.168.200.1",
        "dns_servers": ["192.168.200.1"],
        "ptp_interface": "eno12419",
        "ntp_servers": ["185.254.101.25"],
        "admin_user": "admin",
        "isolcpus": "4-7",
    }
    values.update(overrides)
    return NodeConfig(**values)


def inventory(**overrides) -> Inventory:
    return Inventory(mode=Mode.STANDALONE, hosts={"node1": node(**overrides)})


def rules(result, level: Level = Level.ERROR) -> set[str]:
    return {f.rule for f in result.findings if f.level is level}


def test_a_complete_standalone_inventory_is_accepted() -> None:
    result = validate(inventory())

    assert result.valid
    assert result.errors() == []


def test_a_machine_with_no_administration_account_is_warned_about() -> None:
    # The prerequisites playbook of a package manager distribution needs
    # admin_user and stops on its first task without it. A warning rather than
    # an error, because a Yocto machine has no such account.
    result = validate(inventory(admin_user=None))

    assert result.valid
    assert "admin_user_is_named" in rules(result, Level.WARNING)


def test_a_named_administration_account_is_not_warned_about() -> None:
    assert "admin_user_is_named" not in rules(validate(inventory()), Level.WARNING)


def test_an_empty_inventory_is_refused() -> None:
    result = validate(Inventory(mode=Mode.STANDALONE, hosts={}))

    assert not result.valid
    assert "at_least_one_host" in rules(result)


def test_a_standalone_inventory_holds_exactly_one_machine() -> None:
    result = validate(
        Inventory(
            mode=Mode.STANDALONE,
            hosts={"node1": node(), "node2": node(ansible_host="192.168.200.126")},
        )
    )

    assert "standalone_is_one_machine" in rules(result)


def test_the_host_key_must_be_a_usable_hostname() -> None:
    # It becomes the machine's own name: network_buildhosts sets it from
    # `hostname | default(inventory_hostname)`.
    result = validate(Inventory(mode=Mode.STANDALONE, hosts={"not a hostname": node()}))

    assert "host_key_is_a_hostname" in rules(result)


@pytest.mark.parametrize("address", ["not-an-address", "192.168.200", ""])
def test_the_administration_address_must_be_an_address(address: str) -> None:
    assert "administration_address_is_an_address" in rules(
        validate(inventory(ansible_host=address))
    )


def test_the_loopback_cannot_be_the_administration_address() -> None:
    assert "administration_address_is_not_loopback" in rules(
        validate(inventory(ansible_host="127.0.0.1"))
    )


def test_a_gateway_outside_the_subnet_is_refused() -> None:
    # A gateway this machine could never reach is a machine with no route out,
    # discovered after the network role has already applied it.
    assert "gateway_is_reachable" in rules(validate(inventory(gateway_addr="10.0.0.1")))


def test_a_gateway_inside_the_subnet_is_accepted() -> None:
    assert validate(inventory(gateway_addr="192.168.200.254")).valid


def test_a_dns_server_that_is_not_an_address_is_refused() -> None:
    assert "dns_servers_are_addresses" in rules(
        validate(inventory(dns_servers=["resolver.example.com"]))
    )


def test_a_grub_password_in_clear_is_refused() -> None:
    # The inventory goes into git, so a password in clear is a password in the
    # audit trail forever.
    assert "grub_password_is_a_hash" in rules(
        validate(inventory(grub_password="seapath"))
    )


def test_a_hashed_grub_password_is_accepted() -> None:
    assert validate(inventory(grub_password=hash_password("seapath"))).valid


def test_the_grub_hash_is_the_format_grub_mkpasswd_produces() -> None:
    encoded = hash_password("seapath")

    assert encoded.startswith("grub.pbkdf2.sha512.65536.")
    assert verify("seapath", encoded)
    assert not verify("wrong", encoded)


def test_isolating_cpu_zero_is_refused() -> None:
    # CPU 0 carries work the kernel cannot move, and isolating it strands the
    # machine.
    assert "cpu_zero_stays_housekeeping" in rules(validate(inventory(isolcpus="0-3")))


def test_an_unparseable_isolated_set_is_refused() -> None:
    assert "isolcpus_is_a_cpu_list" in rules(
        validate(inventory(isolcpus="most of them"))
    )


def test_two_machines_cannot_share_an_address() -> None:
    result = validate(
        Inventory(
            mode=Mode.CLUSTER,
            hosts={"node1": node(), "node2": node()},
        )
    )

    assert "addresses_are_unique" in rules(result)


def test_a_missing_ptp_interface_warns_but_does_not_refuse() -> None:
    result = validate(inventory(ptp_interface=None))

    # A hypervisor with no PTP is wrong for IEC 61850, and it is also a machine
    # someone may deliberately be commissioning without it yet.
    assert result.valid
    assert "hypervisor_has_ptp" in rules(result, Level.WARNING)


def test_an_observer_with_a_ptp_interface_warns() -> None:
    result = validate(inventory(role=Role.OBSERVER, ptp_interface="eno12419"))

    assert result.valid
    assert "observer_has_no_ptp" in rules(result, Level.WARNING)


def test_no_isolation_warns_rather_than_refusing() -> None:
    result = validate(inventory(isolcpus=None))

    assert result.valid
    assert "isolation_is_configured" in rules(result, Level.WARNING)


def test_an_address_the_machine_does_not_have_yet_is_accepted() -> None:
    # The rule the specification got wrong. At commissioning the administration
    # address in the inventory is frequently not the one the machine answers
    # on: seapath_setup_network.yaml is what makes it true. Refusing on
    # reachability would make the commissioning flow impossible.
    assert validate(inventory(ansible_host="192.168.200.200")).valid
