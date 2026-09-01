<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Exposed playbooks

The UI runs whole playbooks, never a free form selection of tags. This document
is the catalogue behind `GET /api/v1/playbooks`, and adding an entry to it is a
deliberate act, not a consequence of a playbook existing upstream.

## 1. Why whole playbooks

The tags in `seapath-ansible` were not designed as a public interface, and
`ansible.cfg` already skips `package-install` by default. A tag selector looks
flexible and produces combinations nobody has ever run. A whole playbook is what
the CI executes, so it is the only granularity with evidence behind it.

Scoping comes later, as a small curated set of named operations, if and only if
an operational need is proven. When that happens, each scoped operation gets its
own catalogue entry with its own tags baked in, and never a tag field in the UI.

## 2. Attributes of an entry

Each entry carries what the UI needs to present the run honestly:

| Attribute | Meaning |
|---|---|
| `targets` | Inventory groups the playbook plays against |
| `preview` | `full`, `partial` or `none`, see section 3 |
| `reboots` | Whether the playbook reboots its targets, and whether that is gated by a variable |
| `disruption` | What an operator should expect on a live machine |
| `requires` | Preconditions checked before the run is offered |
| `variables` | The only variables `POST /runs` accepts for this playbook, each with a type and a validation rule. Empty for most entries |

The `targets` attribute is copied from the playbook's own `hosts:` lines and is
not a parameter the caller can override. A caller cannot narrow a run to one
node: Ansible would accept it and the result would be meaningless, since
`cluster_setup_ha.yaml` on a single member of three is not a smaller version of
forming a cluster.

That has a consequence the preconditions have to carry. A run plays **every**
host the inventory declares, and a node begins life with an SSH trust with
itself alone. `peer_reachable` is therefore checked before a run is offered: it
asks whether a key would be presented to the other machines and whether their
host keys are known, and it names the machines that fail. Without it the
operator confirms a disruptive convergence and learns two hosts were
unreachable a minute later, which is a late and expensive way to find out.
Reachability here is about credentials; whether the network answers is the
run's own business, and it says so host by host.

### Which collection a run actually ran

A run records the collection it used, read from disk at launch rather than from
a value baked at build time. `galaxy.yml` declares the same version on every
branch, so `2.0.0` says nothing for a site pinned to a branch, which is the
normal case while a feature is being landed upstream.

`FILES.json`, which `ansible-galaxy` writes beside the collection, holds a
sha256 per file. Hashing that one file fingerprints the whole tree, so a run
records `2.0.0+49c8b604e913`: two branches differ, the same content matches,
and reinstalling the same code reads the same. The build label from the image
is appended only when it says something the fingerprint cannot, such as the
branch it was built from.

This is half of the reproducibility pair. The other half is the inventory
commit, and together they answer "which code, against which desired state".

### What a run is given

