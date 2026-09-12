<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Changelog

A version here is the identity of a build. It is stamped in
`app/__init__.py`, pinned by `seapath-webui.container`, published as an image
tag, and reported by the service on a running machine, so a number is used
once. Each shipped change gets its own version, which is why the list below is
long and each entry is short.

Versions up to 0.3.41 are reconstructed from the git history, where the commit
subjects were the only release notes. Later entries are written when the
version is cut.

Dates are the date of the release commit. The milestones these versions belong
to are described in [README.md](README.md), and what remains to be checked on
real hardware is in [docs/validation.md](docs/validation.md).

## 0.3.79 - 2026-09-12

- feat: a run is watched over the page that launched it

## 0.3.78 - 2026-09-12

- fix: an unreachable host says what SSH answered, and a guest key can be accepted

## 0.3.77 - 2026-09-12

- fix: the interrupt row asks where the process bus interrupts are
- feat: nics_affinity is checked against the isolated set when it is written
- fix: the top bar keeps its switch and its identity from page to page
- fix: a host nothing could reach is not reported as a task failure

## 0.3.76 - 2026-09-12

- fix: the narrowing reaches Ansible, and not only the recorded command
- fix: the vCPUs of a guest are chosen from a list, like a machine's CPUs
- fix: a tuned profile shipped by the distribution reads as installed

## 0.3.75 - 2026-09-12

- feat: the latency is measured inside a guest, over the operator's key

## 0.3.74 - 2026-09-11

- feat: a guest declared by a failed attempt can be declared again

## 0.3.73 - 2026-09-11

- feat: the pinning profile field offers a basic profile

## 0.3.72 - 2026-09-11

- fix: an upload refused by a reverse proxy says so

## 0.3.71 - 2026-09-11

- fix: the guest filter counts the rows of the table it sits on

## 0.3.70 - 2026-09-11

- feat: a cluster guest can be disabled and enabled, and filtered
- feat: the real time conformance reads the clock and the PTP level
- feat: the measurement tabs name the tool behind them

## 0.3.69 - 2026-09-10

- fix: the update panel offers the apply the inventory already asks for

## 0.3.68 - 2026-09-10

- feat: the machines a run plays are chosen before Apply, by checkbox

## 0.3.67 - 2026-09-10

- fix: the completion stops offering four other collections

## 0.3.66 - 2026-09-10

- feat: a run leaves the guests out, and can be narrowed

## 0.3.65 - 2026-09-09

- feat: the seed proposes a resolver instead of none

## 0.3.64 - 2026-09-09

- feat: the completion offers what the installed collection declares

## 0.3.63 - 2026-09-09

- fix: the completion list opens under the line it completes

## 0.3.62 - 2026-09-09

- docs: the README describes the editor as it is drawn today

## 0.3.61 - 2026-09-09

- feat: the editor completes a variable name where one is being typed
- fix: a commit sha that is all digits is not offered as a newer version

## 0.3.60 - 2026-09-09

- fix: a variable the inventory itself reads is read by something

## 0.3.59 - 2026-09-09

- fix: the assistant reads the collection before saying nothing reads a name

## 0.3.58 - 2026-09-09

- feat: the inventory editor says what no role reads, behind a switch

## 0.3.57 - 2026-09-09

- feat: the panels that carry Read again can take it on a timer, from a
  switch in the top bar

## 0.3.56 - 2026-09-09

- fix: the API docs page finds its specification behind a reverse proxy

## 0.3.55 - 2026-09-09

- ci: the Docker Hub description step follows the other actions to Node 24

## 0.3.54 - 2026-09-09

- feat: the service names the variables a SEAPATH inventory is written in,
  answered by `GET /inventory/vocabulary`

## 0.3.53 - 2026-09-09

- feat: a panel that ages is read again where it is, without a page reload
- docs: D37 records the control, and why it is manual for now

## 0.3.52 - 2026-09-09

- fix: the ACPI check reads a path the container can see

## 0.3.51 - 2026-09-09

- fix: a machine has one reading of itself, whichever node serves the page
- docs: D36 narrows D27, the local node is judged on its exporter

## 0.3.50 - 2026-09-09

- fix: a guest reports its state in the same words on both kinds of machine
- docs: the README explains the two measurement forms

## 0.3.49 - 2026-09-09

- feat: the inventory editor shifts a block, keeps an indentation and comments
  one out

## 0.3.48 - 2026-09-09

- fix: the VMs page fits a screen, and its badge fits a line

## 0.3.47 - 2026-09-09

- fix: a button and a field are the size of the text beside them
- fix: the two Inventory lines stay inside their column
- fix: the header paints the node this browser already saw

## 0.3.46 - 2026-09-09

- fix: the credentials window on Deployment waits to be asked
- feat: Escape dismisses the window on top, on every page
- feat: the Inventory page fits a screen, the machines and the history in a
  window each
