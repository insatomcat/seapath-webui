# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The variables a site writes in a SEAPATH inventory, and what each one is.

An inventory is a YAML file with no schema, so an editor over it can offer
nothing: a name typed wrong is a name the file accepts, and the answer arrives
three minutes into a convergence from a role that read a variable nobody set.
This is the table that lets the service say something before then.

The table is curated, the way `references.KNOWN` and the playbook catalogue
are, and for the same reason. The collection was measured rather than guessed
at: `roles/*/defaults` and `roles/*/vars` hold 101 names between them and
almost all of them are role plumbing, `cephadm_install_registryurl` and
`configure_ha_crm_command_path`, while the variables a site actually writes are
in no `defaults` at all. A role that requires a variable does not default it.
Reading them off the `{{ }}` of the task files instead yields 418 identifiers,
124 after the obvious filtering, and that list has `stdout_lines`,
`to_datetime` and `getent_passwd` sitting next to `ceph_osd_disks` and
`cluster_ip_addr`. Offering `stdout_lines` as an inventory variable in a box
that configures substation hypervisors costs more than offering nothing.

So each entry here was read off one of four authorities, and every one of them
is checked by a test rather than trusted:

- `inventories/examples/*.yaml` in `seapath-ansible`, which is what a site
  copies and therefore what it writes;
- the roles of the installed collection, for what reads the variable and what
  it defaults to when it does;
- `templates/vm/guest.xml.j2`, for what a guest entry may carry;
- what this service already knows, in `model.NodeConfig`,
  `renderer.FIXED_HOST_VARS` and `references.KNOWN`.

What is deliberately absent is the derived tail: every other name the
collection mentions, listed and marked unreviewed the way `catalogue.resolve`
lists a playbook nobody has read. It belongs here eventually. It does not
belong here before the reviewed half exists, because a completion list is
judged on its worst entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel


class Scope(str, Enum):
    """Where the variable is written, which is not where it is read."""

    HOST = "host"
    """On one machine. Writing it on a group sets it for every machine."""
    GROUP = "group"
    """On the group whose members it describes, `hypervisors` or the cluster."""
    ANY = "any"
    """On `all` or on a machine, whichever a site prefers. Both are correct."""
    GUEST = "guest"
    """On an entry of the `VMs` group, which is a guest and never a machine."""
    CONNECTION = "connection"
    """On any host at all, machine or guest.

    Ansible's own connection variables, which describe how it reaches a host
    rather than what the host is. A guest gets them too: the reference VM
    inventory writes `ansible_host` and `ansible_user` on its entries so the
    play can wait for the guest to answer over SSH once it is created.
    """


class Kind(str, Enum):
    """The YAML shape, so a completion can offer the punctuation with it."""

    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    LIST = "list"
    MAPPING = "mapping"
    ENTRIES = "entries"
    """A list of mappings, `upload_extra_files_upload_files` and `bridges`."""


class Written(str, Enum):
    """Who writes it, which decides whether editing it here is the way."""

    FORM = "form"
    """A form of this service writes it. Editing the file by hand also works."""
    FIXED = "fixed"
    """This service writes it and offers no field for it. See the renderer."""
    HAND = "hand"
    """No form writes it. The file is the only way, which is what this is for."""


@dataclass(frozen=True)
class Term:
    """One variable, as an operator writing the file needs it explained."""

    name: str
    kind: Kind
    scope: Scope
    summary: str
    """One line, in the imperative of the file: what setting it does."""
    role: str = ""
    """The role that reads it. Empty for Ansible's own connection variables."""
    default: str = ""
    """What the role falls back to, written as the role writes it."""
    example: str = ""
    """A value from the reference inventories, never one invented here."""
    written: Written = Written.HAND
    caution: str = ""
    """What goes wrong when it is absent or wrong, when that is not obvious."""


