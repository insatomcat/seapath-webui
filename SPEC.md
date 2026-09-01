<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# seapath-webui - specification

## 1. Problem

A SEAPATH deployment needs an Ansible control machine and a hand written
inventory before the first machine does anything useful. Installing the ISO
leaves a hypervisor that is technically ready but operationally inert.

Proxmox VE solves the equivalent problem with a node local manager: install,
browse to the node, join nodes into a cluster from the UI, deploy Ceph or not.
The goal is that experience on SEAPATH.

The constraint that shapes everything: SEAPATH is sold as infrastructure as
code, and it is one, in the strong sense. SEAPATH **is** a function converting
an inventory into a running physical infrastructure. A UI that mutates machines
imperatively would destroy the property that defines the product.

## 2. The principle

**The UI does not configure machines. It edits the inventory, and asks the
cluster to converge.**

`f(inventory) -> infra` is preserved literally. The fourth machine disappears as
a machine, not as a function. Its two real jobs, holding the desired state and
running the playbooks, move into the cluster itself.

Four responsibilities, and nothing else:

1. **Hold and edit the inventory.** A git repository replicated across the
   nodes, edited through guided forms, exportable at any time.
2. **Broker trust between nodes,** so that any node can act as the Ansible
   control machine for the others. A manual secret exchange, Proxmox style,
   bootstraps it.
3. **Run the upstream playbooks** with `ansible-runner`, and turn their event
   stream into something an operator can read.
4. **Expose the runtime plane,** meaning starting, stopping and migrating VMs,
   which is not configuration and does not belong in an inventory.

What follows from this, and what makes this design worth building rather than a
Proxmox clone:

- **No SEAPATH role is rewritten.** `configure_ha`, `cephadm`, the network
  roles, all keep their `delegate_to`, `add_host` and `run_once` logic. What the
  UI runs is exactly what the CI tests.
- **No file rendering is duplicated.** There is no second implementation of
  `corosync.conf` to keep in sync, and therefore no convergence contract to
  enforce with golden files.
- **The fourth machine stays possible.** It clones the same repository. Nothing
  in this design forbids it, and a site that wants one keeps it.
- **Conformance becomes visible.** A cluster can be checked against its
  inventory. Proxmox cannot answer that question at all.

## 3. Scope

### In scope

- Node local HTTPS service on every SEAPATH machine, shipped in the ISO.
- A guided inventory editor, seeded by hardware discovery on first boot.
- The trust exchange that lets nodes drive each other.
- Running the upstream playbooks, with progress, logs, and history.
- Read only observation of what a node **is**: its hardware, its identity and
  its cluster membership, which is what the inventory form is prefilled from.
- VM runtime operations through `vm_manager`.

### Out of scope

- Reimplementing anything a role already does.
- Editing files on the host outside of an Ansible run. The service writes the
  inventory and the trust material, and nothing else.
- Fleet operations across several clusters.
- Replacing Cockpit, which keeps shell, logs and low level access.
- **Monitoring.** Unit states, the journal, the clock offset, load: every node
  runs `prometheus-node-exporter`, and that is where live state is read and
  alerted on. A node local UI holding a second source of truth for it earns
  nothing and costs this container a route to the host's systemd, which is the
  most expensive mount in the design. This was in scope once, and taking it
  back out removed eight bind mounts and about seven hundred lines. See
  [deployment.md](docs/deployment.md).

### Deliberately deferred

Arbitrary cluster sizes. The reference inventory encodes a **three node ring**:
`team0_0` and `team0_1` are the two cluster interfaces, with
`cluster_next_ip_addr` and `cluster_previous_ip_addr` naming the neighbours, and
`br_rstp_priority` breaking the loop. The cluster network is physically a ring,
so a fourth node is not a matter of adding a line to a file. Three nodes, in the
two hypervisors plus one observer or the three hypervisors variant, covers the
target deployment.

## 4. Reference flow

1. **Install.** The ISO is installed on three machines. Each boots as a SEAPATH
   standalone hypervisor running `seapath-webui`.
2. **First boot.** The node discovers its hardware and writes a minimal local
   inventory describing itself: admin interface and address, NICs, disks, CPU
   topology. The inventory is generated, not authored.
3. **Configure standalone.** The operator fills the guided form, which fills the
   `TODO` fields of the inventory, then applies. The service runs
   `seapath_setup_main.yaml` against this machine only, over the same SSH path
   it would use for any other node. A standalone machine is configured by
   Ansible, exactly like a cluster one, which is why the self trust is
   provisioned at first boot and not at cluster formation.
