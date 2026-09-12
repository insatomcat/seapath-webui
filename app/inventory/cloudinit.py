# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The network a guest is given, as the variables its entry carries.

A guest declared through the form arrives with no network at all. `guest.xml.j2`
renders an interface only where the entry declares one, and a SEAPATH VM image
built with the `SEAPATH_CLOUD_INIT` class carries no address of its own, which
is the point of building it that way. So one section of the form writes three
variables, and each has a different reader:

- `bridges`, read by `guest.xml.j2`, the interface the domain gets and the MAC
  on it;
- `cloud_init`, read by `cloud_init_seed`, which becomes the NoCloud seed the
  guest applies on its first boot: its hostname and a netplan document for that
  interface;
- `ansible_host`, read by Ansible itself, which is how a play reaches inside the
  guest afterwards.

Where the form asks for it, the seed also installs this node's public key in the
account every run connects as, and the entry names that account as
`ansible_user`. That is the trust [D41](../../docs/decisions.md) left to the
operator, and it stays within the rule D41 held it to: the key reaches the
guest through the upstream role, from the inventory, on the guest's first boot,
exactly as it would from a control machine running the same playbook.

The address is typed once and written twice, because the two statements are
different: one gives the guest an address, the other says where to find it.
Writing both from one field is what keeps them equal.

**The netplan document matches the interface by its MAC rather than by a name.**
What a guest calls its first interface is the guest's own business: `enp1s0` on
a q35 domain under systemd naming, `eth0` where that is disabled, something else
on an appliance image. None of it is readable from here. The MAC is in the
entry, because the entry is what puts it in the domain, so it is the one handle
that is true on both sides. A guest whose XML the operator brings themselves
carries its MAC in that file, and the form asks for it rather than guessing.

Nothing here writes to a machine, and nothing here builds the seed. This
produces inventory variables, the same ones a site writes by hand, and
[D48](../../docs/decisions.md) records why the seed itself is the role's to
build.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from typing import Any

from pydantic import BaseModel, Field

# The netplan netdef id. It names nothing inside the guest: `match` selects the
# device, so this is a label in a document an operator reads in a diff.
NETDEF = "primary"

# The OUI of the QEMU virtual interfaces, which is what the reference VM
# inventory writes on every guest it declares.
_OUI = (0x52, 0x54, 0x00)

_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.IGNORECASE)
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
# An interface name on the hypervisor, so the kernel's limit rather than a
# hostname's: 15 characters, and no slash or space in any of them.
_BRIDGE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,14}$", re.IGNORECASE)


class GuestNetwork(BaseModel):
    """What one form section says about a guest's network."""

    bridge: str | None = Field(
        default=None,
        description=(
            "The hypervisor bridge the guest's interface is attached to, "
            "written as `bridges` on the entry. Absent where the XML the "
            "operator brings declares the interface itself"
        ),
    )
    mac_address: str | None = Field(
        default=None,
        description=(
            "The MAC of that interface. Generated in the QEMU range when a "
            "bridge is named and this is left out, because the netplan "
            "document matches on it and `guest.xml.j2` requires one"
        ),
    )
    address: str | None = Field(
        default=None,
        description=(
            "The guest's address with its prefix, `10.0.0.42/24`. Written "
            "into the seed, and without the prefix as `ansible_host`"
        ),
    )
    gateway: str | None = Field(default=None, description="The guest's default route")
    dns: list[str] = Field(
        default_factory=list, description="The resolvers the guest is given"
    )
    dhcp: bool = Field(
        default=False,
        description=(
            "Take the address, the route and the resolvers from a lease. The "
            "guest then has no `ansible_host`, since nothing here knows what "
            "it will be given"
        ),
    )
    hostname: str | None = Field(
        default=None,
        description=(
            "The name the guest calls itself. The role defaults it to the "
            "guest's own name, which is what this leaves alone"
        ),
    )
    trust_this_node: bool = Field(
        default=False,
        description=(
            "Install this node's public key, through the seed, in the guest "
            "account runs connect as, and name that account as `ansible_user` "
            "on the entry. What lets a run from here, the latency measurement "
            "among them, reach inside the guest with nothing pasted by hand. "
            "The account's sudo rights stay the image's"
        ),
    )

    @property
    def asked_for(self) -> bool:
        """Whether the section was filled in at all."""
        return bool(
            self.bridge
            or self.mac_address
            or self.address
            or self.dhcp
            or self.dns
            or self.gateway
            or self.hostname
            or self.trust_this_node
        )

    def completed(self) -> GuestNetwork:
        """The same network with the MAC this service supplies.

        Only where a bridge is named, which is where the interface is this
        form's to declare. A form naming no bridge is configuring an interface
        somebody else's XML declares, and a MAC generated here would match
        nothing in it.
        """
        if self.bridge and not self.mac_address:
            return self.model_copy(update={"mac_address": generate_mac()})
        return self


