# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The Pacemaker view: which machines are in the cluster, and what runs where.

Read only, and it stays read only. `ha_cluster_exporter` publishes what
`crm_mon` said, this asks it over HTTP and shapes the answer, and no command
reaches a host. That is not a limitation of the current version: putting a
resource in standby or cleaning up a failure means running `crm` from this
container, which is the thing AGENTS.md forbids in the same words it forbids
writing `corosync.conf`. What a machine should be is the inventory and a run;
what the cluster is doing right now is Pacemaker's, and this reports it.

The inventory decides who is asked. Every host it declares with an address is
asked in parallel, so a member whose exporter is down is a line on the page
rather than a page that fails.
"""

from __future__ import annotations

from app.cluster import ha
from app.cluster.exporters import (
    Exposition,
    MetricsClient,
    UrllibMetricsClient,
    read_all,
)
from app.cluster.ha import NodeReach, PacemakerCluster, PacemakerReader
from app.inventory.model import Mode
from app.inventory.service import InventoryService

# Said when no machine of the inventory publishes a cluster, and the inventory
# does not describe one either. It is the ordinary state of a standalone node
# and not a failure of anything.
_STANDALONE = (
    "This inventory describes a standalone machine, which has no Pacemaker "
    "cluster. Joining nodes into one is an inventory change and a run of "
    "cluster_setup_ha."
)
_NO_CLUSTER = (
    "No machine of this inventory published Pacemaker metrics. "
    "cluster_setup_ha deploys the cluster and the ha_cluster_exporter that "
    "reports it, and until it has run there is nothing here to read."
)
_NO_INVENTORY = (
    "There is no inventory yet, so there is no machine to ask. The Inventory "
    "page is where a cluster is described."
)


class ClusterService:
    def __init__(
        self,
        inventory: InventoryService,
        client: MetricsClient | None = None,
        port: int = ha.DEFAULT_PORT,
        timeout: float = 2.0,
    ) -> None:
        self._inventory = inventory
        self._client = client or UrllibMetricsClient()
        self._port = port
        self._timeout = timeout
        self._reader = PacemakerReader()

    def pacemaker(self) -> PacemakerCluster:
        """The cluster as its coordinator sees it.

        Every member is asked, because which of them is the coordinator is one
        of the things the reading answers, and because a member that cannot be
        reached is itself a finding: a page that only ever spoke to one node
        would report a healthy cluster while two thirds of it was unreachable.
        """
        state = self._inventory.state()
        if state.inventory is None:
            return PacemakerCluster(error=_NO_INVENTORY, inventory_commit=state.commit)

        targets = [
            (name, node.ansible_host)
            for name, node in state.inventory.hosts.items()
            if node.ansible_host
        ]
        expositions = read_all(self._client, targets, self._port, timeout=self._timeout)
        reach = [
            NodeReach(
                host=exposition.host,
                address=exposition.address,
                reachable=exposition.answered,
                reporting=ha.reporting(exposition),
                error=exposition.error,
            )
            for exposition in expositions
        ]

        reporting = [item for item in expositions if ha.reporting(item)]
        if not reporting:
            return PacemakerCluster(
                error=(
                    _STANDALONE
                    if state.inventory.mode is Mode.STANDALONE
                    else _NO_CLUSTER
                ),
                reach=reach,
                this_host=state.this_host,
                inventory_commit=state.commit,
            )

        cluster = self._reader.read(self._coordinator(reporting))
        cluster.reach = reach
        cluster.this_host = state.this_host
        cluster.inventory_commit = state.commit
        return cluster

    def _coordinator(self, reporting: list[Exposition]) -> Exposition:
        """The exposition to believe, which is the coordinator's when it answered.

        Falling back to the first answer rather than to nothing: a cluster
        whose DC is unreachable is exactly when an operator opens this page,
        and `from_dc` tells them what they are looking at.
        """
        return next(
            (item for item in reporting if ha.is_coordinator(item)), reporting[0]
        )