4. **Exchange the secret.** On node A, "Add a node" produces a blob. On node B,
   paste it. The two services authenticate each other, then provision the SSH
   trust between the `ansible` accounts of both nodes, in both directions,
   without further copying by hand.
5. **Form the cluster.** The inventories merge into one, the operator fills the
   cluster network fields, and applies. The service runs
   `cluster_setup_ha.yaml` from the node the operator is on, against the three
   machines. Corosync, the authkey and Pacemaker are set up by `configure_ha`,
   unchanged.
6. **Deploy Ceph, or not.** Select the OSD disks, which fills `ceph_osd_disks`,
   and apply `cluster_setup_cephadm.yaml`. Skipping it leaves a Pacemaker
   cluster with local storage, which is supported.
7. **Day two.** Every later change is the same loop: edit the inventory, see
   what will change, apply, keep the history. A fourth machine can join the
   party at any point by cloning the repository.

## 5. Architecture

### 5.1 Components

```
  browser
     |  HTTPS
     v
+--------------------------------------------------+
|  seapath-webui (container, one per node)         |
|                                                  |
|  inventory service  -> git repo, replicated      |
|  trust service      -> SSH mesh between nodes    |
|  run service        -> ansible-runner            |
|  runtime service    -> vm_manager                |
|  read services      -> what this machine is      |
+--------------------------------------------------+
     |  SSH as the `ansible` user, sudo         |  local sockets
     v                                          v
  every node of the cluster, including      libvirt, ceph,
  the local one                             /sys (read)
```

The service reaches **every** node over SSH, including the machine it runs on,
because the inventory sets `ansible_connection: ssh` for all hosts. The
consequence is worth stating plainly: the configuration plane never writes to
the host filesystem from inside the container. The container needs the libvirt
socket and the Ceph configuration for the runtime plane, and a read only `/sys`
and `/dev/disk` to describe its own hardware. It does not need to be
privileged, and it needs no route to the host's systemd, its bus or its
journal.

### 5.2 Two planes

Mixing them is the mistake Proxmox made and that this design refuses.

| Plane | Contents | Nature | Where the truth lives |
|---|---|---|---|
| Configuration | network, RT tuning, cluster membership, Ceph topology, hardening, defined VMs | declarative, converges | the inventory |
| Runtime | start, stop, migrate, snapshot, console | imperative, ephemeral | Pacemaker and libvirt |

A start button has nothing to do in an inventory. An OVS bridge has nothing to
do behind an imperative API call. `vm_manager` already draws roughly this line.

### 5.3 Running a playbook

`ansible-runner` drives the upstream playbooks and emits a JSON event per task
and per host, which is what makes a readable progress view possible instead of a
wall of text.

- Artefacts persist under `/var/lib/seapath-webui/runs/<run-id>/`: the inventory
  used, the exact command, the event stream, the final status per host.
- Every run records the inventory commit hash it was produced from. "Which
  version of the desired state is this machine actually running" has an answer.
- A run is **resumable by being re-run**. Ansible is idempotent, so an
  interrupted run is recovered by relaunching it, which is the entire point of
  declarative convergence. The UI says so rather than pretending to checkpoint.
- Interruption is a real case, not a theoretical one: `seapath_setup_hardening.yaml`
  ends with a reboot of every machine, including the one running the playbook,
  and the network roles can cut the connection under the process. The UI warns
  before launching those, and offers to relaunch after the reboot.
- One run at a time per cluster, enforced by a lock. Two operators on two nodes
  must not converge the same machines concurrently.
- `any_errors_fatal = True` means a single host failure aborts everything. The
  run view shows which hosts were done, which were not, and what to do next.

### 5.4 Preview, and its limits

Check mode is the preview, and it must not be oversold. Roles that write files
through `template`, `copy` and `lineinfile` produce an honest diff: network,
tuning, most of the host configuration. Roles built on `command` and `shell`,
which is most of `configure_ha` and `cephadm`, are either skipped or lie in
check mode.

The UI therefore labels each playbook as fully, partially or not previewable,
and never displays "no change" as a guarantee where it cannot be one.

## 6. Trust between nodes

Specified in [docs/cluster-join.md](docs/cluster-join.md). Summary:

- The target account is **`ansible` with sudo**, never `root`. The hardening
  role sets `PermitRootLogin no`, `PasswordAuthentication no` and
  `AuthenticationMethods publickey`, so a root based trust would break on the
  first hardened machine. The inventories already assume `ansible_user: ansible`.