def generate_mac() -> str:
    """A MAC for a guest's interface, in the range the examples use.

    From `secrets` rather than `random`: two guests sharing a MAC on one bridge
    is an outage that takes a packet capture to explain, and the cost of not
    having it happen is nothing.
    """
    return ":".join(f"{byte:02x}" for byte in (*_OUI, *secrets.token_bytes(3)))


def variables(
    guest: str,
    network: GuestNetwork,
    account: str | None = None,
    key_line: str | None = None,
) -> dict[str, Any]:
    """The entry's network variables, in the order they read well in the file.

    Each piece is written only where the form gave it something to say. A
    section naming a bridge and nothing else declares an interface and leaves
    the guest's own configuration alone, which is the entry for an image that
    carries its address already.

    `account` and `key_line` are this node's trust, read by the caller from the
    trust material, which this module has no business opening. They are
    written only where `trust_this_node` asked for them.
    """
    written: dict[str, Any] = {}
    if network.address and not network.dhcp:
        written["ansible_host"] = network.address.split("/", 1)[0]
    trusted = bool(network.trust_this_node and account and key_line)
    if trusted:
        # Beside the address, because the pair is what a run needs: where the
        # guest is and who to log in as. Without it Ansible connects as
        # whoever runs it, and in this container that is no account any guest
        # has.
        written["ansible_user"] = account
    if network.bridge:
        written["bridges"] = [
            {"name": network.bridge, "mac_address": network.mac_address}
        ]
    seed = _seed(guest, network)
    if trusted:
        seed["users"] = [_user(str(account), str(key_line))]
    if seed:
        written["cloud_init"] = seed
    return written


def _user(account: str, key_line: str) -> dict[str, Any]:
    """One cloud-config `users` entry: the account runs use, with this key.

    Three things are left out, and each is a decision.

    `default` is absent from the list, so cloud-init creates no distribution
    default user. On Debian that is a `debian` account with passwordless sudo,
    and a substation guest gaining one because this node wanted to log in is a
    hole nobody asked for.

    `sudo` is absent too. A SEAPATH VM image creates `ansible` with the narrow
    rights its FAI class grants, and `ALL=(ALL) NOPASSWD:ALL` written here
    would widen them behind the image's back. A guest from another image, whose
    account has no sudo, fails at `become` and says so.

    The password is left to cloud-init's default, which locks it on every
    account the list names. On `ansible` that changes nothing: the account
    logs in by key alone.
    """
    return {"name": account, "ssh_authorized_keys": [key_line]}


def _seed(guest: str, network: GuestNetwork) -> dict[str, Any]:
    """The `cloud_init` mapping: what the guest is told about itself."""
    seed: dict[str, Any] = {}
    if network.hostname and network.hostname != guest:
        # The role already defaults `local-hostname` to the guest's name, so
        # writing the name again would be an entry that says what it says
        # anyway. An operator reads these lines in a diff.
        seed["hostname"] = network.hostname
    interface = _interface(network)
    if interface:
        seed["network"] = {"ethernets": {NETDEF: interface}}
    return seed


def _interface(network: GuestNetwork) -> dict[str, Any]:
    """One netplan v2 `ethernets` entry, without the version the role adds."""
    if not network.mac_address or not (network.address or network.dhcp):
        return {}
    interface: dict[str, Any] = {"match": {"macaddress": network.mac_address}}
    if network.dhcp:
        interface["dhcp4"] = True
        return interface
    interface["addresses"] = [network.address]
    if network.gateway:
        # `to: default` rather than the `gateway4` the older documents use:
        # netplan deprecated that key and warns on it, and what the guest ends
        # up with is this route either way.
        interface["routes"] = [{"to": "default", "via": network.gateway}]
    if network.dns:
        interface["nameservers"] = {"addresses": list(network.dns)}
    return interface