# 1. Ansible's own, which describe the connection rather than the machine.
#
# All but the first are written by the renderer on every host and offered
# nowhere: they are what makes a generated inventory equivalent to a hand
# written one. Two of them are less inert than they look, and both are recorded
# in `renderer.FIXED_HOST_VARS` with the same warning.

_CONNECTION: tuple[Term, ...] = (
    Term(
        "ansible_host",
        Kind.STRING,
        Scope.CONNECTION,
        "The administration address every playbook reaches this machine at.",
        example="192.168.200.121",
        written=Written.FORM,
        caution="An address, never a name. SEAPATH addresses machines by address.",
    ),
    Term(
        "ip_addr",
        Kind.STRING,
        Scope.ANY,
        "The administration address again, under the name the roles read.",
        default="{{ ansible_host }}",
        example="{{ ansible_host }}",
        written=Written.FIXED,
    ),
    Term(
        "hostname",
        Kind.STRING,
        Scope.ANY,
        "What the machine is called once a run has configured it.",
        role="network_buildhosts",
        default="{{ inventory_hostname }}",
        example="{{ inventory_hostname }}",
        written=Written.FIXED,
        caution=(
            "This renames the machine. `network_buildhosts` sets the system "
            "hostname from it, so the host key here is what the machine ends "
            "up called. It is not a label."
        ),
    ),
    Term(
        "apply_network_config",
        Kind.BOOLEAN,
        Scope.ANY,
        "Let the network playbook actually configure the network.",
        role="network_basics",
        default="false",
        example="true",
        written=Written.FIXED,
        caution=(
            "`seapath_setup_network.yaml` defaults it to false, so an "
            "inventory that omits it configures no network at all, converges "
            "cleanly, and changes nothing."
        ),
    ),
    Term(
        "ansible_connection",
        Kind.STRING,
        Scope.CONNECTION,
        "How Ansible reaches the machine.",
        example="ssh",
        written=Written.FIXED,
    ),
    Term(
        "ansible_user",
        Kind.STRING,
        Scope.CONNECTION,
        "The account a run connects as, and the one this service is trusted in.",
        example="ansible",
        written=Written.FIXED,
    ),
    Term(
        "ansible_python_interpreter",
        Kind.STRING,
        Scope.CONNECTION,
        "The interpreter the modules run under on the machine.",
        example="/usr/bin/python3",
        written=Written.FIXED,
    ),
    Term(
        "ansible_remote_tmp",
        Kind.STRING,
        Scope.CONNECTION,
        "Where a module unpacks itself on the machine.",
        example="/tmp/.ansible/tmp",
        written=Written.FIXED,
    ),
    Term(
        "ansible_ssh_private_key_file",
        Kind.STRING,
        Scope.CONNECTION,
        "A private key other than the default one, for a control machine.",
        example="~/.ssh/seapath-v2.0.0-artifacts-key",
        caution=(
            "It describes a conventional control machine's key. This service "
            "reaches a machine with its own, provisioned into authorized_keys."
        ),
    ),
)


# 2. The administration network, which is what a machine is reachable on.

_NETWORK: tuple[Term, ...] = (
    Term(
        "network_interface",
        Kind.STRING,
        Scope.HOST,
        "The interface carrying the administration address.",
        role="network_buildhosts",
        example="eno1",
        written=Written.FORM,
    ),
    Term(
        "gateway_addr",
        Kind.STRING,
        Scope.ANY,
        "The default gateway of the administration network.",
        role="network_buildhosts",
        example="192.168.200.1",
        written=Written.FORM,
        caution="Outside the administration subnet it is a gateway nothing reaches.",
    ),
    Term(
        "dns_servers",
        Kind.LIST,
        Scope.ANY,
        "The resolvers the machine is configured with.",
        role="network_resolved",
        example="192.168.200.1",
        written=Written.FORM,
        caution=(
            "The cluster example writes one address as a bare string rather "
            "than a list. Both are accepted, and a list is the shape to grow."
        ),
    ),
    Term(
        "subnet",
        Kind.INTEGER,
        Scope.ANY,
        "The prefix length of the administration network, in CIDR notation.",
        role="network_buildhosts",
        example="24",
        written=Written.FORM,
    ),
    Term(
        "hosts_path",
        Kind.STRING,
        Scope.ANY,
        "A hosts file the run copies onto the machine, resolved from the playbook.",
        role="network_buildhosts",
    ),
)


