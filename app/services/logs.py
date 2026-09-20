# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The journal of every machine the inventory declares, asked now, merged here.

[D63](../../docs/decisions.md) settled where this reading goes and what it may
cost. The short version:

- **Over the SSH path a run takes.** One `ssh` per machine, `journalctl` at the
  far end, nothing stored. It reuses the trust D2 established, holds no new
  credential, opens no port, and reaches a machine whose container is dead,
  which is the case an operator asks for logs in.
- **This machine included.** The container has no route to the host's journal
  and must not grow one, so the node an operator is browsing is one entry of
  the fan out like any other. The configuration plane already reaches its own
  machine this way.
- **The administration address, never the cluster one.** The relations carry
  `from=` bound to the peer's administration address, so a read that went out
  over the cluster network would be refused by `authorized_keys` before sshd
  looked at the key.
- **In parallel, and a machine that does not answer is a result.** The page
  waits on the slowest, and a cluster half built or half up is the ordinary
  state of one. `app/cluster/exporters.py` fans out the same way for the same
  reason.

What this does not do is keep anything. There is no store, no index and no
history here: what an operator reads is what the machines still hold, and a
site that needs the journal of a machine that burned sends it to a collector,
which `syslog_ng_client` is the supported way to do.

The merge is on the entries' own timestamps, and it means something because
SEAPATH holds the clocks of a cluster with PTP. Two machines whose clocks have
drifted produce a page whose lines interleave wrongly, which is why the drift
is a reading of its own on the Cluster page rather than something this module
tries to correct.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.core.auth import Role
from app.hosts.remote import RemoteRefused, RemoteRequest, RemoteRunner
from app.inventory.service import InventoryService
from app.logs import journal
from app.logs.journal import Entry, Query, Refused
from app.runs.service import RunPaths

logger = logging.getLogger(__name__)

#: How long a machine is given to answer its part of one reading. Shorter than
#: `hosts/remote.py`'s own default, because that one bounds a reading an
#: operator asked for and waits on alone, and this one bounds a page: a machine
#: that has gone away costs this before the ones that answered can be drawn.
CONNECT_TIMEOUT_SECONDS = 5
TIMEOUT_SECONDS = 20.0

#: How many machines are asked at once. A SEAPATH cluster is three and the
#: inventory may hold a fourth, so this is a ceiling rather than a policy.
MAX_WORKERS = 8

_NO_INVENTORY = (
    "This node holds no inventory yet, so there is no machine to ask. "
    "Write one on the Inventory page first."
)


class Scope(BaseModel):
    """A question, written as the matches that answer it cheaply.

    D63 asks for views bounded by construction rather than a search box over
    the cluster, and these are the bounds. Each one is a match on a field, so
    each one is served from the journal's index, and an operator who picks one
    from a list cannot write the query that scans.

    A unit named here that a machine does not run matches nothing, which is
    the right answer on a mixed inventory: an observer runs no `libvirtd` and
    should say so by being empty rather than by failing.
    """

    name: str
    label: str
    units: tuple[str, ...] = ()
    priority: int | None = None


#: The scopes the API offers, and the page's buttons. Ordered as an operator
#: reaches for them: the guests first, because a guest that will not migrate is
#: the question this feature was asked for.
SCOPES: tuple[Scope, ...] = (
    Scope(
        name="guests",
        label="Guests, libvirt and their placement",
        units=("libvirtd.service", "pacemaker.service", "corosync.service"),
    ),
    Scope(
        name="cluster",
        label="Membership and quorum",
        units=("corosync.service", "pacemaker.service"),
    ),
    Scope(
        name="errors",
        label="Everything at error and above",
        # `err`, `crit`, `alert` and `emerg`. A priority is a match on a field
        # like any other, so this one is indexed too, which is what lets it be
        # offered without a unit beside it.
        priority=3,
    ),
    Scope(
        name="service",
        label="This management service",
        units=("seapath-webui.service",),
    ),
)

_BY_NAME = {scope.name: scope for scope in SCOPES}


def scope(name: str) -> Scope:
    """The named scope, or a refusal naming the ones that exist."""
    found = _BY_NAME.get(name)
    if found is None:
        raise Refused(
            f"{name!r} is not a scope. The scopes are "
            + ", ".join(sorted(_BY_NAME))
            + "."
        )
    return found


@dataclass(frozen=True)
class Target:
    """One machine, as the fan out addresses it."""

    host: str
    address: str


class MachineAnswer(BaseModel):
    """What one machine gave, or the reason it gave nothing.

    A failure is a row rather than an exception, because the answer an operator
    needs is usually the other two machines' and because which machine is
    silent is itself a finding.
    """

    host: str
    address: str
    answered: bool
    returned: int = Field(
        default=0, description="Entries this machine sent, before the merge"
    )
    capped: bool = Field(
        default=False,
        description="It sent as many as it was asked for, so it holds older ones",
    )
    error: str = ""


class Reading(BaseModel):
    """Every machine's answer to one query, merged.

    `entries` is the whole cluster's, oldest first, which is the order a log is
    read in. `machines` says who answered and who did not, and `warnings` names
    what could not be read, as every reading in this API does.
    """

    entries: list[Entry] = Field(default_factory=list)
    machines: list[MachineAnswer] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description="Older entries matched and were left out of this answer",
    )
    inventory_commit: str | None = None


