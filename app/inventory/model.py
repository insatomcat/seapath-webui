# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The typed shape of a SEAPATH inventory.

The reference is `inventories/examples/seapath-standalone.yaml` and
`seapath-cluster.yaml` in `seapath-ansible`. This service does not invent
variables: its job is to fill the fields those files mark `TODO`, and to write
the fixed ones the same way every time.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Mode(str, Enum):
    STANDALONE = "standalone"
    CLUSTER = "cluster"


class Role(str, Enum):
    HYPERVISOR = "hypervisor"
    OBSERVER = "observer"


# The variable naming the container image of this service. It lives in `extra`
# rather than as a field of NodeConfig: no form edits it, the seed writes it
# from what the machine is already running, and a site changes it by hand or
# from the Deployment page. Named here so the schema, the seed, the API and the UI
# all say the same two words.
WEBUI_IMAGE_VARIABLE = "seapath_webui_image"

# The group the VM roles loop over, and the line between a machine and a guest.
# It is spelled in capitals in every reference inventory and in the playbooks,
# so it is matched and written as written.
GUEST_GROUP = "VMs"


class NodeConfig(BaseModel):
    """The variables of one machine.

    Every field here is a `TODO` in the reference inventory, which is the test
    of whether it belongs: a value the examples hardcode is written by the
    renderer instead, and is not an operator's decision.
    """

    role: Role = Role.HYPERVISOR

    # Administration network
    ansible_host: str = Field(description="Administration address. ip_addr derives")
    network_interface: str = Field(description="Administration interface name")
    subnet: int = Field(default=24, ge=1, le=32, description="Prefix length")
    gateway_addr: str | None = None
    dns_servers: list[str] = Field(default_factory=list)

    # Time synchronisation. An observer has no PTP interface.
    ptp_interface: str | None = None
    ptp_domain_number: int | None = Field(default=None, ge=0, le=255)
    ntp_servers: list[str] = Field(default_factory=list)

    # Debian only, absent on Yocto.
    admin_user: str | None = None
    # Always a PBKDF2 hash, never a password. The UI computes it.
    grub_password: str | None = None

    # Real time. Expert field, and the one that changes latency guarantees.
    isolcpus: str | None = None

    # Variables this service does not model, read back from the file and
    # written out again untouched. A site that added `ceph_conf_overrides` or
    # anything else by hand keeps it: silently dropping a variable on the next
    # form submission would be a configuration change nobody asked for and
    # nobody would see until a run behaved differently.
    extra: dict[str, Any] = Field(default_factory=dict)


class Guest(BaseModel):
    """One member of the `VMs` group, which is a guest and never a machine.

    The distinction is load bearing. `deploy_vms_cluster` and
    `deploy_vms_standalone` loop over this group and take the host key as the
    libvirt domain name, while every other playbook of the collection plays
    the machines. A guest read as a machine is a machine this service would
    try to reach over SSH, scrape an exporter on, and hold against the rules
    that describe a hypervisor.

    The fields are the ones a confirmation and a file check have to name. The
    rest of what the roles read off an entry, most of it consumed by
    `guest.xml.j2`, is carried in `extra` unchanged, the way a machine's
    unmodelled variables are.
    """

    vm_disk: str | None = None
    vm_template: str | None = None
    xml_path: str | None = None
    """A libvirt XML that is not a template, which the cluster role also takes."""
    force: bool = False
    """Destroy and recreate the guest, rather than leave an existing one alone."""
    enable: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class Inventory(BaseModel):
    """The whole desired state, as the forms edit it.

    `hosts` holds the machines and `guests` the members of the `VMs` group.
    They are two kinds of thing in one file, and everything that reaches a
    machine, the SSH trust, the exporter fan out, the rules, reads the first
    of the two.
    """

    mode: Mode = Mode.STANDALONE
    hosts: dict[str, NodeConfig] = Field(default_factory=dict)
    guests: dict[str, Guest] = Field(default_factory=dict)

    def host_names(self) -> list[str]:
        return list(self.hosts)

    def guest_names(self) -> list[str]:
        return list(self.guests)

    def hypervisors(self) -> list[str]:
        return [
            name for name, node in self.hosts.items() if node.role is Role.HYPERVISOR
        ]

    def observers(self) -> list[str]:
        return [name for name, node in self.hosts.items() if node.role is Role.OBSERVER]
