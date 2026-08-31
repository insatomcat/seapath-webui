<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# The inventory as the product

The inventory is the desired state. Everything the UI does to a machine, it does
by changing this file and asking Ansible to converge. This document specifies
where it lives, who may write it, and how a form becomes YAML.

## 1. Storage

A git repository per node, at `/etc/seapath/inventory/`, containing the
inventory and nothing else.

- The working tree holds `inventory.yaml` and, later, `host_vars/` and
  `group_vars/` if the size justifies it.
- Every change through the UI is a commit whose author is the authenticated
  user and whose message is generated from the form (`network: set gateway_addr
  on node2`). `git log` is the configuration audit trail.
- The commit hash is the version of the desired state. Every run records it.
- Rollback is `git revert` followed by an apply. The UI exposes it as "restore
  this version", showing the diff first.
- The repository is exportable and clonable as is. A site that wants a real
  Ansible control machine clones it and loses nothing.

### Adopting an inventory that already exists

The service initialises the repository at first start and commits a seed
inventory describing this machine, from discovery. It only does that when the
repository holds no inventory, which is what makes the other direction possible:
a site that already keeps its SEAPATH inventory in git puts it in place first,
and the service adopts it, history included.

```bash
# Before the first start. The directory is created by the unit, and git clone
# wants it empty or absent.
git clone https://git.example/seapath-inventory /etc/seapath/inventory
systemctl start seapath-webui
```

If the service has already started once, the repository holds a seed commit
describing this machine and nothing else. Removing the directory and cloning
over it loses nothing that was not derived from the machine itself.

### The service has to be able to reproduce a file before it writes it

Adoption was specified above before any real inventory had been read. The first
one that was tried, from a three node cluster deployed the conventional way,
showed the specification was optimistic: the service read a fraction of the file
and would have destroyed the rest on the first form save.

So there is now a rule, and it is mechanical. `app/inventory/fidelity.py`
resolves the file the way Ansible resolves it, parses it into the model, renders
the model back, resolves that, and compares. A file the service produced comes
back identical. A file it cannot reproduce is served, exported and applied, and
never written, with the list of what a save would have changed shown on the
configuration page.

`app/inventory/resolve.py` is what makes the comparison meaningful, and a test
asserts it agrees with `ansible-inventory --list` variable for variable on the
reference inventories and on a real one.

