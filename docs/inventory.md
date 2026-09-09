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
inventory and the files it names.

- The working tree holds `inventory.yaml` at its root, and beside it whatever
  the inventory references: `inventories_private/`, `files/`, `templates/`,
  `host_vars/`, `group_vars/`. Section 1bis is about that folder, because an
  inventory is rarely alone.
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

One write follows the opposite rule, because it writes every machine at once:
pinning the version of this service. The tag goes on the group that already
carries `seapath_webui_image` when the pin covers every machine of that group
and they pull from one repository, and the host lines repeating the value are
taken out, so the file names the version once. A group is left as it stands
when the pin does not cover all of it, a machine pinned by digest for instance,
or when its machines pull from two registries, and then every machine is
written on its own. What each machine receives is checked before the commit
either way: a line removed that changed a machine's effective image is a
refusal.

An inventory this service rendered itself keeps being rendered, so a freshly
installed machine keeps the canonical shape.

What the editor refuses, rather than approximates: adding or removing a machine,
and changing a role, which means moving a host between groups. Both are cluster
formation, and they arrive with it.

### The editor is the folder

The desired state of these machines is a folder, so the page is the shape of
one: the files on the left, the file open on the right, and the acts a folder
has. Open, edit, save, add, delete. Every one of them is a commit, and the
history under it is the audit trail.

- **The inventory** is edited as itself and saved with `PUT /inventory/raw`,
  carrying `If-Match` on the commit the editor read it at.
  `POST /inventory/raw/check` says what is wrong without committing anything.
- **The files it names** are edited the same way, through
  `PUT /inventory/files/{path}`, one commit each. A file too large for git is
  stored as an artefact instead, and a file the inventory names and the folder
  lacks is listed among the ones that exist, in the colour of a warning, so
  that writing it is a click rather than a discovery three minutes into a
  convergence.
- **A whole inventory a site brings** arrives through the same control as any
  other file: one named `inventory.yaml` goes to `POST /inventory/import`,
  which replaces the desired state and leaves the version it replaced one
  revert away.

#### The keys the box answers to

A textarea moves the caret out of the field on Tab and drops it in column zero
on Enter, which is a poor box to write a file whose indentation carries meaning
in. `app/ui/static/yamledit.js` is the four bindings that close that gap, and
the line under the editor says they are there:

| Key | What it does |
|---|---|
| `Tab` | shifts the lines the selection touches one level in, or types to the next even column when nothing is selected |
| `Shift`+`Tab` | shifts them one level out, taking what a line has when it has less than a level |
| `Enter` | carries the indentation of the line it leaves: one level in under a key ending in a colon, aligned under the first key of a `- src:` entry, and the dash repeated in a list. An empty item ends the list and goes with it |
| `Ctrl`+`/` | comments the block out at its outermost column, and back |

Every one of them writes through `document.execCommand("insertText")`. It is
deprecated and it is the only write that leaves the browser's own undo stack
intact: `value` and `setRangeText` both empty it, so one `Ctrl`+`Z` after an
automatic indent would throw away everything typed before it.

The bindings are the editing half. The half that knows what a variable *is*,
the vocabulary the roles define and completion over it, is a separate matter
and is not there.