# 3. Time. PTP distributes it to the guests, NTP is what is left when PTP is
# lost, and the domain number propagates to two further variables the roles
# read under their own names.

_TIME: tuple[Term, ...] = (
    Term(
        "ptp_interface",
        Kind.STRING,
        Scope.HOST,
        "The interface receiving PTP frames.",
        role="timemaster",
        example="eno12419",
        written=Written.FORM,
        caution=(
            "An observer receives no sampled values and usually has none. A "
            "hypervisor running IEC 61850 guests needs one."
        ),
    ),
    Term(
        "ptp_domain_number",
        Kind.INTEGER,
        Scope.ANY,
        "The PTP domain the machines take their time from, 0 to 255.",
        role="timemaster",
        example="0",
        written=Written.FORM,
    ),
    Term(
        "timemaster_ptp_domain_number",
        Kind.STRING,
        Scope.ANY,
        "The same domain, under the name the timemaster role reads.",
        role="timemaster",
        example="{{ ptp_domain_number }}",
        written=Written.FIXED,
    ),
    Term(
        "ptp_status_vsock_domain_number",
        Kind.STRING,
        Scope.ANY,
        "The same domain again, for the status channel the guests read it on.",
        role="ptp_status_vsock",
        example="{{ ptp_domain_number }}",
        written=Written.FIXED,
    ),
    Term(
        "ntp_servers",
        Kind.LIST,
        Scope.ANY,
        "The NTP servers that hold the clock when PTP is lost.",
        role="timemaster",
        example="185.254.101.25",
        written=Written.FORM,
    ),
)


# 4. The account and the bootloader. Both are package manager distributions
# only: a Yocto machine has neither, and an inventory omitting them is a
# legitimate one.

_ACCOUNTS: tuple[Term, ...] = (
    Term(
        "admin_user",
        Kind.STRING,
        Scope.ANY,
        "The administration account created on the machine.",
        role="configure_seapath_distro",
        example="admin",
        written=Written.FORM,
        caution=(
            "The prerequisites playbook of a package manager distribution "
            "stops on its first task without it. Yocto has no such account."
        ),
    ),
    Term(
        "grub_password",
        Kind.STRING,
        Scope.ANY,
        "The bootloader password, as a PBKDF2 hash.",
        role="configure_hardening",
        written=Written.FORM,
        caution=(
            "A hash, produced by grub-mkpasswd-pbkdf2. A password in clear in "
            "the inventory is a password in `git log`, forever."
        ),
    ),
    Term(
        "livemigration_user",
        Kind.STRING,
        Scope.GROUP,
        "The account libvirt migrates a guest over.",
        role="add_libvirtadmin_user",
        example="libvirtadmin",
    ),
)


# 5. Real time. The variable that changes the latency guarantee, and the only
# expert field the node form offers.

_REALTIME: tuple[Term, ...] = (
    Term(
        "isolcpus",
        Kind.STRING,
        Scope.GROUP,
        "The CPUs taken away from the kernel scheduler and given to the guests.",
        role="configure_hypervisor",
        example="4-N",
        written=Written.FORM,
        caution=(
            "This is the latency guarantee. Isolating every CPU leaves the "
            "housekeeping ones with nothing, this service included, and the "
            "machine reboots into it."
        ),
    ),
    Term(
        "configure_hypervisor_tuned_path",
        Kind.STRING,
        Scope.ANY,
        "A tuned profile the run copies, instead of the one the role ships.",
        role="configure_hypervisor",
    ),
)


# 6. The cluster. The ring is physical: `cluster_next_ip_addr` only means
# something in a cycle somebody cabled, which is why the form asks for the
# cabling order once and derives the rest.

