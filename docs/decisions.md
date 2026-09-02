<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Decisions

Settled decisions are recorded with their reasoning, because the reasoning is
what a later reader needs in order to know whether the decision still holds.
Open ones carry a recommendation so implementation is never blocked.

## D1 - Settled: the UI edits the inventory, it does not configure machines

**The structural decision of the project.** Three candidates were weighed:

- **A. Reimplement the logic** in Python, writing `corosync.conf` and running
  `cephadm` directly, coordinating nodes over an mTLS channel between the
  services.
- **B. Pull convergence,** each node running Ansible against `localhost` from a
  shared inventory, with the services orchestrating the ordering.
- **C. Push convergence,** a manual secret exchange establishing SSH trust
  between nodes, after which any node runs the existing playbooks against the
  others.

**C is chosen.** A destroys the property that defines SEAPATH: the product is a
function from an inventory to an infrastructure, and a UI that mutates machines
imperatively is no longer that product. It also duplicates the most dangerous
logic in the codebase, cluster formation and OSD handling, into a second
implementation that CI does not test.

B preserves the paradigm but requires making `configure_ha` and `cephadm`
mono-host, since both are multi-host by construction through `delegate_to`,
`add_host` and a `fetch` of the corosync authkey through the control machine.
That means rewriting tested code on the most dangerous path, for the sole
benefit of avoiding SSH between nodes.

C keeps every role untouched, keeps the CI tested execution paths, and pays for
it with an SSH mesh whose trust is established by an explicit operator gesture.
The three machines already share a corosync secret, a Ceph cluster and each
other's VM storage, so the mesh makes an existing trust domain explicit rather
than creating a new one.

## D2 - Settled: the trust targets the `ansible` account, permanently, restricted

Root SSH is not an option: `configure_hardening` sets `PermitRootLogin no`,
`PasswordAuthentication no` and `AuthenticationMethods publickey`, so a root
based trust breaks on the first hardened machine. The target is the `ansible`
account with sudo, which the reference inventories already assume.

The trust is permanent rather than armed per run. Ephemeral trust sounds safer,
but every day two operation goes through a playbook, so it would be re-armed
constantly, and a cleanup that fails leaves exactly the state it was meant to
avoid. Permanent and visible beats ephemeral and unreliable.

Restriction means `from=` bound to the peer's administration and cluster
addresses, `restrict`, one key pair per direction, and revocation from the UI.
The only exception is `pty` on the relation a node has with itself, which the
console needs and D19 explains; sudo needs none, the ISO sets
`Defaults:ansible !requiretty`. It does not mean a command restriction,
because Ansible needs arbitrary root and pretending otherwise would be theatre.
Say so in the security documentation rather than implying a limit that is not
there.

## D3 - Settled: the inventory is a git repository replicated across nodes

Single writer under quorum, the commit hash as the version of the desired
state, `git log` as the audit trail, `git revert` as the rollback, and export
as a tarball for a site that wants a conventional control machine.

Rejected alternatives: plain files synchronised by the service, which loses
history and still has to solve concurrent edits; and an external git remote,
which is the most orthodox infrastructure as code answer but makes an offline
commissioning impossible, and commissioning is precisely when a substation is
least connected.

## D4 - Settled: three nodes

Not an arbitrary limit. The reference cluster inventory encodes a physical ring:
`team0_0` and `team0_1` are the two cluster interfaces, `cluster_next_ip_addr`
and `cluster_previous_ip_addr` name the neighbours, and `br_rstp_priority`
breaks the loop. `/etc/cluster.conf` also has room for exactly three entries and
`vm_manager` reads `observer` from it. A fourth node is a topology question, not
a form field. Two hypervisors plus one observer, or three hypervisors, covers
the target deployment.

## D5 - Open: VM console

A serial or VNC console is what makes a UI feel complete, and it is also a
websocket proxy into a guest.

**Recommendation: out of scope until M5.** Then reconsider, starting with the
serial console for `operator` and above, proxied through the owning node, bound
to the authenticated session, with a hard timeout.

*Recommendation followed. Nothing is built, and the API surface has no room
reserved for it, which is deliberate: reserving room is how scope creeps.*

## D6 - Open: first login credentials

The ISO must produce a machine reachable from a browser immediately, with no
prior Ansible run.

