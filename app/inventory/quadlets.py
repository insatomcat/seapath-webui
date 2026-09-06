# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The containers an inventory declares, which are quadlets and nothing more.

A SEAPATH container has no role of its own and needs none. It is a `.container`
file that `upload_extra_files` copies to `/etc/containers/systemd/`, where
podman's generator turns it into a systemd unit at the next `daemon-reload`.
That is the whole mechanism, and [D17](../../docs/decisions.md#d17) met it in
the first real inventory this service was given:

```yaml
upload_extra_files_upload_files:
  - src: '../inventories_private/node-exporter.container.j2'
    dest: '/etc/containers/systemd/node-exporter.container'
    mode: "0644"
```

So there is no container variable to invent, no group to add and no schema to
extend. This module reads that list back, host by host, and says which entries
are quadlets. Everything the Containers page shows about the desired state
comes from here, and everything it writes is an entry of exactly this shape.

**The unit name is derived, not stored.** podman's generator names the unit
after the file, and the rule differs per extension. It is reproduced here
because the unit name is what Pacemaker is told, what `node_exporter` publishes
and what a start acts on: getting it wrong means a page reporting a unit
nothing runs.

**What is deliberately not read.** The machine's own
`/etc/containers/systemd/`. A quadlet somebody dropped on a host by hand is
invisible here, the same way a VM created outside the inventory is invisible to
the deployment roles, and for the same reason: this service answers for the
desired state, and the desired state is this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.inventory.resolve import resolve

# Where podman's generator looks. A file copied anywhere else is an ordinary
# upload, whatever its extension says.
QUADLET_DIR = "/etc/containers/systemd"

UPLOAD_VARIABLE = "upload_extra_files_upload_files"
COMMANDS_VARIABLE = "upload_extra_files_commands_to_run_after_upload"
CRM_VARIABLE = "extra_crm_cmd_to_run"

# What has to run once the files have landed, and the only command this service
# ever adds. The generator reads `/etc/containers/systemd` on a reload and
# writes the units; without it the file is on the machine and systemd has never
# heard of it. Starting is not in here on purpose: a convergence that started
# every container it uploaded would be an apply with a runtime act hidden in
# it, and the first boot after it does the job anyway.
DAEMON_RELOAD = "systemctl daemon-reload"

# How podman names the unit it generates, per extension. From
# `podman-systemd.unit(5)`. `.container` and `.kube` take the file's own name;
# the others carry the kind, which is what keeps a `web.volume` from colliding
# with the `web.container` that mounts it.
_UNITS = {
    ".container": "{stem}.service",
    ".kube": "{stem}.service",
    ".pod": "{stem}-pod.service",
    ".network": "{stem}-network.service",
    ".volume": "{stem}-volume.service",
    ".image": "{stem}-image.service",
    ".build": "{stem}-build.service",
}

# The kinds an operator starts and stops. The others exist to be pulled in by
# one of these as a dependency, so offering a button on them would offer to
# stop a network out from under the container using it.
ACTIONABLE = (".container", ".kube")

# A quadlet name has to survive being a file name, a unit name and a Pacemaker
# resource id at once, which is the same constraint a guest name carries.
NAME = re.compile(r"^[a-z0-9]([a-z0-9_.-]{0,61}[a-z0-9])?$", re.IGNORECASE)

# `[Install]` in a quadlet is what makes systemd start the container at boot.
# Read to be reported rather than to be enforced: it is the right thing on a
# standalone machine and it is a conflict on a cluster, where Pacemaker decides
# where the container runs and a unit that starts itself is a second opinion.
_INSTALL = re.compile(r"^\s*\[Install\]\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Quadlet:
    """One quadlet an inventory declares, for one host."""

    host: str
    name: str
    """The file's own name without its extension, which is what a unit, a
    resource and a page all call it."""
    kind: str
    """The extension, `.container` for the ordinary case."""
    unit: str
    src: str
    dest: str
    mode: str = ""

    @property
    def actionable(self) -> bool:
        return self.kind in ACTIONABLE


def unit_for(dest: str) -> str:
    """The unit podman's generator writes for this file, or an empty string."""
    name, kind = _split(dest)
    if not name:
        return ""
    return _UNITS[kind].format(stem=name)


def declared(document: str | dict[str, Any]) -> list[Quadlet]:
    """Every quadlet the inventory declares, one entry per host that gets it.

    Resolved the way Ansible resolves a variable, so a site that writes the
    list once on `all` gets it on every machine, which is what the roles will
    do with it.
    """
    found: list[Quadlet] = []
    for host, variables in sorted(resolve(document).items()):
        for item in _entries(variables.get(UPLOAD_VARIABLE)):
            quadlet = _quadlet(host, item)
            if quadlet is not None:
                found.append(quadlet)
    return found


def hosts_of(document: str | dict[str, Any], name: str) -> list[str]:
    """The machines an inventory sends this quadlet to."""
    return [quadlet.host for quadlet in declared(document) if quadlet.name == name]


def upload_entry(name: str, source: str, kind: str = ".container") -> dict[str, str]:
    """The `upload_extra_files` entry that deploys one quadlet.

    Written in the shape the inventories in the field already use, `src`,
    `dest` and `mode` in that order, so a site reading its own file afterwards
    finds the line it would have written.
    """
    return {
        "src": source,
        "dest": f"{QUADLET_DIR}/{name}{kind}",
        "mode": "0644",
    }


def primitive(name: str, unit: str) -> str:
    """The `crm` line that makes a quadlet a Pacemaker resource.

    Pacemaker's `systemd` resource agent, which starts the unit podman's
    generator wrote. One primitive and its three operations, with nothing about
    where it runs: placement is Pacemaker's, and the constraints that narrow it
    are the roles'.

    Written in the shape a real SEAPATH inventory already carries, timeouts
    included, so a site reading its own `extra_crm_cmd_to_run` afterwards finds
    the line it would have written itself. The timeouts are there because the
    agent's defaults are the ones a container pulling an image on first start
    overruns.
    """
    return (
        f"primitive {name} systemd:{unit} "
        "op monitor interval=30s "
        "op start timeout=60s interval=0s "
        "op stop timeout=60s interval=0s"
    )


def reloads(command: str) -> bool:
    """Whether a post upload command is the `daemon-reload` this needs.

    Matched on the end of the line rather than on equality: the inventories in
    the field write `/usr/bin/systemctl daemon-reload`, and adding a second
    command that does the same thing would be this service editing a file it
    does not understand.
    """
    return command.strip().endswith("daemon-reload")


def starts_itself(content: str) -> bool:
    """Whether the quadlet asks systemd to start the container at boot.

    True is correct on a machine with no Pacemaker and a finding on one with
    it: the unit and the cluster would both be starting the same container,
    and the resource Pacemaker could not stop is the one that comes back at
    every boot.
    """
    return bool(_INSTALL.search(content))


def _entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _quadlet(host: str, item: dict[str, Any]) -> Quadlet | None:
    dest = item.get("dest")
    source = item.get("src")
    if not isinstance(dest, str) or not isinstance(source, str):
        return None
    if item.get("extract"):
        # An archive unpacked into a directory. What comes out of it is
        # whatever the site put in, and this service is not going to guess.
        return None
    if dest.rsplit("/", 1)[0] != QUADLET_DIR:
        return None
    name, kind = _split(dest)
    if not name:
        return None
    mode = item.get("mode")
    return Quadlet(
        host=host,
        name=name,
        kind=kind,
        unit=_UNITS[kind].format(stem=name),
        src=source.strip(),
        dest=dest.strip(),
        mode=str(mode) if mode is not None else "",
    )


def _split(dest: str) -> tuple[str, str]:
    """The name and the extension of a quadlet path, or two empty strings."""
    if "{{" in dest:
        # A templated destination is Ansible's business at run time, and a unit
        # name guessed from it would be a confident wrong answer.
        return "", ""
    base = dest.strip().rsplit("/", 1)[-1]
    for kind in _UNITS:
        if base.endswith(kind) and len(base) > len(kind):
            return base[: -len(kind)], kind
    return "", ""
