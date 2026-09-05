<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Packaging and deployment

## 1. Image

Multi stage `Dockerfile`, same shape as `insatomcat-exporter`. Published as
`docker.io/insatomcat/seapath-webui`, built and pushed by `buildpush.sh` on a
laptop and by `.github/workflows/image.yml` on every push to `main`. The
workflow runs `ruff`, `black` and `pytest` first, builds, smoke tests the image
the way `buildpush.sh` does, then pushes two tags: the commit's short sha, and
`latest`. It needs two repository secrets, `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN`, the second an access token from Docker Hub rather than an
account password.

Contents:

- Python 3.11, FastAPI, uvicorn, Jinja2, `ansible-runner`;
- `ansible-core` pinned to `~=2.16.0`, which `prepare.sh` enforces in
  `seapath-ansible` and which this image must match;
- the `seapath_ansible` collection with its galaxy dependencies and its
  submodules, installed at build time by running the upstream `prepare.sh`;
- an OpenSSH client;
- `rsync`, which `ansible.posix.synchronize` runs on the controller as well as
  on the target. Four roles push files with it, `configure_physical_machine`
  among them, so a commissioning run needs it;
- `libvirt0` and the `vm_manager` package, for the runtime plane;
- the `ceph` client libraries that `vm_manager` needs in cluster mode;
- `iproute2`, for the one reading that is not a file under `/proc` or `/sys`:
  sysfs carries no IPv4 address. No `systemd` and no `chrony`, which this image
  carried while it read unit states, the journal and the clock offset from the
  host. See section 2.1.

Each layer arrives with the milestone that uses it, so that the image never
carries the dependency tree, or the CVEs, of something no code calls yet. M0
ships the service and the reading tools. The Ansible layer, meaning
`ansible-core`, the collection, `git` and the OpenSSH client, arrives at M1 with
the run adapter. The runtime layer, `libvirt0`, `vm_manager` and the Ceph client
libraries, arrives at M2.

`git` is not incidental: the inventory repository is the configuration audit
trail, and the service shells out to `git` for every commit, diff and revert.

### Building the collection into the image

A dedicated stage clones `seapath-ansible` and runs its own `prepare.sh`. No
role is patched.

The branch is `seapathalloc`, carried by the `SEAPATH_ANSIBLE_REF` build
argument. `seapath_setup_prometheus_exporters` and
`seapath_setup_deploy_seapath_alloc` exist there and are absent from `main`, so
an image built from `main` offers neither. Every other entry in the catalogue is
present on both, and a site pinned elsewhere overrides the argument.

Two things about that build are worth knowing, and both were found by running it
rather than by reading it:

- `prepare.sh` installs the local collection **before** it updates the git
  submodules, so the copy it installs carries an empty
  `roles/deploy_cukinia/files/cukinia`. The image installs the collection a
  second time, afterwards.
- `build_ignore` in `galaxy.yml` is matched against whole relative paths, so
  `"*.tar.gz"` strips `roles/deploy_cockpit_plugins/files/*.tar.gz`. Those two
  archives are what `deploy_cockpit_plugins` unarchives, and
  `seapath_setup_main.yaml` imports that role on every distribution except
  Yocto. Without them the commissioning run fails on any machine that has
  Cockpit, which is every machine installed from the SEAPATH ISO. The image
  restores the two files after installing the collection. See
  [playbooks.md](playbooks.md).

The collection version is stamped into the image with `--build-arg
COLLECTION_VERSION`, reported by `GET /api/v1/node`, and recorded on every run
next to the inventory commit. `galaxy.yml` says `2.0.0` on every branch, so the
label carries the branch and the upstream commit instead: both `buildpush.sh`
and the workflow resolve the branch head with `git ls-remote` and stamp
`seapathalloc@<commit>`. That label, with the inventory commit, is what makes a
deployment reproducible, and the catalogue refuses to offer an entry the
shipped collection does not contain.

**That resolved commit is also passed into the build, and the build checks
it.** The clone command is otherwise identical from one build to the next, so
its layer is a cache hit and a build run just after a push to `seapathalloc`
ships the collection of the previous one, silently and with a label naming the
new commit. Passing the commit moves the cache key exactly when the branch
moves. The clone then compares what it got against what was asked for, so a
branch that moved in between fails the build rather than producing an image
whose label is a lie.

