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

### Importing one from the browser

`POST /inventory/import` takes the file whole and commits it whole, and the
configuration page offers it as a file picker. That is the deployment path this
service exists for: three machines installed from the ISO, one inventory, and a
cluster to converge, with no shell anywhere. The version it replaces stays one
`git revert` away, so importing over the seed destroys nothing.

The document is parsed and validated before it is committed. A file that is not
YAML is refused with 400, a file that breaks a rule with 422 and the rule named,
and neither reaches the repository.

### Editing one, without rewriting it

A save against an imported inventory is an **edit**. The lines that change are
the lines the form changed, and the rest of the file survives byte for byte,
comments and all.

This is the second design of this path. The first rendered the whole file from
the model on every save, which is harmless for a file this service wrote and
catastrophic for a file an engineer wrote: the model holds a dozen fields, a
real inventory holds fifty, and the render kept the dozen. Read the first real
inventory this service met, in `tests/golden/adopted-cluster.yaml`, and count
what a form submission would have destroyed. [D14](decisions.md#d14) has the
list.

So:

- `app/inventory/resolve.py` resolves a document the way Ansible does, groups
  included, and a test asserts it agrees with `ansible-inventory --list`
  variable for variable;
- `app/inventory/editor.py` uses `ruamel.yaml` as a parser that reports the
  line and column of every value, and writes a change as a splice into the
  original text;
- `app/inventory/fidelity.py` resolves both versions after every edit and
  refuses the commit unless the difference is exactly what the form asked for.

Two rules decide where a change lands. A variable already on the host is
changed where it sits. A variable the host inherits from a group is written on
the host as an override, because the form edits one machine and rewriting the
group would silently change the other two.

An inventory this service rendered itself keeps being rendered, so a freshly
installed machine keeps the canonical shape.

What the editor refuses, rather than approximates: adding or removing a machine,
and changing a role, which means moving a host between groups. Both are cluster
formation, and they arrive with it.

### The editor is the form and the file

Two ways in, because a form that models a dozen variables cannot be the only
way to change a file that holds fifty.

- **The form** edits one machine at a time and **any** machine in the
  inventory, chosen from a selector that defaults to this one. A three node
  cluster is configured from one browser.
- **The file** is edited directly, in a text area on the same page, and saved
  with `PUT /inventory/raw`. `POST /inventory/raw/check` says what is wrong
  without committing anything.

A whole file arriving by either route is checked three ways before it becomes a
commit: it parses into something shaped like an inventory, it satisfies the
rules of section 5, and `ansible-inventory --list` accepts it. That last one
has a trap in it worth naming: **`ansible-inventory` exits 0 on a file it could
not read**, having printed a warning and returned an empty inventory. Reading
the exit status alone would wave through exactly the files the check exists to
catch, so its output is read too.

### Which entry describes this machine

The host key is the obvious answer and frequently the wrong one. A site is free
to key its inventory `node1`, `node2`, `node3` and carry the real names in
`hostname`, which `network_buildhosts` honours. `this_host` in `GET /inventory`
is the answer: the key, then `hostname`, then the administration address against
the addresses this machine answers on. A node that recognises no entry says so,
because putting an operator in front of another machine's configuration is worse
than admitting the file does not describe this one.

Four things have to be true of what lands there:

- **One file, `inventory.yaml`, at the root.** That is the only file read and
  the only file written. An inventory kept as `seapath-cluster.yaml` is renamed
  with `git mv` and committed.
- **This machine appears somewhere in it.** The host key is what
  `inventory_hostname` resolves to, and `hostname` overrides the machine's
  name, so a site may key its hosts `node1..node3` and name the machines
  something else. Both are recognised, see "Which entry describes this
  machine".
- **Groups carry the meaning they carry upstream.** `cluster_machines` present
  means cluster mode, membership of `observers` rather than `hypervisors` is the
  role. That is how the reference inventories express it and how the playbooks
  read it.
- **Variables this service does not model survive**, wherever they live. Host
  variables and group variables alike are read resolved, so the form is filled
  from them, and a save touches only the lines it changes.

One limit of M1 is worth knowing before importing a cluster inventory: a run
plays every host the inventory declares, since the adapter passes no `--limit`,
while M1 only provisions the trust between this node and itself. Applying
against the other machines fails on them, as unreachable, until the trust mesh
of [cluster-join.md](cluster-join.md) exists at M3. Importing, reading and
editing all work today.

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