_CLUSTER: tuple[Term, ...] = (
    Term(
        "team0_0",
        Kind.STRING,
        Scope.HOST,
        "The cluster interface cabled towards the next machine in the ring.",
        role="network_clusternetwork",
        example="eno2",
    ),
    Term(
        "team0_1",
        Kind.STRING,
        Scope.HOST,
        "The cluster interface cabled towards the previous machine.",
        role="network_clusternetwork",
        example="eno3",
    ),
    Term(
        "cluster_ip_addr",
        Kind.STRING,
        Scope.HOST,
        "This machine's address on the cluster network.",
        role="network_clusternetwork",
        example="192.168.55.1",
    ),
    Term(
        "cluster_next_ip_addr",
        Kind.STRING,
        Scope.HOST,
        "The next machine's address on the cluster network.",
        role="network_clusternetwork",
        example="192.168.55.2",
        caution=(
            "The ring has to close. A cycle that does not come back to "
            "this machine is a cluster that will not form."
        ),
    ),
    Term(
        "cluster_previous_ip_addr",
        Kind.STRING,
        Scope.HOST,
        "The previous machine's address on the cluster network.",
        role="network_clusternetwork",
        example="192.168.55.3",
    ),
    Term(
        "br_rstp_priority",
        Kind.INTEGER,
        Scope.HOST,
        "The RSTP priority that decides which machine holds the ring open.",
        role="network_configovs",
        example="12288",
        caution="Set on one machine of the ring, as the reference inventory does.",
    ),
    Term(
        "cephadm_network",
        Kind.STRING,
        Scope.GROUP,
        "The subnet Ceph talks on, which is the cluster network.",
        role="cephadm",
        example="192.168.55.0/24",
        caution="It has to contain every cluster_ip_addr.",
    ),
    Term(
        "ceph_osd_disks",
        Kind.LIST,
        Scope.HOST,
        "The disks Ceph takes whole, one OSD each.",
        role="cephadm",
        example="/dev/disk/by-path/pci-0000:03:00.0-scsi-0:2:1:0",
        caution=(
            "Always a by-path name. A `/dev/sdb` moves between boots, and the "
            "OSD would be created on whatever landed there. An observer has none."
        ),
    ),
    Term(
        "deploy_cephfs",
        Kind.BOOLEAN,
        Scope.GROUP,
        "Deploy CephFS on the cluster, beside the RBD pool.",
        role="deploy_cephfs",
        example="false",
    ),
    Term(
        "ceph_conf_overrides",
        Kind.MAPPING,
        Scope.GROUP,
        "Ceph settings written straight into ceph.conf, by section.",
        role="cephadm",
        caution=(
            "Ansible replaces a mapping rather than merging it. A value on a "
            "group and a value on a machine means the machine sees only its own."
        ),
    ),
    Term(
        "cephadm_spec_path",
        Kind.STRING,
        Scope.ANY,
        "A cephadm service specification the run copies, instead of the derived one.",
        role="cephadm",
    ),
)


# 7. Open vSwitch. Not required for a cluster to work, and the reference
# inventory ships it as an example of what the bridges can carry.

_OVS: tuple[Term, ...] = (
    Term(
        "ovs_bridges",
        Kind.ENTRIES,
        Scope.GROUP,
        "The OVS bridges and the ports on them, which is how a guest keeps its "
        "network across a live migration.",
        role="network_configovs",
        example="- name: brBRIDGE1",
    ),
    Term(
        "bridges",
        Kind.ENTRIES,
        Scope.GUEST,
        "The bridges this guest is attached to, and the MAC address on each.",
        role="deploy_vms",
        example="- name: br0",
    ),
)


# 8. The files a role copies from the control machine. The list is
# `references.KNOWN`, which is what answers whether a run would find each one,
# and a test holds the two together.