`PUT /inventory` and `PATCH /inventory/hosts/{name}` take values rather than a
file. The page stopped using them when the form went ([D20](decisions.md#d20)),
and they remain the path for a client that holds variables rather than a
document.

A whole file arriving by any route is checked three ways before it becomes a
commit: it parses into something shaped like an inventory, it satisfies the
rules of section 5, and `ansible-inventory --list` accepts it. That last one
has a trap in it worth naming: **`ansible-inventory` exits 0 on a file it could
not read**, having printed a warning and returned an empty inventory. Reading
the exit status alone would wave through exactly the files the check exists to
catch, so its output is read too.

### Machines and guests

A SEAPATH inventory holds two kinds of entry, and the difference decides what
this service does with one. `cluster_machines`, `standalone_machine`,
`hypervisors` and `observers` hold **machines**: this service reaches them over
SSH, scrapes their exporter, and holds them against the rules of section 5.
`VMs` holds **guests**: `deploy_vms_cluster.yaml` and
`deploy_vms_standalone.yaml` loop over that group, one include per member, and
take the host key as the libvirt domain name. Nothing here connects to a guest.

So the model keeps them apart. `hosts` in `GET /inventory` are the machines,
`guests` are the members of `VMs`, and a guest carries the files it names,
`vm_disk`, `vm_template` and `xml_path`, plus `force` and `enable`. Everything
else a VM entry holds is preserved the way a machine's unmodelled variables
are: `guest.xml.j2` alone reads some thirty variables, and modelling them would
be this service inventing an interface over a template a site is expected to
replace.

### Which deployment a guest belongs to

`VMs` is one group and there are two playbooks that loop over it,
`deploy_vms_cluster` from a cluster member and `deploy_vms_standalone` on the
standalone machine. A file that declares both kinds of machine therefore has no
way of saying which deployment a guest belongs to, and running the two would
create every guest twice, once in the Ceph pool and once in the local one.

`cluster_VMs` and `standalone_VMs`, declared as children of `VMs`, say it:

```yaml
VMs:
  children:
    cluster_VMs:
      hosts:
        rtvm:
    standalone_VMs:
      hosts:
        localvm:
```

Ansible lists the hosts of a group's children under the parent, so
`groups['VMs']` stays the union of the two and every play that configures the
guests themselves is unchanged. Only the deployment separates.

It is read the way a machine's hypervisor or observer role is read, from the
group rather than from a variable, because that is how the inventory expresses
it and how the playbooks read it. Everything that differs between a Pacemaker
guest and a libvirt one follows from it: the playbook that creates it, the
module that starts and stops it, the variables its entry may carry, and whether
it has an RBD image to hold metadata at all.

A file with one flat `VMs` group says nothing, which is every inventory written
before these groups existed. Its guests take the file's own mode, since the
group is claimed whole by whichever playbook is run. Once a file declares
either group, a guest in neither is refused: both playbooks would claim it.

Reading a guest as a machine is what an early version did, and the cost was
immediate. A standalone deployment running two VMs arrived as three machines,
two of them without an administration interface, and the import was refused by
the rule that a standalone inventory describes exactly one machine. The same
mistake offered those guests to the exporter fan-out and counted them among the
machines an apply has to be able to reach.

### Which entry describes this machine

The host key is the obvious answer and frequently the wrong one. A site is free
to key its inventory `node1`, `node2`, `node3` and carry the real names in
`hostname`, which `network_buildhosts` honours. `this_host` in `GET /inventory`
is the answer: the key, then `hostname`, then the administration address against
the addresses this machine answers on. A node that recognises no entry says so,
because putting an operator in front of another machine's configuration is worse
than admitting the file does not describe this one.

Four things have to be true of what lands there:

- **`inventory.yaml`, at the root.** That is the one file parsed as the desired
  state. An inventory kept as `seapath-cluster.yaml` is renamed with `git mv`
  and committed. The files beside it are carried, versioned and staged for a
  run, and section 1bis says where they have to sit.
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
  variables and group variables alike are read resolved, so the rules and the
  file references are computed from them, and a save through the value API
  touches only the lines it changes.

One limit of M1 is worth knowing before importing a cluster inventory: a run
plays every host the inventory declares, since the adapter passes no `--limit`,
while M1 only provisions the trust between this node and itself. Applying
against the other machines fails on them, as unreachable, until the trust mesh
of [cluster-join.md](cluster-join.md) exists at M3. Importing, reading and
editing all work today.

The repository is an ordinary git repository, so a remote survives the clone.
Nothing pushes or pulls it by itself: replication towards the other machines of
the inventory is an act the operator asks for, and section 2 describes it. It
needs what a run needs, which is a key those machines accept and their host
keys known here, so on a node that has neither it reports every machine as
unreachable until the site key of [cluster-join.md](cluster-join.md) §2b is
uploaded or the mesh exists.

## 1bis. The folder, because an inventory is rarely alone

A dozen SEAPATH roles take a path to a file the control machine holds, written
in the inventory as an ordinary variable:

```yaml
upload_extra_files_upload_files:
  - { src: '../inventories_private/quadlet-macvlan.network', dest: '/etc/containers/systemd/quadlet-macvlan.network', mode: "0644" }
```

and, in the same family, `iptables_rules_path`,
`iptables_rules_template_path`, `syslog_conf_template`, `syslog_tls_ca`,
`syslog_tls_key`, `syslog_tls_server_ca`, `cephadm_spec_path`,
`configure_hypervisor_tuned_path`, `hosts_path`, `update_swu_image_path`,
`vm_disk`, `vm_template`, `xml_path`, `additional_disk`,
`cloud_init.user_data_file`. A
folder holding `inventory.yaml` alone would describe machines that no run from
here could converge.

### Where those paths point

Ansible resolves a relative `src` against the role's own directories and **the
directory the playbook sits in**, and against nothing else. The directory
holding the inventory has no say in it, and neither does the working directory
or the command line. On a control machine that is a checkout of
`seapath-ansible` the playbooks are in `<checkout>/playbooks`, so
`../inventories_private/x` means `<checkout>/inventories_private/x`.

**The inventory folder is that checkout root.** A run builds a mirror of the
collection in its own directory, overlays the folder at the mirror's root, and
runs the playbook by its fully qualified name with the mirror searched first.
So the paths an existing inventory already carries mean here exactly what they
mean on a control machine, unchanged. [D17](decisions.md#d17) has the mechanism
and the two designs it replaces, `app/runs/staging.py` has the code, and
`tests/test_run_staging.py` ends by asking a real `ansible-playbook` whether it
works.

Practically, for an inventory arriving from a conventional deployment: upload
the directory it lived next to. An inventory kept in `inventories_private/` on
a control machine, referencing `../inventories_private/quadlet.network`, needs
a `inventories_private/quadlet.network` in this folder. The page takes several
files, or a whole directory, in one act, and a directory keeps its shape. A
site that clones its repository into `/etc/seapath/inventory` before the first
start, as in "Adopting an inventory that already exists" above, brings the
folder along with the history and uploads nothing.

### Two stores, one root

| Store | Holds | Limit |
|---|---|---|
| The repository, `/etc/seapath/inventory` | The inventory and the configuration files it names: quadlets, iptables rules, `snmpd.conf`, a libvirt XML, a Jinja template | 4 MB per file, versioned |
| The artefacts, `/var/lib/seapath-webui/artefacts` | VM images, archives, anything larger | The disk, unversioned |

Both are overlaid under the same root at run time, so `vm_disk:
"../files/guest.qcow2"` resolves whichever store holds `files/guest.qcow2`, and
the versioned copy wins where both do. A file over the size limit is refused by
the repository with the artefacts named in the refusal.

What the split costs is worth knowing before relying on it: **a change to an
artefact leaves no trace in `git log`, and the export carries the inventory
without the images it names.** The run record lists every file a run was given,
with its size and its store, which is what remains to answer "which image did
that run push". [D18](decisions.md#d18).

### Saying so before the run, rather than during it

`GET /inventory/references` answers, for every path any of those variables
carries, whether a run would find the file and where it resolved: the
repository, the artefacts, the collection itself for the defaults a role ships,
or this machine's filesystem for an absolute path. A missing one comes back
with the name to upload it under, and the inventory page offers that name as a
button.

A missing file is a **warning**, and never a refused commit. Committing the
variable before uploading the file it names is an ordinary order of work.
Refusing at the commit would forbid it, and the run is where it matters: with
`any_errors_fatal`, a `copy` that cannot find its source ends the convergence
on every host at once, three minutes in.

A templated path, `../files/{{ inventory_hostname }}.qcow2`, is left alone.
Guessing at its value would produce a confident wrong answer and tell an
operator to upload a file named after a variable.

## 2. Who may write, and how the copies meet

Every node owns its own repository and accepts writes to it. The copies are
brought together by an explicit act, and never by the service on its own.
[D32](decisions.md#d32) records the reasoning and what the earlier design, a
lead elected under quorum with an automatic push, was traded for.

- **Standalone**: the node owns its repository outright. No coordination, and
  nothing to replicate to.
- **Cluster**: the inventory page carries a **Replicate** button. It pushes
  this node's repository to the machines the inventory declares, minus this one
  and minus the guests of the `VMs` group, over the SSH connection a run
  already makes. The button names the machines it reaches, and the table above
  it says what each of them holds right now. What it needs on the far side is
  this service: the peer's host git where there is one, and the git in the
  peer's own container where there is not, which is the case on a Yocto
  observer.
- **A push that would lose commits is refused.** Git accepts a fast forward
  only, so a node carrying edits this one lacks comes back as a named failure
  and keeps its history. The operator opens the UI on that node and pushes from
  there. The act has one direction, always from the node being looked at
  towards the others.
- **Force says this node holds the copy that wins.** A checkbox beside the
  button, an administrator's act, and one audit line naming the machines. Each
  machine is made to match this one: its branch moves to this commit whatever
  it held, which survives there only in its reflog, and its files follow, a
  file nobody committed there included. It is carried by hooks the receiving
  node writes on its own repository at every start, so a node that has not been
  updated reports that it cannot be forced.
- **Each machine is reported on its own.** A node that is down is one line in
  the result, and the machines that were updated keep what they received.
- **What a machine serves is its `HEAD`**, and that is what is asked of it,
  with `git ls-remote` over the same connection, every time the page is opened.
  So a copy that is behind is shown with the commit its files are actually at,
  and no stored replication state exists that could disagree with the machines.
  A push targets that same ref: a repository sitting on another branch would
  take the commit into one nobody reads and keep its old files, which is
  reported as such rather than as a success. A node running this version moves
  its own repository to `main` when it starts.
- **A run records the commit it ran from**, which is what answers "which
  version of the desired state converged these machines" whichever node the run
  was launched from.

What travels is the repository, meaning the inventory and the configuration
files it names. The artefacts store stays where it is, by the reasoning of
[D18](decisions.md#d18), so a replica holds an inventory naming images it does
not have.

The whole mechanism is a small repository pushed between nodes when an operator
asks, which is the entire reason it is affordable. Proxmox answers the same
need with `pmxcfs`, a replicated filesystem mounted on every node, and that is
a far larger machine than this problem calls for.

## 3. Seeding by discovery

On first boot the node writes its own entry from what it can observe, so that
the operator starts from a filled form rather than from a blank file:

| Discovered | Becomes |
|---|---|
| interface carrying the default route, and its address | `ansible_host`, `network_interface`, `ip_addr` |
| default gateway, prefix length | `gateway_addr`, `subnet`, plus `dns_servers` proposed as `8.8.8.8`, a resolver that answers from anywhere so a fresh machine can reach a mirror before the site says which resolver it runs |
| hostname | the host key in the inventory, which is what `inventory_hostname` resolves to, and the `hostname` variable |
| CPU topology, and the kernel command line if it already carries an isolated set | `isolcpus`, proposed from the topology on a freshly installed machine, since the ISO has not applied any isolation yet |
| the account holding UID 1000, the one the installer created | `admin_user`, falling back to `admin` with a warning when no account holds that UID |
| block devices by path, with their claim state | candidates for `ceph_osd_disks` |
| NICs with link state and driver, PTP capability | candidates for `ptp_interface`, `team0_0`, `team0_1` |
| the image the installed quadlet names for this service | `seapath_webui_image`, pinned to the version answering when the machine boots on a tag that moves |

Discovery proposes, it never decides. Every discovered value reaches the
operator as a candidate to confirm, because a NIC that is up is not necessarily
the NIC that carries sampled values.

`seapath_webui_image` is the one variable the seed writes that describes this
service rather than the machine. It is read from
`/etc/containers/systemd/seapath-webui.container`, the unit the ISO installs and
`deploy_seapath_webui` rewrites, so the inventory names the image the node
actually boots on. The tag the ISO installs is `latest`, and the seed resolves
it to the version answering, since a variable saying `latest` names no version
and the point of writing it is that the inventory says which code a machine is
meant to run. A reference already carrying an exact tag or a digest is a
decision somebody made, and it is seeded unchanged. A machine whose unit file
could not be read pins nothing, and `GET /node/update` reports that the
inventory names no image for it.

Editing that variable and applying `seapath_setup_deploy_seapath_webui` is how
this service is replaced, which is [D23](decisions.md#d23). The seed only makes
the starting point say something: an inventory that already exists is never
rewritten, so a machine seeded before this carries no pin until somebody sets
one.

`admin_user` is read from the machine for a reason worth spelling out.
`configure_seapath_distro` asks the same question with `getent passwd 1000`
and removes that account when `admin_user` names a different one, so a seed
that guessed the name would delete the operator's own account on the first
convergence. Leaving the variable out is not an option either: the same role
evaluates it in a conditional and the prerequisites run stops on its first
task.

The same proposal is available at any time, and not only at first boot:
`GET /inventory/proposed` renders the standalone inventory this machine
describes for itself, and the editor opens it as an unsaved candidate under
"Propose a standalone inventory". Nothing is committed until the operator
saves, which is what keeps it a proposal. It is the answer for a machine
re-cabled since installation, one whose discovery failed then, and one whose
file was emptied.

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
| Admin account | `admin_user` | package manager distributions only, seeded from UID 1000 |
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

### The three variables a container is

A container in SEAPATH has no variable of its own, which is why nothing above
mentions one. It is a quadlet, and it is three ordinary entries:

```yaml
upload_extra_files_upload_files:
  - src: '../files/mosquitto.container'
    dest: '/etc/containers/systemd/mosquitto.container'
    mode: "0644"
upload_extra_files_commands_to_run_after_upload:
  - systemctl daemon-reload
# Cluster only, loaded into the CIB by configure_ha.
extra_crm_cmd_to_run: |
  primitive mosquitto systemd:mosquitto.service op monitor interval=30s
```

The Containers page reads exactly that back and writes exactly that shape. Two
consequences worth knowing while editing the file by hand:

- The scope of the first entry decides which machines get the container, and
  **Ansible replaces a variable rather than merging it**. A list on `all` and a
  list on `node1` means `node1` receives only its own. The page refuses to
  write an entry anywhere but where the affected machines already read the list
  from, and the same rule applies to a hand written edit.
- `extra_crm_cmd_to_run` is read `run_once`, so it belongs on the group whose
  members form the cluster, and a per host value is a coin toss between the
  hosts.

See [D33](decisions.md#d33).

### 4bis. The vocabulary, so the editor knows what a variable is

An inventory is a YAML file with no schema. A name typed wrong is a name the
file accepts, and the answer arrives three minutes into a convergence from a
role that read a variable nobody set. `app/inventory/vocabulary.py` is the
table that lets the service say something before then, and
`GET /inventory/vocabulary` is how a page asks for it.

Each entry carries the shape, where the variable is written, the role that
reads it, its default, an example taken from the reference inventories, whether
a form of this service writes it, and what goes wrong when it is absent or
wrong. `scope` narrows the answer to what one place accepts: `host`, `group` or
`guest`.

**The table is curated, and that is the design.** The collection was measured
rather than assumed about. `roles/*/defaults` and `roles/*/vars` hold 101 names
between them, and almost all of them are role plumbing:
`cephadm_install_registryurl`, `configure_ha_crm_command_path`. The variables a
site actually writes are in no `defaults` at all, because a role that requires
a variable does not default it. Reading them off the `{{ }}` of the task files
instead yields 418 identifiers, 124 after the obvious filtering, and that list
has `stdout_lines`, `to_datetime` and `getent_passwd` sitting beside
`ceph_osd_disks` and `cluster_ip_addr`. Offering `stdout_lines` as an inventory
variable in a box that configures substation hypervisors costs more than
offering nothing at all. This is the same judgement `references.KNOWN` records
for the path variables, and the same one the playbook catalogue records for the
playbooks.

What keeps a curated table honest is that nothing in it is trusted. Every entry
is checked against an authority the repository already holds:

| Claim | Checked against |
|---|---|
| a variable is written by a form | `model.NodeConfig` and `model.Guest` |
| a variable is written and not offered | `renderer.FIXED_HOST_VARS`, `renderer.PTP_DOMAIN_ALIASES` |
| a path variable is described | `references.KNOWN` |
| a variable a rule names is described | the `field` of every `validation` finding |
| a variable a site writes is described | the four `inventories/examples/*.yaml`, when the `seapath-ansible` checkout is beside this one |

The last one is the test that decides whether the table is worth having. A site
starts from those four files, and a variable one of them writes that the table
cannot name is a variable the service would report as unknown on an inventory
that came straight from upstream, which teaches an operator to ignore the
warning.

**Behind the curated half comes the derived tail**, the way `catalogue.resolve`
lists a playbook nobody has read. It is every variable the collection installed
on this node declares, carrying the role that declares it, the value it falls
back to and the sentence its README wrote about it, marked `reviewed: false`
and ranked after everything above. `lexicon.py` reads it, `vocabulary.py`
shapes it, and neither invents a word of it.

The tail is what keeps the curated table from being a second place to edit. A
collection that grows a variable grows it here on the next scan, on the node
that installed it, without a release of this service. That is the only
arrangement under which a curated table can be allowed to exist at all: the
four authorities move, and a site runs the collection it installed rather than
the one this was written against. `seapath_alloc_strategy` is the case that
settled it: written by a real site, read by a real role, absent from the four
reference inventories, and so absent from the table nobody had thought to add
it to.

Two rules keep the tail from costing what the curation bought:

- **a curated entry is never replaced.** A name the table already carries is
  dropped from the tail rather than merged into it. The prose above was written
  knowing what the role says, and `isolcpus` carries a caution about a machine
  that reboots into a state where the housekeeping CPUs have nothing left,
  which no README says;
- **a derived entry says who declares it and nothing more** when the collection
  says nothing more. `Declared by configure_ha.` is the whole summary of a
  variable no README documents. A sentence guessed here would be
  indistinguishable from the ones that were read off a role.

A derived entry is scoped `any`, which is the honest reading of a role default:
Ansible resolves it wherever the machine is described, on the group or on the
host entry, and nothing in a `defaults` file says which. That leaves the tail
out of a guest entry entirely, which is deliberate. What a guest may carry is
`guest.xml.j2` and the deployment roles, reviewed above, and offering
`cephadm_network` inside a VM would put two hundred names in the one place the
file is hardest to get right.

### 4ter. The assistant, and the switch that decides whether it is asked

`app/inventory/assistance.py` reads the file being typed against the vocabulary
and answers `POST /inventory/raw/assist`. It says two things:

- **a variable nothing in the installed collection reads**, with the name that
  was probably meant when one is within `difflib`'s reach at 0.88.
  `cephadm_netwrok` gets `cephadm_network`; `cluster_nxt_ip_addr` is close to
  two different addresses on two different machines, and the cutoff is set
  where a confident wrong answer stops being offered;
- **a variable written where nothing will read it**, which today means the
  guest boundary alone: `vm_disk` on a hypervisor, `isolcpus` on an entry of
  the `VMs` group.

**Remarks rather than findings.** None of them reaches `validate()`, so none of
them refuses a commit. A variable of a site's own is a legitimate name this
service has never read, and it is written exactly the way a misspelling is.

Reaching a reader is the test for a misplaced variable, rather than reaching
only readers. `vm_disk` written on `all` is untidy and it does reach the
guests, so it is left alone. Written on `hypervisors` it reaches none, and that
is the mistake worth a line. Only the guest boundary is judged: a host variable
written on a group sets it for every machine of that group, which is how the
reference cluster writes `isolcpus`.

Read off the document rather than off the model, the way `references.py` reads
it. The model keeps what it does not know in `extra` and loses which group a
variable was written on, and that is what a remark has to name: a misspelling
on `all` is one mistake, and reporting it once per machine reads like three.

#### Completion

`app/ui/static/complete.js` offers a name where one is being typed, drawn from
`GET /inventory/vocabulary`, fetched once per page load and filtered in the
browser. Four things decide whether it helps or annoys.

**It offers where a variable name goes.** A key sits at the start of a line,
after the indentation and after an optional `- `, and anything past the colon
is a value. Offering `ceph_osd_disks` inside an IP address is a list that has
to be dismissed rather than one that has to be read.

**It offers what may be written there.** The scope of the caret is worked out
from the shape of the file above it, by walking back through the lines that are
less indented than the last one taken: `<group>: hosts: <host>:` is a machine,
or a guest when the group is one of the three that hold guests, and
`<group>: vars:` is the group. A YAML parser would be exact and would also have
to parse a file that is half typed; the shape is what a half typed file still
has. This is the same boundary the assistant reports after the fact, applied
before the mistake.

**It says what the variable is.** The role that reads it, and the caution when
there is one, since a list of names an operator could have guessed is a list
they stop opening.

**The reviewed entries come first, always.** Ranking is the position of what
was typed inside the name, so `ceph` offers `ceph_osd_disks` before
`deploy_cephfs`, and a reviewed entry then wins the tie. That is what keeps the
eight rows an operator sees from filling with role plumbing named after the
same prefix, `cephadm_install_registryurl` and the rest. A derived entry says
so in the row, in the words the deployment page uses for a playbook read off
the collection: `not reviewed`.

Accepting writes `name: `, colon and space included, through
`document.execCommand` for the reason `yamledit.js` uses it: one `Ctrl`+`Z`
after an accepted name would otherwise throw away everything typed before it.

While the list is open it owns `Enter`, `Tab`, the arrows and `Escape`, and its
listener is registered before the one `yamledit.js` attaches so those keys never
reach the ones that indent a block. `complete.js` is loaded before
`inventory.js` for the same reason.

The caret's position on screen comes from a mirror: a div carrying the
textarea's own metrics, filled with the text up to the caret, whose last span
is where the caret is. A textarea offers no other way to ask.

#### The switch

The Inventory page carries a switch in the head of the editor card, remembered
per browser in `localStorage` under `seapath-assistant`, the way the theme and
the automatic reading are. One switch covers the whole assistant, the remarks
and the completion alike: off means the page never calls the endpoint and never
fetches the vocabulary, rather than an answer arriving and being hidden. It appears only while the inventory
is the file open, since the vocabulary says nothing about a quadlet or a syslog
template, and the reading is debounced 800 ms so that a held down key does not
send one round trip per character.

#### The collection is what answers

`app/inventory/lexicon.py` scans the collection this node runs and returns two
sets. The first shipped version of the assistant answered from the curated
table alone, and the first real site inventory it met reported `apt_repo`,
`nics_affinity`, `admin_ssh_keys`, `interfaces_to_wait_for` and seven more as
read by no role. Every one of them is read by a role. The four reference
inventories exercise some forty five variables between them, so passing against
them proved much less than it looked.

- `mentioned` is every identifier in every file of the tree, **templates
  included**: `interfaces_to_wait_for` appears only in a `.j2`, and so does
  `ptp_vlanid`. Reading it loosely is deliberate. A word in a comment marking a
  name as known costs silence about one variable; a real variable missing from
  the set costs a warning about working configuration.
- `declared` is the keys of `defaults/main.yml` and `vars/main.yml`, the names
  the role READMEs document, and the curated table, and it is the only pool a
  suggestion is drawn from. A wrong answer given confidently is worse than no
  answer.
- `declarations` is `declared` with what the collection says about each name
  beside it: the role that declares it, the value it falls back to, the type
  its README writes and the sentence in its Comments column. It is what the
  derived tail of the vocabulary is built from.

**The variable tables of the role READMEs are read, and only those.** A role
that requires a variable does not default it, so the names a site actually
writes are in no `defaults` file: `cephadm_network`, `isolcpus`, `cpumachines`,
`ceph_osd_disks` are documented in a markdown table and nowhere else machine
readable. The tables are regular across the collection, and the one rule for
taking one is its first column header: `Variable`. That header is what keeps
the tables listing metrics, allocation strategies, paths and thresholds out of
a list of variable names, since all of them are tables of the same shape in the
same files. On the installed collection this yields 202 declarations, of which
some ninety carry a sentence written by whoever wrote the role.

The prose of a README stays out of `mentioned`. Feeding it every English word
of the documentation would answer "something here knows this name" for half the
typos an operator can make, and take the warning that catches a misspelling
with it. The variable names in those tables do go in: a documented variable is
one the collection knows, whether or not a task file spells it out.

**An inventory reads its own variables, and that counts.** A site that writes

```yaml
custom_network:
  eno1:
    Network:
      - Address: "{{ sec_ip_address | default(omit) }}"
```

and sets `sec_ip_address` on the machines that have a second address has a
variable no role will ever mention and that is read on every run. So every
`{{ }}` and `{% %}` in the file is scanned too, off the raw text rather than
off the loaded structure, which catches a template inside a key, a list entry
or a block scalar alike. The filter names come with it, `default` and `omit`
among them, which costs silence about a variable somebody named `default` and
saves keeping a list of Jinja's builtins in step with Jinja.

The two halves of a mismatched pair are both reported, whichever side carries
the mistake. A definition the templates never ask for is a value that reaches
nothing; a reference nothing defines is a machine configured without the
address it was given.

Three rules keep the claim honest:

- **`ansible_*` is never reported.** Those are read by Ansible rather than by a
  role, so no collection mentions them and no table here would ever list them
  all.
- **No collection, no claim.** `roles_read` is false when nothing could be
  read, the unknown half is not attempted, and the page says so rather than
  reporting a clean file. The placement half still works, since it reads the
  curated table alone.
- **A tree holding under 500 identifiers is read as no collection.** A partial
  install or a clone whose submodules never came down would otherwise report
  almost every variable of a real inventory, which is the same failure from the
  other side.

The answer is therefore right for *this* node: a role that exists only on
`seapathalloc` reads variables a laptop's install has never heard of, and the
node that runs that branch is the one being asked.

#### What keeps it usable

The four reference inventories must draw **no remark at all**. A file that came
straight from upstream producing a warning is what teaches an operator to
ignore every warning, and the ones that matter go with it. That test is what
caught the first version: `seapath-vm-deployement.yaml` writes `ansible_host`
and `ansible_user` on its guest entries, for the play that waits for the guest
over SSH once it is created, and calling those misplaced was a remark about a
file this project ships. `Scope.CONNECTION` exists for that: Ansible's own
connection variables describe how it reaches a host rather than what the host
is, so they are at home on a machine and on a guest alike. `ip_addr`,
`hostname` and `apply_network_config` stay a machine's, because the roles that
read them play machines.

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
`deploy_vms_cluster.yaml` and `deploy_vms_standalone.yaml`. It is the `VMs`
group, read as guests rather than as machines, which is the subsection
["Machines and guests"](#machines-and-guests) above.