A run plays the playbooks of the installed collection, and it reads the site's
own files from a mirror of that collection built in the run directory: one
symlink per entry of the installed tree, the inventory folder and the artefacts
overlaid at its root. That is what makes `src: '../inventories_private/quadlet.network'`,
written against a control machine, resolve on a node with no control machine
anywhere. [D17](decisions.md#d17) and
[inventory.md](inventory.md#1bis-the-folder-because-an-inventory-is-rarely-alone)
have the mechanism.

The folder is copied into the run rather than pointed at, so the trace says what
was pushed rather than what the repository holds now, and the run record lists
every file it was given with its size and its store. An artefact leaves no trace
in `git log`, so that listing is where "which image did this run push" is
answered.

### Where the time went

`ansible-runner` reports a `duration` on every host result, so the run view
lists the tasks by the time they took without `profile_tasks` being enabled and
without anything parsing stdout. The number kept per task is the **longest**
host rather than the sum: hosts run in parallel with `forks = 20`, and a sum
would describe a run nobody waited through.

## 3. Preview quality

The value is read off the modules a playbook's roles use, so it can be checked
against the collection rather than argued about:

- **`full`**: every task runs a module check mode understands, `template`,
  `copy`, `file`, `user`, `systemd`. The preview is a real diff of what an apply
  would write.
- **`partial`**: some tasks are `command` or `shell`. Check mode skips them, the
  run still reaches the end, and what it reports is a subset. Most of
  `configure_ha`, `cephadm` and three of the network roles are in this case.
- **`none`**: a preview would crash or say nothing. Either the playbook is
  command driven from end to end, or a later task reads the `.stdout` of a
  command check mode skipped and dies on the undefined attribute.

That last case is why `cluster_setup_libvirt` and `cluster_setup_users` carry
`none` rather than `full`. `configure_libvirt_rdb_secret` reads the existing
secret with `virsh secret-list` and the next `set_fact` reads that result's
`stdout`; `add_libvirtadmin_user` finds root's home with `getent` and then
fetches a key from the path that shell printed. Check mode skips the shell in
both, and the play fails on the attribute that is not there. A preview that
crashes is worse than no preview, because the operator reads the failure as a
statement about the machine.

The UI never renders a `partial` check as a guarantee, and a `none` entry offers
no preview button at all rather than a button that lies.

## 4. Catalogue, first version

### Machine configuration

| Playbook | Targets | Preview | Reboots | Notes |
|---|---|---|---|---|
| `seapath_setup_main.yaml` | `cluster_machines`, `standalone_machine`, `VMs` | partial | yes, gated by `skip_reboot_setup` | The full convergence. Imports prerequisites, network, timemaster, libvirt, snmp, exporters, the cluster playbooks and `deploy_seapath_alloc`. This is the commissioning path and what the CI runs. |
| `seapath_setup_network.yaml` | `cluster_machines`, `standalone_machine` | partial | yes | Applies only when `apply_network_config` is true. The playbook most likely to cut the connection under the run. Warn hard when launched from a target machine. |
| `seapath_setup_timemaster.yaml` | `cluster_machines`, `standalone_machine` | full | no | PTP and NTP, plus `ptp_status_vsock` unless `disable_vsock`. |
| `seapath_setup_libvirt.yaml` | `hypervisors` | full | no | Writes `libvirtd.conf` and restarts `libvirtd`. The daemon's own configuration, on every hypervisor, cluster or not. |
| `seapath_setup_prometheus_exporters.yaml` | `cluster_machines`, `standalone_machine` | full | no | |
| `seapath_setup_snmp.yaml` | `cluster_machines`, `standalone_machine` | full | no | |
| `seapath_setup_deploy_seapath_alloc.yaml` | `hypervisors` | full | no | Dynamic CPU pinning. RT relevant, confirmation names the impacted machines. |
| `seapath_setup_hardening.yaml` | `cluster_machines`, `standalone_machine`, `VMs` | partial | yes | Ends with a reboot of every host. Sets `PermitRootLogin no` and restricts `ListenAddress`, which is why the trust targets the `ansible` account. Offered only after the rest converges cleanly. |

### Cluster

| Playbook | Targets | Preview | Reboots | Notes |
|---|---|---|---|---|
| `cluster_setup_ha.yaml` | `cluster_machines` | none | no | Corosync, the authkey, Pacemaker, stonith disabled. Command driven, so no preview. |
| `cluster_setup_cephadm.yaml` | `cluster_machines` | none | no | Bootstrap, monitors, OSDs. Destructive on the selected disks. The inventory diff is the review step, since check mode cannot be one. |
| `cluster_setup_libvirt.yaml` | `hypervisors:&cluster_machines` | none | no | The RBD secret libvirt presents to Ceph. The second playbook that touches libvirt, see below. |
| `cluster_setup_users.yaml` | `hypervisors:&cluster_machines` | none | no | The `libvirtadmin` user, needed for live migration and console access. |
| `cluster_remove_machine.yaml` | `cluster_machines` | none | no | Requires `machine_to_remove`, chosen from a list. See section 5. |

### The two libvirt entries

They are two playbooks upstream, and they stay two entries here.
`seapath_setup_libvirt.yaml` configures the libvirt daemon itself and runs on
every hypervisor, standalone included. `cluster_setup_libvirt.yaml` runs only
where there is a Ceph cluster and does one thing: define the RBD secret libvirt
presents when it opens a disk that lives in the pool. Merging them into one
button would offer a standalone machine a Ceph credential it has no use for, and
the titles now say which is which.

### VMs

| Playbook | Targets | Preview | Reboots | Notes |
|---|---|---|---|---|
| `deploy_vms_cluster.yaml` | first host of `cluster_machines` | partial | no | Deploys every VM in the `VMs` group. Note it already runs from one node, so which node drives is irrelevant. |
| `deploy_vms_standalone.yaml` | `standalone_machine` | partial | no | |

Neither is in the catalogue yet: the UI has no VM model, and a run that deploys
the `VMs` group needs one before the confirmation can say which guests it
touches.

### Not exposed in the first version

- `seapath_update_debian.yaml` and the Yocto update playbooks. They snapshot the
  root LVM, temporarily disable the GRUB password, arm a boot counter and
  reboot. That sequence deserves its own screen with its own rollback story, not
  a line in a generic run list.
- `seapath_revert_hardening.yaml`. Reachable from a console, not from a browser.
- `ci_*.yaml`, `test_*.yaml`. CI and test helpers, no operational meaning here.
- `seapath_setup_vmmgrapi.yaml`. Deprecated by this service.
- `seapath_setup_custom_hardware.yaml`, `seapath_setup_configure_nic_irq_affinity.yaml`.
  Site specific, driven by variables the UI does not model yet.

## 5. Naming the machine to remove

`cluster_remove_machine.yaml` is the only entry that needs to be told something
the inventory does not already say, and the machine it names is usually one that
has died. The playbook plays `cluster_machines`, computes `first_node` as a
member that is not the one leaving, and sends `crm_node -R` and
`ceph orch host rm --offline` there. Nothing is asked of the machine being
removed, which is the point.

The UI offers the machines the inventory declares as a list rather than a text
field, because the name has to match an inventory entry exactly: the playbook
reads `hostvars[machine_to_remove]` to find its hostname. The API checks the
same thing, and refuses two values:

- a name the inventory does not carry, which would otherwise fail halfway
  through an eviction on an undefined host;
- **this** node's name. The eviction is delegated to a surviving member, and a
  node cannot both drive the run and be its subject. The removal is launched
  from a machine that stays.

A dead machine is still in the inventory when the run starts, so the run reports
it unreachable while the eviction succeeds on the survivors. The confirmation
says so, otherwise the operator reads a successful removal as a failure. Taking
the entry out of the inventory file is a separate, deliberate edit afterwards.

## 6. The reboot question

`seapath_setup_main.yaml` reboots at the end unless `skip_reboot_setup` is set.
On a substation, a reboot is scheduled, not improvised, so the UI asks before
launching:

- **reboot now**, the default at commissioning, when nothing runs yet;
- **converge without rebooting**, which sets `skip_reboot_setup` and tells the
  operator plainly that the configuration is not fully applied until a reboot
  happens, and keeps that state visible in the node view.

Never silently set `skip_reboot_setup`. A machine that believes it is converged
and is not is worse than one that rebooted at an inconvenient time.

## 7. The catalogue and the collection move separately

An entry names a playbook in a collection released on its own schedule. Two
things follow, both found by building the image rather than by reading the
repository:

- **The catalogue is checked against the shipped collection.** Every entry whose
  playbook is absent from `/opt/ansible/collections` is reported unavailable,
  naming the collection version, instead of being offered as a button that
  fails at the first task. `seapath_setup_prometheus_exporters` and
  `seapath_setup_deploy_seapath_alloc` are what found this: they exist on the
  `seapathalloc` branch of `seapath-ansible` and not on `main`. The image is
  built from that branch for exactly this reason, and an image built from
  `main` correctly offers neither.
- **A role that spawns `ssh` itself is given the connection's arguments only if
  it asks.** `ansible.posix.synchronize` builds its own ssh command line for
  rsync, and it appends what `ansible.cfg` sets only when the task carries
  `use_ssh_args: true`. Every synchronize task in the collection carries it,
  which is a property of the collection and not of the module. A task written
  without it is given the connection's private key alone, so a machine this
  node drives with the site key is offered the wrong identity, ssh falls back
  to asking for a password, and the run hangs on a prompt nobody can see.
  `deploy_seapath_alloc` was that task until it was fixed upstream. The task is
  where this is repaired; the service writes an ssh client configuration naming
  every key, plus `BatchMode`, before each run, so that the next one written
  without the option fails in seconds instead of hanging. The image carries
  `rsync` for the same family of reasons.
- **`galaxy.yml` decides what a playbook can actually reach.** Its `build_ignore`
  list is matched against whole relative paths, so `"*.tar.gz"` strips
  `roles/deploy_cockpit_plugins/files/*.tar.gz` along with any archive at the
  root. `deploy_cockpit_plugins` unarchives precisely those two files and
  `seapath_setup_main.yaml` imports it on every distribution except Yocto, so
  an unmodified collection cannot commission a machine that has Cockpit, which
  is every machine installed from the SEAPATH ISO. `any_errors_fatal` then
  takes the whole run down. The image restores the two archives after
  installing the collection. It changes no role, and it should go away once
  `build_ignore` narrows the pattern.

`prepare.sh` has a related ordering problem: it installs the local collection
**before** updating the git submodules, so the installed copy carries an empty
`roles/deploy_cukinia/files/cukinia`. The image installs the collection a second
time, after `prepare.sh`, which is enough.

## 8. Ordering

The UI does not invent an orchestration engine. `seapath_setup_main.yaml`
already imports the right playbooks in the right order, and it is the entry
point for commissioning. The individual entries exist for day two, when an
operator changes one thing and wants to converge that thing, and the UI states
which playbook covers which part of the inventory so the choice is obvious from
the form the operator just edited.

When a run fails, the UI does not chain into the next playbook. `any_errors_fatal`
means the cluster is in a partial state, and the operator decides what happens
next.