_FILES: tuple[Term, ...] = (
    Term(
        "upload_extra_files_upload_files",
        Kind.ENTRIES,
        Scope.ANY,
        "Files the run copies onto the machine, each with its destination and mode.",
        role="upload_extra_files",
        example="- src: '../files/mosquitto.container'",
        caution=(
            "Ansible replaces a list rather than merging it. A list on `all` "
            "and a list on one machine means that machine receives only its own."
        ),
    ),
    Term(
        "upload_extra_files_commands_to_run_after_upload",
        Kind.LIST,
        Scope.ANY,
        "Commands the run executes once the files are in place.",
        role="upload_extra_files",
        example="- systemctl daemon-reload",
    ),
    Term(
        "extra_crm_cmd_to_run",
        Kind.STRING,
        Scope.GROUP,
        "Pacemaker primitives loaded into the CIB after the cluster is configured.",
        role="configure_ha",
        caution=(
            "Read `run_once`, so it belongs on the group whose members form "
            "the cluster. A value per machine is a coin toss between them."
        ),
    ),
    Term(
        "iptables_rules_path",
        Kind.STRING,
        Scope.ANY,
        "A rules file the run copies as the firewall configuration.",
        role="iptables",
    ),
    Term(
        "iptables_rules_template_path",
        Kind.STRING,
        Scope.ANY,
        "A rules template the run renders as the firewall configuration.",
        role="iptables",
    ),
    Term(
        "syslog_conf_template",
        Kind.STRING,
        Scope.ANY,
        "The syslog-ng configuration template the run renders.",
        role="syslog_ng_client",
        default="syslog-ng.conf.j2",
    ),
    Term(
        "syslog_tls_ca",
        Kind.STRING,
        Scope.ANY,
        "The CA certificate the machine presents its syslog client with.",
        role="syslog_ng_client",
    ),
    Term(
        "syslog_tls_key",
        Kind.STRING,
        Scope.ANY,
        "The private key of the syslog client.",
        role="syslog_ng_client",
    ),
    Term(
        "syslog_tls_server_ca",
        Kind.STRING,
        Scope.ANY,
        "The CA the syslog server's certificate is checked against.",
        role="syslog_ng_client",
    ),
    Term(
        "update_swu_image_path",
        Kind.STRING,
        Scope.ANY,
        "The SWUpdate image a Yocto machine is updated from.",
        role="update",
    ),
)


# 9. Guests. Every entry of the `VMs` group, which is a libvirt domain and
# never a machine. The first group is read by the deploy_vms roles, the second
# only by `guest.xml.j2`, so a site rendering its own XML uses none of it.

