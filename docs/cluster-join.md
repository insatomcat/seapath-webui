<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Trust between nodes, and cluster formation

Forming a cluster is two very different problems. Establishing trust between
machines that have never met is a bootstrap problem, and it is the only part of
this design that is irreducibly imperative. Configuring corosync, Pacemaker and
Ceph is a convergence problem, and `seapath-ansible` already solves it. This
document keeps them separate.

## 1. The target account is `ansible`, not `root`

Verified in the repository, not assumed:
`roles/configure_hardening/templates/ssh-audit_hardening.conf.j2` sets
`PermitRootLogin no`, `PasswordAuthentication no`,
`AuthenticationMethods publickey`, and restricts `ListenAddress` to `ip_addr`
and `cluster_ip_addr`. Any trust built on root SSH breaks the first time
`seapath_setup_hardening.yaml` runs, which is to say on every real deployment.

The trust therefore provisions the **`ansible` account with sudo**, which is
what `inventories/examples/*.yaml` already assume through `ansible_user:
ansible`. The pleasant consequence: the UI path and the fourth machine path use
the same account, the same key type and the same privileges, so a site can
switch between them without touching anything.

## 2. Trust to itself, before any peer exists

A consequence of running Ansible over SSH even against the local machine: a
standalone node needs a trust relation **with itself** before it can configure
anything, and that is the very first thing the service does.

At first boot, before any UI interaction:

1. generate an SSH key pair into `/etc/seapath/webui/ssh/`;
2. install the public key in the `ansible` account's `authorized_keys` on this
   machine, with the same `from=` and `restrict` treatment as a peer key;
3. record the relation so the trust view shows it, because an operator debugging
   a failed run needs to see that this relation exists and is used.

The prerequisites are already satisfied by the ISO, verified in
`seapath-build_debian_iso`:

- `srv_fai_config/scripts/SEAPATH_COMMON/10-rootpw` creates the `ansible`
  account, uid and gid 1005, with a home directory;
- `srv_fai_config/scripts/SEAPATH_COMMON/40-networking` creates
  `/home/ansible/.ssh/authorized_keys`, mode 0600, owned by the account, and
  seeds it with the `ansiblekey` baked into the image at build time;
- sudo is granted as `NOPASSWD:EXEC:SETENV: /bin/sh` plus `/usr/bin/rsync`,
  which is exactly what Ansible needs for `become`.

Two consequences for the implementation:

- **Append, never rewrite.** `authorized_keys` already contains the site key
  from the ISO, which is how a conventional Ansible control machine reaches the
  node. Overwriting it would lock out the fourth machine on the first boot of
  the service. Add and remove single lines, matched by their comment.
- The service therefore does not create the account. If it ever meets a machine
  where the account is missing, it refuses to converge and says why, rather than
  inventing a user with privileges nobody reviewed.

### Provisioned at every start, not only at the first

The self relation is re-provisioned on every start rather than guarded by a
"first boot" flag, which turns it from an initialisation into a repair. Two
cases make that worth the trouble:

- `seapath_setup_network.yaml` can move the administration address, and a
  `from=` clause naming the old one authorises nothing. Rewriting the line from
  the addresses currently observed is what fixes it.
- A renamed machine would otherwise leave behind a line authorising the same
  key under a name nobody recognises. Our lines are deduplicated by key, so the
  stale one goes.

### The host key, on the first connection

A first SSH connection to a machine whose host key is unknown either prompts,
which hangs a run forever, or is waved through with
`StrictHostKeyChecking=no`, which is a genuine man in the middle window on the
administration network. For the local machine there is a third answer and it is
strictly better than both: read the host's own public host keys off its
filesystem, through the read only `/etc/ssh` mount, and write them into a
`known_hosts` the service owns. No network is involved, so there is nothing to
intercept, and host key checking stays on.

The same shape works for a peer at M3, where the host key travels over the
mutually authenticated TLS channel rather than over the wire unverified.

### The connection credentials are not in the inventory

Which private key this control machine uses is a fact about **this** control
machine, not about the desired state, so it is passed to `ansible-runner` in
the environment and never written into the inventory. That is precisely why the
exported inventory works unchanged on a conventional control machine that has
its own key.

## 2b. Before the handshake exists: the site key

