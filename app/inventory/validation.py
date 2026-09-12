# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Rules a candidate inventory must satisfy before it becomes a commit.

An invalid desired state must never reach the repository, because a broken
inventory that is committed and then applied is how a cluster dies. Each rule
is named, so a refusal tells the operator which one and about which field.

Errors refuse the commit. Warnings do not, and the distinction is not
cosmetic: at commissioning the administration address in the inventory is
frequently **not** the address the machine currently answers on, because
`seapath_setup_network.yaml` is what makes it true. A rule that blocked on
reachability would make the commissioning flow impossible.
"""

from __future__ import annotations

import ipaddress
import re
from enum import Enum

from pydantic import BaseModel

from app.hosts.local import parse_cpu_list
from app.inventory.model import (
    NIC_AFFINITY_VARIABLE,
    Inventory,
    Mode,
    NodeConfig,
    Role,
    nics_affinity,
)

# A host key becomes the machine's name: `network_buildhosts` sets it from
# `hostname | default(inventory_hostname)`. So it has to be a valid hostname,
# not merely a valid Ansible label.
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

_GRUB_HASH_PREFIX = "grub.pbkdf2."


class Level(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class Finding(BaseModel):
    level: Level
    rule: str
    message: str
    field: str | None = None
    host: str | None = None


class ValidationResult(BaseModel):
    findings: list[Finding] = []

    @property
    def valid(self) -> bool:
        return not any(finding.level is Level.ERROR for finding in self.findings)

    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level is Level.ERROR]


def validate(inventory: Inventory) -> ValidationResult:
    findings: list[Finding] = []

    if not inventory.hosts:
        findings.append(
            Finding(
                level=Level.ERROR,
                rule="at_least_one_host",
                message="An inventory with no machine in it converges nothing.",
            )
        )
    if inventory.mode is Mode.STANDALONE and len(inventory.hosts) > 1:
        findings.append(
            Finding(
                level=Level.ERROR,
                rule="standalone_is_one_machine",
                message=(
                    "A standalone inventory describes exactly one machine. "
                    "Add the others by forming a cluster."
                ),
            )
        )

    for name, node in inventory.hosts.items():
        findings.extend(_validate_host(name, node))

    findings.extend(_validate_across_hosts(inventory))
    findings.extend(_validate_guests(inventory))
    findings.extend(_validate_guest_networks(inventory))
    return ValidationResult(findings=findings)


def _validate_guests(inventory: Inventory) -> list[Finding]:
    """A guest belongs to one deployment, or the file says nothing at all.

    `cluster_VMs` and `standalone_VMs` are how a file says which playbook
    creates a guest. Once it declares either of them, a guest in neither is a
    guest both playbooks claim: `deploy_vms_cluster` and
    `deploy_vms_standalone` each loop over what is left of `VMs`, so it would
    be created twice, once in the Ceph pool and once in the local one.

    A file with one flat group says nothing and is left alone: its guests are
    deployed by whichever playbook the mode calls for, which is what every
    inventory did before the two groups existed.
    """
    if not inventory.guests:
        return []
    declared = [
        name for name, guest in inventory.guests.items() if guest.deployment is not None
    ]
    if not declared:
        return []

    orphans = sorted(set(inventory.guests) - set(declared))
    if not orphans:
        return []
    return [
        Finding(
            level=Level.ERROR,
            rule="guest_belongs_to_one_deployment",
            host=name,
            message=(
                f"{name} is in neither cluster_VMs nor standalone_VMs, and "
                "this inventory declares them. Both deployment playbooks loop "
                "over what is left of VMs, so this guest would be created "
                "twice, once in the Ceph pool and once in the local one."
            ),
        )
        for name in orphans
    ]


# The two guest variables this service reads as a shape rather than as a
# scalar. The parser keeps whatever it cannot read in `extra`, which is where
# this finds it: a value in the model is a value that had the right shape.
_GUEST_SHAPES = {
    "cloud_init": "a mapping of cloud-config keys",
    "bridges": "a list of interfaces, each with a name and a MAC",
}


def _validate_guest_networks(inventory: Inventory) -> list[Finding]:
    """A guest's network variables, held against the shape the roles read.

    A warning rather than an error, for the same reason
    `malformed_nics_affinity` is one: an error refuses the commit, and the
    commit it would refuse includes the edit that fixes the guest. The run
    that would fail on it is refused where it can be, which is the
    `seed_buildable` precondition of the two deployment entries.
    """
    return [
        Finding(
            level=Level.WARNING,
            rule="malformed_guest_network",
            host=name,
            field=variable,
            message=(
                f"{variable} on {name} is not {shape}, so the roles that read "
                "it would fail rather than skip it. A deployment of this guest "
                "stops there."
            ),
        )
        for name, guest in inventory.guests.items()
        for variable, shape in _GUEST_SHAPES.items()
        if variable in guest.extra
    ]


def _validate_host(name: str, node: NodeConfig) -> list[Finding]:
    findings: list[Finding] = []

    if not _HOSTNAME.match(name):
        findings.append(
            Finding(
                level=Level.ERROR,
                rule="host_key_is_a_hostname",
                host=name,
                message=(
                    f"{name!r} is not a usable host name. The key in the "
                    "inventory becomes the machine's own hostname."
                ),
            )
        )

    address = _address(node.ansible_host)
    if address is None:
        findings.append(
            Finding(
                level=Level.ERROR,
                rule="administration_address_is_an_address",
                host=name,
                field="ansible_host",
                message=(
                    f"{node.ansible_host!r} is not an IP address. SEAPATH "
                    "addresses machines by address, not by name."
                ),
            )
        )
    elif address.is_loopback:
        findings.append(
            Finding(
                level=Level.ERROR,
                rule="administration_address_is_not_loopback",
                host=name,
                field="ansible_host",
                message=(
                    "The loopback address cannot be the administration "
                    "address: no other machine could reach this one."
                ),
            )
        )

    if not node.network_interface:
        findings.append(
            Finding(
                level=Level.ERROR,
                rule="administration_interface_is_named",
                host=name,
                field="network_interface",
                message="The administration interface must be named.",
            )
        )

    # A warning, because a Yocto machine has no such account and its
    # inventory is a legitimate one. On every other distribution the
    # prerequisites run stops on its first task without this variable, which
    # is late enough to be worth saying here.
    if not node.admin_user:
        findings.append(
            Finding(
                level=Level.WARNING,
                rule="admin_user_is_named",
                host=name,
                field="admin_user",
                message=(
                    "No administration account is named. The prerequisites "
                    "playbook of a package manager distribution needs "
                    "admin_user, and fails on its first task without it."
                ),
            )
        )

    gateway = _address(node.gateway_addr) if node.gateway_addr else None
    if node.gateway_addr and gateway is None:
        findings.append(
            Finding(
                level=Level.ERROR,
                rule="gateway_is_an_address",
                host=name,
                field="gateway_addr",
                message=f"{node.gateway_addr!r} is not an IP address.",
            )
        )
    elif gateway is not None and address is not None:
        network = ipaddress.ip_network(f"{address}/{node.subnet}", strict=False)
        if gateway not in network:
            findings.append(
                Finding(
                    level=Level.ERROR,
                    rule="gateway_is_reachable",
                    host=name,
                    field="gateway_addr",
                    message=(
                        f"The gateway {gateway} is outside {network}, so this "
                        "machine could never reach it."
                    ),
                )
            )

    for server in node.dns_servers:
        if _address(server) is None:
            findings.append(
                Finding(
                    level=Level.ERROR,
                    rule="dns_servers_are_addresses",
                    host=name,
                    field="dns_servers",
                    message=f"{server!r} is not an IP address.",
                )
            )

    if node.role is Role.OBSERVER and node.ptp_interface:
        findings.append(
            Finding(
                level=Level.WARNING,
                rule="observer_has_no_ptp",
                host=name,
                field="ptp_interface",
                message=(
                    "An observer receives no sampled values, so it usually has "
                    "no PTP interface."
                ),
            )
        )
    if node.role is Role.HYPERVISOR and not node.ptp_interface:
        findings.append(
            Finding(
                level=Level.WARNING,
                rule="hypervisor_has_ptp",
                host=name,
                field="ptp_interface",
                message=(
                    "No PTP interface. A hypervisor running IEC 61850 guests "
                    "needs one to distribute time to them."
                ),
            )
        )
    if node.ptp_interface and node.ptp_interface == node.network_interface:
        findings.append(
            Finding(
                level=Level.WARNING,
                rule="ptp_is_not_the_administration_interface",
                host=name,
                field="ptp_interface",
                message=(
                    "PTP is configured on the administration interface. That "
                    "works, but sampled values usually arrive elsewhere."
                ),
            )
        )

    if not node.ntp_servers:
        findings.append(
            Finding(
                level=Level.WARNING,
                rule="ntp_fallback_exists",
                host=name,
                field="ntp_servers",
                message=(
                    "No NTP server. Without one there is no fallback if PTP " "is lost."
                ),
            )
        )

    if node.grub_password and not node.grub_password.startswith(_GRUB_HASH_PREFIX):
        findings.append(
            Finding(
                level=Level.ERROR,
                rule="grub_password_is_a_hash",
                host=name,
                field="grub_password",
                message=(
                    "The GRUB password must be a PBKDF2 hash. A password in "
                    "clear in the inventory is a password in `git log`."
                ),
            )
        )

    findings.extend(_validate_isolcpus(name, node))
    findings.extend(_validate_nics_affinity(name, node))
    return findings


def _validate_nics_affinity(name: str, node: NodeConfig) -> list[Finding]:
    """Where the process bus interrupts are sent, against what is isolated.

    Warnings and never errors. This is a variable no form writes, carried out
    of a file a site wrote by hand, and an error here would lock an adopted
    inventory out of the editor over a line this service does not own.

    The finding worth having is the one that is silent everywhere else: an
    interface pinned to a housekeeping CPU. The role applies it, the daemon
    logs a success, the mask is exactly what was asked for, and the sampled
    values queue behind whatever else that core is doing.
    """
    raw = node.extra.get(NIC_AFFINITY_VARIABLE)
    if raw is None:
        return []
    wanted = nics_affinity(node)
    if not isinstance(raw, list) or not wanted:
        return [
            Finding(
                level=Level.WARNING,
                rule="malformed_nics_affinity",
                host=name,
                field=NIC_AFFINITY_VARIABLE,
                message=(
                    "nics_affinity names no interface and a CPU to pin it to. "
                    'It is a list of one key mappings, eno1: "4" or '
                    'eno1: "slot=sv0:4", and configure_nic_irq_affinity '
                    "applies nothing it cannot read."
                ),
            )
        ]

    isolated = set(parse_cpu_list(node.isolcpus))
    if not isolated:
        return []
    findings: list[Finding] = []
    for iface in sorted(wanted):
        outside = sorted(cpu for cpu in wanted[iface] if cpu not in isolated)
        if not outside:
            continue
        findings.append(
            Finding(
                level=Level.WARNING,
                rule="nic_irqs_land_on_an_isolated_cpu",
                host=name,
                field=NIC_AFFINITY_VARIABLE,
                message=(
                    f"{iface} sends its interrupts to CPU "
                    f"{', '.join(str(cpu) for cpu in outside)}, which "
                    f"isolcpus does not isolate. The pinning will be applied "
                    "and the sampled values will still arrive on a "
                    "housekeeping core."
                ),
            )
        )
    return findings


def _validate_isolcpus(name: str, node: NodeConfig) -> list[Finding]:
    if not node.isolcpus:
        return [
            Finding(
                level=Level.WARNING,
                rule="isolation_is_configured",
                host=name,
                field="isolcpus",
                message=(
                    "No isolated CPUs. Latency guarantees come from isolation, "
                    "so a hypervisor without it is not a real time one."
                ),
            )
        ]

    isolated = parse_cpu_list(node.isolcpus)
    if not isolated:
        return [
            Finding(
                level=Level.ERROR,
                rule="isolcpus_is_a_cpu_list",
                host=name,
                field="isolcpus",
                message=(
                    f"{node.isolcpus!r} is not a CPU list. Use the kernel "
                    "syntax, for example 4-7 or 4-7,12."
                ),
            )
        ]
    if 0 in isolated:
        return [
            Finding(
                level=Level.ERROR,
                rule="cpu_zero_stays_housekeeping",
                host=name,
                field="isolcpus",
                message=(
                    "CPU 0 cannot be isolated. It carries work the kernel "
                    "cannot move, and isolating it strands the machine."
                ),
            )
        ]
    return []


def _validate_across_hosts(inventory: Inventory) -> list[Finding]:
    """One address, one host, guests included.

    A guest's `ansible_host` is where a play reaches inside it, and the VMs
    page now writes it beside the address it gives the guest. Two hosts on one
    address is a network where neither is reliably reachable, and it makes no
    difference whether the second of them is a machine or a VM.
    """
    findings: list[Finding] = []
    seen: dict[str, str] = {}
    addressed = [(name, node.ansible_host) for name, node in inventory.hosts.items()]
    addressed += [
        (name, guest.ansible_host)
        for name, guest in inventory.guests.items()
        if guest.ansible_host
    ]
    for name, address in addressed:
        owner = seen.get(address)
        if owner is not None:
            findings.append(
                Finding(
                    level=Level.ERROR,
                    rule="addresses_are_unique",
                    host=name,
                    field="ansible_host",
                    message=(
                        f"{address} is already the administration "
                        f"address of {owner}."
                    ),
                )
            )
        seen[address] = name
    return findings


def _address(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value or "")
    except ValueError:
        return None