_GUEST_DEPLOYMENT: tuple[Term, ...] = (
    Term(
        "vm_disk",
        Kind.STRING,
        Scope.GUEST,
        "The disk image the guest is created from.",
        role="deploy_vms",
        default="<images directory>/<name>.qcow2",
        example="../files/guest.qcow2",
        written=Written.FORM,
    ),
    Term(
        "vm_template",
        Kind.STRING,
        Scope.GUEST,
        "The libvirt XML template the guest is rendered from.",
        role="deploy_vms",
        example="../templates/vm/guest.xml.j2",
        written=Written.FORM,
    ),
    Term(
        "xml_path",
        Kind.STRING,
        Scope.GUEST,
        "A libvirt XML taken as it is, instead of a template rendered.",
        role="deploy_vms",
        caution=(
            "Read with `lookup('file')` on the control machine, which resolves "
            "against the playbook and fails just as hard as a missing template."
        ),
        written=Written.FORM,
    ),
    Term(
        "additional_disk",
        Kind.LIST,
        Scope.GUEST,
        "Further disk images attached to the guest.",
        role="deploy_vms",
        default="[]",
    ),
    Term(
        "cloud_init",
        Kind.MAPPING,
        Scope.GUEST,
        "The cloud-init seed built for the guest, naming its user data file.",
        role="cloud_init_seed",
    ),
    Term(
        "disk_extract",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Decompress the image while it is being deployed.",
        role="deploy_vms",
    ),
    Term(
        "disk_bus",
        Kind.STRING,
        Scope.GUEST,
        "The bus the disk is attached on.",
        role="deploy_vms",
        default="omit",
    ),
    Term(
        "enable",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Deploy this guest at all.",
        role="deploy_vms",
        default="true",
        written=Written.FORM,
    ),
    Term(
        "force",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Destroy an existing guest of this name and create it again.",
        role="deploy_vms",
        caution="This destroys a running guest. It is not an update in place.",
        written=Written.FORM,
    ),
    Term(
        "nostart",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Create the guest without starting it.",
        role="deploy_vms",
        default="false",
    ),
    Term(
        "autostart",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Start the guest when the hypervisor boots.",
        role="deploy_vms",
        default="true",
    ),
    Term(
        "live_migration",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Let Pacemaker move this guest without stopping it.",
        role="deploy_vms_cluster",
        default="false",
        example="true",
    ),
    Term(
        "migrate_to_timeout",
        Kind.INTEGER,
        Scope.GUEST,
        "How long Pacemaker waits for a live migration to complete.",
        role="deploy_vms_cluster",
        default="omit",
    ),
    Term(
        "migration_downtime",
        Kind.INTEGER,
        Scope.GUEST,
        "The pause a live migration is allowed to cost the guest.",
        role="deploy_vms_cluster",
        default="omit",
    ),
    Term(
        "priority",
        Kind.INTEGER,
        Scope.GUEST,
        "Which guests Pacemaker places first when it cannot place them all.",
        role="deploy_vms_cluster",
        default="omit",
    ),
    Term(
        "preferred_host",
        Kind.STRING,
        Scope.GUEST,
        "The machine Pacemaker runs the guest on when it can.",
        role="deploy_vms_cluster",
        default="omit",
    ),
    Term(
        "pinned_host",
        Kind.STRING,
        Scope.GUEST,
        "The only machine Pacemaker may run the guest on.",
        role="deploy_vms_cluster",
        default="omit",
        caution="A pinned guest does not fail over. That is the point, and the cost.",
    ),
    Term(
        "colocated_vms",
        Kind.LIST,
        Scope.GUEST,
        "Guests Pacemaker keeps on the same machine as this one.",
        role="deploy_vms_cluster",
    ),
    Term(
        "strong_colocation",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Make that colocation mandatory rather than preferred.",
        role="deploy_vms_cluster",
        default="false",
    ),
    Term(
        "crm_config_cmd",
        Kind.STRING,
        Scope.GUEST,
        "Extra crm configuration applied to this guest's resource.",
        role="deploy_vms_cluster",
        default="omit",
    ),
    Term(
        "wait_for_connection",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Wait for the guest to answer over SSH before the play moves on.",
        role="deploy_vms",
        example="true",
    ),
)


