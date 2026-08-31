<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Packaging and deployment

## 1. Image

Multi stage `Dockerfile`, same shape as `insatomcat-exporter`. Published as
`docker.io/insatomcat/seapath-webui`, built and pushed by `buildpush.sh`.

Contents:

- Python 3.11, FastAPI, uvicorn, Jinja2, `ansible-runner`;
- `ansible-core` pinned to `~=2.16.0`, which `prepare.sh` enforces in
  `seapath-ansible` and which this image must match;
- the `seapath_ansible` collection with its galaxy dependencies and its
  submodules, installed at build time by running the upstream `prepare.sh`;
- an OpenSSH client;
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
role is patched. Two things about that build are worth knowing, and both were
found by running it rather than by reading it:

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
next to the inventory commit. That pair is what makes a deployment
reproducible, and the catalogue refuses to offer an entry the shipped
collection does not contain.

The collection version is part of the image identity. It determines which
playbooks exist and what they do, so it is recorded at build time, reported by
`/api/v1/node`, and shown in the run view next to the inventory commit. A run
is identified by the pair "inventory commit, collection version", and that pair
is what makes a deployment reproducible.

The image must therefore be released in step with SEAPATH. An image carrying a
collection newer than the machines is how a playbook meets a host it was not
written for.

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
| Hardware and identity | `/sys`, `/dev/disk`, `/etc/hostname`, `/etc/os-release`, `/etc/corosync` | What the inventory form is prefilled from |
| Runtime plane, M2 | libvirt socket, `/etc/ceph` | Starting, stopping and migrating VMs |
| Authentication | `/etc` at `/run/host/etc` | PAM against the machine's own accounts |

The file itself is [`seapath-webui.container`](../seapath-webui.container) at the
root of the repository, kept there rather than copied here so the two cannot
drift apart.

No `--privileged`, no host podman socket, no `--pid=host`, and no route to the
host's systemd, its bus or its journal. If an implementation finds itself
needing one of those, that is the signal that it is about to configure the host
directly, or to reimplement monitoring, and both are things this design
refuses.

### What the reading needs, and the reading that was removed

The reading answers one question: **what is this machine?** Its hardware, its
identity, its cluster membership. That is what the inventory form is prefilled
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
- **`/etc/corosync`, the directory, not `corosync.conf`.** That file only
  appears once `cluster_setup_ha.yaml` has run, and its presence is what tells a
  cluster member from a standalone machine. Bind mounting a source that does not
  exist keeps the container from starting, so naming the file rather than the
  directory would have broken every standalone node, which is exactly the
  machine M1 targets.
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
podman pull docker.io/insatomcat/seapath-webui:latest
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
# Once. It survives every later git pull.
ansible-galaxy collection install \
    git+https://github.com/seapath/ansible.git -p /opt/ansible/collections

# Then, from the checkout, on the housekeeping CPUs.
taskset -c 0-1 .venv/bin/python -m app
```

Without that first command **every catalogue entry is unavailable and the Apply
section has no buttons**, because the playbooks live in the collection and the
image is what usually installs it. That is correct behaviour and it read as a
broken page the first time it happened, so the service now says it in the
journal at startup and the System page says it once above the list.

Three things a source checkout does not reproduce, and one of them can do harm:

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
| `seapath_webui_image` | no | `docker.io/insatomcat/seapath-webui:latest` | Image reference |
| `seapath_webui_bind_address` | no | `{{ ip_addr }}` | Listen address, administration network |
| `seapath_webui_port` | no | `8006` | Listen port |
| `seapath_webui_admin_group` | no | `seapath-admin` | Unix group granting the admin role |
| `seapath_webui_cpu_affinity` | no | computed from `isolcpus` | Housekeeping CPUs |
| `seapath_webui_ansible_user` | no | `{{ ansible_user }}` | The account the trust targets, must match the inventory |
| `seapath_webui_ansible_user_home` | no | looked up with `getent` | Source of the `.ssh` mount, never hardcoded to `/home/ansible` |

Tasks: create the state directories, initialise the inventory repository if
absent, create the three Unix groups, template the quadlet, `daemon-reload`,
enable and start. Strictly idempotent, with a handler restarting only the
service and only when the quadlet or the configuration changed.

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
