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

from app.hosts.local import parse_cpu_list


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

# Which of the two deployments a guest belongs to, when the file says. Declared
# as children of `VMs`, so `groups['VMs']` stays the union and every play that
# configures the guests themselves is unchanged. A file with one flat group
# says nothing, and its guests are deployed by whichever playbook the mode
# calls for.
CLUSTER_GUEST_GROUP = "cluster_VMs"
STANDALONE_GUEST_GROUP = "standalone_VMs"


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


# `configure_nic_irq_affinity`'s variable, and the one placement in this
# inventory that decides whether a sampled value is received on an isolated
# core or behind whatever the housekeeping CPUs are doing. It is not a field of
# `NodeConfig`: no form writes it yet, and a site's own file already carries
# it, so it is read out of `extra` where the parser leaves every variable this
# service does not model.
NIC_AFFINITY_VARIABLE = "nics_affinity"


def nics_affinity(node: NodeConfig) -> dict[str, list[int]]:
    """`nics_affinity`, as the interfaces and the CPUs it names.

    The role takes a list of one key mappings, and the value is either a CPU
    list, `9` or `7,10-13`, or `slot=<name>:<cpu>`, which pins to the same CPU
    and additionally declares a seapath-alloc slot on it. Both forms pin, so
    both are read here for the CPUs; the slot is the pool view's to show.

    What it cannot read it leaves out rather than raising: this is an
    unmodelled variable of a file a site wrote by hand, and the writers here
    are a check and a warning, neither of which may refuse a save over it.
    `malformed_nics_affinity` in `validation.py` is what says so out loud.
    """
    raw = node.extra.get(NIC_AFFINITY_VARIABLE)
    if not isinstance(raw, list):
        return {}
    found: dict[str, list[int]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        for iface, value in entry.items():
            cpus = _pinned_cpus(value)
            if cpus:
                found[str(iface)] = cpus
    return found


def _pinned_cpus(value: Any) -> list[int]:
    text = str(value).strip()
    if text.startswith("slot="):
        name, separator, cpus = text.partition("=")[2].partition(":")
        # `slot=` with no name or no CPU pins nothing: the daemon logs it and
        # returns. Nothing is declared, so nothing is checked.
        if not separator or not name:
            return []
        text = cpus
    return parse_cpu_list(text)


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
    deployment: Mode | None = None
    """Which playbook creates this guest, when the file says.

    `cluster_VMs` and `standalone_VMs` are how it says it. `None` where the
    file has one flat `VMs` group, and then the deployment is the file's own
    mode: the group is claimed whole by whichever playbook is run.
    """
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
    cluster_members: list[str] = Field(default_factory=list)
    """The machines of `cluster_machines`, which is a smaller set than `hosts`.

    A file may declare a cluster and a standalone machine at once, and several
    do: an administration box beside the three hypervisors. `mode` says what
    the file describes as a whole; this says which of its machines Pacemaker
    knows about, and that is a different question.
    """

    def host_names(self) -> list[str]:
        return list(self.hosts)

    def placement_hosts(self) -> list[str]:
        """The machines a guest may be placed on.

        Cluster members that are hypervisors, which is `hypervisors:&cluster_machines`,
        the pattern `cluster_setup_libvirt` plays. A standalone machine has no
        Pacemaker to hear the constraint, and an observer is a cluster member
        with no libvirt to run the guest: naming either is a guest that never
        starts and a constraint nobody can read.
        """
        return [
            name
            for name in self.cluster_members
            if name in self.hosts and self.hosts[name].role is Role.HYPERVISOR
        ]

    def guest_names(self) -> list[str]:
        return list(self.guests)

    def deployment_of(self, name: str) -> Mode:
        """Which playbook creates this guest.

        The group it is in when the file declares the two, and the file's own
        mode otherwise. One place for the fallback, because getting it wrong
        means offering an operator a Pacemaker option on a guest libvirt owns.
        """
        guest = self.guests.get(name)
        if guest is not None and guest.deployment is not None:
            return guest.deployment
        return self.mode

    def hypervisors(self) -> list[str]:
        return [
            name for name, node in self.hosts.items() if node.role is Role.HYPERVISOR
        ]

    def observers(self) -> list[str]:
        return [name for name, node in self.hosts.items() if node.role is Role.OBSERVER]
