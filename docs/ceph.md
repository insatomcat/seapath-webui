<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Ceph from the UI

Ceph is optional. A Pacemaker cluster with local storage is a supported SEAPATH
configuration and the UI must never imply otherwise.

This document is short, and that is the point. The UI does not deploy Ceph. It
fills the Ceph variables of the inventory and runs
`cluster_setup_cephadm.yaml`. Every subtlety of `roles/cephadm`, and there is a
lot of it, stays where it is, tested by the CI, with no second implementation to
keep in sync.

## 1. What the UI actually contributes

The hard part of the operator's job is not running the playbook, it is choosing
the right disks and typing paths without mistakes. That is where the UI earns
its place.

| Variable | How the UI fills it |
|---|---|
| `ceph_osd_disks` | Disk selector, always writing `/dev/disk/by-path/...`, never `/dev/sdX` |
| `cephadm_network` | Derived from the `cluster_ip_addr` values already in the inventory |
| `deploy_cephfs` | Checkbox |
| `ceph_conf_overrides` | Prefilled from the reference inventory, expert section for the rest |
| `cephadm_release` | Shown, with the image the node can actually resolve |

## 2. The disk selector

`GET /api/v1/node/disks` returns candidates per node with a claim state, derived
the same way `roles/cephadm` derives it, by comparing the OSD fsid found in the
`ceph-volume` LVM tags against the running cluster's OSD map:

| State | Meaning | UI |
|---|---|---|
| `free` | no Ceph metadata, not mounted, not in a used VG | selectable |
| `claimed` | carries an OSD the running cluster claims | shown as in use, not selectable |
| `orphan` | carries Ceph LVM metadata unknown to the cluster | selectable, with a warning that adding it zaps the volume |
| `in-use` | mounted, part of a used VG, or holding the root filesystem | never selectable |

The states are computed for display only. The decision to zap or preserve is
taken by the role at run time, from the state of the machine at that moment, not
from what the browser showed a minute earlier. The UI must never pass a "zap
this" flag derived from a stale view. Read the role before implementing this
screen, and mirror its rule exactly: a volume is preserved if and only if the
OSD uuid in its LVM tags is registered in the cluster OSD map.

An observer has no `ceph_osd_disks`, and the form enforces it.

## 3. Flow

1. The cluster exists and has quorum. Without that, the Ceph section is not
   offered.
2. Select the disks per node, confirm the derived `cephadm_network`, choose
   whether CephFS is wanted.
3. The inventory is validated and committed, which gives a reviewable diff and a
   rollback point before anything is destroyed.
4. Apply `cluster_setup_cephadm.yaml`. The run view streams the tasks. Bootstrap
   is slow and chatty, and the operator needs to see it move.
5. The storage view then reads the live cluster: health and the reason when it
   is not `HEALTH_OK`, monitors and their quorum, OSDs with host, device and
   usage, pools, raw and usable capacity, with a warning when the cluster
   approaches full. It is the Storage tab of the Cluster page, read from the
   active manager's own Prometheus module rather than from `ceph -s` over SSH.
   See [D29](decisions.md#d29).

## 4. The image reference

`roles/cephadm` bootstraps with `--image localhost:5000/ceph:v{{ cephadm_release }}`,
from a local registry, because a substation is not necessarily connected to the
internet. The UI shows which image the node can actually resolve before letting
the run start, and refuses to launch a bootstrap that would fail on an image
pull ten minutes in. It does not silently rewrite the variable.

## 5. Not offered

No pool creation, no CRUSH rule editing, no erasure coding choices, no tuning.
Those need judgment a form cannot capture and they belong to the inventory's
expert section or to the `ceph` CLI.

**Removing a single OSD is not offered either**, and the reason is worth stating
because it is tempting. `roles/cephadm` only ever adds: dropping a device from
`ceph_osd_disks` makes the role ignore it, it does not evict the OSD from the
cluster. Offering "remove this OSD" would therefore mean running `ceph osd`
commands from the service, which is exactly the thing this design forbids.
Replacing a failed disk is a real field operation and it deserves support, but
it needs an upstream playbook first. Until that exists, the UI shows the failed
OSD and points at the `ceph` CLI rather than pretending to own the operation.

The whole node case is different and is supported, because
`cluster_remove_machine.yaml` exists and already calls `ceph orch host rm`.

[D29](decisions.md#d29) generalises this section to the whole Cluster page: the
storage view reports a failed OSD, names its host and its device, and offers no
button that would evict it.
