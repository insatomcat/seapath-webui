<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Exposed playbooks

The UI runs whole playbooks, never a free form selection of tags.

`GET /api/v1/playbooks` answers with every playbook the installed collection
carries, in two halves. This document is the reviewed half: entries a human
read the playbook for and wrote the sentences below. Everything else the
collection ships is derived by `app/runs/analysis.py`, which opens the YAML and
counts what it finds, and is offered marked as unreviewed. [D21](decisions.md#d21)
has the reasoning; section 9 has what the reader can and cannot answer.

Writing a reviewed entry stays a deliberate act. What changed is that not
having written one yet no longer hides the playbook from the operator.

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
| `seapath_setup_main.yaml` | `cluster_machines`, `standalone_machine`, `VMs`, `hypervisors` | partial | yes, gated by `skip_reboot_setup` and `skip_reboot_setup_network` | The full convergence. It reboots in two places: the network playbook it imports, when a role decided the new configuration needs a boot, and its own last play. Declining sets both switches. Imports prerequisites, network, timemaster, libvirt, snmp, exporters, the cluster playbooks and `deploy_seapath_alloc`. This is the commissioning path and what the CI runs. |
| `seapath_setup_prerequisitesdebian.yaml` | `cluster_machines`, `standalone_machine`, `VMs`, `hypervisors` | partial | no | Syslog, the distribution configuration, kernel modules, initramfs, tuned, `vm_manager`. The only one of the five that **removes packages**: `ceph`, `fdisk`, `ifupdown` and four trixie libraries, purged with `autoremove`. |
| `seapath_setup_prerequisitescentos.yaml` | `cluster_machines`, `standalone_machine`, `VMs`, `hypervisors` | partial | no | Same shape, `grub2-mkconfig` and dracut. Removes nothing. |
| `seapath_setup_prerequisitesoraclelinux.yaml` | `cluster_machines`, `standalone_machine`, `VMs` | partial | no | The one with **no hypervisor play**, so no tuned profile is applied. |
| `seapath_setup_prerequisitessles.yaml` | `cluster_machines`, `standalone_machine`, `VMs`, `hypervisors` | partial | no | Same shape as CentOS. |
| `seapath_setup_prerequisitesyocto.yaml` | `cluster_machines`, `standalone_machine`, `hypervisors`, `VMs` | partial | yes | A different playbook from the other four: kernel command line, hugepages, SR-IOV, and none of the package or syslog work. Reboots when the kernel parameters changed and `kernel_parameters_restart` is set. |
| `seapath_setup_network.yaml` | `cluster_machines`, `standalone_machine`, `hypervisors` | partial | yes, gated by `skip_reboot_setup_network` | The playbook most likely to cut the connection under the run. It always writes the network configuration; `apply_network_config`, true in every inventory this service writes, decides whether the roles apply it to the running machine or leave it for the next boot and set `need_reboot`. The reboot sits in a block and the switch that declines it sits on the block. Warn hard when launched from a target machine. |
| `seapath_setup_timemaster.yaml` | `cluster_machines`, `standalone_machine` | full | no | PTP and NTP, plus `ptp_status_vsock` unless `disable_vsock`. |
| `seapath_setup_libvirt.yaml` | `hypervisors` | full | no | Writes `libvirtd.conf` and restarts `libvirtd`. The daemon's own configuration, on every hypervisor, cluster or not. |
| `seapath_setup_prometheus_exporters.yaml` | `cluster_machines`, `standalone_machine` | full | no | |
| `seapath_setup_snmp.yaml` | `cluster_machines`, `standalone_machine` | full | no | |
| `seapath_setup_deploy_seapath_alloc.yaml` | `hypervisors` | full | no | Dynamic CPU pinning. RT relevant, confirmation names the impacted machines. |
| `seapath_setup_deploy_seapath_webui.yaml` | `cluster_machines`, `standalone_machine` | full | no | This service, on every machine the inventory declares. The version each one gets is `seapath_webui_image`, so an update is an edit and an apply. The run ends without a final status on the machine it was launched from, because the service recording it is the service being replaced: the entry says so before the confirmation, and the record says so afterwards. See [D23](decisions.md#d23). |
| `seapath_setup_hardening.yaml` | `cluster_machines`, `standalone_machine`, `VMs` | partial | yes | Ends with a reboot of every host. Sets `PermitRootLogin no` and restricts `ListenAddress`, which is why the trust targets the `ansible` account. Offered only after the rest converges cleanly. |

### Cluster

| Playbook | Targets | Preview | Reboots | Notes |
|---|---|---|---|---|
| `cluster_setup_ha.yaml` | `cluster_machines` | none | no | Corosync, the authkey, Pacemaker, stonith disabled. Command driven, so no preview. |
| `cluster_setup_cephadm.yaml` | `cluster_machines` | none | no | Bootstrap, monitors, OSDs. Destructive on the selected disks. The inventory diff is the review step, since check mode cannot be one. |
| `cluster_setup_libvirt.yaml` | `hypervisors:&cluster_machines` | none | no | The RBD secret libvirt presents to Ceph. The second playbook that touches libvirt, see below. |
| `cluster_setup_users.yaml` | `hypervisors:&cluster_machines` | none | no | The `libvirtadmin` user, needed for live migration and console access. |
| `cluster_remove_machine.yaml` | `cluster_machines` | none | no | Requires `machine_to_remove`, chosen from a list. See section 5. |

### Measurement

| Playbook | Targets | Preview | Reboots | Notes |
|---|---|---|---|---|
| `test_run_cyclictest.yaml` | `cluster_machines`, `standalone_machine` | none | no | The `cyclictest` role on its own. Copies a script to a temporary directory, runs `cyclictest`, fetches the histogram, leaves. Changes nothing on the machines. Launched from the Real time page, where its parameters and its chart are. |
| `test_run_hwlatdetect.yaml` | `cluster_machines`, `standalone_machine` | none | no | The `hwlatdetect` role on its own. Measures the interruptions the kernel never sees. Records the absence of the `hwlat` tracer in the fetched result rather than failing, so one kernel that cannot answer does not take down a run that has already loaded the other machines. |

The two entries in the catalogue that measure rather than converge, and the
distinction earns a flag on the entry (`measures`) because the confirmation has
to say a different sentence. A convergence is dangerous through what it
*writes*. This is dangerous through what it *runs*: a thread per measured CPU
at real time priority, on machines that are carrying live guests, for as long
as the operator asked. Neither sentence covers the other.

The entry's `disruption` names no figure, and the confirmation appends the
three the operator actually chose. A sentence that said "priority 90" while the
field held 50 is one an operator learns to stop reading, and this is the page
where that costs the most.

Preview is `none`, and not as a judgement call: the playbook is one `command`
followed by a `fetch` of the file that command wrote. Check mode skips the
command, the fetch then has nothing to bring back, and the preview would report
a green run that measured nothing.

Every variable is checked before it reaches a command line on every machine.
`cyclictest_duration` in seconds, `cyclictest_priority` bounded to 1-98, and
`cyclictest_affinity` as `smp` or a CPU list: 99 is refused because it sits
above the kernel's own threads on a PREEMPT_RT machine, which is how a
measurement wedges the host it was measuring. For `hwlatdetect`, a duration in
seconds and `threshold`, `width` and `window` in microseconds, each bounded to
a second, since `width` is the interval during which the machine's interrupts
are held off. The results folder of each is filled by the service with the
run's own directory and refused from a caller: it is a path inside this
container rather than an operator's decision.

**Why `hwlatdetect` is worth a second entry rather than a flag on the first.**
The two measure different things and only one of them has anything to do with
the inventory. `cyclictest` measures what the scheduler delivered, which every
conformance check on the Real time page can move. `hwlatdetect` measures what
the firmware took: an SMI carries the CPU into firmware without telling the
operating system, so the time is missing from the kernel's own accounting and
from the `cyclictest` figures alike. A machine that passes every check and
still misses its deadline is either a firmware problem or a configuration one,
and this is what separates them. Nothing in an inventory reaches it, and the
page says so: the fix is in the BIOS.

**This is a `test_*` playbook, and section 4 says those are refused.** The rule
stands and this is its one exception, which is narrow by construction: the rule
forbids *deriving* an entry for a CI playbook from a YAML read, and this entry
was written by hand after reading the role. Analysis still refuses to derive
`ci_*` and `test_*`; naming a reviewed id lets the counted facts sit under the
prose, and nothing else. What made the exception worth making is that the role
already exists upstream and is the one the CI runs, so the alternative was a
second implementation of a measurement in Python, inside a container that must
never hold real time privileges.

Neither playbook existed upstream when these entries were written.
`cyclictest` was only reachable through `ci_all_machines_tests.yaml`, which
runs it after the Yocto functional tests and is therefore unusable on a Debian
machine or on a running deployment, and `hwlatdetect` had no role at all. Both
now exist on the branch the image builds from. Where a site pins a collection
that predates them, the entries report themselves unavailable through
`playbook_present`, which is exactly what [D12](decisions.md#d12) prescribes.

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

### Not reviewed, and offered as such

Every other playbook of the collection is read off the disk and listed under
its own heading. They are launchable, described by what the reader counted, and
carry no sentence a human wrote. Several deserve a reviewed entry and have not
had one yet:

- `seapath_update_debian.yaml`. It snapshots the root LVM, temporarily disables
  the GRUB password, arms a boot counter and reboots. That sequence deserves
  its own screen with its own rollback story.
- `seapath_revert_hardening.yaml`. Sitting next to `seapath_setup_hardening` in
  the list, which is where an operator looks for it.
- `seapath_setup_vmmgrapi.yaml`. Deprecated by this service, and the entry that
  says so has to be written.
- `seapath_setup_custom_hardware.yaml`,
  `seapath_setup_configure_nic_irq_affinity.yaml`. Site specific, driven by
  variables the UI does not model.

Two families are refused outright rather than derived:

- `ci_*.yaml`, `test_*.yaml`. They reinstall an ISO, restore a snapshot and
  reboot on a USB drive. They build a machine from nothing, and no reading of a
  YAML file makes them safe to offer next to the network configuration. A
  reviewed entry can still name one, `test_run_cyclictest` being the only one:
  what the rule forbids is deriving such an entry from a file nobody read.
- Any playbook needing a variable this page has no field for. It is listed with
  the variable named and stays unavailable, `seapath_update_yocto_cluster` and
  its `{{ machine_to_update }}` being the case. A free text field wired to an
  Ansible run is the extra vars box this service refuses to have.

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

The System page is shaped like that sentence. `seapath_setup_main` is a card of
its own with its two buttons, and the rest of the catalogue is one picker: an
entry, its scope, what it disturbs and its buttons, one at a time. Stacking
thirteen entries put the one an operator came for below the fold, and the one
they came for is never the first. The picker lists the unavailable entries too,
with the reason, because an operator told to run `cluster_setup_ha` has to find
it and read why it is not offered.

When a run fails, the UI does not chain into the next playbook. `any_errors_fatal`
means the cluster is in a partial state, and the operator decides what happens
next.

## 9. What reading the collection can answer

`app/runs/analysis.py` opens every playbook, follows its `import_playbook`
chain, expands the roles of each play and the roles those roles include, and
reads every task file of a role rather than `main.yml` alone. It never imports
Ansible, never evaluates a template and never runs anything, so a collection
built from a branch nobody here has seen can produce a poor description and
nothing worse.

| Question | How it is answered | Where it is weak |
|---|---|---|
| Which machines a run reaches | The `hosts:` lines of the plays, `localhost` dropped | A pattern built from a variable is reported verbatim, and the variable becomes a required input |
| What check mode is worth | The modules the tasks use: `full` with no `command`/`shell`, `partial` with some, `none` when nothing writes through a module at all | A command that only reads, like `detect_seapath_distro` running `grep`, still counts as command driven, so a reviewed `full` beats a derived `partial` |
| Whether it reboots | A `reboot` task anywhere in the chain | Only a `skip*` variable is offered as a gate |
| Which variables it needs | A `fail` task guarded by `is undefined` in the playbook's own tasks, plus a `hosts:` built from a variable | A role's internal sanity check is deliberately not counted: `detect_seapath_distro` fails when it cannot work out the distribution, and that is the role talking to its author |
| Whether a preview can crash | A task reading `.stdout` or `.rc` of a command registered in the same file | Reported as a warning on the preview button rather than by removing it |

One question was asked and withdrawn: whether a preview would crash on a task
reading the output of a command check mode skipped. The check works, and it
fired on twenty of the collection's twenty-six playbooks, because nearly all of
them import `detect_seapath_distro`, which registers a `grep` and reads its
`rc` inside a block guarded by a condition that is almost never true. Telling a
rarely taken guarded path apart from a real dependency needs the run.
`cluster_setup_libvirt` and `cluster_setup_users` carry `none` for exactly this
reason, written by someone who read the roles, which is the difference between
the two halves of this document.

The polarity rule is the one worth stating on its own. A reboot behind
`skip_reboot_setup` is `gated` and the UI offers the checkbox. A reboot behind
any other condition is reported as a plain reboot, because a checkbox reading
"converge without rebooting" that reboots a substation hypervisor is worse than
a warning that overstates.

Where the reviewed value and the derived value disagree, the reviewed one wins
whole and the counts are shown beside it. `seapath_setup_snmp` is the example:
reviewed `full`, derived `partial`, and the human is right because the single
command in its chain detects a distribution and writes nothing.

### The five prerequisites, and what none of them checks

`seapath_setup_main.yaml` imports one of the five after `detect_seapath_distro`,
and that choice is the only thing standing between a machine and the wrong
playbook. Launched on its own, none of the five looks at what it landed on:
`seapath_setup_prerequisitesdebian.yaml` runs `configure_seapath_distro` with
`update-grub` and `/etc/vim` wherever it is sent, and the Debian and SLES
playbooks call `detect_seapath_distro` without ever reading its answer. Each
entry says so, because the operator launching one directly is exactly the
operator who has bypassed the choice.

So the service filters them. It reads which of the five distributions this
machine runs from `/etc/os-release`, the way `detect_seapath_distro` works it
out from the facts it gathers, and refuses the four that do not match: a run
plays every machine the inventory declares, this node among them, so the wrong
one of the five is wrong for at least this machine before it starts. Both
directions of doubt leave the entry available. An unreadable `/etc/os-release`
blocks nothing, because refusing all five over a container mounted wrong is
worse than the risk; and an inventory that does not declare this node says
nothing about what a run will reach, so the check has no standing over it. It
is a statement about this machine and never about the others: a mixed inventory
still needs `seapath_setup_main`.

They differ by more than the package manager. OracleLinux has no hypervisor
play at all, so a machine prepared with it has had no tuned profile applied,
which stays invisible until the latency is measured. Debian is the only one
that removes packages. Yocto shares nothing with the other four: kernel command
line, hugepages and SR-IOV, and a reboot when the parameters changed.