The handshake below is the destination. It needs M3, and a site with three
machines and an inventory needs to apply today, so there is an interim path and
it is the one the site already uses.

Every machine installed from the SEAPATH ISO carries a site public key in the
`ansible` account, and a conventional control machine holds the private half.
An operator uploads that private half to one node, and that node reaches every
machine in the inventory exactly as the fourth machine does. Two acts, both in
the configuration page, both undone in one click:

- **the site key.** Stored `0600` in `/etc/seapath/webui/ssh/id_site`, never
  returned by the API, never logged, reported only as a fingerprint to compare
  against `ssh-keygen -lf`. A key protected by a passphrase is refused rather
  than stored, because nothing here can type one during a run.
- **the host keys** of the machines it will drive, read with `ssh-keyscan` and
  written only after an operator has compared each fingerprint against
  `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the machine itself.
  They live in a record of their own, since `known_hosts` is rebuilt from this
  machine's filesystem at every start.

What is being accepted, stated plainly rather than buried: **that key is root
on every machine that trusts it**, through the `NOPASSWD:EXEC:SETENV: /bin/sh`
rule the ISO grants the `ansible` account. Uploading it makes this node as
powerful as the control machine it came from. The node was already in that
trust domain, since these machines share a corosync secret, a Ceph cluster and
each other's VM storage, so what changes is the number of places the private
key exists. That is a real change and it is why this is an explicit act with a
visible fingerprint and a remove button, rather than something the service
arranges quietly.

The handshake below removes the need for it: each pair of nodes gets its own
key, generated where it is used, and no private key ever moves. A site that has
uploaded a site key removes it once the mesh exists.

## 3. The manual exchange, and what it buys

Same gesture as Proxmox, one paste per added node.

On node A, "Add a node" produces a blob:

```json
{
  "version": 1,
  "cluster_name": "seapath",
  "issued_at": "2026-08-11T09:00:00Z",
  "peers": [{"name": "node1", "addr": "192.168.200.121", "port": 8006}],
  "fingerprint": "SHA256:ab:cd:...",
  "token": "<one time, 15 minutes, single use>"
}
```

No secret of the cluster is in it. The token authorises exactly one join and
expires. On node B, the operator pastes it and confirms.

Issuing the first invitation is also what creates the **cluster CA**, on A, into
`/etc/seapath/webui/pki/ca/`. It cannot wait for cluster formation, since the
handshake below needs it and `cluster_setup_ha.yaml` has not run yet. A node
that never invites anyone never has a CA, which is correct: a standalone machine
has no peers to authenticate.

What happens then is the part worth getting right. The naive version of this
idea has the operator copy an SSH key by hand in each direction, which is six
manual operations for a three node cluster and a guaranteed source of typos.
Instead, **the paste bootstraps mutual authentication between the two services**,
and the services provision SSH themselves:

1. B opens TLS to A, verifying the presented certificate against the pinned
   fingerprint and nothing else. A mismatch aborts, naming both fingerprints,
   with no override.
2. B presents the token and its own certificate signing request.
3. A validates the token, signs the CSR with the cluster CA, and returns the CA
   certificate. From here the two services have a mutually authenticated
   channel.
4. Over that channel, each side generates a dedicated SSH key pair and sends its
   public key to the other, which installs it in the `ansible` account. Both
   directions, automatically.
5. B receives the cluster inventory and the list of existing members, and
   provisions trust with each of them the same way, over mutual TLS. The mesh
   closes without further operator action.

Two pastes for a three node cluster. The operator has still performed an
explicit act of trust for each machine, which is the property that matters.

## 4. Shape of the installed key

Permanent, restricted, and auditable:

```
from="192.168.200.121,192.168.55.1",restrict ssh-ed25519 AAAA... seapath-webui:node1->node2
```

- `from=` limits the source to the administration and cluster addresses of the
  peer, which are known because they are in the inventory.
- `restrict` disables forwarding, agent, X11 and tunnelling. A peer key keeps
  all of that off, runs included: the ISO sets `Defaults:ansible !requiretty`
  in `sudoers`, so sudo never asks for a terminal.
- The relation a node has with **itself** adds `pty` after `restrict`, and only
  that one. It is the relation the console connects over, and without it
  `sshd` answers "PTY allocation request failed on channel 0" and the terminal
  closes as it opens. The option grants nothing the key could not already do,
  since it carries no `command=` and can therefore spawn a pty of its own. See
  [D19](decisions.md#d19---settled-the-shell-is-served-here-over-the-connection-a-run-makes).
- The trailing comment is the relation identifier, and it is how the service
  finds its own lines in a file it shares with the ISO's site key.
- One key pair per direction and per pair of nodes, so revoking one relation
  does not touch the others, and the comment names the relation.
- The private key lives in `/etc/seapath/webui/ssh/`, mode 0600, and never
  leaves the node.

What this restriction does **not** do is limit the commands. The sudoers rule
shipped by the ISO already grants `/bin/sh` with `EXEC:SETENV:`, which is
arbitrary root by construction, and it has to be, since that is how Ansible
runs. A `command=` restriction on the SSH key that pretended otherwise would be
theatre. The honest statement in the security file is: these
three machines share a corosync secret, a Ceph cluster and the storage of each
other's VMs, so they are already in the same trust domain, and the SSH mesh
makes that explicit rather than adding to it.

Revocation is a UI action: remove the authorised key on both sides, drop the
member certificate from the CA. It is offered per relation and for a whole node.

## 5. Forming the cluster

Once the mesh exists, there is nothing left to invent:

1. The inventories merge. B's host entry moves into the cluster inventory, the
   `cluster_machines` group gains the node, and the operator fills the ring
   fields for the new node, as described in [inventory.md](inventory.md).
2. Validation runs. A ring that does not close, or a duplicate cluster address,
   is refused before anything touches a machine.
3. The commit lands, replicated to the members.
4. The operator applies. The service runs `cluster_setup_ha.yaml` against
   `cluster_machines`, from the node the operator is on.
5. `configure_ha` does the rest, unchanged: `corosync-keygen` on the first node,
   the authkey fetched and distributed, `corosync.conf` templated,
   `/etc/cluster.conf` written, corosync and Pacemaker started, stonith
   disabled. The UI shows the task stream and the per host result.
6. The other cluster playbooks follow the same pattern:
   `cluster_setup_libvirt.yaml`, `cluster_setup_users.yaml`, and
   `cluster_setup_cephadm.yaml` when Ceph is wanted.

The corosync authkey is generated and distributed by the role, over the SSH
mesh, through the machine running the playbook. The service never handles it,
never stores it, and never logs it. That is a direct benefit of not
reimplementing the logic.

## 6. Removing a node

`cluster_remove_machine.yaml` exists and takes `machine_to_remove`. It evicts
from Pacemaker with `crm_node -R` and from Ceph with `ceph orch host rm`,
delegating to the first node that is not the one being removed.

The UI runs it from a **surviving** node, which is why the mesh is full rather
than star shaped: the node being removed is frequently the one that failed, and
if only it could drive Ansible, the cluster could not repair itself. After the
run, the node leaves the inventory in a commit, and its trust relations are
revoked on every remaining member.

## 7. Failure modes to handle explicitly

| Situation | Behaviour |
|---|---|
| Fingerprint mismatch on paste | Abort, show both fingerprints, no override path |
| Token expired or already used | Abort, offer to generate a new one on A |
| Trust established but the playbook fails midway | The mesh and the inventory stay, the run is re-runnable, the run view names the hosts that were reached |
| A member is unreachable when replicating the inventory | Commit succeeds on the lead, the stale member is flagged, applying from a stale copy is refused |
| Two operators join two nodes at once | The cluster wide run lock serialises them, the second is refused with the running run named |
| Quorum lost | Inventory read only, no apply, cluster view degraded but still readable |

## 8. Test plan

With a fake host and a fake peer:

- the trust handshake, including fingerprint mismatch, expired token, replayed
  token, and a peer that dies between steps 3 and 4;
- the generated `authorized_keys` line, byte for byte, including `from=` derived
  from the inventory;
- the mesh closure logic when a third node joins two existing members;
- revocation removing exactly the intended keys and nothing else.

On real machines, as the M3 acceptance criterion:

- form a three node cluster entirely from the UI, then run the same playbooks
  from a conventional Ansible control machine with the exported inventory, and
  assert no change is reported. If the UI produced a genuine inventory, this
  test passes by construction, and it is the proof that the infrastructure as
  code property survived.
