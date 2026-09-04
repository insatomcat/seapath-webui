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
from app.inventory.model import Inventory, Mode, NodeConfig, Role

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
    return ValidationResult(findings=findings)


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
    findings: list[Finding] = []
    seen: dict[str, str] = {}
    for name, node in inventory.hosts.items():
        owner = seen.get(node.ansible_host)
        if owner is not None:
            findings.append(
                Finding(
                    level=Level.ERROR,
                    rule="addresses_are_unique",
                    host=name,
                    field="ansible_host",
                    message=(
                        f"{node.ansible_host} is already the administration "
                        f"address of {owner}."
                    ),
                )
            )
        seen[node.ansible_host] = name
    return findings


def _address(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value or "")
    except ValueError:
        return None