The collection version is part of the image identity. It determines which
playbooks exist and what they do, so it is recorded at build time, reported by
`/api/v1/node`, and shown in the run view next to the inventory commit. A run
is identified by the pair "inventory commit, collection version", and that pair
is what makes a deployment reproducible.

The image must therefore be released in step with SEAPATH. An image carrying a
collection newer than the machines is how a playbook meets a host it was not
written for.

That coupling is what makes a corrected playbook wait for an image build, and
[D23](decisions.md#d23) is where the way out is written down: the collection
moves to a volume fed by the artefact store, and the image is updated by a
playbook this service runs against its own machine.

### The collection the node runs, which is not always the image's

The service reads two roots and picks one whole:

| Root | What it is |
|---|---|
| `/var/lib/seapath-webui/collections` | The site's own, installed on the node. Empty on a machine nobody has updated |
| `/opt/ansible/collections` | What the image built, and the fallback. `SEAPATH_WEBUI_COLLECTIONS_PATH` names it, which is what a source checkout is pointed at |

The first wins as soon as it holds an installed collection, meaning a
`MANIFEST.json` under `ansible_collections/seapath/ansible`. The directory
existing is not enough: the quadlet creates the state volume, so an empty
`collections/` in it is the ordinary shape of every node, and choosing on it
would leave those nodes with no playbook at all.

The two are never stacked. A run records one fingerprint, and a tree assembled
from two installs is one no CI has ever executed. The root is resolved once, at
start, for the same reason: a collection dropped in the volume while a
convergence is going must not change what that run is halfway through
executing.

The choice lands in the journal at every boot, with the fingerprint, whenever
it is the site's collection that is running. That is code which arrived outside
an image release, and the boot that starts applying it is where an operator
looks for it.

The volume is the one the quadlet already mounts, so this asks for no new mount
and no new host surface.

### Installing one

`PUT /api/v1/collection`, and the panel at the bottom of the System page. The
file is the tarball `ansible-galaxy collection build` writes, built from the
SEAPATH `ansible` repository:

```bash
# On a machine that has the repository, once per release.
git clone -b seapathalloc https://github.com/seapath/ansible && cd ansible
./prepare.sh
ansible-galaxy collection build --output-path /tmp
# Then upload /tmp/seapath-ansible-2.0.0.tar.gz through the System page.
```

What the node does with it, in order: read the manifest and refuse anything
that is not `seapath.ansible`, take the run lock, copy the image's tree into a
staging directory beside the live one, unpack the archive over it with
`ansible-galaxy collection install --force --no-deps`, and rename the staging
directory over the live one. A failure at any step leaves the node running
exactly what it ran before, which matters: a node whose collection is half
replaced cannot converge, and cannot be repaired by converging.

Three consequences worth knowing:

- **The install is refused while a run is going.** A run stages a mirror of
  symlinks into the collection tree, so replacing that tree mid convergence
  breaks it on a live machine. It takes the same lock a second run would.
- **The seed comes from the image every time.** `community.general` and
  `ansible.posix` are what the roles call, and they come from the image's tree,
  so the installed one is self contained. Installing twice never accumulates:
  what the previous archive left behind and this one does not carry is gone.
- **The trace is a commit with no diff.** The desired state did not move and
  the code that applies it did. `git log` in the inventory repository carries
  who installed which fingerprint, and every run records the fingerprint it
  ran.

`DELETE /api/v1/collection`, or the button beside the upload, removes the tree
and the node runs the image's collection again. That is the undo, and it is
what makes installing on a live node safe to attempt.

## 2. Quadlet

`seapath-webui.container`, installed to `/etc/containers/systemd/`.

Note how much smaller the host surface is than in a design where the service
configures the machine itself. The configuration plane goes out over SSH, even
to the local node, so no host **configuration** is written from here. Exactly
two host paths are mounted writable: the service's own state, and the
`authorized_keys` of the `ansible` account, which is the trust material. Every
other mount serves the runtime plane or a read only view.

Thirteen bind mounts, in four groups:

| Group | Mounts | Why |
|---|---|---|
| Service state and trust | `/etc/seapath/webui`, `/etc/seapath/inventory`, `/var/lib/seapath-webui`, `/home/ansible/.ssh`, `/etc/ssh` | The configuration plane, which is SSH and nothing else |
| Hardware and identity | `/sys`, `/dev/disk`, `/etc/hostname`, `/etc/os-release`, `/etc/corosync` | What the seed inventory and the node view are written from |
| Runtime plane, M2 | libvirt socket, `/etc/ceph` | Starting, stopping and migrating VMs |
| Authentication | `/etc` at `/run/host/etc` | PAM against the machine's own accounts |

The file itself is [`seapath-webui.container`](../seapath-webui.container) at the
root of the repository, kept there rather than copied here so the two cannot
drift apart.

### The image reference

`Image=` names an exact tag, and it follows this service's `__version__`.
`latest` would leave a machine unable to say which code is answering on it,
which is the half of "which code, against which desired state" that the run
record cannot supply on its own. A test in `tests/test_packaging.py` holds the
tag and the version together, and `buildpush.sh` reads the version from the
source rather than repeating it.

Releasing is therefore one gesture: **bump `__version__`**, then build. The
`image` workflow publishes three tags and only one of them is a promise:

| Tag | What it names |
|---|---|
| `<sha7>` | Exactly this build, always published |
| `latest` | What a first deployment pulls, always moved |
| `<version>` | What the quadlet pins, published **once** |

A push to main carrying no bump leaves the version tag where it is and says so
in a notice. That is the point: a tag that moves says nothing about which code
answers on a machine, and the whole update path rests on it saying something.
`seapath_webui_image` in the inventory names that tag, `deploy_seapath_webui`
pulls it, and `GET /api/v1/node/update` compares it with the version answering.

### The listen socket

`Network=host` puts the service on every network the hypervisor is cabled to,
including the ones carrying sampled values and the storage traffic, so the
socket is bound to a single address: `SEAPATH_WEBUI_BIND_ADDRESS`, which the
Ansible role substitutes with `ip_addr` from the inventory.

That address is not known on a machine that has never converged, and a fresh
ISO is exactly the case where the UI has to be reachable. The quadlet ships the
value `auto`, which the entry point resolves to the IPv4 address of the
interface carrying the default route, the same address the inventory form
proposes as `ip_addr`. It ends up in the certificate too, since it is the name
the operator types. A machine with no default route has no administration
network to resolve, and refusing to serve there would leave no way in at all,
so that case falls back to the wildcard and says so on the console.

No `--privileged`, no host podman socket, no `--pid=host`, and no route to the
host's systemd, its bus or its journal. If an implementation finds itself
needing one of those, that is the signal that it is about to configure the host
directly, or to reimplement monitoring, and both are things this design
refuses.

### What the reading needs, and the reading that was removed

The reading answers one question: **what is this machine?** Its hardware, its
identity, its cluster membership. That is what the seed inventory is written
from at first boot, and it is the question no exporter answers.

It used to answer a second one, what the machine is currently *doing*, and that
is where every expensive mount in this quadlet came from. It is gone, and the
reasoning is section 2.1 below, because the temptation to add it back will
recur and it deserves a straight answer.

What is left:

- **`/sys`, read only.** CPU topology and the isolated set, the netdevs, the
  block devices. `/proc` is deliberately **not** mounted: `uptime`, `cpuinfo`,
  `cmdline` and `stat` are not namespaced, and with the host network namespace
  neither is `/proc/net`, so the container's own `/proc` already reports the
  host's values.
- **`/dev/disk`.** The stable `by-path` names are symlinks created by udev, and
  `ceph_osd_disks` is written in that form. Only the symlink directory is
  mounted, not the device tree. A metric about a disk is no substitute: what
  the inventory needs is the disk's stable name.
- **`/etc/hostname` and `/etc/os-release`.** The container has its own UTS
  namespace, so without the first the node view would show a container id where
  the machine's name belongs, and the certificate would be issued to one.
- **`/etc/corosync`, the directory.** What is read inside it is `authkey`,
  which is what tells a cluster member from a standalone machine. `dpkg -S
  /etc/corosync/corosync.conf` answers `corosync`: the Debian package ships a
  default configuration, so that file is on every machine that installs
  corosync and says nothing about membership. It was the first signal here, and
  it made the badge say "cluster" on a standalone node. The authkey is written
  by `corosync-keygen` in `configure_ha` and distributed to the members by the
  same role. Its presence is read and its content never is, which is the rule
  about the authkey this service holds to everywhere.
- **`/etc/ssh`, read only,** added at M1. It carries the machine's public SSH
  host keys, and reading them off the filesystem is how the first SSH
  connection is verified without either prompting, which hangs a run forever,
  or `StrictHostKeyChecking=no`, which is a real man in the middle window on
  the administration network. No network is involved, so there is nothing to
  intercept. See [cluster-join.md](cluster-join.md).
- **`/etc`, read only, at `/run/host/etc`,** for PAM, instead of a bind mount of
  `/etc/passwd`, `/etc/group` and `/etc/shadow`. `usermod` and `passwd` write a
  new file and rename it over the old one, so the container went on reading the
  files as they were when it started: adding an operator to `seapath-admin` did
  nothing until the service was restarted, and so did changing a password. The
  image symlinks the three files into that mount, and a symlink is resolved at
  every open. If the mount is missing the symlinks dangle, and the service says
  so in the journal at startup rather than silently refusing every password.

Some of those paths do not exist on a freshly installed machine:
`/etc/seapath/webui`, `/etc/seapath/inventory`, `/var/lib/seapath-webui`,
`/etc/ceph` and `/etc/corosync`. A missing source is a container that does not
start, and a node that does not answer its browser is the one failure this whole
project exists to prevent. So the quadlet creates them itself, in
`ExecStartPre`, rather than leaving them to the Ansible role or to the ISO.
Dropping the file on a machine and starting the unit is meant to be enough, and
it was not: the first deployment on real hardware needed three directories
created by hand before the container would start.

Each of those paths is inert when empty, which is what makes creating them
harmless, and that constraint is why the list may never grow carelessly:
`/var/log/journal` is the counterexample, because creating that one is how
journald switches to a persistent journal.

### 2.1 The monitoring that was here, and why it left

This service once served unit states, a journal tail, the chrony offset and the
active tuned profile. Removing them took eight bind mounts and about seven
hundred lines out of the project, and the argument is short: **every SEAPATH
node runs `prometheus-node-exporter`.** Live state is already collected,
already stored with history, and already alerted on. A node local UI holding a
second source of truth for it earns nothing, and cannot even be the better one:
a browser tab nobody has open is not monitoring.

The mounts it cost were `/run/systemd/system`, `/run/dbus`, `/var/log`,
`/run/log`, `/etc/machine-id`, `/etc/tuned` and `/run/tuned`, plus
`/var/lib/pacemaker`, which nothing ever read. The image carried `systemd` and
`chrony` for it.

The part worth keeping in writing is what reading a unit state from a container
actually takes, because it is the reason this section exists and the reason
nobody should propose it again lightly.

`systemctl` running as **root** does not go to the bus. It connects to
`/run/systemd/private`, deliberately, so that it still works during early boot
before dbus exists. That socket comes with `/run/systemd`, so the container had
it and the connection succeeded. What failed is the check immediately after:
systemd reads the peer credentials with `SO_PEERCRED`, the kernel cannot express
the host's PID 1 in a container that has its own PID namespace, so it reports
`pid 0`, and systemd rejects that as unusable with `ENODATA`. On screen:
`Failed to connect to system scope bus via local transport: No data available`.

Hiding the private socket only changed the errno to `ENOENT`, because
`bus_connect_system_systemd()` lost its fallback to the bus in systemd v257,
which is what the image shipped. Read as root, that route has no working end.
What made it work was running the reading under an unprivileged uid:
`bus_connect_transport_systemd()` branches on `geteuid()`, and any uid other
than 0 goes straight to `/run/dbus/system_bus_socket`, where the peer is dbus
rather than PID 1.

Two deployments went into that, and it worked. It was still the wrong thing to
build: the cost was not the debugging, it was that four of the mounts above and
thirty five lines of comment about `SO_PEERCRED` became part of what anyone
deploying this service has to read. The alternative on the table at the time
was `--pid=host`, and the design was right to refuse it. What the design missed
is that the weaker version of the same idea was not needed either.

The test `test_the_container_is_given_no_route_to_the_live_state` in
`tests/test_packaging.py` fails if one of those mounts comes back, so that
adding one is a decision taken on purpose rather than a line that slips in.

`/home/ansible/.ssh` is not created either, and that is deliberate too. The
account comes from the ISO, and a service that invents a home directory for a
user nobody created has invented a second problem. If that mount is missing, the
machine was not installed from the SEAPATH ISO.

### Bringing a machine up by hand

The ISO does this at first boot, and the role at section 4 does it on a machine
that is already running. Both come later than the first real deployments, so
here is the same thing by hand, and it is short on purpose:

```bash
# The exact tag the quadlet pins, which is this service's own version. The
# quadlet never says `latest`: a machine has to be able to say which code is
# answering on it.
podman pull docker.io/insatomcat/seapath-webui:0.3.10
install -m 0644 seapath-webui.container /etc/containers/systemd/
systemctl daemon-reload
systemctl start seapath-webui

# The URL and the certificate fingerprint, printed once at startup. The
# fingerprint is what an operator verifies in the browser, and what the trust
# exchange between nodes pins.
journalctl --unit seapath-webui --boot
```

The state directories are created by the unit itself, so nothing has to exist
beforehand.

Then the accounts. Authentication is PAM against the machine's own accounts, and
the role comes from Unix group membership, so an operator needs an account on
the machine and a place in one of three groups:

```bash
groupadd --force seapath-admin
groupadd --force seapath-operator
groupadd --force seapath-viewer
usermod --append --groups seapath-admin alice
```

`root` is an administrator without any of this, which is what makes a freshly
installed machine usable before a single playbook has run. An account that
authenticates but is in none of the groups is refused, and told which groups
exist: that is the intended answer, not a failure.

The group takes effect at the next login. It used to take effect at the next
restart of the service, because the quadlet bind mounted `/etc/group` and
`usermod` replaces that file rather than editing it in place. See the mount
notes above.

To start from an inventory that already exists rather than from what this
machine discovers about itself, see
[inventory.md](inventory.md#adopting-an-inventory-that-already-exists), and do
it before the first start.

### Running from a source checkout on a node

`git pull` and a restart is seconds, and a build and a push is minutes, so
iterating on the service from a checkout on the machine is the reasonable thing
to do. It needs the collection installed once, and it is worth knowing what it
does not prove.

```bash
# Once. It survives every later git pull. -b takes the branch a site needs;
# seapathalloc is the one the image is built from, and the run view then
# reports that branch's fingerprint rather than the version every branch
# declares.
. .venv/bin/activate                 # prepare.sh reads `ansible` and `python3`
                                     # off PATH, and Debian 13 ships 2.19
git clone -b seapathalloc https://github.com/seapath/ansible /src/seapath-ansible
cd /src/seapath-ansible && ./prepare.sh
mkdir -p /opt/ansible && cp -a collections /opt/ansible/collections

# Then, from the checkout, on the housekeeping CPUs.
taskset -c 0-1 .venv/bin/python -m app
```

`prepare.sh` runs four preflight checks under `set -e` and creates
`collections/` only after all four pass, so a failed check leaves no directory
at all and the reason is one line of output that scrolls past easily:

| Check | What fails it |
|---|---|
| `ansible` on PATH | The venv is not activated and ansible is only in it |
| `core 2.16` | The venv is not activated: Debian 13 ships ansible-core 2.19 |
| `ansible-galaxy` on PATH | Same as the first |
| `import netaddr` | It reads `python3` off PATH, so it is the venv's when activated. `requirements.txt` carries netaddr for this and because the network roles import it at run time |

**`prepare.sh` rather than `ansible-galaxy collection install git+...`.** The
short form installs `seapath.ansible` and none of what it depends on, and the
roles call `community.general` and `ansible.posix` modules. The playbooks then
parse until the first task that uses one, and Ansible refuses the whole run
with `couldn't resolve module/action 'community.general.modprobe'` before
reaching any machine. `prepare.sh` installs `ansible-requirements.yaml` first,
fetches the git submodules, then installs the collection, which is the sequence
the Dockerfile follows for the same reason.

Without that first command **every catalogue entry is unavailable and nothing
on the System page can be launched**, because the playbooks live in the
collection and the image is what usually installs it. That is correct behaviour
and it read as a broken page the first time it happened, so the service now
says it in the journal at startup and the System page says it once, at the top,
above the commissioning entry and the picker.

Three things a source checkout does not reproduce. The CPU row is the one
that can perturb a running machine; the other two only mislead:

| | Image | Source checkout |
|---|---|---|
| Collection | Installed at build, version baked into `SEAPATH_WEBUI_COLLECTION_VERSION` | Whatever was installed by hand, version reported as `unknown`, and the version is what a run records |
| PAM | Ships `/etc/pam.d/seapath-webui` | Falls back to `/etc/pam.d/other`, so an authentication result here says nothing about the deployed one |
| CPUs | `CPUAffinity=0-1` and `Nice=5` from the quadlet | Free to run anywhere, **including the isolated CPUs**, which on a machine running real time guests is a latency source. `taskset` is the substitute |

So: iterate from the checkout, and confirm from the image before believing a
result. The image is what ships.

## 3. Surviving the runs it launches

A playbook can reboot the machine running it, and
`seapath_setup_hardening.yaml` does exactly that, on every host. The network
roles can also cut the connection. So a run will die mid flight, and the design
answer is not to prevent it but to make it harmless:

- artefacts are written to `/var/lib/seapath-webui/runs/<id>/` as the run
  progresses, never buffered in memory, so the trace survives;
- a run that ends without a final status is marked `interrupted`, not `failed`,
  and the view offers to relaunch it;
- relaunching is safe because the playbooks are idempotent, which is the whole
  point of converging rather than mutating;
- playbooks that reboot are flagged in `GET /playbooks` and the UI warns before
  launching one from the machine it will reboot, suggesting the operator drive
  it from another node instead.

Running the Ansible process in a sibling container, so that it survives a
restart of the service itself, is a possible hardening. It costs access to the
podman socket, which is root on the host, and buys little given that the
interruptions that matter are reboots. Left as D9, deliberately unimplemented.

## 4. Ansible role

`seapath-ansible/roles/deploy_seapath_webui`, following the `deploy_*`
conventions.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `seapath_webui_enabled` | no | `true` | Deploy and enable |
| `seapath_webui_image` | no | `docker.io/insatomcat/seapath-webui:latest` | Image reference. The default is the tag the ISO installs and preloads, so a first apply on a node with no route to a registry finds the image already there. Set it to an exact tag to pin a version: this is the variable [D23](decisions.md#d23) turns into the update lever, and editing it in the inventory then applying is how the service replaces itself |
| `seapath_webui_bind_address` | no | `{{ ip_addr }}` | Listen address, administration network. `auto` resolves it from the default route, which is what a fresh ISO boots with |
| `seapath_webui_port` | no | `8006` | Listen port |
| `seapath_webui_admin_group` | no | `seapath-admin` | Unix group granting the admin role |
| `seapath_webui_cpu_affinity` | no | computed from `isolcpus` | Housekeeping CPUs |
| `seapath_webui_ansible_user` | no | `{{ ansible_user }}` | The account the trust targets, must match the inventory |
| `seapath_webui_ansible_user_home` | no | looked up with `getent` | Source of the `.ssh` mount, never hardcoded to `/home/ansible` |

Tasks: create the state directories, initialise the inventory repository if
absent, create the three Unix groups, template the quadlet, `daemon-reload`,
enable and start. Strictly idempotent, with a handler restarting only the
service and only when the quadlet or the configuration changed.

### The one constraint the role has to honour: the restart is detached

The role deploys the tool that ran it, and on the machine serving the page it
is replacing the process that is recording the run. A `systemd` restart handler
would kill `ansible-runner` mid task: the SSH session dies, the play never
reaches its end, and the run record is whatever was on disk at that instant.

So the pull and the restart are handed to a job that outlives the container:

```yaml
- name: Restart seapath-webui out of band
  ansible.builtin.command:
    argv:
      - systemd-run
      - --unit=seapath-webui-update
      - --no-block
      - /bin/sh
      - -c
      - podman pull {{ seapath_webui_image }} && systemctl restart seapath-webui
  changed_when: true
```

`--no-block` returns as soon as systemd has queued the job, so the task
succeeds, the play finishes, and the container goes away a moment later with
its trace already written. What an operator sees is a page that stops
answering for a few seconds and comes back on the new version.

The run still ends without a final status on that machine, because the process
that would have written it is gone. The catalogue entry says so before the
confirmation, and the record says so afterwards rather than calling it a
failure. `GET /api/v1/node/update` is how the result is checked: it reports the
version answering now next to the one the inventory names.

Two things the role must **not** do. It must not restart the service from a
handler in the ordinary way, for the reason above. And it must not be given
`podman-auto-update`: the update is an operator's decision, taken in front of a
confirmation that names the machines, and a timer decides nothing. See
[D23](decisions.md#d23).

The role has a pleasing property: it deploys the tool that runs the role. A
site can bootstrap from a fourth machine, then let the cluster take over, or the
other way round.

## 5. ISO integration

The ISO ships the image and the quadlet so a fresh machine serves the UI on
first boot with no network fetch. First boot must:

1. generate the TLS certificate and the session secret;
2. provision the **self trust**: an SSH key pair for the service, installed in
   the `ansible` account of this same machine, without which the service cannot
   converge even a standalone node, since the inventory sets
   `ansible_connection: ssh` for every host including the local one;
3. run hardware discovery and write the seed inventory;
4. print the URL and the certificate fingerprint on the console, because the
   whole trust exchange depends on the operator being able to verify it;
5. ensure one account can log in, per D6.

The ISO already provides what step 2 needs, verified in
`seapath-build_debian_iso`: the `ansible` account with sudo, and
`/home/ansible/.ssh/authorized_keys` seeded with the site key at build time. The
service appends its own line to that file and never rewrites it, because the
site key is how a conventional Ansible control machine reaches the node.

## 6. Migration from vmmgrapi

`roles/vmmgrapi` exposes four `vm_manager` endpoints through gunicorn and nginx.
At M5 its README gains a deprecation notice, the ISO stops enabling it, and
`enable_vmmgr_http_api` stays default false so nothing breaks. The ports differ,
so both can run side by side for at least one release. The role is not deleted:
someone has automation against those endpoints.

## 7. Behind a reverse proxy

The service answers on its own HTTPS port and needs no proxy. A site that
already fronts its machines with one can nonetheless mount it under a path,
because nothing this service serves names a path from the root of the origin:
every link, script, `fetch`, `EventSource`, websocket and redirect is relative.

That property rests on an invariant. Every page sits exactly one segment deep
(`/`, `/inventory`, `/system`, `/realtime`, `/runs`, `/login`), so all of them
resolve a relative URL against the same base directory. A page nested deeper
would break the others, silently and only behind a proxy. `test_ui_pages.py`
asserts the property on the served bytes, pages and scripts alike, so a
regression fails on a laptop rather than at a substation.

The prefix therefore lives entirely in the proxy, and the service is never told
what it is:

```nginx
# The entry point without its trailing slash. Without this redirect a browser
# arriving at /seapath resolves every relative URL one directory too high, and
# the page loads nothing.
location = /seapath { return 308 /seapath/; }

location /seapath/ {
    # The trailing slash strips the prefix, so the service sees the paths it
    # was written against and needs no root_path.
    proxy_pass https://127.0.0.1:8006/;

    # The certificate is the one the service generated for this node, signed by
    # nobody. An operator verifies its fingerprint in the browser; a proxy on
    # the same machine has nothing to verify it against.
    proxy_ssl_verify off;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # The console websocket, and the run event stream. A convergence sends
    # nothing for minutes at a time, and a buffered proxy would hold the
    # progress an operator is watching.
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

`$connection_upgrade` comes from the usual map, in the `http` block:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Two limits. The session and CSRF cookies are set with `path=/`, so under a
prefix they are offered to the proxy's other applications as well; their names
carry a per node suffix, so they collide with nothing, but a site that cares
scopes them at the proxy. And `/api/v1/docs` is the one page that stays
root anchored, because FastAPI builds it from `openapi_url` without a
`root_path`; the OpenAPI document itself, and every endpoint, are unaffected.