- feat: a run's per host counts fold, and its timings open in a window

## 0.3.45 - 2026-09-08

- feat: the whole interface at four fifths, which is how it was being read
- feat: the top bar stays put on a page that scrolls
- feat: the two deployment panels open in a window instead of folding the page

## 0.3.44 - 2026-09-08

- fix: a pin moves the tag where the inventory keeps it, group included

## 0.3.43 - 2026-09-08

- fix: the image is decided by the commit, not by the day it was built
- docs: the deployment page describes the workflow that runs today

## 0.3.42 - 2026-09-08

- feat: tell the browser what a page of this service may do

## 0.3.41 - 2026-09-07

- fix: the resources card counts the active ones instead of assuming

## 0.3.40 - 2026-09-06

- fix: a placement says which node, and never the one it is already on

## 0.3.39 - 2026-09-06

- docs: the README says the inventory travels, and drops what is not there
- feat: a resource is placed on a named node, and a node is emptied

## 0.3.38 - 2026-09-06

- feat: the containers a site deploys are read, declared and started

## 0.3.37 - 2026-09-06

- fix: a node with no git is replicated to through its own container

## 0.3.36 - 2026-09-06

- feat: the whole cluster's resource history is cleared in one act

## 0.3.35 - 2026-09-06

- feat: a resource is refreshed from the page that reports its failures

## 0.3.34 - 2026-09-06

- fix: forcing a replication reaches the files, and the helper only needs git

## 0.3.33 - 2026-09-06

- fix: a replication reaches the files of the machine it is sent to
- docs: the service says what it writes, rather than what it never does

## 0.3.32 - 2026-09-06

- fix: the add form follows the deployment it is told
- ci: the Docker Hub page follows the image that was just published
- feat: the inventory reaches the other machines when an operator asks

## 0.3.31 - 2026-09-06

- fix: a page loads the script its own version served, and says when it cannot
- fix: the catalogue answers on a machine with no ansible account

## 0.3.30 - 2026-09-06

- feat: read what libvirt says, for the guests Pacemaker does not answer for

## 0.3.29 - 2026-09-06

- fix: stop saying a standalone guest is not deployed
- test: assert the runtime note where Pacemaker actually answers

## 0.3.28 - 2026-09-06

- feat: a guest says which deployment creates it, and everything follows

## 0.3.27 - 2026-09-06

- fix: a variable outside the model is written, rather than silently dropped
- feat: the version the registry holds is offered as a pin and an apply

## 0.3.26 - 2026-09-06

- fix: a guest is placed on a cluster member that runs libvirt, and not on any host

## 0.3.25 - 2026-09-06

- fix: the real time profile is not a cluster feature, and say what disk_bus is

## 0.3.24 - 2026-09-05

- feat: ask for what a creation bakes in, where it is still cheap

## 0.3.23 - 2026-09-05

- feat: confirm a metadata removal, and add a VM in its own window

## 0.3.22 - 2026-09-05

- refactor: ask Ceph for the metadata instead of asking a machine to ask it

## 0.3.21 - 2026-09-05

- feat: read and edit a guest's RBD image metadata from the VMs page

## 0.3.20 - 2026-09-05

- feat: start and stop a guest from the VMs page

## 0.3.19 - 2026-09-05

- feat: reviewed catalogue entries for the two VM deployment playbooks
- feat: add a VM from the VMs page, in one act

## 0.3.18 - 2026-09-05

- fix: the libvirt XML a guest names is a file the page looks for

## 0.3.17 - 2026-09-05

- style: quote the workflow assertion the way black wants it
- fix: read the VMs group as guests rather than as machines
- feat: a VMs page, joining what is declared to what is running

## 0.3.16 - 2026-09-05

- build: label the image, so a machine can recognise its own old versions

## 0.3.15 - 2026-09-05

- feat: add a Cluster page, reading Pacemaker and Ceph from their exporters
- fix: say in green that a firmware measurement came back clean

## 0.3.14 - 2026-09-05

- refactor: name the System page after what it does, Deployment

## 0.3.13 - 2026-09-05

- docs: bring the README up to the pages the service now has
- ci: retry prepare.sh, so a galaxy timeout does not fail a build
- fix: keep the conformance matrix at the width the pool answered with
- fix: show the real time page's timestamps in the browser's timezone
- fix: say what a machine printed when hwlatdetect brought back no report

## 0.3.12 - 2026-09-05

- feat: give each real time panel the screen, and summarise it on its tab

## 0.3.11 - 2026-09-05

- fix: a console is root, so require admin to open one
- fix: name the machine in an inventory validation warning

## 0.3.10 - 2026-09-05

- fix: give the histogram ramp a name the chart does not already use
- fix: stack the "this node" tag under the hostname it marks

## 0.3.9 - 2026-09-05

- feat: draw the UI in the system's palette, with a switch

## 0.3.8 - 2026-09-05