- One manual paste per added node bootstraps mutual authentication between the
  two services. The SSH keys are then exchanged automatically over that
  authenticated channel, in both directions. Two pastes for a three node
  cluster, and no six way key ceremony.
- The trust is **permanent and restricted**: `from=` limited to the cluster
  administration addresses, `restrict` options, one dedicated key pair per
  direction, and every use audited. It is not command restricted, because
  Ansible legitimately needs arbitrary root, and pretending otherwise would be
  security theatre.
- The mesh is full, not star shaped. `cluster_remove_machine.yaml` runs from a
  surviving node to evict a dead one, so a single designated control node would
  mean losing the ability to repair when that node is the one that failed.

## 7. Inventory

Specified in [docs/inventory.md](docs/inventory.md). Summary: a git repository
per node, a single writer under quorum, the commit hash as the version, guided
forms mapping to the documented variables, and hardware discovery to seed it.

The repository holds a folder rather than one file, because a dozen roles take
a path to a file the control machine holds. A run mounts that folder where a
checkout of `seapath-ansible` would be, so the paths an inventory already
carries mean the same thing here. The large files it names, VM images and
archives, live in a store beside the repository that git does not carry.

## 8. Security

- HTTPS only on port 8006, self signed certificate generated at first boot, the
  fingerprint printed on the console. It is what the operator verifies and what
  the trust exchange pins.
- Users authenticate through PAM against local Unix accounts. Roles by group:
  `seapath-admin`, `seapath-operator`, `seapath-viewer`. Automation uses bearer
  tokens, node local, revocable.
- Node to node calls use mutual TLS against a cluster CA. The CA is created when
  a node issues its **first invitation**, not when the cluster is formed: the
  trust handshake happens before `cluster_setup_ha.yaml` has ever run, so a CA
  created at cluster formation would not exist when it is first needed. The SSH
  mesh is provisioned over that channel and never over an unauthenticated one.
- Audit: every inventory commit carries the authenticated user, every run
  records who launched it, and both are exported to the journal. `git log` is
  the configuration audit trail, which is a better answer than any UI log.

## 9. Real time safety

- The container runs on housekeeping CPUs only, with `CPUAffinity` computed from
  the isolated set, a `CPUQuota`, and a positive `Nice`. No real time priority,
  the exact opposite of the `rtperfui` quadlet.
- The service itself never restarts a service on the host. Only an explicit,
  named playbook run does, through the roles that already own those handlers.
- Applying an inventory change on a live substation restarts whatever the roles
  decide to restart. The UI shows the impacted machines and requires an explicit
  confirmation naming them. This is the single most dangerous button in the
  product and it must look like it.

## 10. Milestones

**M0 - skeleton and node view.** FastAPI service, PAM auth, TLS bootstrap, read
only node view, container, quadlet, test harness. No writing anywhere.

**M1 - standalone by inventory.** Self trust at first boot, hardware discovery,
inventory repository, guided forms, `ansible-runner` integration, run view with
the event stream, and `seapath_setup_main.yaml` applied to the local machine. At
the end of M1 the ISO produces a machine configurable from a browser with no
fourth machine, which is the core of the request.

**M2 - VM runtime.** `vm_manager` integration for the runtime plane, and VM
definitions in the inventory for the declarative side.

**M3 - cluster.** Trust exchange, inventory merge and replication, cluster
network forms, `cluster_setup_ha.yaml` and the rest of the cluster playbooks,
cluster wide views, node removal.

**M4 - Ceph.** Disk inventory and selection feeding `ceph_osd_disks`, cluster
network fields for `cephadm_network`, and `cluster_setup_cephadm.yaml`.

**M5 - integration.** Ansible role `deploy_seapath_webui`, ISO integration,
conformance view based on periodic check runs, `vmmgrapi` deprecation.

## 11. Risks

| Risk | Mitigation |
|---|---|
| A UI apply restarts services under live VMs | Confirmation naming the impacted machines, preview where it is honest, and a catalogue that flags what each playbook disrupts |
| The inventory diverges between nodes | Single writer under quorum, commit hash on every run, and a visible warning when a node's copy is stale |
| The SSH mesh becomes the weak point of a hardened site | `ansible` user rather than root, `from=` restriction, per direction keys, revocation from the UI, and audit |
| A run dies with the machine it converges | Idempotent re-run, artefacts persisted, and explicit warnings on the playbooks that reboot |
| Scope creep towards a Proxmox clone | Section 2 is binding. The UI edits the inventory and runs playbooks. Anything that mutates a machine directly is a design bug |
