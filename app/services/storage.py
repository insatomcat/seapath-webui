# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The Ceph view: health, capacity, daemons and pools.

The reading `docs/ceph.md` promised at the end of a bootstrap: health and the
reason when it is not `HEALTH_OK`, monitors and their quorum, OSDs with their
host, device and usage, pools, raw and usable capacity. It comes from the
manager's own Prometheus module, which is Ceph's view of itself.

Read only, and for a sharper reason than the Pacemaker view. `docs/ceph.md`
already refuses to offer the removal of a single OSD, because `roles/cephadm`
only ever adds and evicting one would mean running `ceph osd` from this
service. Everything an operator might want to press here falls under that same
rule: a failed OSD is shown, named, and pointed at the `ceph` CLI or at an
upstream playbook. Adding storage is `ceph_osd_disks` in the inventory and a
run of `cluster_setup_cephadm`.

Ceph is optional. A Pacemaker cluster with local storage is a supported SEAPATH
configuration, and this answers "no Ceph here" as a sentence rather than as a
failure.
"""

from __future__ import annotations

from app.cluster import ceph
from app.cluster.ceph import CephCluster, CephReader
from app.cluster.exporters import MetricsClient, UrllibMetricsClient, read_all
from app.inventory.service import InventoryService

_NO_CEPH = (
    "No machine of this inventory is serving Ceph metrics. Either this cluster "
    "uses local storage, which is a supported configuration, or Ceph has not "
    "been deployed yet: that is ceph_osd_disks in the inventory and a run of "
    "cluster_setup_cephadm."
)
_NO_INVENTORY = (
    "There is no inventory yet, so there is no machine to ask. The Inventory "
    "page is where the storage is described."
)


class StorageService:
    def __init__(
        self,
        inventory: InventoryService,
        client: MetricsClient | None = None,
        port: int = ceph.DEFAULT_PORT,
        timeout: float = 2.0,
    ) -> None:
        self._inventory = inventory
        self._client = client or UrllibMetricsClient()
        self._port = port
        self._timeout = timeout
        self._reader = CephReader()

    def ceph(self) -> CephCluster:
        """The cluster as its active manager sees it.

        Every machine is asked because only one of them is serving: the active
        manager publishes the cluster and a standby publishes nothing, so the
        first exposition that carries the health is the one to read. Which node
        that was is on the page, since it moves when the manager fails over.
        """
        state = self._inventory.state()
        if state.inventory is None:
            return CephCluster(error=_NO_INVENTORY, inventory_commit=state.commit)

        targets = [
            (name, node.ansible_host)
            for name, node in state.inventory.hosts.items()
            if node.ansible_host
        ]
        expositions = read_all(self._client, targets, self._port, timeout=self._timeout)
        serving = next(
            (item for item in expositions if ceph.reporting(item)),
            None,
        )
        if serving is None:
            return CephCluster(
                error=_NO_CEPH,
                this_host=state.this_host,
                inventory_commit=state.commit,
            )

        cluster = self._reader.read(serving)
        cluster.this_host = state.this_host
        cluster.inventory_commit = state.commit
        return cluster
