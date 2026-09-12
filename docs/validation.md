<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Manual validation on a real machine

Point 4 of the definition of done in [AGENTS.md](../AGENTS.md). The test suite
runs against fakes, which is what makes it fast and portable, and which is
exactly why it cannot answer the questions below. Each milestone adds its
checklist here, with the result and the machine it was run on.

## M0

Nothing on this list changes the machine. If any step does, that is a bug of
the highest severity in this project.

### Prerequisites

Install the image and the quadlet, and start it:

```bash
sudo cp seapath-webui.container /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start seapath-webui
```

That is the whole prerequisite. It used to be preceded by a `mkdir -p` of six
directories, because podman refuses to start a container whose bind mount source
is missing, and the first real deployment needed exactly that. The unit now
creates them itself. See [deployment.md](deployment.md#2-quadlet).

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | The container starts on a **standalone** node, where `/etc/corosync/authkey`, and possibly `/etc/ceph`, do not exist, and the badge reads `standalone` | A missing bind mount source is a podman behaviour, and the Debian package puts a `corosync.conf` on a machine that is in no cluster | |
| 2 | `journalctl -u seapath-webui` shows the URL and the certificate fingerprint | The console banner is the whole trust story of the first connection | |
| 3 | The browser reaches `https://<ip_addr>:8006/` and the certificate fingerprint matches the one on the console | | |
| 3b | The certificate common name is the **node's** name, not a container id | The container's UTS namespace, which `Network=host` does not share | |
| 4 | `root` signs in with the password set by the installer | D6, and no group exists yet on a machine that never converged | |
| 5 | An account in `seapath-viewer` signs in and sees the node view; an account in no SEAPATH group is refused, naming the groups | PAM and `getgrnam` against the host's real files | |
| 6 | The node view shows the **machine's** hostname, not a container id | `/etc/hostname` mount and the UTS namespace | |
| 7 | The kernel release, distribution and uptime match `uname -r`, `/etc/os-release` and `uptime` | | |
| 8 | The isolated set matches `cat /sys/devices/system/cpu/isolated` | | |
| 9 | Interface addresses match `ip addr`, and the default route interface matches `ip route` | `ip -j` output and the host network namespace | |
| 10 | The disk list shows every disk with the same `by-path` name as `ls -l /dev/disk/by-path`, the boot disk marked in use and any spare marked available | The OSD selector at M4 depends on exactly this | |
| 11 | `podman exec seapath-webui systemctl list-units` fails, and `podman exec seapath-webui journalctl -n1` fails | The container is meant to have no route to the host's systemd. This one is expected to fail and passes when it does | |
| 12 | `systemctl show seapath-webui -p CPUAffinity` reports the housekeeping CPUs only | Real time safety | |
| 13 | `cyclictest` results on the isolated CPUs are unchanged with the service running and stopped | The service must be invisible to a real time guest | |
| 14 | After a `podman stop` and start, the certificate fingerprint is unchanged and sessions are still valid | The material must be generated once, and the session secret persisted | |
| 15 | Nothing outside `/etc/seapath/webui` was written. Compare `find /etc /var/lib -newer <marker> -not -path '/etc/seapath/*'` before and after a full browse of the UI | **The point of M0.** No writing anywhere | |

This list used to carry three more checks: unit states against `systemctl
status`, the journal button, and the time card's offset. They were the ones
expected to need a quadlet adjustment, and check 10 duly did, twice. The
readings are gone, along with the mounts they needed, because every node runs
`prometheus-node-exporter` and that is where live state belongs. Check 11 is
what is left of them, and it passes when the command fails. See
[deployment.md](deployment.md#21-the-monitoring-that-was-here-and-why-it-left).

Checks 8, 9 and 10 remain the ones a laptop cannot rehearse: they read the real
`/sys`, the real `ip -j` output and the real udev symlinks. If one of them
fails, the reading must degrade with a message naming what is missing, never
fall back to a plausible looking value.

### Result

Not yet run.

## M1

M1 is the first milestone that changes a machine, so the checklist is mostly
about the two things a laptop cannot rehearse: SSH to the local machine, and a
playbook that reboots the host running it.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | After the first start, `/home/ansible/.ssh/authorized_keys` still holds the ISO's site key, with one line appended | The suite proves the editing; only a real ISO proves the file it starts from | |
| 2 | `ssh -i /etc/seapath/webui/ssh/id_ed25519_self ansible@<ip_addr> true` succeeds from inside the container, with no prompt | The whole self trust: the key, the `from=` restriction, and the `known_hosts` read from `/etc/ssh` | |
| 3 | The seed inventory describes this machine correctly: address, interface, prefix, gateway | Discovery against a real `ip -j addr` and a real default route | |
| 4 | Editing the inventory in the page and saving produces a commit whose author is the operator, visible in `git -C /etc/seapath/inventory log` | | |
| 5 | Exporting the inventory, then running `seapath_setup_main.yaml` from a conventional Ansible control machine, reports **no change** | **The acceptance criterion that matters.** If it fails, something configured a machine behind Ansible's back | |
| 6 | A preview run (`check: true`) of `seapath_setup_main.yaml` completes and changes nothing | Check mode against real roles | |
| 7 | A real `seapath_setup_main.yaml` with "converge without rebooting" succeeds, and the node view keeps saying the machine has not rebooted | | |
| 8 | A real `seapath_setup_main.yaml` **with** the reboot ends as `interrupted`, not `failed`, and the run view offers to relaunch | The case the whole interruption design exists for, and the one no fake can produce | |
| 9 | Relaunching after that reboot succeeds and reports mostly unchanged tasks | Idempotence is the recovery story | |
| 10 | The artefacts under `/var/lib/seapath-webui/runs/<id>/` survive the reboot, event stream included | Written as the run progresses, never buffered | |
| 11 | After the reboot, the service marks the interrupted run closed and the run lock is free | A lock nobody releases is a node that can never converge again | |
| 12 | Cockpit still works after the run, meaning `deploy_cockpit_plugins` found its archives | The `build_ignore` problem: without the image's restore step this task fails and takes the run with it | |
| 13 | `GET /playbooks` marks as unavailable any entry the shipped collection does not carry, naming the collection version | Depends on what the image was built from | Passed on elabo1 on 2026-08-31, against the image built from `seapathalloc`: the thirteen entries come back available, `seapath_setup_prometheus_exporters` and `seapath_setup_deploy_seapath_alloc` included, and those two are what an image built from `main` reports unavailable |
| 14 | The administration address changed in the page, then applied, leaves the self trust working after the reboot | The `from=` repair at startup | |
| 15 | `cyclictest` on the isolated CPUs is unchanged with a run in progress | A convergence must not disturb a running guest | |
| 16 | On a node whose repository holds only the seed, a save from the page commits the file byte for byte as it was typed, comments included | The page must leave a freshly installed machine alone | Pending |
| 17 | The site's own inventory, imported from the browser, shows every machine with its group variables resolved, no validation finding, and `this_host` naming this machine | The read only version of this check passed on elabo1 on 2026-08-31. Re-run against the importer | Pending |
| 18 | After 17, changing one variable in the page produces a commit whose diff is that one line, with every comment and every group variable of the site file still in place | The claim the page makes, against a file no fixture can fully stand in for | Pending |
| 19 | The site key uploaded through the page reports the fingerprint `ssh-keygen -lf` prints for it, and `/etc/seapath/webui/ssh/id_site` is `0600` | | Pending |
| 20 | Scanning the inventory's machines reports fingerprints matching `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` read on each machine | The scan is over the network, and comparing it against the machine is the whole point | Pending |
| 21 | After accepting them, a check mode run of `seapath_setup_main.yaml` reaches all three machines and reports no unreachable host | **The one that says the interim path works.** Everything before it can pass with a cluster nobody can converge | Pending |
| 22 | Removing the site key makes the same run fail on the two other machines, and only on them | A revocation that does not revoke is worse than no button | Pending |
| 23 | The console opens from the node view, the prompt is the `ansible` account of this machine, and `hostname` in it answers the machine's name | The whole point: a pseudo terminal, a real sshd, and the key the self trust provisioned. The suite only ever spawns a fake. This is where `restrict` without `pty` shows up, as "PTY allocation request failed on channel 0" | Failed on the first attempt for exactly that reason, fixed by `pty` on the self relation. To re-run |
| 24 | Resizing the browser window reflows the shell, meaning `stty size` in the console reports the new geometry | `TIOCSWINSZ` on the master and the `SIGWINCH` that follows it, which only a real `ssh` acts on | Pending |
| 25 | `sudo -n true` succeeds in the console, and the panel had already said it would | The consequence the site has to accept before enabling this, said out loud rather than discovered | Pending |
| 26 | A run launched while a console is open is unaffected, and the console survives the run | The run holds a multiplexed connection open; the console must not join it or break it | Pending |
| 27 | Leaving the console untouched for the idle timeout closes it, saying why, and the Reconnect button opens a new one | A timeout nobody sees firing is a timeout nobody trusts | Pending |
| 28 | With `SEAPATH_WEBUI_CONSOLE_ENABLED=0`, the button is gone and the websocket refuses with `console_disabled` | A site that turns the shell off must find it off, endpoint included | Pending |

Check 5 is the one that decides whether the milestone is real. Everything else
can pass while the product claim is false.

Checks 16 to 18 are the adoption rule of [D14](decisions.md#d14), and 17 is
worth running on the site inventory rather than on a copy of the fixture: the
fixture was written from one real file, and the next real file will have a
shape neither of them has.

The read only version of these checks ran on elabo1 on 2026-08-31 and passed:
31 divergences named, led by `hostname` reverting to the host key on all three
machines, and the repository byte for byte what it was. That version is gone,
replaced by an editor that makes the same file editable, so the checks above are
the ones that now matter and they are unrun. What the earlier run established
stands: the shapes in the site file are the shapes in the fixture.

### First contact with real hardware

The checklist above has not been filled in yet, because the first attempt to run
it turned up seven things that had to be fixed before the answers would mean
anything. All seven are packaging and interface faults rather than product ones,
and none of them is visible from a laptop, which is the point of this document.

| Found | Cause | Fix |
|---|---|---|
| The quadlet needed `/etc/seapath/webui`, `/etc/seapath/inventory` and `/var/lib/seapath-webui` created by hand | A missing bind mount source is a container that does not start, and the directories were left to a role that does not exist yet | The unit creates them in `ExecStartPre` |
| Nothing said how to start from an inventory that already exists | Only the discovery path was documented | [inventory.md](inventory.md#adopting-an-inventory-that-already-exists) |
| Adding an operator to a SEAPATH group needed the service restarted | `usermod` renames a new `/etc/group` over the old one, and the quadlet bind mounted the file, pinning the inode | The host's `/etc` is mounted read only at `/run/host/etc` and the image symlinks the three account files into it |
| Every unit read "unknown", with `Failed to connect to system scope bus` on screen at every sign in | `systemctl` as root uses `/run/systemd/private` and nothing else, and no container can use that socket: its peer is PID 1, whose credentials do not survive a PID namespace. Since v257 it does not fall back to the bus either | The reading ran under an unprivileged uid, which is the branch that uses the bus, plus `/run/dbus` and `/run/systemd/system` mounted. This was check 10, expected to need a quadlet adjustment, and it turned out not to be a mount problem at all. **Since removed entirely**: the reading duplicated `prometheus-node-exporter`, so the right fix was not to make it work but to delete it, mounts included |
| The banner went on naming faults that had been fixed, across service restarts | The node view accumulated warnings in a set that lived as long as the page, and the page refreshes itself without ever reloading | Rebuilt every poll, so a repaired condition disappears on its own |
| A permanent caveat about how disk claim state is derived sat in the warning banner | It was a warning on every reading, whether or not anything went wrong | Moved into the disks card, next to the table it qualifies |
| Tables spilled out of their cards, and clicking Configuration or Runs opened a confirmation dialog that could not be dismissed | The cards sit in flexible grid tracks, so nothing widens one to fit a `by-path` name; and the modal is `display: grid`, which beats the `hidden` attribute | The tables of machine values take the whole page width, scroll inside their own card when that is still not enough, and `[hidden]` is enforced |

The last two are the ones worth remembering: a warning nobody can act on, and a
dialog that appears unasked, both teach an operator to stop reading what the UI
says. On a substation hypervisor that is a safety property, not a cosmetic one.

### Result

Not yet run.

## Real time

Added after M1, and separate from it because the checks below are about one
page and one playbook. See D24 in [decisions.md](decisions.md).

The conformance half changes nothing and can be read on any machine. The
measurement half loads every machine the inventory declares, at real time
priority, for as long as the duration says, so it wants a machine that is not
carrying production traffic the first time it is run.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | On a converged hypervisor, the tuned check reads `seapath-rt-host` and marks it a conformance pass | `/etc/tuned/active_profile` reached through `/run/host/etc`, on a machine `configure_hypervisor` actually ran on | |
| 2 | On a machine with no `isolcpus` in the inventory, the same check is advice and reports no profile, without a red badge | The role gates the whole tuned block on that variable, so this is what an unconfigured machine legitimately looks like | |
| 3 | Editing `isolcpus`, converging, and **not** rebooting leaves the isolation check reporting a mismatch that names the reboot | The kernel reads `isolcpus` at boot. This is the finding the page exists for and no fake can produce it | |
| 4 | After the reboot, the same check passes with the observed and declared columns equal | | |
| 5 | The preemption check reads `PREEMPT_RT` on a SEAPATH image | `/proc/version` of the real kernel, which is the one place the build flag appears | |
| 6 | On a machine whose inventory carries `nics_affinity`, the NIC interrupt check reads the process bus interface on the declared CPU and marks it a conformance pass | A real NIC with real MSI vectors, and `configure_nic_irq_affinity` having actually run. The role applies the placement on link up, so bringing the interface down and up again must leave the check passing | |
| 6b | Declaring an interface on a CPU the kernel is not isolating is reported as such, naming the CPU, rather than as an absence | The reading describes only what reached an isolated CPU, so this is the one cause it cannot show and the check derives. A wrong CPU here is silent everywhere else | |
| 7 | Hugepages are reported per NUMA node on a two socket machine | The fixture has one node; a starved second node is the case that costs a guest its start | |
| 8 | Launching a measurement asks for confirmation naming the machines and saying what it runs, rather than what it writes | | |
| 9 | The run completes and `results/cyclictest_<host>.txt` exists under the run directory for **every** machine of the inventory | The role's `fetch` with `flat: true`, over the real SSH mesh | |
| 10 | The histogram is charted per thread, and the per thread maximum matches the `# Max Latencies` footer of the fetched file | The parser is tested; only a real run proves the file it parses is the one the role produces | |
| 11 | A measurement pinned with `cyclictest_affinity` to the isolated set labels each series with the right CPU | The mapping is read off the command line the role's script built | |
| 12 | While the measurement runs, this service stays responsive and its container stays on the housekeeping CPUs | The whole point of measuring on the target instead of here. `systemd-cgls` and `taskset -pc` on the container answer it | |
| 13 | Nothing on any machine changed: `seapath_setup_main.yaml` from a conventional control machine still reports no change afterwards | **The acceptance criterion.** A measurement that configured something would be the worst kind of bug here | |
| 14 | `hwlatdetect` completes and `results/hwlatdetect_<host>.txt` exists for every machine | The role's `fetch`, over the real SSH mesh | |
| 15 | On a SEAPATH kernel the result reports samples rather than a missing tracer | The `hwlat` tracer detection, against a real `available_tracers` | |
| 16 | On a machine whose kernel has no `hwlat` tracer, the page says the machine **could not be asked**, visibly apart from a machine that was asked and found nothing | The failure this whole card exists to avoid: an unmeasurable machine reading as clean firmware. Needs a non-RT kernel to reproduce | |
| 17 | That machine does not fail the run, and the other machines still return their results | `any_errors_fatal` is set, so one kernel refusing must not take down a run that has already loaded the others | |
| 18 | An interruption the detector reports is absent from the `cyclictest` figures taken at the same time | The whole claim of the card: an SMI is invisible to the kernel and therefore to cyclictest. Needs a machine with real SMIs | |
| 19 | While `hwlatdetect` runs, the guests on the machine feel it | Honesty about the cost. The detector holds interrupts off for the sampling width of every window, and the confirmation says so before the run | |
| 20 | On a three node cluster, every node has a column and every column has ten checks | The whole of D27. Each node answers from its own exporter, and only real machines have a real `/etc/tuned` and a real `/proc/irq` | |
| 21 | A node whose collector predates `seapath_rt_*` shows no rows and one sentence naming `deploy_seapath_alloc`, beside nodes that answered | A site pinned to an older collection is the ordinary state during an upgrade, and it must read as a node to upgrade rather than as ten failures | |
| 22 | Editing `isolcpus` for **another** node, converging it and not rebooting it shows the mismatch on that node's column, from the machine the browser is on | The finding this reversal exists for. Before it, the mismatch was invisible from anywhere but that machine | |
| 23 | Fetching `localhost:9100/metrics` on a converged hypervisor returns a `seapath_rt_` block carrying the tuned profile, the command line, the sysctls and the interrupt count | The exporter side, on a real machine, before believing anything the page says about it | |
| 24 | The tuning columns and the pool grid come from one request per node: `tcpdump` or the exporter's own access log shows one GET per node per refresh | Two panels of the same reading must not double what a page refresh costs a hypervisor | |
| 25 | On a cluster of four machines with 48 threads each, no value in the conformance view is cut, and the pool view holds every machine without scrolling | The layout of D28. A laptop cannot produce four real machines of that width, which is where the old three panel page was truncating everything | |
| 26 | Each of the four tabs carries its own status and figure before it is opened, and switching between them asks no node for anything | The bar is the page's summary, and a view is a show and a hide. `tcpdump` on the exporter port answers the second half | |
| 27 | The ACPI row reads the same on every column, and the same from every node's page | D36. Podman masks `/sys/firmware`, so the reading a container makes of itself differed from the one its own exporter published, and only a real container reproduces that | |
| 28 | Stopping `node-exporter` on the node serving the page leaves that column answered from its own files, while the other columns fall back to their reason | The other half of D36. The fallback needs a real `/proc` and a real `/sys` under a real mask, which no fake provides | |
| 29 | With the exporter stopped, a machine whose tuned profile comes from the distribution rather than from `configure_hypervisor` still reads as installed | The `/usr/lib/tuned` mount, on the fallback path. Only a real container, whose own `/usr` carries no tuned, distinguishes a mounted profile directory from a missing one | |

### Result

Not yet run. Checks 8 to 19 need `test_run_cyclictest.yaml` and
`test_run_hwlatdetect.yaml` in the collection the image ships. Both are on the
`seapathalloc` branch the Dockerfile builds from, so an image built after that
branch moved has them; a site pinned to an older collection sees both entries
report themselves unavailable through `playbook_present`.

Check 16 needs a machine whose kernel lacks `CONFIG_HWLAT_TRACER`, which a
SEAPATH image does not produce. Any ordinary Debian kernel does, and the case
matters enough to be worth borrowing one for.

Checks 20 to 24 need a collection carrying `conformance.py` in
`deploy_seapath_alloc`, and the role run on every node so the timer writes the
block. Check 21 is the easiest to stage deliberately: stop
`seapath-alloc-export.timer` on one node and delete its `.prom` file, which is
what a node running an older collector looks like from here.

## Inventory replication

The **Replicate** button: this node's repository pushed to the machines the
inventory declares, over the connection a run makes. See
[D32](decisions.md#d32).

The suite pushes between real git repositories, so what a fake cannot rehearse
is everything on the far side: the sudo rule the ISO grants, whether the host
carries git at all, and the file the peer's own service reads afterwards.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | `sudo -n /bin/sh -c 'exec git receive-pack "$0"' /etc/seapath/inventory` in the console of a peer starts and waits for input | The ISO's sudo rule is `NOPASSWD:EXEC:SETENV: /bin/sh`, and this is the exact line the push runs through it. The subcommand rather than the `git-receive-pack` binary: node3 answered "not found" for the dashed form with git installed | Pending |
| 2 | With no site key uploaded and no host key accepted, every machine reports unreachable with a sentence naming the reason, and the page still renders | Replication needs what a run needs, and a node that has neither must say so rather than hang | Pending |
| 3 | After the trust exists, the page lists every other machine of the inventory with the commit it holds, and the guests of `VMs` are absent from it | A guest read as a machine is the mistake this list must not make | Pending |
| 4 | Replicating from node1 leaves `git -C /etc/seapath/inventory log` on node2 and node3 carrying the same commits, authors included | The audit trail is what travels, and only a real push proves the history arrives with it | Pending |
| 5 | The file on the peer changed too, meaning `receive.denyCurrentBranch=updateInstead` did its work and the peer's own page shows the new inventory without a restart | A branch that moved without the worktree is a copy whose service reads the old file | Pending |
| 6 | Editing the inventory on node2, then replicating from node1, is refused with node2 named, and node2 keeps its commit | The refusal is the whole safety of pushing a branch rather than copying a folder | Pending |
| 7 | Replicating from node2 afterwards is accepted, and node1 is then behind until it replicates in turn | The one direction the act has, seen from both ends | Pending |
| 8 | With node3 powered off, node2 is updated and node3 is one line naming the timeout, in under a minute | A partial success is the ordinary outcome, and the page must not wait on a dead machine | Pending |
| 9 | A machine whose repository was never created reports it by name, and nothing is created there | The service does not create a repository on a machine that runs no service | Pending |
| 9b | A node with no `git` on its host, a Yocto observer typically, is replicated to through its own `seapath-webui` container, and `git -C /etc/seapath/inventory log` inside that container carries the commits | The fallback exists for a machine only a real deployment has. node3 answered "exec: git: not found" with the service running | Pending |
| 9c | With that node's `seapath-webui` stopped, the same replication names it and says to start the service, rather than carrying the shell's own words | A message naming no machine and no fix is what this replaced | Pending |
| 10 | Nothing else changed on any peer: `seapath_setup_main.yaml` from a conventional control machine still reports no change after a replication | **The acceptance criterion.** The push writes the inventory repository and nothing else | Pending |
| 11 | On a node whose repository predates this version, `git -C /etc/seapath/inventory symbolic-ref HEAD` says `refs/heads/main` after a restart, and `git log` still has its commits | The repair only matters on a repository nobody made here. Failed on ccv-admin, which sat on `master` and took a push into a branch it did not serve | Pending |
| 12 | A machine that still serves another branch is reported as such rather than as updated, and its files are checked to be unchanged | The failure that shipped once: the page said up to date while three inventories were empty | Pending |
| 13 | A file placed by hand on a peer, where the inventory carries one of the same name, stops an ordinary push with the file named and is still there afterwards | Git refuses even when the two are byte for byte identical | Pending |
| 14 | With **Force** ticked, that same peer is overwritten: the file matches this node's, its branch is this node's commit, and the journal carries one `inventory.replicated.forced` line naming the machines | The case ccv-admin hit, where an uncommitted `inventories/` stopped every replication. It runs through hooks the peer installed at its own start | Pending |
| 15 | Forcing towards a node still running an older image reports that it cannot be forced, and that machine is unchanged | Doing the ordinary push under a button that says Force is the failure this avoids | Pending |

### Result

Not yet run. It needs the trust of [cluster-join.md](cluster-join.md), which
in the interim means the site key uploaded on the node that replicates.

## Cluster

The Cluster page: Pacemaker membership, the resources and where they run, and
Ceph. See D29 in [decisions.md](decisions.md).

Every check below is a reading, and none of them changes a machine. That is
also what makes the last one the important one: a page that monitors a cluster
has to leave the cluster exactly as it found it.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | On a converged cluster, `curl localhost:9664/metrics` returns `ha_cluster_pacemaker_nodes` on every member | The exporter side, on a real machine, before believing anything the page says about it. `configure_ha` is what deploys it | |
| 2 | Every member of the cluster has a row, with the coordinator tagged, and the page names the node its reading came from | Only a real cluster elects a DC | |
| 3 | Putting one node in standby with `crm node standby` shows that node as `standby` and still online, and the Membership tab turns amber rather than red | A deliberate operator action must never read as a fault | |
| 4 | Stopping `corosync` on one member of three leaves the cluster quorate, marks that member unclean or offline, and turns the tab red | The distinction the page is for. Two members losing each other is the case an operator opens it in | |
| 5 | Stopping two of three shows the surviving node reporting no quorum, from the browser pointed at it | The reading is then the survivor's rather than the coordinator's, and `from_dc` must say so on screen | |
| 6 | With the cluster partitioned, the two halves disagree and the page reports whose answer it is showing rather than merging them | The whole reason the coordinator's exposition is the one believed | |
| 7 | A VM that has failed on its node shows its failure count and its migration threshold, and the Resources tab names it | A real `crm_mon` failure. `crm resource fail` stages it | |
| 8 | A resource Pacemaker will not start anywhere reports `INFINITY` rather than a broken page | Pacemaker's INFINITY reaches the exporter as `+Inf`, which JSON cannot carry. The reader is tested; only a real cluster proves the exporter writes what the test assumes | |
| 9 | A cloned resource running on three members has three rows, one per node | | |
| 10 | Clearing the failure with `crm resource cleanup` on the machine is reflected on the next page load | The fix is on the machine, which is the boundary this page keeps | |
| 11 | On a cluster with Ceph, the Storage tab reports the same health, capacity and daemon counts as `ceph -s` on the machine, figure for figure | The one comparison that says the reading is right. Binary units on both sides | |
| 12 | Stopping one OSD shows it down and out with its host and device class, and lists `OSD_DOWN` among the messages Ceph itself raises | `ceph_health_detail` is published from Pacific on. A cluster older than that answers the health alone, which is check 13 | |
| 13 | On a Ceph release too old to publish `ceph_health_detail`, the health is still reported and the page claims nothing about why | Degrading rather than blanking is the behaviour, and only an old cluster proves it | |
| 14 | Failing the active manager over with `ceph mgr fail` leaves the Storage tab answering, from the new manager, with no configuration change | The reason every machine is asked instead of one | |
| 15 | On a cluster with local storage and no Ceph, the Storage tab is hollow and says so, and the other two tabs are unaffected | A supported SEAPATH configuration must never render as a fault | |
| 16 | With one member powered off, the Membership tab lists it as unreachable with the reason, and the members that answered are still drawn | A cluster half built, or half up, is the ordinary state | |
| 17 | The page costs at most one GET per machine per exporter per load: the exporters' access logs, or `tcpdump`, answer it | Three readings on one page must not multiply what opening it costs a hypervisor | |
| 18 | A viewer can open the page and read all three tabs | The role an operator on call is likely to have | |
| 19 | Nothing changed on any machine from *reading*: with no button pressed, `crm configure show` and `ceph config dump` are identical before and after a session on this page, and `seapath_setup_main.yaml` from a conventional control machine still reports no change | **The acceptance criterion.** A monitoring page that configured something would be the worst kind of bug here. The acts the page offers are in the Placement section below, each one a run in the history | |
| 20 | On a resource with a failure count, **Refresh** launches a run whose one task is `crm resource refresh <resource>`, and `crm_mon` on the machine reports the count cleared afterwards | The button exists for a state only a real cluster reaches. `crm resource fail` stages it | Pending |
| 21 | Refreshing a resource that is running leaves it running, on the same node, and the guests on that machine are undisturbed | The whole reason this is offered rather than a stop and a start | Pending |
| 22 | After 20, `crm configure show` is identical before and after: the refresh cleared history and changed no configuration | What makes this act belong on a page that configures nothing | Pending |
| 23 | A viewer sees neither Refresh button, and both endpoints as a viewer are refused | They reach a live cluster, so they are an operator's act | Pending |
| 24 | **Refresh every resource** runs `crm resource refresh` with no resource named, and `crm_mon` reports every fail count cleared | The wide act, whose cost only a real cluster shows | Pending |
| 25 | During 24, the guests keep running and `cyclictest` on the isolated CPUs is unchanged | A burst of monitor operations on a live substation is the thing to measure before trusting this button | Pending |

### Result

Not yet run. Checks 1 to 10 need a real Pacemaker cluster with
`ha_cluster_exporter` deployed, which is `configure_ha` on a collection that
carries it. Checks 11 to 15 need Ceph, and check 13 needs a cluster older than
Pacific, which is worth staging only if a site runs one.

Check 6 wants a partition rather than a stopped service: `iptables -j DROP` on
the cluster interface of one member reproduces it and `crm_mon` on both halves
says whether it took.

## Placement

Moving a resource, giving its placement back, and putting a node in standby.
See D34 in [decisions.md](decisions.md).

These are the only acts in this service that write to a live CIB on purpose, so
what has to be proved on real hardware is twofold: that each one does exactly
the one thing it says, and that the inventory still describes the cluster
afterwards. Every check runs on a converged three node cluster with guests on
it, and the guests are what an operator watches while it happens.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | On a guest deployed with `preferred_host`, `crm configure show` names a `cli-prefer-<guest>` constraint on that node | The premise of the whole design, read off a real deployment rather than off the `vm_manager` source | Pending |
| 2 | **Move** on that guest launches a run whose one task is `crm resource move <guest> <node>`, and afterwards `crm configure show` names the same constraint id on the new node | The constraint is replaced rather than doubled, which only the real crmsh proves | Pending |
| 3 | With `live_migration` on the guest's image, check 2 leaves the guest running throughout: `virsh list` on both machines, and a ping to the guest, show a migration rather than a stop and a start | What the confirmation promises. A guest under a substation function must not be stopped by a button that said it would not | Pending |
| 4 | Without `live_migration`, check 2 stops the guest on one machine and starts it on the other, and the confirmation had said so | The other half of the same promise | Pending |
| 5 | **Return** on that guest runs `crm resource clear` then `crm resource move` with the node the inventory declares, and `crm configure show` ends with the declared constraint back | The trap the second task exists for: a bare clear would drop a declared placement | Pending |
| 6 | On a guest the inventory declares no `preferred_host` for, **Return** runs the clear alone and `crm configure show` names no `cli-prefer` for it afterwards | The other branch of the same act | Pending |
| 7 | On a guest deployed with `pinned_host`, no Move or Return button is drawn and both endpoints refuse with `resource_pinned`, naming the `pin-` constraint | A pinned guest runs there or nowhere, and a clear would not remove that constraint anyway | Pending |
| 8 | **Standby** on a member launches `crm node standby`, `crm_mon` shows the node standby and online, and every resource it held is placed elsewhere | The act with no per resource residue, and the one an operator uses before a reboot | Pending |
| 9 | After check 8, `crm configure show` names no new constraint: a standby leaves nothing behind on any resource | What makes standby the better gesture than a move for showing a cluster place its guests | Pending |
| 10 | During check 8, quorum is unchanged and the node still votes: `corosync-quorumtool` before and after | The confirmation says so, and a wrong claim here is one an operator acts on | Pending |
| 11 | **Bring online** ends the standby, and Pacemaker places what it chooses to place, which may be nothing | A resource with nothing holding it elsewhere staying put is the cluster behaving correctly | Pending |
| 12 | The Move button is absent for a member in standby as a destination, and the endpoint refuses one with `node_in_standby` | Pacemaker places nothing there, so the constraint would hold the guest where it already is | Pending |
| 13 | On a cloned resource, no Move is drawn and the endpoint refuses with `resource_is_cloned` | Only a real cluster carries a clone with the exporter's `clone` label filled in | Pending |
| 14 | The VMs page marks a guest whose `cli-prefer` constraint names a node its entry does not declare, and leaves an as declared guest unmarked | The only way an override is visible at all: the CIB cannot say who asked for a constraint | Pending |
| 15 | A viewer sees no Move, Return or Standby button, and all four endpoints as a viewer are refused | They write to a live CIB, so they are an operator's act | Pending |
| 16 | **The acceptance criterion.** After a session of moves and standbys, `disable` then `enable` on the guest through the VMs page restores the declared placement, and `deploy_vms_cluster` from a conventional Ansible control machine reports no change | Placement written here must never outlive a redeployment, or the inventory has stopped describing the cluster | Pending |
| 17 | Every one of these appears in the run history with the machine it ran on, the command line, and the user who launched it | The audit trail is the product claim, and a CIB write with no run behind it would break it | Pending |

### Result

Not yet run. Every check needs a converged cluster with guests, and checks 3
and 4 need two guests differing only in `live_migration` so the two costs can be
watched side by side.

Check 16 is the one to run last and the one to run twice: once after a move
that was returned, and once after a move that was left in place, because the
second is the state a demonstration actually leaves behind.

## Containers

The Containers page: the quadlets the inventory uploads, the systemd unit each
machine made of them, and the Pacemaker resource where the cluster holds one.
See D33 in [decisions.md](decisions.md).

The reading is new only in what it asks for: the units come out of the
`node_exporter` exposition the CPU pool is already read from, so the first
checks are about the collector actually being there and publishing what the
parser expects. The writing checks are the ones that matter most, because a
declaration written at the wrong scope produces a clean commit, a green run and
a machine quietly missing files.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | On a converged machine, `curl localhost:9100/metrics \| grep node_systemd_unit_state` returns one series per state for the site's quadlet units | The collector has to be enabled and its unit filter has to include those units. Everything on the page rests on this | Pending |
| 2 | A quadlet the inventory uploads shows `active` on the machines where `systemctl is-active <unit>` says active, and the same start time | The parser against a real collector, unit by unit | Pending |
| 3 | A machine whose `node_exporter` runs without the systemd collector is named as such, and the rest of the page still renders | Only a real exporter proves the difference between "no collector" and "no answer" | Pending |
| 4 | A container declared in the inventory and never converged shows "no unit yet" rather than stopped | The machine has never received the file, and calling it stopped would call it deployed | Pending |
| 5 | On a cluster, a quadlet handed to Pacemaker shows one row, its resource, and the node the cluster placed it on, matching `crm_mon` | The resource is matched on the unit rather than on the resource id | Pending |
| 6 | Declaring a container on the group the site's `upload_extra_files_upload_files` already sits on appends one entry, and `git diff` on the repository touches those lines alone | The write against a real hand written inventory, which is the case `fidelity` exists for | Pending |
| 7 | After 6, `ansible-inventory --list` on the exported repository gives every machine the full list, the site's entries included | **The check that catches the whole class of failure this page could cause.** Ansible replaces a variable rather than merging it | Pending |
| 8 | Declaring on a scope the machines do not read that variable from is refused, and the message names where they do read it | The refusal is the feature; a real inventory is where the two places differ | Pending |
| 9 | Running the prerequisites playbook after a declaration puts the file on the machines, `systemctl daemon-reload` writes the unit, and the page reports it | The end to end path, and the only proof the entry this service wrote is the entry the role wanted | Pending |
| 10 | With `pacemaker` asked for, `cluster_setup_ha` loads the primitive, `crm configure show` holds it, and the container starts on one member | `extra_crm_cmd_to_run` is loaded with `crm config load update`, and only a real CIB says whether the generated line is accepted | Pending |
| 11 | Start and stop on a Pacemaker container write the target role, and `crm_mon` shows the container stopped and staying stopped | The runtime act against a real cluster | Pending |
| 12 | Start and stop on a systemd container act on the machine named and on no other, checked with `systemctl is-active` on all three | The reason the machine is part of the act | Pending |
| 13 | A quadlet carrying `[Install]` under Pacemaker is flagged, and the machine confirms the conflict: the container comes back at the next boot with the resource stopped | The finding is about what systemd does at boot, which no fake reproduces | Pending |
| 14 | A viewer sees no start or stop button, and both endpoints as a viewer are refused | They reach a live machine, so they are an operator's act | Pending |
| 15 | Nothing else changed: `seapath_setup_main.yaml` from a conventional control machine reports no change on the machines after a session on this page, and the exported inventory produces the same containers from that machine | **The acceptance criterion.** A container declared here has to be a container that control machine would deploy | Pending |

### Result

Not yet run. Checks 1 to 4 need a machine running the collection that ships
`deploy_prometheus_exporters` with the systemd collector on; 5, 10 and 11 need
a Pacemaker cluster; 6 to 8 need a site inventory that keeps its upload list on
a group, which the reference file `tests/golden/adopted-cluster.yaml` came from.

## The version pin on a hand written inventory

The suite pins against fixtures this repository wrote. What it cannot see is a
file a site wrote over a year, where `seapath_webui_image` sits on the group
that holds the hypervisors and every machine repeats it, which is the shape the
first real cluster had. See [D23](decisions.md#d23).

The planner was run offline against a copy of that file, and it produced
exactly five changed lines: the group's tag moved, and the four host lines that
repeated it removed, with no divergence in what any machine resolves. The
checklist below is what remains, on the machines themselves.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | Pinning a version on a cluster whose inventory carries the variable on a group produces one commit, whose diff moves the group's tag and removes the host lines | The file is the site's, with its comments, its blank lines and its variables this service never models | Pending |
| 2 | `ansible-inventory --list` on the committed file gives every machine the new reference, and gives the guests what it gave them before | The resolver agrees with Ansible on the fixtures, and this is the file that matters | Pending |
| 3 | The apply that follows replaces the service on all four machines, and each one then reports the pinned version as the one answering | The run reaches the machines over SSH, and the last of them is the one recording the run | Pending |
| 4 | An inventory this service seeded, with the variable on the machine and no group carrying it, is still pinned on the machine | The seed path, which is what a freshly installed node has | Pending |

### Result

Not yet run.

## Response headers

The tests assert what the header says. A browser is the only thing that acts on
it, so what the suite cannot see is a page that renders and then does nothing
because the policy blocked the script that fills it. See
[D35](decisions.md#d35).

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | Each of the eight pages loads with an empty browser console, no CSP violation reported, and no flash of the wrong palette | Only a browser enforces the policy, and the theme script is one of the two the nonce exists for | Pending |
| 2 | The console opens, the terminal renders its grid, and it takes keystrokes | xterm builds a style element at run time, which is what `'unsafe-inline'` on `style-src` is there for | Pending |
| 3 | A run's progress keeps arriving while it plays, and the terminal stays connected, on Firefox and on Chromium | `connect-src` covers an `EventSource` and a WebSocket, and the two engines have read `'self'` differently for a ws: URL | Pending |
| 4 | Signing out and back in leaves no page blank | The nonce is new on every response, so a document served with a stale one would be silently inert | Pending |
| 5 | On a browser that has already visited another node, the certificate warning of a fresh node is still a click the operator can accept | The decision to send no HSTS, visible only against a real self signed certificate | Pending |
| 6 | `/api/v1/docs` renders on a laptop with a route to the internet | The one path allowed a CDN, and the check that the exception is still needed | Pending |
| 7 | `curl -ksI https://<node>:8006/` shows the policy, `nosniff`, `DENY`, `no-referrer`, and no `Strict-Transport-Security` | The headers as the machine actually serves them, past uvicorn and any proxy in front | Pending |

### Result

Not yet run.

## The scope of a run

The suite asserts the command line, against fakes. What no fake answers is what
Ansible does with `--limit all:!VMs` on a real inventory: whether the guests are
really left out of the plays that reach into them, and whether the roles that
loop over `groups['VMs']` still see every guest. See [D39](decisions.md#d39).

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | `seapath_setup_main` on a site whose guests are not SEAPATH machines converges the machines and reaches no guest, where it used to die on the first guest that refused a connection | The failure being fixed is an SSH connection to a machine nobody prepared for Ansible | Pending |
| 2 | The run's `PLAY RECAP` lists the machines and none of the guests, and the command line in the run view carries `--limit all:!VMs` | The recap is Ansible's own account of what it played | Pending |
| 3 | `deploy_vms_standalone` under the default scope still creates, defines and starts every guest the inventory declares | The claim that a limit narrows the plays and leaves `groups['VMs']` alone, which only a real run proves | Pending |
| 4 | A run narrowed to one machine of a cluster converges that machine and leaves the others untouched, `crm status` unchanged on them | What a narrowed convergence does to a live cluster | Pending |
| 5 | A run narrowed to one guest that *is* a SEAPATH machine converges it, from a `VMs` entry that the default scope leaves out | The escape hatch the default needs, over a real SSH path to a guest | Pending |
| 6 | On a node whose neighbour is down, narrowing to this node produces a run that is offered and succeeds | `peer_reachable` following the scope, against a machine that really does not answer | Pending |
| 7 | Exporting the inventory and running the same playbooks from a control machine with the same `--limit` reports no change | Point 5 of the definition of done, on the scope this service now chooses | Pending |

### Result

Not yet run.

## The latency inside a guest

Every part of this runs against fakes here: the catalogue entry, the refusal of
a launch that names no guest, the `--limit`, and the parsing of a histogram whose
host name is a guest's. What no fake answers is whether the measurement happens
at all, which is an SSH connection into a VM, `sudo`, `rt-tests` and a real
`cyclictest` inside a guest whose vCPUs are pinned by `seapath-alloc`. See
[D41](decisions.md#d41).

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | A guest carrying an `ansible_host`, this node's key in its own account and `rt-tests` installed is offered on the Guest latency panel, and a guest carrying none of it is not | The reading of a real inventory against a real guest an operator prepared | Pending |
| 2 | The run reaches the guest, `cyclictest` runs inside it and the histogram comes back named after the guest | The SSH path into a VM, `become` in the guest, and the role's `fetch` from it | Pending |
| 3 | The guest figure is worse than the machine figure taken under the same inventory commit, and the difference is stable across two runs | The number itself, which is the whole point and which no fake produces | Pending |
| 4 | The application in the guest is visibly disturbed while the measurement runs, as the confirmation says | What the measuring threads do to the guest's own real time threads | Pending |
| 5 | A guest with no `rt-tests` fails at the role's own check, naming the package, rather than with a traceback | The upstream role's guard, on a guest that really lacks the package | Pending |
| 6 | The public key the panel copies, pasted into the guest's `authorized_keys`, is enough for the run: nothing else was needed | The trust into a guest, which this service deliberately does not install | Pending |
| 7 | The same playbook run from a control machine with the same `--limit` measures the same guest and reports the same shape of result | Point 5 of the definition of done, on the one entry that reaches into a guest | Pending |

### Result

Not yet run.

## The run watched over the page that launched it

Everything here is reachable against the fakes: the window opens, the stream
draws, the panels underneath are read again. What a fake never produces is the
thing the change exists for, an operator standing in a page while a real
convergence runs against real machines, and the failure modes belong to a run
that lasts minutes rather than milliseconds. See [D43](decisions.md#d43).

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | Starting a guest from the VMs page opens the window over that page, the log fills as the run goes, and the table behind it is still the one the operator was reading | The whole gesture, over a run that takes long enough to watch | Pending |
| 2 | Closing the window leaves the run going: the Runs page shows it finishing, with the events that arrived after the window was shut | A run long enough that closing it happens in the middle of one | Pending |
| 3 | When the run ends with the window open, the panel underneath shows the new state without a navigation or a reload | The reading, against machines that really changed | Pending |
| 4 | A `seapath_setup_main` that reboots this node ends the window's stream without a final state, and the Runs page reports `interrupted` | The connection dying under the window, which only a real reboot does | Pending |
| 5 | Cancelling from the window stops the run, and the record says `cancelled` | A process that is really running under `ansible-runner` | Pending |
| 6 | A measurement launched from the Real time page lands in the measurement list when its window reports the end, without leaving the page | The run and the file it fetches back | Pending |
| 7 | Clicking beside any window of this UI shuts it, and selecting text inside one and releasing the button outside it does not | Pointer behaviour, in a real browser | Pending |

### Result

Not yet run.

## A navigation inside a release

Every assertion here is about what a browser does with a header, a cached file
and a first paint, and the suite can only check what the service sends. See
[D44](decisions.md#d44).

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | With the network panel open, clicking through the eight tabs after the first page shows one request per navigation, the document, and none for the stylesheet or any script | Only a browser decides to honour `immutable`, and only it shows what a navigation really asked for | Pending |
| 2 | Neither switch in the bar moves when tabs are changed, with the automatic reading on and the palette set against what the system says | The flicker is a paint, and a paint is the one thing a test client has none of | Pending |
| 3 | The name, the mode and the identity are on screen in the first paint of every page, with no placeholder passing through | Same reason: what the operator sees before the scripts run | Pending |
| 4 | A forced reload, Ctrl+Shift+R, still fetches the current release's stylesheet and scripts | A cache override is a browser gesture | Pending |
| 5 | After pinning and applying a new version of this service, the first page of the new release loads the new assets, and no page is half from one version and half from the other | The case the stamp exists for, over a real upgrade in place, and the one that cost an afternoon once | Pending |
| 6 | A rename or a cluster join applied from this node shows in the bar of the Node page without a navigation | A run that really changes the machine's identity | Pending |
| 7 | A session left to expire with a page open sends the next reading to the sign in page rather than a banner | A session TTL passing in a real browser, with the cookie it was given | Pending |

### Result

Not yet run.

## The cost of answering a page

Every number in D45 was measured against the fakes, on a laptop, with one machine
in the inventory. What a real substation cluster does with it is what is left to
see, and the parts that matter are the ones a fake cannot have: three machines
answering over the administration network, and one of them switched off.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | The Cluster, VMs, Containers and Real time pages draw in well under a second on a three node cluster, from a browser on the administration network | The fan out over a real network, and `node_exporter` reading a real `/proc` on a machine whose CPUs belong to its guests | Pending |
| 2 | With one node switched off, those pages still draw, name the machine that did not answer, and pay its timeout once per page rather than once per panel | The timeout is the expensive case, and only a machine that is really off produces it | Pending |
| 3 | The reread control on a panel reaches the machines: switch a resource off through `crm` on a member and the panel shows it on the next press | A cluster whose state changes behind the service | Pending |
| 4 | The automatic reading, on for ten minutes on the Cluster page, shows a failover within one period of it happening | Same, over a real failover | Pending |
| 5 | `git log` on the inventory repository after an hour of use shows exactly the commits the operator made, and `git fsck` is clean | The commit hash is read out of `.git` now rather than by running git, and a repository on a real node has been gc'd, pushed to and pulled from | Pending |
| 6 | An inventory whose branch git has packed, and one left on a detached HEAD by hand, both still show their commit on every panel | `git gc` on a repository with real history, and an operator working in it on the node | Pending |
| 7 | Uploading a file the inventory names clears the warning that it is missing, on the next reading of the Inventory page | The scan is the part of a reading that is deliberately not kept, and this is what it is not kept for | Pending |
| 8 | `top` during a minute of navigation shows this service staying inside its `CPUQuota` on the housekeeping CPUs, with the real time guests untouched | The whole point of the change, measurable only where the guests are real | Pending |

### Result

Not yet run.

## Moving between the tabs

The numbers in D46 were measured in a headless browser against the fakes, with
80 ms of added latency standing in for an ssh tunnel. What a real deployment adds
is a service answering over a real network, machines that take their time, and an
operator who knows what the pages should say.

### Checklist

| # | Check | Why it cannot be tested against a fake | Result |
|---|---|---|---|
| 1 | Moving between the tabs on a real node shows each page's table straight away after the first visit, with the line saying what it is and how old | The wait this change exists to remove, over the link the operator actually uses | Pending |
| 2 | The line disappears within a second or two on a healthy cluster, and the table it leaves is the current one | The reading behind the kept answer, against machines that really answer | Pending |
| 3 | With one node switched off, a page still draws at once from what was kept, and the reading that follows names the machine that did not answer | The timeout is what makes the kept answer worth having, and only a machine that is really off produces it | Pending |
| 4 | While the line is up, the row controls are dead: starting a guest, migrating a resource, applying a playbook cannot be clicked until the reading lands | The rule the whole design rests on, checked with a pointer rather than by reading the code | Pending |
| 5 | A guest migrated from another machine shows on the next visit to the VMs page, rather than the position it was in when this browser last looked | What the kept answer must never hide: the reading behind it is what is finally on screen | Pending |
| 6 | Signing out and back in as another operator shows nothing of what the first was reading | `sessionStorage` is cleared on the way out | Pending |
| 7 | Two nodes open in two tabs through two ssh tunnels never show each other's tables | One origin, two nodes, which is the case the per node key exists for | Pending |
| 8 | After applying a new version of this service, the first page of the new release draws correctly, and no panel is drawn from what the previous release kept | The per release key, over a real upgrade in place | Pending |
| 9 | The Inventory page opens the file for editing as it always did, with no kept copy of the text | An editor drawn from a kept copy is how somebody saves over a change they never saw | Pending |
| 10 | While the Inventory page shows a kept folder, Add files, New file and Commit cannot be clicked, and the tree can still be browsed | The rule and its exception on the one page whose acts are buttons rather than rows | Pending |
| 11 | The Node page says the age of what it shows and replaces it within five seconds, and the console still opens while the line is up | Its one control is a shell on this machine, which depends on no reading | Pending |

### Result

Not yet run.
