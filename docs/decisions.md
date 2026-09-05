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

## D16 - Superseded by [D20](#d20): the guided form and the file are both editors, on the same page

Kept here for its reasoning, which [D20](#d20) answers point by point after the
form met the workflow it was built for. The page split it describes still
holds.

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
| `/inventory` | What should these machines be? The folder and its history |
| `/deployment` | What makes it so? The site key, the host keys, and the runs |

The related request was for a "system configuration" tab, for fixing a machine
when the ISO got something wrong. It is answered by `/inventory` plus `/deployment`
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
`sudo`, so a console is root on this node.** That is what fixes the default at
`admin`, and the rule it follows is that a console hands out the access the
role already commands and nothing beyond it:

| Role | What it can do through the API | A shell as `ansible` |
|---|---|---|
| `seapath-viewer` | GET requests, nothing else | root, from a read only account |
| `seapath-operator` | the above, plus cancelling a run | root, from one extra verb |
| `seapath-admin` | launch runs, write the inventory, the trust operations | what a run already runs as, on every machine of the inventory |

An admin loses nothing by being handed a shell, since `POST /runs` already
executes `ansible-playbook` as that account with that `sudo` on every machine
the inventory declares. For the other two the console would be the thing that
grants root, which is a decision no service should make on a site's behalf.

The first version of this decision defaulted to `viewer`, on the argument that
someone who can sign in here can already ssh to the machine. The argument does
not hold. Signing in proves the account authenticates against PAM and sits in
a SEAPATH group; it says nothing about that account holding `sudo`. The groups
are supplementary ones, added to ordinary Unix accounts by
`usermod --append --groups seapath-viewer alice`, and the console does not
offer alice a shell as alice: it offers one as `ansible`. Read only in the UI
and root on the hypervisor were one `usermod` apart, which is the opposite of
what putting somebody in `seapath-viewer` is meant to express.

`SEAPATH_WEBUI_CONSOLE_MIN_ROLE` still moves the bar in both directions, and
`SEAPATH_WEBUI_CONSOLE_ENABLED=0` turns the endpoint off along with the button.
A site that wants every account to reach a shell lowers it knowingly, which is
a different act from inheriting it.

The audit story is honest and thin. `git log` records who changed the desired
state and a run record records who launched it; a shell records neither, so
what exists is a journal line naming the account that opened the console and
one naming its exit. That is the whole trace, which is the reason the panel
says what it says.

This decision covers the shell on **this** node. A console into a guest is
still [D5](#d5---open-vm-console), still out of scope, and still a different
problem: it is a proxy into a machine this service does not administer.
## D20 - Settled: the inventory page is an editor over the folder, and the form is gone

[D16](#d16) kept the guided form beside the file and argued that what it held
could not be typed into a text area. Use answered it: the operator's whole
workflow was "Edit the inventory file" and save. The form modelled eleven
variables of a file that holds fifty, so the page offered two ways to change
one file, agreeing about a fraction of it, under a heading that read like a
machine's settings.

SEAPATH configures a machine through an inventory, standalone included. The
page is now the shape of what it edits: the folder on the left, the open file
on the right, and the acts a folder has. Open, edit, save, add, delete, each
one a commit.

What that costs, named rather than discovered:

- **Discovery no longer fills fields in.** It fills the file instead: "Propose
  a standalone inventory" renders what this machine describes for itself,
  through `GET /inventory/proposed`, into the editor as an unsaved candidate.
  That is the seed of first boot offered at any time, and it covers the machine
  re-cabled since installation as well as the one whose discovery failed then.
  The NIC names, the disks by their stable path and the CPU count are also on
  `/`, which is the page that answers what this machine is, and an operator
  writing `eno12429` reads them there.
- **`grub_password` has to be hashed elsewhere.** `grub-mkpasswd-pbkdf2`, or
  `PATCH /inventory/hosts/{name}` with `grub_password_plain`, which hashes it
  and commits only the hash. Typing a password into the file in clear puts it
  in the git history for good, and the file editor cannot stop that.
- **The expert section went with the form**, so `isolcpus` is typed like every
  other variable. The rule it served, that the UI never makes a real time
  relevant change look routine, now lives entirely where the machine actually
  changes: the apply confirmation on `/deployment`, which names the disruption and
  the machines before anything runs. Editing the file changes no machine.

What the page gained is the folder. Every file the inventory names is listed,
editable in place, and the ones it names that nothing here holds are listed
too, in the colour of a warning: a missing quadlet is a convergence that stops
at the task copying it, on every host at once, and the answer is now one click
rather than a search.

## D21 - Settled: the catalogue is the collection, read, with the reviewed entries on top

The catalogue was thirteen entries written by hand against one version of
`seapath-ansible`. The collection ships thirty-five playbooks. An operator told
to run `seapath_setup_prerequisitesdebian` opened the page and did not find it,
and nothing on the page said whether it was missing, forbidden or simply
unknown here. A list that answers "what can this node run" with a subset chosen
in another repository, months earlier, is a list an operator cannot trust.

So the list is now the collection. `analysis.py` opens every playbook the
installed collection carries, follows its `import_playbook` chain and its
roles, and derives what the UI has to know before it may offer a button: the
groups the plays target, what check mode is worth, whether it reboots and
behind which variable, and the variables the playbook refuses to start without.
The reviewed entries keep every word: where one exists, its prose and its
judgement win whole, and analysis is attached beside it as counts.

The two disagree, and the disagreement is instructive rather than alarming.
`seapath_setup_snmp` is reviewed `full`; the reader finds one command in its
role chain and says `partial`. Both are right. The command detects the
distribution and writes nothing, which a human knows and a counter cannot.
That asymmetry is the whole reason the reviewed value wins.

What the reader refuses to do:

- **It offers no `ci_*` or `test_*` playbook.** They reinstall an ISO, restore
  a snapshot, reboot on a USB drive. They build a machine from nothing, and no
  reading of a YAML file makes them safe next to the network configuration.
- **It guesses no variable.** A playbook that needs `machine_to_update` is
  listed, with the variable named, and stays unavailable: a free text field
  wired to an Ansible run is the extra vars box this service refuses to have,
  and typing a value into it is how a UI becomes a shell.
- **It withdraws a check it cannot make useful.** Whether a preview would
  crash on a task reading the output of a skipped command is answerable, and
  the answer fired on twenty of twenty-six playbooks: nearly all of them import
  `detect_seapath_distro`, which reads the `rc` of a `grep` inside a block
  guarded by a condition that is almost never true. A warning on three quarters
  of a list is not a warning.
- **It guesses no polarity.** A reboot behind `skip_reboot_setup` is gated and
  the checkbox is offered. A reboot behind any other condition is reported as a
  reboot, because a checkbox reading "converge without rebooting" that reboots
  a substation hypervisor is worse than a warning that overstates.

An entry nobody reviewed is marked as such in the list, under its own heading,
and its description says in its first sentence that it was counted rather than
written. The way to promote one is to read the playbook and add it to
`CATALOGUE`, which is the same deliberate act it always was. What changed is
that not having done it yet no longer hides the playbook. The five
`prerequisites` playbooks were promoted that way, and section 4 of
[playbooks.md](playbooks.md) carries what reading them found.

The reader turned out to be worth having as a second opinion on the reviewed
entries too. `seapath_setup_network` was described as playing cluster and
standalone machines; it also plays `hypervisors`, for the SR-IOV pools and the
NIC IRQ affinity, and the scope line an operator read before confirming an
apply was wrong by two plays. Two tests now hold that line, skipped where no
collection is installed and run where one is: no reviewed entry may understate
the machines it plays, and none may say a playbook does not reboot when it
does. Both are the dangerous direction, and both are exactly what a human
writing thirteen entries by hand gets wrong.


## D22 - Settled: the prerequisites are filtered by what this machine runs

The five `prerequisites` playbooks are the only entries in the catalogue where
picking the wrong one is both easy and silent. `seapath_setup_main` chooses
between them after `detect_seapath_distro`; launched on their own they choose
nothing, and the Debian one runs `configure_seapath_distro` with `update-grub`
on whatever it reaches.

The node view already reads `/etc/os-release`, so the service knows which of
the five distributions this machine runs. It now uses that to refuse the other
four, with a sentence naming both distributions. The reasoning that makes this
sound rather than convenient: a run plays every machine the inventory declares,
without `--limit`, and this node is one of them, so a playbook for another
distribution is wrong for at least this machine whatever the others run.

Two silences are deliberate, and both leave the entry available:

- **An unreadable `/etc/os-release` blocks nothing.** Refusing all five because
  the container was mounted without `/run/host/etc` is a worse failure than the
  one the check guards against, and it would be indistinguishable from a
  catalogue bug.
- **An inventory that does not declare this node blocks nothing.** The check
  rests entirely on this machine being one of the machines the run plays. Where
  it is not, the service knows nothing about the distributions involved and
  says nothing.

What this is not is a claim about the other machines. The service reads one
`/etc/os-release`, its own. An inventory mixing distributions still needs
`seapath_setup_main`, and the note on every one of the five says so.


## D23 - Open: the collection is updated as an artefact, the image by a playbook

Everything a run executes is built into the image. The collection is cloned and
installed by a dedicated stage, its version is stamped at build time, and the
quadlet points at `:latest`. [deployment.md](deployment.md) draws the
consequence and states it plainly: the image has to be released in step with
SEAPATH. A site that needs one corrected playbook waits for an image build, a
push, a pull and a restart of the service.

Two different objects are frozen in there, and they deserve different answers:
the **collection**, which is what a run executes, and the **image**, which is
this service.

### The collection: a volume, fed by the artefact store

**Recommendation: move the collection out of the image and into a volume**,
with the image keeping the one it builds as the fallback under
`/opt/ansible/collections`. Where the volume carries a collection, it wins.

The code is most of the way there already, and that is the argument rather than
a coincidence:

| Piece | Where | What it already does |
|---|---|---|
| The root is a setting | `app/core/settings.py`, `SEAPATH_WEBUI_COLLECTIONS_PATH` | A source checkout is already pointed elsewhere |
| The version is observed | `RunService.collection_version`, `app/runs/service.py` | Read from `FILES.json` on disk at launch, so a run records the code it ran rather than the code the image shipped |
| The roots stack | `app/runs/staging.py` | `ANSIBLE_COLLECTIONS_PATH` already takes a list, so a site collection layers over the image's |
| An unknown collection is survivable | [D12](#d12), [D21](#d21) | A missing entry is offered as unavailable, and a playbook nobody reviewed is offered as read from the collection |

That last row is what makes this safe. The service was already written for a
collection released on its own schedule, so a 2.0.1 dropped underneath it lands
as reviewed entries where a fiche exists and counted entries everywhere else,
which is the behaviour [D21](#d21) chose on purpose.

Delivery is the artefact store, [D18](#d18). A substation hypervisor may have
no route to a registry and no route to GitHub, so the transport that has to
work is a file: `ansible-galaxy collection build` produces a tarball, an
administrator uploads it the way a VM image is uploaded, the service verifies a
sha256 against what the upload declared, installs it into the volume and reads
the new fingerprint. No registry to reach, no image to build, and the node's
network is not involved at all.

The trace needs a home. A run already records the fingerprint it used, so the
history stays true with no work. What has no home yet is the installation
itself, meaning who replaced the collection and when. The inventory repository
is the audit trail of this service, so an empty commit there is the cheapest
answer that keeps one place to look.

*Recommendation followed.* The service reads two roots and runs one whole, the
site's where it holds a collection, and says in the journal when that is what
is running. `PUT /api/v1/collection` takes the archive, inspects it, seeds the
new tree from the image's so it stays self contained, unpacks with
`ansible-galaxy` and renames it over the live one. It takes the run lock, so
nothing is swapped under a convergence, and the root is resolved at every
access, so the next run executes what just landed with no restart. `DELETE`
falls back to the image's collection, which is the undo that makes installing
on a live node safe to attempt. The volume is the state volume the quadlet
already mounts, so this asked for no new mount and no new host surface.

### The image: a playbook, over the SSH path that already exists

**Recommendation: the service updates itself the way it changes anything else,
by running a playbook against its own machine.** The image reference becomes an
inventory variable, the `deploy_seapath_webui` role at M5 templates the quadlet
from it, and `ansible-runner` reaches `localhost` over the SSH connection every
other run uses. Nothing new is granted to the container, and the rule of
[AGENTS.md](../AGENTS.md) holds: a machine changes because a playbook changed
it.

Three things this has to get right:

- **Pin the tag first.** The quadlet ships `Image=...:latest` today, which makes
  "which code ran here" unanswerable from the machine. Every line of this
  decision rests on the reference being exact.
- **The killer has to survive the victim.** The task restarts the unit, the
  container dies, the SSH session dies with it, and the run never records its
  own end. The restart therefore has to be detached from the run that asks for
  it, with `systemd-run --unit=... --no-block` or an `async` task polled zero
  times, so the pull and the restart happen after the run has finished writing
  its trace.
- **Refuse while a run is in flight.** [D9](#d9) keeps `ansible-runner` in the
  service process, so restarting the service kills whatever it is applying to a
  live hypervisor. The precondition is the same shape as `peer_reachable`, and
  the confirmation names the interruption like every other one here.

*Recommendation followed, for the half that belongs to this repository.* The
tag is pinned, `seapath_webui_image` is the variable, and the catalogue carries
`seapath_setup_deploy_seapath_webui` with what it costs written into the
confirmation. `GET /api/v1/node/update` reports the version answering next to
the one the inventory names for this machine, since an operator who edits the
variable and never applies it has changed nothing. A run of that entry ends
without a final status on the machine it was launched from, and the record says
that is what applying it looks like rather than calling it a failure. The role
itself, with the detached restart that lets the run reach its last task, lands
with M5 and its contract is written down in
[deployment.md](deployment.md#the-one-constraint-the-role-has-to-honour-the-restart-is-detached).

The pin is seeded rather than typed. The read only adapter reads the image
reference out of the quadlet installed on the machine, and the seed inventory
writes it into `seapath_webui_image`, resolving the `latest` the ISO installs to
the version answering. So a node nobody has edited already names the code it is
meant to run, and the role's default stopped carrying a version: a default
pinned by hand rots, and rots into a downgrade of every machine an ISO installed
on something newer. What a site pins stays a site's decision, and it is written
where every other decision about a machine is written.

### What is refused, and what stays available

`podman-auto-update` alone is refused as the answer. `AutoUpdate=registry` on
the container and the shipped timer would work, with the host's systemd doing
the pull and the restart and the container asking for nothing, which is the
appealing part. It is a timer, so nobody decides, nobody confirms, and it can
restart the service in the middle of an apply. A site that wants it can enable
it, and this service should keep the label out of the quadlet it ships.

The podman socket stays refused. It is root on the host, and [D9](#d9) already
declined it for a weaker version of this same wish.

### What does not change

A run is still identified by the pair "inventory commit, collection version",
and that pair gets stronger here rather than weaker: the version stops being a
build label and becomes a fingerprint of what is on disk, which it already is
in the code. What moves is the schedule. The collection stops being tied to the
release of this image, and the image stops being something an operator updates
by hand on a machine they had to ssh into.

## D24 - Settled: the real time measurement runs on the target, over SSH

`rtperfui` is a separate node local service that measures real time
performance: `cyclictest`, `hwlatdetect`, a page of system checks, and a CPU
map showing which VM each core is pinned to. Its four tabs are useful and its
job is one this service should carry, so the question was how to absorb it
rather than whether to.

The obstacle is one line of its quadlet, and that line is load bearing:

```ini
PodmanArgs=--privileged
AddCapability=CAP_SYS_NICE
Ulimit=rtprio=99
```

`cyclictest` measures scheduling latency, so it must run at real time priority
on the machine being measured. `rtperfui` runs it inside its own container and
pays for that with `--privileged` and `rtprio=99`. This service's quadlet says
the opposite in its own comment: `CPUAffinity` on the housekeeping set,
`CPUQuota=50%`, `Nice=5`, "the exact opposite of the rtperfui quadlet". Porting
the code as it stands means granting real time privileges to the container that
serves the management UI on a substation hypervisor, where it would compete
with the guests it exists to describe. Two services on one machine, one of them
privileged, is the same problem wearing a second port number.

**The measurement is an Ansible run.** SEAPATH already ships a `cyclictest`
role, and this service already reaches every node over SSH, the local one
included ([D1](#d1)). So `cyclictest` executes on the target through
`ansible-runner`, the role fetches its histogram to the controller, and this
service parses the artefact and charts it. What runs inside the container is a
regular expression.

That buys three things beyond the privileges:

- **Every machine, rather than this one.** `rtperfui` measures the node it runs
  on. A run plays every machine the inventory declares, so a three node cluster
  is measured in one act and compared in one view.
- **One lock, one history, one confirmation.** Loading three hypervisors at
  SCHED_FIFO 90 is exactly the kind of act the run lock and the naming
  confirmation exist for. A second subsystem with its own launch path would
  have neither, and could start while a convergence is in flight.
- **The measurement carries its desired state.** A run records the inventory
  commit it was produced from, so a latency figure is filed against the
  isolation that produced it. A number with no idea what the machine was
  configured as is an anecdote.

### What the four tabs became

| `rtperfui` | Here |
|---|---|
| `cyclictest` | A catalogue entry, run through Ansible, histogram parsed from the fetched artefact and charted on `/realtime` |
| `systemcheck` | The conformance report on `/realtime`, with every check that has an inventory variable behind it compared against the inventory rather than against an opinion |
| `seapath`, the CPU map | The core grouped map on `/realtime`, drawn from `/node/cpu`. The per VM colouring waits for M2, which is when VM definitions enter the inventory |
| `hwlatdetect` | A role and a playbook written for `seapath-ansible`, and a second catalogue entry here |

`systemcheck` is the tab that gained the most from moving. It ran ten checks
and judged each against a fixed opinion, which is all a standalone tool can do.
Here, `isolcpus` and the tuned profile it selects are declared in the
inventory, so those two become **conformance**: the machine is compared with
what it was told, and the commonest finding is a machine converged and never
rebooted, which the kernel's boot-time reading of `isolcpus` makes invisible
any other way. That is the one thing [D13](#d13) says this service can add that
`prometheus-node-exporter` cannot. The other eight stay **advice**, reported
with what they cost and never as a failure: nothing in a SEAPATH inventory has
an opinion about SMT, and a red badge over a site's own decision would be this
service voting.

The reading costs the container no new mount. The tuned profile comes from
`/etc/tuned/active_profile` through the host `/etc` that PAM already needs at
`/run/host/etc`, and everything else is the container's own `/proc` or the read
only `/sys`. `/run/tuned`, which is the daemon's running profile, stays refused
by the test in `test_packaging.py`: the configured profile is what an inventory
can be held against, and the running one is live state.

The CPU map is worth one note. `rtperfui` reads the Pacemaker CIB and pulls
each VM's libvirt XML out of Ceph RBD metadata, falling back to `nsenter` into
PID 1 when it cannot reach `cibadmin`. None of that is carried: resource
placement is live state by [D13](#d13)'s definition, and `nsenter` into the
host's namespaces is a larger version of the route [D9](#d9) already declined.
What the map shows is the topology and the isolated set, which is what an
operator reads while choosing an isolation. Per VM pinning arrives with M2,
from the VM definitions in the inventory, which is the declarative side of the
same question.

### What was written upstream to make this possible

Neither measurement was reachable as a playbook. `cyclictest` had a role, used
only from `ci_all_machines_tests.yaml`, which runs it after the Yocto
functional tests and is therefore unusable on a Debian machine or on a running
deployment. `hwlatdetect` had no role at all.

Both now exist in `seapath-ansible`: `test_run_cyclictest.yaml` wraps the
existing role, and `hwlatdetect` is a new role plus `test_run_hwlatdetect.yaml`.
Writing them there rather than carrying the commands here is the same decision
as everything else in this document: what a machine runs comes from the
collection the CI tests, and a measurement is no exception. Where a site pins a
collection that predates them, both entries report themselves unavailable
through `playbook_present`, which is what [D12](#d12) prescribes.

`hwlatdetect` earns its place next to `cyclictest` rather than inside it,
because only one of the two has anything to do with the inventory. Every
conformance check on this page reads something the kernel knows, and an SMI is
what the kernel is never told about. So a machine that passes every check and
still misses its deadline is either a firmware problem or a configuration one,
and this is the only measurement that separates them. Nothing in an inventory
reaches it, and the page says so: the fix is in the BIOS.

The role records the absence of the `hwlat` tracer in its fetched result
instead of failing. A measurement plays every machine the inventory declares
and the collection sets `any_errors_fatal`, so a kernel built without
`CONFIG_HWLAT_TRACER` would otherwise take down a run that has already loaded
the others. The page keeps that case visibly apart from a clean result:
reporting an unmeasurable machine as a machine with no interruptions would tell
an operator their firmware is clean when nobody looked.

### The consequence for `rtperfui`

It can be retired. All four of its tabs are answered here or by something that
already existed: `cyclictest` and `hwlatdetect` as runs, `systemcheck` as the
conformance report, and the CPU map by [D25](#d25), which found that
`seapath-alloc` had already removed the problem that map was drawing.

Retiring it is the point rather than a side effect: a second web service on
every hypervisor, privileged, with its own port and its own trust story, is
host surface this design spent considerable effort not having.

## D25 - Settled: the per VM CPU map is dropped, because seapath-alloc removed the problem it drew

[D24](#d24) deferred `rtperfui`'s per VM CPU map to M2, on the grounds that it
needed VM definitions in the inventory. That reading was wrong about what the
map was for, and reading `deploy_seapath_alloc` is what corrected it.

### What the map was working around

In `rtperfui`, a core's occupant is answered by correlating two remote sources:
the Pacemaker CIB says which node a VM is running on, and the VM's libvirt XML,
fetched out of Ceph RBD image metadata, says which cores its `<cputune>` asked
for. The tool also falls back to `nsenter` into PID 1 when it cannot reach
`cibadmin` from its container.

That machinery exists because of a fact that is no longer true: pinning was
**declared statically in the libvirt XML**, so the XML lived with the disk in
Ceph, and **placement was decided by Pacemaker**, so answering "which core is
this VM on" meant first answering "which node is this VM on". Two remote
lookups for a question about one machine.

`seapath-alloc` deleted both halves. Pinning is now decided **locally, at VM
start and at every migration**, by the node the VM landed on, and the result is
read back from `/proc`. `ARCHITECTURE.md` states the principle: *no daemon, no
persistent allocation database, the kernel is always the source of truth*. So
there is nothing to correlate across a cluster any more. The user's reading is
right, and it goes further than deferring the feature.

### Why the map does not move here either

Two reasons, and either is sufficient.

**It cannot be read from this container.** `status.collect()` derives occupancy
from `/proc/*/task/*/status`, the affinity of every QEMU thread on the machine.
This container has its own PID namespace and sees none of them.
[AGENTS.md](../AGENTS.md) forbids `--pid=host` by name, which is precisely the
mount-and-namespace fight [D13](#d13) refused, and `rtperfui`'s `nsenter`
fallback is a larger version of the route [D9](#d9) already declined.

**It is already published.** `seapath-alloc export` writes
`/var/lib/prometheus/node_exporter/seapath-alloc.prom`, served by the
`node_exporter` every SEAPATH node runs, and the repository ships two Grafana
dashboards for it. Which core a VM's vCPU landed on *this boot, after this
migration* is live state by [D13](#d13)'s own definition, it is collected with
history, and it is alerted on. A page in a browser nobody has open would be a
second source of truth for it, worse in every respect.

So what stays on the Real time page is the topology and the isolated set:
which cores exist, which are isolated, and which physical core each thread
belongs to. That is what a machine **is**, it comes from the read only `/sys`
this container already has, and it is what an operator reads while choosing an
isolation.

### What this service should interface with instead

The **declarative** half, which is the half Prometheus cannot answer and the
half that belongs in an inventory. `deploy_vms_cluster` and
`deploy_vms_standalone` already take a `vm_pinning_profile` variable per VM,
carried to the machine as RBD image metadata (`_seapath_alloc`) in a cluster or
as `/etc/seapath/alloc.d/<vm>.yaml` on a standalone node. It is an ordinary
inventory variable applied by an ordinary Ansible run, which is exactly the
shape this service exists to edit.

That gives M2 a sharper target than "a CPU map with VM colours":

- **Edit `vm_pinning_profile`** with the rest of the VM's definition, as
  inventory, validated against the profile schema `config.py` documents.
- **Report conformance**, which is the question no exporter answers: does this
  node's pool match what the profiles asked for. `seapath-alloc` already
  computes the answer and exports it as `seapath_alloc_active_fallbacks`: a
  *hard* fallback means an actor that asked for isolation is running on
  housekeeping cores. That is a machine failing to deliver its declared state,
  and saying so belongs on the conformance list beside `isolcpus` and the tuned
  profile.

Neither is built yet, and both are M2. What is settled here is the direction:
this service edits the profile and checks the outcome against it, and never
draws the placement.

## D26 - Settled: the pool is read from each node's exporter, and the page aggregates the cluster

[D25](#d25) dropped the per VM CPU map on two grounds: this container cannot
see the threads, and the placement is live state that
`prometheus-node-exporter` already publishes. Both facts are still true. The
conclusion drawn from them was wrong, and the page proved it: a map showing
only isolated against housekeeping repeats a row of the conformance list and
tells an operator nothing they did not already have.

What D25 missed is that reading the exporter and duplicating it are opposites.

### Asking rather than computing

`seapath-alloc` derives occupancy from the affinity of every QEMU thread in
`/proc`, which is exactly the reading this container cannot make. It runs on
the host, sees all of it, and writes `seapath_alloc_cpu_detail` to a Prometheus
textfile every fifteen seconds. `node_exporter` serves that on port 9100 on
every SEAPATH machine, because `deploy_prometheus_exporters` puts it there and
[PROMETHEUS.md](../../seapath-ansible/roles/deploy_seapath_alloc/PROMETHEUS.md)
tells a site to scrape it.

So this service asks. One HTTP GET per node, no mount, no privilege, no route
to a host daemon, and nothing computed twice. The metric carries everything the
Grafana dashboard the collection ships draws with: the CPU, whether it is
isolated, its physical core and HT sibling, its state, the actor on it, and the
members of a shared slot.

That is a narrower thing than the observation plane [D13](#d13) removed. D13
refused a **second source of truth** for live state, and the cost it refused was
a route from this container to the host's systemd. Reading a published
exposition is neither: the exporter stays the source, Prometheus stays the
history and the alerting, and this reads the current value into the page an
operator already has open.

Two properties keep it honest, and both are tested:

- **The reading carries its age.** The collector runs on a fifteen second
  timer, so the pool is never quite now, and a page implying otherwise would
  read as live for the whole minute after a node stopped exporting.
- **A node that cannot be reached is reported with its reason, beside the
  nodes that answered.** A cluster half built is the ordinary state of a
  cluster being built, and an exporter that answers without the seapath-alloc
  metrics is a different fault from an unreachable one, fixed by a different
  role. The page names which.

### The page aggregates, and that settles a scope question

The Real time page was incoherent about scope: the conformance list and the CPU
map read the local machine, while a measurement brought back one file per
machine because a run plays the whole inventory and carries no `--limit`. Two
answers were available, local only or cluster wide, and the pool decides it:
once every node's pool is on the page, showing one machine's measurement would
make the measurement the odd panel out.

So the page aggregates. The local node is marked and sorted first, because it
is the machine the operator is standing on and the one the conformance list is
still about.

**Two checks reach every node, and the rest stay local.** The exporter
publishes `isolated` per CPU, so the set each machine actually booted with
comes back from all of them, and `node_uname_info` carries the kernel. Those
two are compared against each node's own inventory entry beside its grid, and
the first is the one that matters most: `isolcpus` is read at boot, so a
machine converged and never rebooted reads exactly like one where the change
never happened, and until now that could only be caught on the node the browser
happened to be pointed at.

The other eight need a reading only the local node can make. The tuned profile,
transparent hugepages, the scheduler sysctls, the boot parameters and the
interrupt affinity are all files under `/sys` and `/proc` that no exporter
publishes. Extending them across the cluster means either running the checks on
each node over SSH, which turns a page refresh into a command execution, or
adding them to what SEAPATH exports, which is upstream work. The page says
which pane is which in its own heading rather than leaving the reader to infer
it.

> The second was written. [D27](#d27) adds those eight readings to what
> `seapath-alloc` publishes, and the whole conformance list now answers for
> every machine. The paragraph above is kept because it is the question D27
> answers.

### What was refused

**Querying Prometheus.** It would give history and one query for the whole
cluster. This repository deliberately does not deploy Prometheus and cannot
know its address: PROMETHEUS.md says it is shared across sites and its
configuration lives with the monitoring infrastructure. Asking each node
directly needs no address a site has not already given us, since the inventory
names every machine.

**An HTTP client dependency.** `urllib` makes this request, and an unused
dependency tree in a substation image is only its CVEs. The requests run in a
thread pool because the page waits on the slowest node, and three nodes in
series with one down is six seconds before anything renders.

**Reading the textfile through a mount.** `/var/lib/prometheus/node_exporter/`
would give the local node with one bind mount and no network. It answers for
one machine, which is the design this decision is moving away from, and it adds
host surface to avoid a request to a port the cluster already serves.

## D27 - Settled: the tuning is published by the exporter, and the checks answer for every machine

[D26](#d26) left the Real time page split down the middle. The CPU pool came
from every node, and the conformance list came from the machine the browser
happened to be pointed at. The readings forced that split, and D26 named the
two ways out of it and left them for later.

Eight of the ten checks read files. The tuned profile is
`/etc/tuned/active_profile`, the throttling window is two sysctls, the hugepage
pools are directories under `/sys/kernel/mm`, the interrupt affinities are
masks under `/proc/irq`, and none of it is published by
`prometheus-node-exporter`. So the eight answered for one machine, and the
question an operator actually has is about three.

### The two ways out, and why the second one wins

**Run the checks on each node over SSH.** This service already reaches every
machine that way ([D1](#d1)), so it is a few lines. It turns a page refresh
into a command execution on live substation hypervisors, on a path that has no
run lock, no confirmation naming the machines, and no run record. Every other
thing this service does to a machine goes through `ansible-runner` for exactly
those three reasons, and a reading that quietly becomes an execution is how
that property is lost.

**Publish the readings.** `seapath-alloc` already runs on every node, on a
fifteen second timer, and already writes a Prometheus textfile that
`node_exporter` serves. Adding the tuning to what it writes costs one file read
per tick on a host the collector is already on, and turns the question into the
HTTP GET this service was already making for the pool.

The second is chosen, and the work is upstream: `conformance.py` in
`deploy_seapath_alloc`, publishing `seapath_rt_*` beside `seapath_alloc_*` in
the same file, under the same `node` job, with no new port, no new daemon and
no new scrape configuration. That is the same decision as [D24](#d24)'s: what a
machine runs and what a machine publishes come from the collection the CI
tests, and this service reads it.

It also pays for itself twice. The same metrics reach Prometheus, so a site
gets history and alerting on values that used to be visible only by logging
into the machine: a tuned profile selected but installed nowhere is an alert
rule now, and it was a phone call before.

### What the service does with it

One implementation of the checks, for every machine. `app/services/checks.py`
holds the ten and takes a `RealtimeReading` and a `CpuReading`, without caring
which machine they describe. `app/cluster/tuning.py` turns an exposition back
into those readings, and `app/hosts/local.py` reads them from this machine's
own files as it always did.

The local node keeps reading its own files rather than its exporter. It needs
no collector, it is never stale, and it answers on a machine where nothing has
been deployed yet, which is exactly the machine an operator reads the tuning of
before writing an isolation down.

The page becomes a matrix: one row per check, one column per machine, local
first. That is the shape the question has. Two nodes converged from the same
inventory answering differently is the finding, whatever the answers are, and a
list of one machine's ten answers had no way to express it.

### Three silences, kept apart

A cell with nothing in it is never a failed check, and there are three reasons
a machine has nothing to say. They are distinguished all the way from the
exporter to the page, because each is fixed by a different act:

| What happened | What it means | What fixes it |
|---|---|---|
| The node did not answer | The exporter is not up, or the machine is not | Deploy the exporters, or look at the machine |
| It answered with no `seapath_rt_*` | The collector predates the block | Upgrade the collection on that node |
| It answered with an empty label | It read, and there was nothing there | Read the check: it is a finding or it is not |

The exporter carries that distinction with two conventions, both tested there
and relied on here: an info family is published even when every reading in it
came back empty, and a numeric gauge is omitted rather than defaulted, because
every value it could carry is a legitimate one. `-1` is a correctly tuned
`sched_rt_runtime_us` and a hugepage pool of `0` is a real answer.

### What was refused

**Ten grey rows for a node with an old collector.** They read as ten failures
and bury the one act that fixes it. The node gets no rows and one sentence
naming the role.

**One series per interrupt.** A machine that keeps nothing off its isolated
cores has every interrupt reaching one, which would be hundreds of series per
node on every scrape, forever. The exporter names the first eight and publishes
the true count beside them, and the count is what the check reports.

**A second endpoint for the cluster conformance.** The pool and the tuning
arrive in one exposition, so the page asks each node once. A second endpoint
would have doubled what a page refresh costs a hypervisor to display two panels
of the same reading.

**Judging in the exporter.** It has no inventory and cannot have one: whether
`isolcpus=4-7` is right for a machine is a question about what that machine was
told, which lives in the repository this service writes. The collector reports
what it read, and the comparison stays here.

## D28 - Settled: the Real time page is four views and a bar that summarises them

[D26](#d26) and [D27](#d27) made every panel of the Real time page answer for
the whole cluster, and the layout did not survive it. Three panels shared one
screen: ten conformance rows across four machines on the left, four pools of
forty-eight threads and a measurement of four histograms on the right. Each
panel got a third of the room its content needs, and each answered by cutting.
`4-23,28-47` and `4-6,17-18` were both drawn as `4-23,28-…` in the column where
the difference between them is the finding. The fourth machine of the pool sat
below the fold. A histogram lost its axis, and an operator reading a result
pressed Expand, which hid the other two panels, which is this decision taken
one panel at a time.

**One panel at a time, and a bar that carries what the other three found.** The
page is a bar of four tabs, Conformance, CPU pool, Latency and Firmware, and
the panel one of them points at. Each tab holds its own worst status as a dot
and the one line its panel would lead with: `9 worth a look on 4 machines`,
`1 machine worth a look, 4 of 4 nodes answered, 12s ago`,
`worst 26us on ccv-admin`. The glance the old layout paid for by truncating
everything is what the bar does, and the panel behind it has the screen.

Switching is local. Every reading the page shows is fetched before the first
tab is drawn, so a view is a show and a hide: an operator who looks at the pool
and comes back costs a substation hypervisor nothing.

### What the dots may claim

A dot is the worst thing the panel found, and no panel is given a verdict it
cannot support. Conformance and the pool have one, because both compare a
machine with what the inventory told it. hwlatdetect has one, because the
threshold is the operator's own and anything above it is time the kernel never
saw.

**Latency has none.** Nothing in the inventory declares a latency budget, so a
number is a number: the tab reports the worst case and the machine it happened
on, in the colour that means "nothing to compare", and the deadline it is held
against is the one the application has. A green dot here would be this page
inventing a threshold that no one wrote down.

### What was refused

**A machine picker, with everything about the machine chosen on one screen.**
It answers "is this machine tuned right" and makes "do these machines agree"
the thing an operator has to assemble by clicking through four of them. The
comparison across machines is what [D27](#d27) was for.

**Keeping the three panels and showing only the status dots, values on click.**
It fits, and it costs the reading that makes the matrix worth having: the
values are the finding, and a page that hides them behind a click is a page
where nobody sees that two machines isolate different CPUs.

## D29 - Settled: the cluster view reads the exporters, and administers nothing

An operator running a SEAPATH cluster has one question this service could not
answer at all: what is the cluster doing right now. Which node is that VM on.
Does the cluster have quorum. Did an OSD go down last night. The Inventory page
holds what the machines should be, the Deployment page makes it so, the Runs
page says what a convergence did, and none of them says where a resource is
running at this moment.

The answer is the one [D26](#d26) reached for the CPU pool, applied twice more.

### Asking rather than computing, again

`configure_ha` deploys `ha_cluster_exporter` on every member, where it runs
`crm_mon`, `corosync-quorumtool` and `cibadmin` and publishes what they said on
port 9664. `roles/cephadm` enables the Ceph manager's own Prometheus module,
which serves the whole cluster on port 9283: health, daemons, the OSD map,
pools, placement groups. Both are already scraped by the site's Prometheus, and
this repository ships a Grafana dashboard over each of them.

So this service asks. One HTTP GET per machine per reading, no mount, no
privilege, no route to a host daemon, and nothing computed twice. The same
narrow thing [D26](#d26) established and [D13](#d13) allows: the exporter stays
the source, Prometheus stays the history and the alerting, and this reads the
current value into the page an operator already has open.

### Whose answer is believed

The two readings are gathered differently, because the two exporters differ.

**Pacemaker: the coordinator's.** Every member's exporter reports the whole
cluster, because `crm_mon` does. The members agree while the cluster is healthy
and stop agreeing exactly when this page matters: a node cut off from the
others reports itself online and everything else lost. The designated
coordinator holds the CIB the cluster is acting on, so its exposition is the one
reported, it is found by the member that names itself DC, and the page says
which node answered and whether it was the coordinator. The SUSE dashboard
takes the same view through a `dc_instance` variable.

**Ceph: the active manager's.** Only one machine serves those metrics; a
standby manager answers the request and publishes nothing. Every machine is
asked and the first exposition carrying `ceph_health_status` is the cluster's,
which finds the manager without asking Ceph where it is, and survives a
failover with no configuration.

Every machine of the inventory is asked in both cases, including the ones that
turn out not to be members. A machine that cannot be reached is a row with its
reason, because on a cluster half joined that row is the finding.

### The line this page does not cross

The page is called Cluster and it monitors. It does not administer, and that is
a decision rather than an unfinished feature.

Standby a node, clean up a failure count, move a resource, evict an OSD: every
one of them is a `crm` or a `ceph` command running inside this container, which
AGENTS.md forbids in the same words it forbids writing `corosync.conf`. The
argument is not squeamishness about shelling out. It is that a cluster whose
state can be moved from a web form is a cluster whose state is no longer a
function of its inventory, and that function is the product.

So each of them is offered where it belongs:

| What an operator wants | Where it is |
|---|---|
| Add or remove a machine | The inventory, then `cluster_setup_ha` or `cluster_remove_machine` |
| Add storage | `ceph_osd_disks` in the inventory, then `cluster_setup_cephadm` |
| Move a VM, put a node in standby, clear a failure | `crm` on the machine, or Cockpit. The console is one click away ([D19](#d19)) |
| Replace a failed OSD | The `ceph` CLI, for the reason [ceph.md](ceph.md) gives |
| Alerting, history, capacity trends | Prometheus and the dashboards, which is what [D13](#d13) settled |

This is the same boundary `docs/ceph.md` drew when it refused to offer the
removal of a single OSD, generalised: the UI shows the failure, names it, and
points at the thing that owns the operation.

### What the page does with the room

Three views on the layout [D28](#d28) settled, one panel at a time, each tab
carrying its own worst status and the line an operator reads from it.
Membership leads with quorum, because a cluster without it moves nothing and
everything below is then a description of a cluster that is idle. Resources is
the table with the failure in it, which in SEAPATH is usually a VM: `vm_manager`
creates one Pacemaker resource per guest. Storage is Ceph, and it is hollow
rather than amber when there is none, because a Pacemaker cluster with local
storage is a supported SEAPATH configuration.

### What was refused

**Throughput, IOPS and latency histories.** Every one of them is a counter, a
rate needs two scrapes and a memory of the first, and a service that keeps that
memory is a monitoring system. The Grafana dashboards draw them because they
have a time series database behind them. This page has one scrape, and says so.

**One combined "cluster health" verdict.** Pacemaker and Ceph fail
independently and are repaired by different people with different urgency. A
cluster that is quorate with a degraded pool is not the same page as a cluster
that has lost a member, and one dot for the pair hides whichever is worse.

**Reading the local node only.** It is one GET instead of three and it answers
the wrong question. The node the browser happens to be pointed at is the one
node whose state an operator can already see; the cluster is what they cannot.

## D30 - Settled: a VM is added from one page, and the mechanism underneath is unchanged

The VMs page in its first form was a reading. It listed what the inventory
declared, whether a run would find each file, and what Pacemaker reported. Use
answered it the way use answered [D16](#d16): to add a VM an operator had to
upload an image on the Inventory page, upload an XML there too, write an entry
into a group called `VMs` by hand, then cross to the Deployment page and pick a
playbook out of a list of twenty. Four screens, one of which asked them to know
a group name and a YAML shape.

The reading was also wrong about the files. A VM deployed from a conventional
control machine has no image and no XML on this node, and there is no reason it
should: in a cluster the disk lives in Ceph and the image is a seed a creation
started from. The page reported four such VMs as eight missing files, which is
a page inventing work.

So the page performs the act. **Add a VM** takes a name, a disk image and a
libvirt XML, and does the four things itself: the image to the artefacts, the
XML committed with the inventory, the guest declared in the `VMs` group, and
the deployment playbook run.

### What does not change, which is the point

Every one of those four is a write this service already made:

- the two uploads go to the two stores of [D18](#d18), through the endpoints
  the Inventory page uses;
- the entry is a splice into `inventory.yaml`, checked by `fidelity` the way
  every other write is, and committed with the authenticated user. `git log`
  still holds it, `git revert` still undoes it;
- the run is a whole playbook of the collection, `deploy_vms_cluster` or
  `deploy_vms_standalone`, launched through `/runs` with its own preconditions
  and its own lock.

No machine is touched outside an Ansible run. The rule that overrides
everything is intact, and it is worth saying why plainly: the rule governs what
this service **writes**, and it says nothing about how many screens an operator
crosses to ask for it. Making the mechanism the interface was a habit.

### Adopting a VM needs no files at all

The corollary, and the thing that removes the noise rather than hiding it. A
guest that already runs is declared by its name alone:

```yaml
VMs:
  hosts:
    ABB15:
```

`deploy_vms_cluster` asks `cluster_vm status` first and skips its whole
creation block when the guest exists and carries no `force`. `vm_disk` and
`xml_path` are read inside that block, so an adopted guest never needs them.
The service therefore writes a file reference only where it was given the file,
and a guest with nothing to upload has nothing reported missing.

### The runtime plane, and how it will reach a machine

Starting, stopping and migrating a guest are next, and they are the first thing
this service will do that is neither a commit nor a whole playbook of the
catalogue. The decision is taken here so the VMs page is designed against a
settled answer:

**A runtime action is a one task play calling the upstream `cluster_vm`
module,** run through `ansible-runner` over the SSH path a convergence already
uses.

[D8](#d8) says whole playbooks, and its reasoning is that the tags of
`seapath-ansible` were never designed as a public interface, so a tag selector
produces combinations nobody has executed. A module's `command:` argument is
the opposite of that: it is the module's documented interface, one value at a
time, and `cluster_vm` is the same module `deploy_vms_cluster` calls. The rule
does not extend to it, and this is where that is written down rather than
assumed.

The alternative was the libvirt socket and `vm_manager` in process, which
[section 5.1 of SPEC.md](../SPEC.md) contemplates. It reaches the local node
only, and the guests of a three node cluster move between all three, so the
page would answer for one machine and stay silent about the others. The run
reaches any node and adds no mount.

One constraint is worth recording before it is discovered: the module reads
metadata and does not write it. `list_metadata` and `get_metadata` are
commands; there is no `set_metadata`, and `pinning_profile` is a parameter of
`create` and `clone`. Changing `_seapath_alloc` on a running guest is
`vm_manager set-pinning-profile` on the target, so it waits for a
`set_metadata` command upstream rather than being done with a `command:` task
here. And `preferred_host`, `pinned_host`, `priority` and `live_migration` are
baked at creation: a panel presenting them beside the things that change live
would be lying about half of them.