class LogService:
    def __init__(
        self,
        inventory: InventoryService,
        remote: RemoteRunner,
        keys: RunPaths,
        ansible_user: str,
        required_role: Role = Role.VIEWER,
        connect_timeout: int = CONNECT_TIMEOUT_SECONDS,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._inventory = inventory
        # The same runner, the same key and the same `known_hosts` a run and a
        # console use, so what this can reach is exactly what the configuration
        # plane already reaches. See D54 and D63.
        self._remote = remote
        self._keys = keys
        self._ansible_user = ansible_user
        # Who may read the journal of a cluster. A viewer by default, since it
        # is a reading and the page it feeds answers the question an operator
        # came to this service with. A journal nonetheless carries command
        # lines, the names of the accounts that ran them and whatever a service
        # chose to print, so a site that treats it as more than a reading
        # raises this, the way D19 lets one lower the console's.
        self.required_role = required_role
        self._connect_timeout = connect_timeout
        self._timeout = timeout

    def scopes(self) -> tuple[Scope, ...]:
        """The questions this service offers, for the page's controls."""
        return SCOPES

    def scope(self, name: str) -> Scope:
        """The named scope, or a refusal naming the ones that exist."""
        return scope(name)

    def machines(self) -> list[Target]:
        """Every machine the inventory declares with an address, in file order.

        The guests are left out. They are the `VMs` group, this service holds
        no trust into them, and a guest's journal belongs to whoever runs the
        guest.
        """
        state = self._inventory.state()
        if state.inventory is None:
            return []
        return [
            Target(host=name, address=node.ansible_host)
            for name, node in state.inventory.hosts.items()
            if node.ansible_host
        ]

    def read(self, query: Query, hosts: list[str] | None = None) -> Reading:
        """One query, asked of every machine at once, merged into one answer.

        `hosts` narrows it to the machines a caller already knows are relevant,
        which is what the views built on runs and guests send. A name the
        inventory does not declare is refused rather than silently dropped: a
        page that quietly asked two machines when it was told three would
        report an incident as absent from a machine nobody had looked at.
        """
        checked = journal.checked(query)
        state = self._inventory.state()
        targets = self.machines()
        if not targets:
            return Reading(warnings=[_NO_INVENTORY], inventory_commit=state.commit)
        targets = _narrowed(targets, hosts)

        answers = self._ask(targets, checked)
        merged: list[Entry] = []
        machines: list[MachineAnswer] = []
        warnings: list[str] = []
        for answer, entries, skipped in answers:
            machines.append(answer)
            merged.extend(entries)
            if answer.error:
                warnings.append(f"{answer.host}: {answer.error}")
            if skipped:
                warnings.append(
                    f"{answer.host}: {len(skipped)} line(s) of its answer could "
                    f"not be read ({skipped[0].reason})."
                )

        merged.sort(key=lambda entry: entry.sort_key)
        truncated = len(merged) > checked.lines or any(
            answer.capped for answer in machines
        )
        if len(merged) > checked.lines:
            # The most recent, because a log is read from its end. What is
            # dropped is older than everything kept, on every machine at once,
            # so the window the operator sees stays a window rather than a
            # sample.
            merged = merged[-checked.lines :]
        if truncated:
            warnings.append(
                "Older entries matched and were left out. Narrow the window, "
                "or add a match, to see them."
            )
        return Reading(
            entries=merged,
            machines=machines,
            warnings=warnings,
            truncated=truncated,
            inventory_commit=state.commit,
        )

    def _ask(
        self, targets: list[Target], query: Query
    ) -> list[tuple[MachineAnswer, list[Entry], list[journal.Unreadable]]]:
        if not targets:
            return []
        command = journal.command(query)
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(targets))) as pool:
            return list(
                pool.map(lambda target: self._one(target, command, query), targets)
            )

    def _one(
        self, target: Target, command: str, query: Query
    ) -> tuple[MachineAnswer, list[Entry], list[journal.Unreadable]]:
        try:
            output = self._remote.run(
                RemoteRequest(
                    address=target.address,
                    user=self._ansible_user,
                    command=command,
                    private_key_file=self._keys.private_key_file,
                    known_hosts_file=self._keys.known_hosts_file,
                    extra_key_files=self._keys.extra_key_files(),
                    timeout=self._timeout,
                    connect_timeout=self._connect_timeout,
                )
            )
        except RemoteRefused as error:
            logger.debug("No journal from %s: %s", target.host, error)
            return (
                MachineAnswer(
                    host=target.host,
                    address=target.address,
                    answered=False,
                    error=str(error),
                ),
                [],
                [],
            )
        entries, skipped = journal.parse(output, target.host)
        return (
            MachineAnswer(
                host=target.host,
                address=target.address,
                answered=True,
                returned=len(entries),
                capped=len(entries) >= query.lines,
            ),
            entries,
            skipped,
        )


def _narrowed(targets: list[Target], hosts: list[str] | None) -> list[Target]:
    if hosts is None:
        return targets
    wanted = list(dict.fromkeys(hosts))
    known = {target.host: target for target in targets}
    missing = [name for name in wanted if name not in known]
    if missing:
        raise Refused(
            "The inventory declares no machine named "
            + ", ".join(repr(name) for name in missing)
            + "."
        )
    return [known[name] for name in wanted]
