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
| 6 | The interrupt check counts the machine's real interrupts and names any that reach an isolated CPU | A real `/proc/irq` on a machine with real devices. On a kernel with `isolcpus=managed_irq` the expected result is none | |
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
| 19 | Nothing changed on any machine: `crm configure show` and `ceph config dump` are identical before and after a session on this page, and `seapath_setup_main.yaml` from a conventional control machine still reports no change | **The acceptance criterion.** A monitoring page that configured something would be the worst kind of bug here | |

### Result

Not yet run. Checks 1 to 10 need a real Pacemaker cluster with
`ha_cluster_exporter` deployed, which is `configure_ha` on a collection that
carries it. Checks 11 to 15 need Ceph, and check 13 needs a cluster older than
Pacific, which is worth staging only if a site runs one.

Check 6 wants a partition rather than a stopped service: `iptables -j DROP` on
the cluster interface of one member reproduces it and `crm_mon` on both halves
says whether it took.