_GUEST_TEMPLATE: tuple[Term, ...] = (
    Term(
        "vm_features",
        Kind.LIST,
        Scope.GUEST,
        "What kind of domain to render: real time, isolated, secure boot.",
        role="deploy_vms",
        example='["rt", "isolated"]',
    ),
    Term(
        "cpuset",
        Kind.LIST,
        Scope.GUEST,
        "The hypervisor CPUs this guest's vCPUs are pinned to.",
        role="deploy_vms",
        example="[4, 5]",
        caution=(
            "These are the isolated CPUs. A guest pinned onto the "
            "housekeeping ones runs beside everything else on the machine."
        ),
    ),
    Term(
        "nb_cpu",
        Kind.INTEGER,
        Scope.GUEST,
        "How many vCPUs the guest has, when they are not pinned.",
        role="deploy_vms",
        default="1",
        example="1",
    ),
    Term(
        "memory",
        Kind.INTEGER,
        Scope.GUEST,
        "The memory given to the guest.",
        role="deploy_vms",
    ),
    Term(
        "emulatorpin",
        Kind.LIST,
        Scope.GUEST,
        "The CPUs the emulator threads are kept on, away from the vCPUs.",
        role="deploy_vms",
    ),
    Term(
        "rt_priority",
        Kind.INTEGER,
        Scope.GUEST,
        "The real time priority the vCPU threads are scheduled at.",
        role="deploy_vms",
        default="1",
    ),
    Term(
        "vm_domain_type",
        Kind.STRING,
        Scope.GUEST,
        "The libvirt domain type.",
        role="deploy_vms",
        default="kvm",
    ),
    Term(
        "vm_legacy_bios",
        Kind.BOOLEAN,
        Scope.GUEST,
        "Boot the guest through a legacy BIOS instead of UEFI.",
        role="deploy_vms",
    ),
    Term(
        "graphics_listen",
        Kind.STRING,
        Scope.GUEST,
        "The address the guest's graphical console listens on.",
        role="deploy_vms",
        default="127.0.0.1",
    ),
    Term(
        "rx_queue_size",
        Kind.INTEGER,
        Scope.GUEST,
        "The receive queue depth of the guest's interfaces.",
        role="deploy_vms",
    ),
    Term(
        "direct_interfaces",
        Kind.ENTRIES,
        Scope.GUEST,
        "Host interfaces handed to the guest directly, in macvtap mode.",
        role="deploy_vms",
    ),
    Term(
        "pci_passthrough",
        Kind.ENTRIES,
        Scope.GUEST,
        "PCI devices handed to the guest whole, by domain, bus, slot and function.",
        role="deploy_vms",
    ),
)


# 10. This service's own, the one variable the seed writes about itself.

_SELF: tuple[Term, ...] = (
    Term(
        "seapath_webui_image",
        Kind.STRING,
        Scope.ANY,
        "The image tag this service runs, which an apply deploys onto a machine.",
        role="seapath_setup_deploy_seapath_webui",
        caution=(
            "Never `latest`: a machine has to be able to say which code is "
            "answering on it, and a run has to be able to change it."
        ),
    ),
)


TERMS: tuple[Term, ...] = (
    _CONNECTION
    + _NETWORK
    + _TIME
    + _ACCOUNTS
    + _REALTIME
    + _CLUSTER
    + _OVS
    + _FILES
    + _GUEST_DEPLOYMENT
    + _GUEST_TEMPLATE
    + _SELF
)

BY_NAME: dict[str, Term] = {term.name: term for term in TERMS}


class Entry(BaseModel):
    """One term, as the API answers it."""

    name: str
    kind: Kind
    scope: Scope
    summary: str
    role: str = ""
    default: str = ""
    example: str = ""
    written: Written
    caution: str = ""


class Vocabulary(BaseModel):
    """Every variable this service can say something about."""

    terms: list[Entry]
    reviewed: int
    """How many were read off a role or a reference inventory by a human."""


def entry(term: Term) -> Entry:
    return Entry(**vars(term))


def vocabulary(scope: Scope | None = None) -> Vocabulary:
    """The table, optionally narrowed to what one place accepts.

    A scope narrows to what may be written there and to what may be written
    anywhere: a machine takes a host variable and an `ANY` one, and a guest
    entry takes neither.
    """
    terms = [term for term in TERMS if _in_scope(term, scope)]
    return Vocabulary(terms=[entry(term) for term in terms], reviewed=len(terms))


def _in_scope(term: Term, scope: Scope | None) -> bool:
    if scope is None:
        return True
    # How Ansible reaches a host is written wherever the host is, so these
    # belong to every answer.
    if term.scope is Scope.CONNECTION:
        return True
    if scope is Scope.GUEST:
        return term.scope is Scope.GUEST
    if term.scope is Scope.GUEST:
        return False
    return term.scope is scope or term.scope is Scope.ANY


def unknown(names: set[str]) -> set[str]:
    """The names this table cannot account for.

    The caller decides what that is worth. A site's own variable is a legitimate
    name this service has never read, so this is the input to a warning and
    never to a refusal.
    """
    return {name for name in names if name not in BY_NAME}