**Recommendation: accept `root` through PAM** with the installer requiring a
root password. It is the Proxmox behaviour and needs no new machinery. Note the
interaction with D2: this is local PAM authentication to the web service, not
SSH, so `PermitRootLogin no` does not affect it. If hardening later forbids
even that, fall back to a one time token printed on the console at first boot.

*Recommendation followed at M0.* `root` resolves to the `admin` role without
being a member of any group, since the groups are created by the Ansible role
and therefore do not exist on a machine that has never converged. The behaviour
is behind `SEAPATH_WEBUI_ALLOW_ROOT_LOGIN`, default true, so a site that has
created real operator accounts can turn it off without a new release.

## D7 - Open: where this repository ends up

The service manages SEAPATH machines and ships the SEAPATH collection. It may
belong under the SEAPATH organisation rather than as a personal project.

**Recommendation:** develop here, keep Apache-2.0 and SPDX discipline from day
one so upstreaming is a move rather than a relicensing, and raise it with the
maintainers once M1 is demonstrable.

*Recommendation followed.* Every source file carries an SPDX header,
`Apache-2.0` for code and `CC-BY-4.0` for documentation, and the RTE copyright
line the sibling repositories use. Nothing in the tree assumes this URL.

## D8 - Settled: whole playbooks, scoping later if the need is proven

A full `seapath_setup_main.yaml` on a live cluster restarts a lot, so scoping by
tags is tempting. It is refused for now: the tags in `seapath-ansible` were
never designed as a public interface, `ansible.cfg` already skips
`package-install` by default, and a tag selector produces combinations nobody
has run. A whole playbook is what the CI executes, which makes it the only
granularity with evidence behind it.

If scoping proves necessary, each scoped operation becomes its own catalogue
entry with its tags baked in. A free form tag field never appears in the UI.

The catalogue is [playbooks.md](playbooks.md), and it is deliberately smaller
than the list of playbooks in the repository.

## D9 - Open: run the Ansible process in a sibling container

Would let a run survive a restart of the service itself, at the cost of access
to the podman socket, which is root on the host.

**Recommendation: do not.** The interruptions that matter are reboots and
network changes, which a sibling container does not survive either. Persisted
artefacts plus idempotent relaunch cover the need. Revisit only if operators
report losing runs for reasons other than a reboot.

*Recommendation followed.* The quadlet asks for no podman socket, and M1 runs
`ansible-runner` in the service process.

## D10 - Settled: a node cannot report its SEAPATH version

Found while implementing the node view, and it contradicted the first draft of
[api.md](api.md). The version is written into the installation media metadata by
`generate_seapath_metadata.py` in `seapath-build_debian_iso`, and lands nowhere
on the installed system: `/etc/os-release` is plain Debian, and no role writes a
version file.

Rather than invent one, `GET /node` reports `seapath_version: null` and carries
the **collection version** baked into the image at build time. That is the
better answer anyway: what decides which playbooks exist and what they do is the
collection this service will run, not the image the machine was installed from.
A run is identified by the pair "inventory commit, collection version".

If a SEAPATH release later writes a version marker on the installed system, the
field is already there to fill.

## D11 - Settled: the service ships its own ansible.cfg

`galaxy.yml` lists `ansible.cfg` under `build_ignore`, so the installed
`seapath.ansible` collection carries none of the repository's Ansible
configuration. Running a collection playbook with Ansible's defaults would
gather facts on every host, continue past a failed one, and install packages,
because `gathering = explicit`, `any_errors_fatal = True` and
`[tags] skip = package-install` all live in that file.

The run adapter therefore writes an `ansible.cfg` per run, reproducing the
settings that change **behaviour** and leaving out the ones that only change
how a terminal looks, since `ansible-runner` owns stdout. The file is kept in
the run's artefact directory, so what a run was configured with is as
recoverable as what it did.

The consequence for reproducibility is worth stating: a run is identified by
the inventory commit, the collection version **and** this configuration. The
first two are reported by the API, and the third is an artefact.

## D12 - Settled: an entry the shipped collection lacks is not offered

The catalogue names playbooks in a collection released on its own schedule, so
a SEAPATH release can add or rename one underneath this service. Rather than
discovering that at the first task of a convergence, the catalogue is checked
against the collection installed in the image, and an entry that is not there
is reported unavailable naming the collection version.