- fix: order the real time nodes by name

## 0.3.7 - 2026-09-04

- feat: seed the image reference from the quadlet the machine boots on
- feat: run the conformance checks on every machine of the cluster

## 0.3.6 - 2026-09-04

- feat: emit every URL relative, so a proxy can serve this under a prefix

## 0.3.5 - 2026-09-04

- fix: name the cookies after the node, so two tunnels can coexist
- fix: give the measurement panel the room a cluster's results need

## 0.3.4 - 2026-09-04

- feat: group the histogram, check every node's isolation, detail every core

## 0.3.3 - 2026-09-04

- feat: read the seapath-alloc pool of every node, and aggregate the cluster

## 0.3.2 - 2026-09-04

- fix: a measurement run could be launched and never relaunched

## 0.3.1 - 2026-09-04

- Merge branch 'realtime': absorb rtperfui as a Real time page
- feat: lay the Real time page out as one screen

## 0.3.0 - 2026-09-04

- fix: move the clone layer's cache key when the branch moves
- ci: publish the version tag the quadlet pins
- feat: absorb rtperfui as a real time page
- feat: carry hwlatdetect, and drop the per VM CPU map

## 0.2.0 - 2026-09-04

- fix: name the node, not the container, in the certificate
- feat: self trust, and the inventory repository
- feat: M1, a machine configured from a browser with no control machine
- fix: run the Ansible this image pins, not the one PATH happens to offer
- bug fixes
- bug fixes
- bug fixes
- refactor: the time reading shrinks to the PTP clock list
- refactor: unit states and the journal are the exporter's job
- refactor: drop the tuned profile reading
- refactor: the quadlet after the observation plane moved out
- doc: record the boundary, not just the deletion
- doc: say what M5 does to vmmgrapi, and what it does not
- feat: refuse to rewrite an inventory this service cannot reproduce
- doc: the adoption rule, checked on a real cluster
- feat: import an inventory, and edit it without rewriting it
- feat: hold the site key, so one node can drive the others
- feat: edit the inventory in the page, machine by machine or as a file
- refactor: one page for what machines should be, one for making it so
- fix: say once what blocks every playbook, instead of thirteen times
- doc: iterating from a source checkout on a node, and its three gaps
- doc: state which gap matters instead of teasing it
- fix: a run that never started is a failure, and it says why
- fix: netaddr belongs in requirements.txt, not in two Dockerfile stages
- fix: static assets revalidate, so an upgraded node stops serving old code
- feat: show where a run spent its time, and stop asking for a typed name
- feat: name a task the way Ansible names it, and show what a debug printed
- fix: a debug task is ansible.builtin.debug, not debug
- feat: a run records the collection it ran, branch included
- fix: jmespath belongs in requirements.txt, like netaddr before it
- fix: two of the previews on offer could only crash
- feat: say which machine leaves the cluster, from a list
- fix: relaunch asked for a typed name, and dropped what the run was given
- feat: the image carries the branch the catalogue needs
- feat: every push to main builds and publishes the image
- docs: check 13 passed on elabo1
- fix: the Apply list never said which playbook a row runs
- ci: the actions were being forced onto Node 24
- fix: the image had no rsync, so synchronize failed on every host
- fix: rsync was given the wrong key, and waited for a password
- docs: name the task that forgets use_ssh_args, not every role
- docs: the task carries use_ssh_args upstream now
- feat: carry the files the inventory names, and mount them for a run
- fix: say which machines a run plays, on the card that launches it
- fix: the host table said failed=1 under a recap saying failed=0
- feat: open a terminal on this machine, from the node view
- docs: CLAUDE.md points at AGENTS.md
- fix: the console asked for a terminal the key was not allowed
- feat: the inventory tab is an editor over the folder
- feat: the system panel is one playbook and a picker
- feat: the playbook list is the collection, read
- feat: the five prerequisites playbooks are reviewed entries
- feat: filter the prerequisites by distribution, and fit three pages on a screen
- fix: give the node page's width to the disks, cap the network table
- fix: shorten the run detail card by a tenth
- docs: show the four pages in the README
- fix: bind the listen socket to the administration address
- fix: keep the waiting cursor for the buttons that are waiting
- fix: the seed inventory names the administration account
- fix: show the command line of a run while it is going
- fix: declare the dark canvas before the stylesheet is fetched
- fix: read every reboot switch, and decline the reboot by default here
- fix: the network entry accepts the reboot switch it offers
- fix: carry the styles in the document instead of fetching them
- docs: record D23, how a playbook update reaches a running node
- feat: run the site's collection when the node carries one
- feat: install a collection on the node, without an image
- feat: say which version of this service the inventory asks for
- fix: read cluster membership from the authkey, not from corosync.conf

## 0.1.0 - 2026-08-11

- doc: specify seapath-webui, a node local UI over the Ansible logic
- feat: M0, skeleton, authentication and the read only node view
