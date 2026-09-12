# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The network a guest is given, as the variables its entry ends up carrying.

One form section writes three variables with three different readers, so what
this holds shut is the shape of each one: the interface `guest.xml.j2` renders,
the netplan document `cloud_init_seed` puts in the seed, and the address a play
reaches the guest at. Every refusal has its accepting case beside it, because a
rule that refuses a valid network is worse than no rule.
"""

from __future__ import annotations

import re

import pytest

from app.inventory import cloudinit
from app.inventory.cloudinit import GuestNetwork

MAC = "52:54:00:e4:ff:02"


def test_a_static_address_writes_the_interface_the_seed_and_the_address() -> None:
    network = GuestNetwork(
        bridge="br0",
        mac_address=MAC,
        address="10.0.0.42/24",
        gateway="10.0.0.1",
        dns=["9.9.9.9"],
    )

    assert cloudinit.variables("vm1", network) == {
        # Where a play reaches the guest, which is the same address without its
        # prefix. Typed once, written twice.
        "ansible_host": "10.0.0.42",
        "bridges": [{"name": "br0", "mac_address": MAC}],
        "cloud_init": {
            "network": {
                "ethernets": {
                    "primary": {
                        # The interface is found by its MAC. What the guest
                        # calls it is the guest's business.
                        "match": {"macaddress": MAC},
                        "addresses": ["10.0.0.42/24"],
                        "routes": [{"to": "default", "via": "10.0.0.1"}],
                        "nameservers": {"addresses": ["9.9.9.9"]},
                    }
                }
            }
        },
    }


def test_the_hostname_is_written_only_where_it_differs_from_the_name() -> None:
    # The role defaults `local-hostname` to the guest's name, so writing the
    # name again would be an entry saying what it says anyway.
    same = GuestNetwork(hostname="vm1")
    other = GuestNetwork(hostname="substation-hmi")

    assert cloudinit.variables("vm1", same) == {}
    assert cloudinit.variables("vm1", other) == {
        "cloud_init": {"hostname": "substation-hmi"}
    }


def test_a_bridge_alone_declares_an_interface_and_configures_nothing() -> None:
    # The entry for an image that carries its own address: the guest gets a
    # NIC, and nothing tells it what to do with it.
    network = GuestNetwork(bridge="br0", mac_address=MAC)

    assert cloudinit.variables("vm1", network) == {
        "bridges": [{"name": "br0", "mac_address": MAC}]
    }


def test_dhcp_writes_no_address_anywhere() -> None:
    # Including no `ansible_host`: nothing here knows what the lease will give
    # the guest, and a guessed address is a run that dies on `unreachable`.
    network = GuestNetwork(bridge="br0", mac_address=MAC, dhcp=True)

    written = cloudinit.variables("vm1", network)

    assert "ansible_host" not in written
    assert written["cloud_init"]["network"]["ethernets"]["primary"] == {
        "match": {"macaddress": MAC},
        "dhcp4": True,
    }


def test_an_empty_section_writes_nothing() -> None:
    assert cloudinit.variables("vm1", GuestNetwork()) == {}
    assert GuestNetwork().asked_for is False
    assert GuestNetwork(bridge="br0").asked_for is True


def test_a_named_bridge_gets_a_mac_and_a_brought_xml_does_not() -> None:
    # The MAC is this service's to supply where the interface is this form's to
    # declare. A form naming no bridge is configuring an interface somebody
    # else's XML declares, and a MAC generated here would match nothing in it.
    generated = GuestNetwork(bridge="br0").completed()
    brought = GuestNetwork(address="10.0.0.42/24").completed()

    assert generated.mac_address is not None
    assert re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", generated.mac_address)
    # The range the reference VM inventory writes on every guest it declares.
    assert generated.mac_address.startswith("52:54:00:")
    assert brought.mac_address is None
    # A MAC the operator typed is never replaced.
    assert GuestNetwork(bridge="br0", mac_address=MAC).completed().mac_address == MAC


def test_two_generated_macs_differ() -> None:
    assert cloudinit.generate_mac() != cloudinit.generate_mac()


# The refusals. Each one is a sentence the form shows, so each is asserted by
# what it names rather than by its whole text.


def test_a_valid_network_is_refused_for_nothing() -> None:
    network = GuestNetwork(
        bridge="br0",
        mac_address=MAC,
        address="10.0.0.42/24",
        gateway="10.0.0.1",
        dns=["9.9.9.9", "1.1.1.1"],
        hostname="substation-hmi",
    )

    assert cloudinit.refusal("vm1", network) is None


@pytest.mark.parametrize(
    "field, value",
    [("address", "10.0.0.42/24"), ("gateway", "10.0.0.1"), ("dns", ["9.9.9.9"])],
)
def test_dhcp_beside_what_a_lease_carries_is_refused(field: str, value) -> None:
    network = GuestNetwork(bridge="br0", mac_address=MAC, dhcp=True, **{field: value})

    refusal = cloudinit.refusal("vm1", network)

    assert refusal is not None
    assert "lease" in refusal


def test_an_address_with_no_interface_to_put_it_on_is_refused() -> None:
    # Neither a bridge for this form to attach nor the MAC of an interface the
    # operator's XML declares. The seed would configure an interface that
    # exists nowhere, and the guest would come up with no network at all.
    refusal = cloudinit.refusal("vm1", GuestNetwork(address="10.0.0.42/24"))

    assert refusal is not None
    assert "MAC" in refusal


def test_an_address_without_its_prefix_is_refused() -> None:
    refusal = cloudinit.refusal(
        "vm1", GuestNetwork(mac_address=MAC, address="10.0.0.42")
    )

    assert refusal is not None
    assert "prefix" in refusal


def test_something_that_is_not_an_address_is_refused() -> None:
    refusal = cloudinit.refusal(
        "vm1", GuestNetwork(mac_address=MAC, address="10.0.0.512/24")
    )

    assert refusal is not None
    assert "not an IP address" in refusal


def test_a_gateway_outside_the_guests_network_is_refused() -> None:
    # netplan accepts it and the guest ends up with no default route, which is
    # a guest that answers on its own subnet and nowhere else.
    network = GuestNetwork(mac_address=MAC, address="10.0.0.42/24", gateway="10.0.1.1")

    refusal = cloudinit.refusal("vm1", network)

    assert refusal is not None
    assert "10.0.0.0/24" in refusal


def test_a_gateway_inside_it_is_accepted() -> None:
    network = GuestNetwork(
        mac_address=MAC, address="10.0.0.42/24", gateway="10.0.0.254"
    )

    assert cloudinit.refusal("vm1", network) is None


def test_a_resolver_that_is_not_an_address_is_refused() -> None:
    network = GuestNetwork(mac_address=MAC, address="10.0.0.42/24", dns=["dns.example"])

    refusal = cloudinit.refusal("vm1", network)

    assert refusal is not None
    assert "resolver" in refusal


@pytest.mark.parametrize("mac", ["52:54:00:e4:ff", "52-54-00-e4-ff-02", "not a mac"])
def test_something_that_is_not_a_mac_is_refused(mac: str) -> None:
    refusal = cloudinit.refusal("vm1", GuestNetwork(bridge="br0", mac_address=mac))

    assert refusal is not None
    assert "MAC address" in refusal


def test_a_multicast_mac_is_refused() -> None:
    # No interface can carry one, so the domain would fail to start with a
    # message about its MAC.
    refusal = cloudinit.refusal(
        "vm1", GuestNetwork(bridge="br0", mac_address="53:54:00:e4:ff:02")
    )

    assert refusal is not None
    assert "multicast" in refusal


def test_an_address_another_host_already_has_is_refused() -> None:
    network = GuestNetwork(mac_address=MAC, address="10.0.0.42/24")

    refusal = cloudinit.refusal("vm1", network, {"10.0.0.42": "node1"})

    assert refusal is not None
    assert "node1" in refusal


def test_the_guests_own_address_is_not_a_collision_with_itself() -> None:
    # What a replacement does: the same guest, declared again, with the address
    # the file already gives it.
    network = GuestNetwork(mac_address=MAC, address="10.0.0.42/24")

    assert cloudinit.refusal("vm1", network, {"10.0.0.42": "vm1"}) is None


def test_a_mac_another_guest_already_has_is_refused() -> None:
    network = GuestNetwork(bridge="br0", mac_address=MAC)

    refusal = cloudinit.refusal("vm1", network, {}, {MAC: "vm2"})

    assert refusal is not None
    assert "vm2" in refusal


def test_a_mac_is_compared_whatever_its_case() -> None:
    network = GuestNetwork(bridge="br0", mac_address=MAC.upper())

    refusal = cloudinit.refusal("vm1", network, {}, {MAC: "vm2"})

    assert refusal is not None
    assert "vm2" in refusal


def test_a_bridge_name_no_interface_could_carry_is_refused() -> None:
    refusal = cloudinit.refusal(
        "vm1", GuestNetwork(bridge="a bridge with a very long name")
    )

    assert refusal is not None
    assert "fifteen characters" in refusal


def test_a_hostname_the_guest_could_not_answer_to_is_refused() -> None:
    refusal = cloudinit.refusal("vm1", GuestNetwork(hostname="not a hostname"))

    assert refusal is not None
    assert "hostname" in refusal