Found by building the image: `seapath_setup_prometheus_exporters` and
`seapath_setup_deploy_seapath_alloc` exist on the `seapathalloc` branch of
`seapath-ansible` and not on `main`, so an image built from `main` correctly
offers neither. The image is built from `seapathalloc`, which is what makes the
two entries offerable, and the check stays: it is what a SEAPATH release
renaming a playbook underneath this service runs into next.

## D13 - Settled: live state is the exporter's, this service reads what a machine *is*

The node view first served both halves of "what about this machine": what it
**is**, meaning its hardware, identity and cluster membership, and what it is
**doing**, meaning unit states, the journal, the clock offset and the tuned
profile. Only the first half survives, and the split is the useful decision
rather than the deletion.

Every SEAPATH node runs `prometheus-node-exporter`. Live state is therefore
already collected, already kept with history, and already alerted on, and none
of that is true of a page in a browser nobody has open. A second source of
truth for it earned nothing.

What it cost is the part worth recording, because the reasoning has to survive
the next person who wants a services table. Reading a unit state from inside a
container needs a route to the host's systemd: `systemctl` as root uses
`/run/systemd/private`, whose peer credentials cannot cross a PID namespace, so
the reading has to run under an unprivileged uid to reach the bus instead. That
took two deployments to work out. It brought `/run/systemd/system`, `/run/dbus`,
`/var/log`, `/run/log` and `/etc/machine-id` into the quadlet, `systemd` and
`chrony` into the image, and thirty five lines about `SO_PEERCRED` into a file
whose whole virtue is being short enough to read.

The line drawn, for the next reading somebody proposes:

- it stays if it answers **what the machine is** and comes from a file the
  container already sees, meaning its own `/proc`, the read only `/sys`, or a
  mount that is already there for another reason;
- it goes if it answers **what the machine is doing** and needs a route to a
  host daemon, whether that is systemd, the journal, chronyd or dbus.

`load_average` and the per CPU busy ratio sit on the near side of that line and
stayed: `/proc` is not namespaced for either, so they cost no mount, and the
CPU grid they feed is read while choosing an isolated set rather than as
monitoring.

The reverse is enforced rather than remembered:
`test_the_container_is_given_no_route_to_the_live_state` in
`tests/test_packaging.py` fails if one of those mounts returns.

This also settles what the cluster view will be at M3. Corosync ring state and
Pacemaker resource placement are live state by the same definition. What this
service can add that Prometheus cannot is **conformance**: whether the machines
match the inventory they were converged from, which is a question about the
desired state and belongs here.

## D14 - Settled: the service earns the right to write a file by reproducing it