What the check found on that first real inventory, and what the writer therefore
still has to learn, is in [decisions.md](decisions.md#d14):

- groups declared under `all.children` rather than at the top level, which is
  the shape the parser missed entirely, reading the file as a standalone one;
- variables held on groups rather than on hosts, which is where a real
  inventory keeps almost all of them;
- groups the service has never heard of, `mons`, `osds` and `clients`;
- `hostname` set to something other than the host key, so that rewriting the
  key renames a running machine;
- `subnet` absent, where the renderer writes one on every host.

Four things have to be true of what lands there:

- **One file, `inventory.yaml`, at the root.** That is the only file read and
  the only file written. An inventory kept as `seapath-cluster.yaml` is renamed
  with `git mv` and committed.
- **This machine appears under `all.hosts`, keyed by its own hostname.** The
  host key is what `inventory_hostname` resolves to, and `hostname` renames the
  machine, so a key that does not match is not a naming detail.
- **Groups carry the meaning they carry upstream.** `cluster_machines` present
  means cluster mode, membership of `observers` rather than `hypervisors` is the
  role. That is how the reference inventories express it and how the playbooks
  read it.
- **Variables this service does not model survive.** A variable held on a
  host is parsed into `extra` and written back unchanged. A variable held on a
  group is read for display and is not preserved by the writer yet, which is
  why an inventory that has any is read only.

Two limits of M1 are worth knowing before adopting a cluster inventory. The
configuration form edits the first host under `all.hosts`, because M1 configures
one machine. And a run plays every host the inventory declares, since the
adapter passes no `--limit`, while M1 only provisions the trust between this
node and itself: applying against hosts this node has no trust with fails on
them, as unreachable.

The repository is an ordinary git repository, so a remote survives the clone.
Nothing pushes or pulls it by itself: replication between nodes arrives with the
cluster, at M3.

## 2. Who may write

Single writer, under quorum.

- **Standalone**: the node owns its repository outright. No coordination.
- **Cluster**: writes are accepted only by the node holding the configuration
  lead, and only while quorum holds. A UI opened on another node forwards the
  write to the lead over the mutual TLS channel, so the operator never has to
  know which node is the lead.
- **Between the two**, from the first invitation until corosync is running,
  there is no quorum and no nodeid to elect a lead from. During that window the
  lead is the **founder**, meaning the node that issued the invitation, and it
  is the only node that accepts writes. Once corosync is up, the lead becomes
  the lowest live nodeid with quorum. Getting this window wrong is how two nodes
  end up each believing they own the inventory while the cluster is being
  formed.
- After a successful commit, the lead pushes to every member. A member that
  could not be updated is flagged, and the cluster view shows the stale copies
  until they catch up. Applying from a stale copy is refused.
- Without quorum the inventory is read only. A cluster that cannot agree on its
  membership has no business rewriting its desired state.

This is not a distributed filesystem and not `pmxcfs`. It is a small repository
synchronised on explicit writes, which is the whole reason it is affordable.

## 3. Seeding by discovery

On first boot the node writes its own entry from what it can observe, so that
the operator starts from a filled form rather than from a blank file:

| Discovered | Becomes |
|---|---|
| interface carrying the default route, and its address | `ansible_host`, `network_interface`, `ip_addr` |
| default gateway, resolvers, prefix length | `gateway_addr`, `dns_servers`, `subnet` |
| hostname | the host key in the inventory, which is what `inventory_hostname` resolves to, and the `hostname` variable |
| CPU topology, and the kernel command line if it already carries an isolated set | `isolcpus`, proposed from the topology on a freshly installed machine, since the ISO has not applied any isolation yet |
| block devices by path, with their claim state | candidates for `ceph_osd_disks` |
| NICs with link state and driver, PTP capability | candidates for `ptp_interface`, `team0_0`, `team0_1` |

Discovery proposes, it never decides. Every discovered value is presented as a
prefilled field the operator confirms, because a NIC that is up is not
necessarily the NIC that carries sampled values.

## 4. Form to variable mapping

The reference is `inventories/examples/seapath-cluster.yaml` and
`seapath-standalone.yaml`. The UI does not invent variables. Its job is to fill
the fields those files mark `TODO`.

### Node, always

| Form field | Variable | Notes |
|---|---|---|
| Administration address | `ansible_host` | `ip_addr` derives from it |
| Administration interface | `network_interface` | |
| Gateway, DNS, prefix | `gateway_addr`, `dns_servers`, `subnet` | |
| NTP servers | `ntp_servers` | |
| PTP interface | `ptp_interface` | omitted on an observer |
| PTP domain | `ptp_domain_number` | propagates to the timemaster variables |
| Admin account | `admin_user` | Debian only |
| GRUB password | `grub_password` | stored as a PBKDF2 hash, generated by the UI, never in clear |
| Isolated CPUs | `isolcpus` | expert field, warned about, see section 6 |

### Cluster

| Form field | Variable | Notes |
|---|---|---|
| Role | membership of `hypervisors` or `observers` | an observer has neither `ceph_osd_disks` nor `ptp_interface` |
| Cluster interfaces | `team0_0`, `team0_1` | the two ring interfaces, towards the next and previous node |
| Cluster address | `cluster_ip_addr` | |
| Ring neighbours | `cluster_next_ip_addr`, `cluster_previous_ip_addr` | computed by the UI from the ring order, not typed |
| Ring priority | `br_rstp_priority` | set on one node only, as in the example |
| Cluster subnet | `cephadm_network` | derived from the cluster addresses |
| OSD disks | `ceph_osd_disks` | selected from the discovered devices, always `by-path` |
| CephFS | `deploy_cephfs` | |

The ring is the reason the UI caps a cluster at three nodes: the topology is
physical, and `cluster_next_ip_addr` only makes sense in a cycle the operator
actually cabled. The form asks for the cabling order once and derives the rest.

### Fixed values

`ansible_connection`, `ansible_python_interpreter`, `ansible_remote_tmp`,
`ansible_user`, `hostname`, `ip_addr` and `apply_network_config` are written by
the UI and not editable. They are what makes the generated inventory equivalent
to a hand written one.

Two of them are less inert than they look, and both were found by reading the
roles rather than the examples:

- `hostname` **renames the machine**. `network_buildhosts` sets the system
  hostname from `hostname | default(inventory_hostname)`, so the host key in
  this file is what the machine ends up called. It is not a label.
- `apply_network_config` must be `true` and must be written.
  `seapath_setup_network.yaml` defaults it to `false`, so an inventory that
  omits it configures no network at all, converges cleanly, and changes
  nothing. The standalone example sets it for exactly this reason.

### Variables this service does not model

They are preserved. The inventory is read back into the model on every edit,
and anything the model does not know about is written out again untouched. A
site that added `ceph_conf_overrides` or a variable of its own keeps it:
silently dropping one on the next form submission would be a configuration
change nobody asked for and nobody would see until a run behaved differently.

What is not preserved is the layout. The service rewrites the file, so comments
and ordering are its own.

## 5. Validation

Before a commit is accepted:

- schema validation of the known variables, with types and ranges;
- cross node coherence: no duplicate addresses, a ring that closes, a
  `cephadm_network` containing every `cluster_ip_addr`, an observer with no OSD
  disk;
- `ansible-inventory --list` parses the result, which catches YAML mistakes the
  schema would miss.

A commit that fails validation is refused with the failing rule named. Invalid
desired state never reaches the repository, because a broken inventory that is
committed then applied is how a cluster dies.

**Errors refuse the commit, warnings do not,** and the distinction carries
weight. "This hypervisor has no PTP interface" is worth saying and is not worth
refusing: it may be a machine somebody is deliberately commissioning without
one yet. "The gateway is outside the subnet" is worth refusing, because the
network role will apply it and the machine will lose its route.

**Reachability is not a commit rule.** An earlier version of this document
listed "`ansible_host` answers on the trust channel" among the conditions for
accepting a commit, and that would make commissioning impossible: at
commissioning the administration address in the inventory is frequently *not*
the address the machine currently answers on, because
`seapath_setup_network.yaml` is precisely what makes it true. Declaring an
address the machine does not have yet is the normal use of an inventory, not an
error in one. Reachability is checked as a **precondition of an apply**, where
it names the address it could not reach, and it is offered as an explicit check
from the inventory view.

Some rules are also rules about the machine and not only about the file. `0`
cannot be in `isolcpus`, because CPU 0 carries work the kernel cannot move and
isolating it strands the host. `grub_password` must already be a PBKDF2 hash,
because the inventory goes into git and a password in clear is a password in
the audit trail forever.

## 6. Fields that touch real time

`isolcpus`, and anything the tuning roles consume, change latency guarantees.
They are editable, because refusing to expose them would push the operator back
to a shell, but:

- they live behind an expert section that is collapsed by default;
- changing them shows what will be restarted and requires typing the node name
  to confirm;
- the previous value stays visible in the diff, and `git revert` is one click.

The rule is that the UI never makes an RT relevant change look routine.

## 7. What is not in the inventory

The runtime plane. Whether a VM is currently running, on which host it landed,
whether it was migrated by Pacemaker last night. None of that is desired state,
none of it belongs in git, and putting it there would make every convergence
run fight the cluster manager.

The VM **definition**, meaning its image, its libvirt XML and its placement
preferences, is configuration and does belong in the inventory, consumed by
`deploy_vms_cluster.yaml` and `deploy_vms_standalone.yaml`.