def refusal(
    guest: str,
    network: GuestNetwork,
    addresses: dict[str, str] | None = None,
    macs: dict[str, str] | None = None,
) -> str | None:
    """Why this network cannot become an entry, in one sentence for the form.

    `addresses` and `macs` are what the inventory already hands out, each
    mapping the value to the host that holds it. A duplicate address is a guest
    that half works on a network somebody else is using, and a duplicate MAC on
    one bridge is worse: both of them answer, and which one a frame reaches is
    the switch's decision.

    Checked before anything is written, and every one of these reaches the
    form as the reason it refused.
    """
    taken_addresses = addresses or {}
    taken_macs = macs or {}

    if network.dhcp:
        also = [
            name
            for name, value in (
                ("an address", network.address),
                ("a gateway", network.gateway),
                ("resolvers", network.dns),
            )
            if value
        ]
        if also:
            return (
                f"A guest on DHCP takes its address, its route and its "
                f"resolvers from the lease, so {', '.join(also)} written "
                "beside it would say the guest got something the lease "
                "decides."
            )

    if (network.address or network.dhcp) and not network.mac_address:
        return (
            "An address reaches an interface, and the seed names that "
            "interface by its MAC. Name the bridge to attach one, or give the "
            "MAC of the interface the XML you brought declares."
        )

    if network.address:
        message = _address_refusal(network.address)
        if message:
            return message
        bare = network.address.split("/", 1)[0]
        owner = taken_addresses.get(bare)
        if owner and owner != guest:
            return (
                f"{bare} is already the address of {owner} in this inventory. "
                "Two hosts on one address is a network where neither is "
                "reliably reachable."
            )

    if network.gateway:
        message = _gateway_refusal(network.gateway, network.address)
        if message:
            return message

    for server in network.dns:
        if _address(server) is None:
            return f"{server!r} is not an IP address, so it is not a resolver."

    if network.mac_address:
        if not _MAC.match(network.mac_address):
            return (
                f"{network.mac_address!r} is not a MAC address. Six pairs of "
                "hexadecimal digits separated by colons, for example "
                "52:54:00:e4:ff:02."
            )
        if int(network.mac_address.split(":", 1)[0], 16) & 1:
            return (
                f"{network.mac_address} is a multicast address, so no "
                "interface can carry it. The first octet of a unicast MAC is "
                "even."
            )
        owner = taken_macs.get(network.mac_address.lower())
        if owner and owner != guest:
            return (
                f"{network.mac_address} is already the MAC of {owner}. Two "
                "interfaces with one MAC on the same bridge is a guest that "
                "receives somebody else's frames."
            )

    if network.bridge and not _BRIDGE.match(network.bridge):
        return (
            f"{network.bridge!r} cannot be the name of a bridge. It is an "
            "interface on the hypervisor, so fifteen characters at most, "
            "letters, digits, dots, dashes and underscores."
        )

    if network.hostname and not _HOSTNAME.match(network.hostname):
        return (
            f"{network.hostname!r} cannot be a hostname. Letters, digits and "
            "dashes, which is what the guest will answer to and what a "
            "certificate for it would name."
        )

    return None


def _address_refusal(value: str) -> str | None:
    address, separator, prefix = value.partition("/")
    if not separator:
        return (
            f"{value!r} carries no prefix length. netplan configures an "
            "interface with an address and its prefix, `10.0.0.42/24`, since "
            "the prefix is what says which addresses are on the same network."
        )
    if _address(address) is None:
        return f"{address!r} is not an IP address."
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return (
            f"{prefix!r} is not a prefix length for {address}. A /24 holds "
            "254 addresses, a /32 only this one."
        )
    return None


def _gateway_refusal(gateway: str, address: str | None) -> str | None:
    if _address(gateway) is None:
        return f"{gateway!r} is not an IP address, so it is not a gateway."
    if not address or _address_refusal(address):
        return None
    network = ipaddress.ip_network(address, strict=False)
    if _address(gateway) not in network:
        return (
            f"The gateway {gateway} is outside {network}, which is the network "
            f"{address} puts the guest on, so the guest could never reach it."
        )
    return None


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None