Adoption of an existing inventory was specified in
[inventory.md](inventory.md#adopting-an-inventory-that-already-exists) before
any inventory written by somebody else had been read. Then one was: a three node
cluster deployed the conventional way, from a fourth machine. The service read
three host entries out of it and would have destroyed everything else on the
first form save.

Five shapes did it, and all five are ordinary:

| Shape in the file | What the service did |
|---|---|
| Groups under `all.children` | Read no group at all, so a three node cluster parsed as `standalone` |
| Variables on groups | Read none of them: the model reads host variables and `hypervisors.vars.isolcpus` |
| `mons`, `osds`, `clients` | Unknown groups, absent from anything the renderer writes |
| `hostname: elabo1` under the key `node1` | Would write `hostname: "{{ inventory_hostname }}"`, renaming three running machines at the next network run |
| No `subnet` | Would write `subnet: 24` on every host, a value nobody chose |

The tempting reading is that the parser needs a few more cases. The durable one
is that **a writer that cannot reproduce a file has no business writing it**,
and that this has to be checked rather than believed, because the failure is
silent: a form save that drops thirty group variables produces a valid
inventory, a clean commit, and a cluster that breaks on the next convergence for
reasons nobody connects to the save.

The first answer was to serve such a file read only, listing what a save would
have destroyed. It shipped, it was checked on the cluster above, and it lasted
one conversation: the target is that an ISO and an inventory are enough to
deploy a cluster, and an inventory nobody can edit fails that on the second
word.

So the writer changed instead. A save against a file this service did not
produce is an **edit**:

- `app/inventory/resolve.py` resolves a document the way Ansible does, groups
  included, in both declaration shapes;
- `app/inventory/editor.py` uses `ruamel.yaml` as a parser that reports the line
  and column of every value, and writes a change as a splice into the original
  text. One changed variable is one changed line, and the comments an engineer
  wrote for the next engineer are still there;
- `app/inventory/fidelity.py` resolves both versions after every edit and
  refuses the commit unless the difference is exactly what the form asked for.

Where a change lands is itself a decision. A variable already on the host is
changed in place. A variable inherited from a group is written **on the host**,
as an override, because the form edits one machine and rewriting
`cluster_machines` would silently reconfigure the other two.

Three consequences worth stating:

- **A freshly installed machine is unaffected.** A file the renderer produces
  round trips exactly, which is how the service recognises its own work, and it
  keeps being rendered rather than edited.
- **The resolver is anchored to Ansible rather than to our belief about
  Ansible.** A test asserts it agrees with `ansible-inventory --list` variable
  for variable on both reference inventories and on the real one. A resolver
  wrong in the same way in both directions would make every check here say
  "safe" and mean nothing.
- **The verification survived its own bug.** The first editor spliced a value
  and swallowed the line below it, and the resolved comparison named the
  swallowed variable before anything was committed. That failure is now a test.

What the editor refuses, rather than approximates: adding or removing a machine,
and changing a role, which means moving a host between groups. Both are cluster
formation and arrive with it at M3.

## D15 - Settled: the site key is an interim path to the other machines, and it is explicit

An ISO and an inventory have to be enough to deploy a cluster. Importing and
editing an inventory landed with [D14](#d14); applying it to three machines
needs this node to reach the other two, and the mutual handshake that gets it
there properly is M3.

The interim answer uses what the site already has. Every machine from the ISO
trusts a site key, a control machine holds its private half, and an operator can
hand that half to one node. `PUT /trust/site-key` takes it, `0600`, no read
back, no logging, fingerprint only. `ssh` is given it alongside this node's own
key with `IdentityFile`, resolved at each launch so adding or removing it takes
effect on the next run.

The cost is stated where an operator reads it rather than in a footnote: that
key is root on every machine that trusts it. The mitigation is that it is
visible, revocable in one click, and temporary by design.

Host keys are the other half and get the same treatment. `ssh-keyscan` is trust
on first use, so the scan reports fingerprints and writes nothing, and an
operator compares them against the machines before accepting. Accepting is what
writes. `StrictHostKeyChecking=no`, which several real inventories carry in
`ansible_ssh_common_args`, would have made all of this unnecessary and pointless
at the same time.

Two things this deliberately does not do. It does not generate a key and install
it on the peers, which would need the peers' cooperation and is the handshake.
And it does not accept a passphrase protected key: nothing here can type a
passphrase during a run at three in the morning, and storing the passphrase next
to the key it protects is a decision dressed as a feature.

## D16 - Settled: the guided form and the file are both editors, on the same page

Once the inventory could be edited as a file, the per machine form looked
redundant, and the question was asked directly: why is there a machine section
inside the inventory page at all?

It is not redundant, and what it holds is everything a text area cannot:

- **discovery.** The interface selector lists the NICs this machine actually
  has, with link state and PTP capability; the disks come with their stable
  `by-path` name; the CPU count is what `isolcpus` is chosen against. Nobody
  types `eno12429` into a text area without first knowing it exists.
- **values that must not be typed.** `grub_password` has to be a PBKDF2 hash.
  The form takes the password once and stores only the hash. By hand it means
  running `grub-mkpasswd-pbkdf2` in a shell, and a password typed in clear into
  the file is in the git history for good.
- **guard rails.** `isolcpus` behind a collapsed expert section, CPU 0 refused,
  the machine's name typed out before a real time change.

On a machine fresh from the ISO with an empty inventory, the form **is** the
commissioning path. For a site arriving with a complete inventory it is nearly
useless, which is exactly why it felt redundant to the first site that did.

What was actually wrong was the page: two editors of one file stacked on each
other, under a heading that read like "this machine's settings" while it edited
YAML. So the page split along the line the whole design already draws:

| Page | Question |
|---|---|
| `/inventory` | What should these machines be? The file, guided form and text area side by side, and its history |
| `/system` | What makes it so? The site key, the host keys, and the runs |

The related request was for a "system configuration" tab, for fixing a machine
when the ISO got something wrong. It is answered by `/inventory` plus `/system`
and by nothing else, because **there is no path in this design where the UI
writes to a machine's files.** A wrong administration address is fixed by
declaring the right one and applying, which is the ordinary commissioning flow
and the reason reachability is not a commit rule. What genuinely cannot be
fixed from here is a missing `ansible` account or a missing key, since the
service refuses to create an account nobody reviewed. That is a console job or
a reinstall, and saying so is better than a button that half works.


## D17 - Settled: the inventory is a folder, mounted where a control machine would put it

The first real inventory this service met uploads two quadlets:

```yaml
upload_extra_files_upload_files:
  - { src: '../inventories_private/node-exporter.container.j2', dest: '/etc/containers/systemd/node-exporter.container', mode: "0644" }
  - { src: '../inventories_private/nginxquadlet.container', dest: '/etc/containers/systemd/nginxquadlet.container', mode: "0644" }
```

That is an ordinary inventory. A dozen roles take a path to a file the control
machine holds: `iptables_rules_path`, `iptables_rules_template_path`,
`syslog_conf_template` and the three syslog certificates, `cephadm_spec_path`,
`configure_hypervisor_tuned_path`, `hosts_path`, `update_swu_image_path`,
`vm_disk`, `vm_template`, `additional_disk`, `cloud_init.user_data_file`. A
repository holding `inventory.yaml` and nothing else describes machines that no
playbook run from here could converge.

So the repository holds a **folder**, and every file in it is versioned with the
inventory: one commit per upload, `git log` as the audit trail, `git revert` as
the rollback, the export carrying all of it.

Where the folder is mounted at run time is the part that had to be discovered
rather than decided. When Ansible resolves a relative `src`, the anchors it uses
are the role's own directories and **the directory the playbook sits in**
(`DataLoader.path_dwim_relative_stack`). The directory holding the inventory has
no say in it, and neither does the working directory or the command line. On a
control machine that is a checkout of `seapath-ansible`, the playbooks are in
`<checkout>/playbooks`, so `../inventories_private/x` means
`<checkout>/inventories_private/x`, and that is the convention every inventory
in the field is written against. Run out of the installed collection, the same
path means a file inside `seapath.ansible`, which the site does not own and
`galaxy.yml` does not ship.

Three ways out were considered:

| Option | Why not |
|---|---|
| Rewrite the paths on import | The service would edit an inventory nobody asked it to edit, against everything [D14](#d14) settles |
| Require absolute paths under `/etc/seapath/inventory` | Every existing inventory has to be rewritten by hand, and the exported one no longer runs on a control machine |
| Copy the site's files into the image's collection | Writes into the tree whose fingerprint identifies the code a run used |

What ships instead is a **mirror of the collection, per run**, in the run
directory: one symlink per entry of the installed collection, the site's folder
overlaid at its root, and `ANSIBLE_COLLECTIONS_PATH` naming the mirror before
the image's own root so the dependency collections still resolve. The playbook
is still addressed by its fully qualified name.

`playbooks/` inside the mirror is a real directory holding one symlink per
playbook, and that detail is the whole trick: `..` is resolved by the kernel
after a symlink, so a symlinked `playbooks/` would send every relative `src`
straight back into the installed collection.

The result is that a path means the same thing here as on a control machine,
which is the property this service has claimed since its first page and could
not previously honour for anything but the inventory itself. `tests/test_run_staging.py`
ends by handing the staged tree to a real `ansible-playbook`, with the `copy`
task of the real `upload_extra_files` role and the `src` quoted above, and
asserting the file arrived. Ansible is the only authority on how Ansible
resolves a path.

Two consequences worth stating:

- **A run copies the folder rather than pointing at it.** The repository moves
  on and the trace must not, which is the reason a run has kept a copy of the
  inventory since the first version. The copy is what the mirror overlays.
- **A file the inventory names and nothing holds is a warning, never a
  refusal.** Committing the variable before uploading the file is an ordinary
  order of work. `GET /inventory/references` answers where each path resolves,
  and where a missing one should be uploaded, which is worth more than a
  refusal: the alternative is finding out three minutes into a convergence,
  from a task that failed on every host at once because `any_errors_fatal` is
  on.

## D18 - Settled: the large files live beside the repository

`vm_disk: "../files/guest.qcow2"` names the same kind of thing as a quadlet
does, resolves the same way, and has to be in the same tree at run time. Putting
it in git is where the resemblance has to stop. A twenty gigabyte image in a
repository stays in its history forever, one copy per upload, and takes the
export, the clone and the whole "the inventory is a small git repository" idea
with it. `git-lfs` is not on a SEAPATH node and adding it would put the desired
state behind a server that has to be reachable.

So there are two stores, and a run overlays both under the same root:

| Store | Holds | Answers to |
|---|---|---|
| `/etc/seapath/inventory` | The inventory and the configuration files it names, up to 4 MB each | `git log`, `git revert`, the export |
| `/var/lib/seapath-webui/artefacts` | VM images, archives, anything larger | The run record, which lists what it staged |

The cost is stated rather than hidden: **a change to an artefact leaves no trace
in the history, and the export carries the desired state without the images it
names.** What remains is the run record, which lists every file the run was
given with its size and its store, so "which image did that run actually push"
has an answer even though `git log` has nothing to say about it.

The limit between the two is a size rather than a file type. A file over the
limit is refused by the versioned folder with the artefacts named in the
refusal, because the alternative is an operator who cannot tell why one upload
worked and another did not.

## D19 - Settled: the shell is served here, over the connection a run makes

[SPEC.md](../SPEC.md) put the shell with Cockpit, and this takes it back. The
node view has a button that opens a terminal on the machine the service runs
on, in the browser, beside the readings the same page shows.

The reasoning is that the alternative was worse in the one way that matters
here. Sending an operator to Cockpit for a shell means a second service, a
second port, a second authentication and a second certificate to trust, at the
moment when what is wanted is one command on the machine already on screen. The
first minutes of a node are exactly when something is wrong with it, and D6
exists because that machine must be usable from a browser before anything has
converged.

What keeps this from being a hole in [D1](#d1---settled-the-ui-edits-the-inventory-it-does-not-configure-machines):

- **It opens no access this service did not already have.** The console is one
  `ssh` to the `ansible` account with the key the self trust provisioned, over
  the loopback, with the `known_hosts` the startup wrote. That is the
  connection every run makes. A console gives what `ansible-runner` is given,
  and this service holds no other credential to offer.
- **It costs one option on one line.** `restrict` forbids a terminal, so the
  self relation carries `pty` after it and a peer relation does not. That was
  found the way these things are found: the first console on a real machine
  answered "PTY allocation request failed on channel 0". The option grants
  nothing that key could not already do, since it carries no `command=` and
  can spawn a pty of its own, and the startup rewrites the line, so an
  existing node picks it up when the service restarts.
- **It configures nothing.** The service still writes only the inventory and
  the trust material. What an operator types is theirs, and the panel says, on
  every open, that it is invisible to the inventory and undone by the next run
  that touches it.
- **It is bounded.** The container spawns the client the image already carries,
  in a pseudo terminal, with `BatchMode` so a refused key cannot become a
  prompt nobody can answer, without the multiplexed connection a run holds
  open, and with the ssh client configuration ignored so the connection is what
  the command line says it is. Sessions are capped and idle ones are closed.

The cost, stated rather than hidden: **the `ansible` account has passwordless
`sudo`, so a console is root on this node.** The role required to open one is
therefore a setting, `SEAPATH_WEBUI_CONSOLE_MIN_ROLE`, and the console can be
turned off entirely with `SEAPATH_WEBUI_CONSOLE_ENABLED=0`. The default is
`viewer`, meaning every authenticated account: on a node local UI whose accounts
are the machine's own Unix accounts, someone who can sign in here can already
ssh to the machine. A site that wants those to be different rights raises the
setting, and no release is needed to do it.

The audit story is honest and thin. `git log` records who changed the desired
state and a run record records who launched it; a shell records neither, so
what exists is a journal line naming the account that opened the console and
one naming its exit. That is the whole trace, which is the reason the panel
says what it says.

This decision covers the shell on **this** node. A console into a guest is
still [D5](#d5---open-vm-console), still out of scope, and still a different
problem: it is a proxy into a machine this service does not administer.
