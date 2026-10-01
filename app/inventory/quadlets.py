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

This module reads that list back, host by host, and says which entries are
quadlets.

A cluster has a second way since `deploy_containers_cluster`: a workload in
`cluster_containers`, keyed by name, which that role puts on every hypervisor
of the cluster with its images, its RBD image and its Pacemaker resource.
`workloads` reads those back in the same shape, so the page joins both to what
the machines publish the same way.

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

from app.inventory.resolve import groups, members, resolve

# Where podman's generator looks. A file copied anywhere else is an ordinary
# upload, whatever its extension says.
QUADLET_DIR = "/etc/containers/systemd"

UPLOAD_VARIABLE = "upload_extra_files_upload_files"
COMMANDS_VARIABLE = "upload_extra_files_commands_to_run_after_upload"
WORKLOADS_VARIABLE = "cluster_containers"

# The machines `deploy_containers_cluster.yaml` plays,
# `cluster_machines:&hypervisors`: the cluster members Pacemaker may start a
# workload on, the observers left out.
WORKLOAD_GROUPS = ("cluster_machines", "hypervisors")

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

# The keys through which a quadlet names another one, and what such a name
# looks like once it is found in their value.
_NAMING = re.compile(r"^\s*(?:Network|Pod|Volume|Image|Mount)\s*=(.*)$", re.MULTILINE)
_QUADLET_NAME = re.compile(
    r"[A-Za-z0-9_.@-]+\.(?:container|kube|pod|network|volume|image|build)\b"
)


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
    workload: bool = False
    """Declared in `cluster_containers` rather than uploaded by
    `upload_extra_files`. `name` is then the workload's, which is the
    Pacemaker resource, and `unit` the one the resource starts."""
    preferred_host: str = ""
    """The member a workload's entry asks the cluster to run it on, which
    `deploy_containers_cluster` writes as a `prefer-` rule."""
    rbd: bool = False
    """The workload keeps its state on an RBD image named after it, which a
    removal deletes only when it is asked to."""

    @property
    def actionable(self) -> bool:
        # A workload is started as a whole through its resource, whatever the
        # kind of the quadlet its unit comes from: a pod included.
        return self.workload or self.kind in ACTIONABLE

    @property
    def file_name(self) -> str:
        """What the file is called once it is on the machines.

        The basename of `dest`, which is the name podman's generator reads,
        the name the unit is derived from, and the one an operator finds by
        listing `/etc/containers/systemd`. Where the file sits on the control
        machine is `src`, and that is a fact about the inventory rather than
        about the container.
        """
        return self.dest.rsplit("/", 1)[-1]


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


def workloads(document: str | dict[str, Any]) -> list[Quadlet]:
    """Every workload `cluster_containers` declares, one entry per machine.

    The machines are the ones the playbook plays, whatever group the variable
    is written on. A workload being removed, `state: absent`, is no longer a
    container: the next run takes it away.
    """
    table = groups(document)
    if any(name not in table for name in WORKLOAD_GROUPS):
        return []
    played = members(table, WORKLOAD_GROUPS[0]) & members(table, WORKLOAD_GROUPS[1])
    found: list[Quadlet] = []
    for host, variables in sorted(resolve(document).items()):
        value = variables.get(WORKLOADS_VARIABLE)
        if host not in played or not isinstance(value, dict):
            continue
        for name, spec in sorted(value.items(), key=lambda item: str(item[0])):
            quadlet = _workload(host, name, spec)
            if quadlet is not None:
                found.append(quadlet)
    return found


def removed(document: str | dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The workloads `cluster_containers` marks `state: absent`, by name.

    The next run of `deploy_containers_cluster` takes them off every node, and
    until then they are the one thing about them the page still has to say.
    """
    table = groups(document)
    if any(name not in table for name in WORKLOAD_GROUPS):
        return {}
    played = members(table, WORKLOAD_GROUPS[0]) & members(table, WORKLOAD_GROUPS[1])
    found: dict[str, dict[str, Any]] = {}
    for host, variables in sorted(resolve(document).items()):
        value = variables.get(WORKLOADS_VARIABLE)
        if host not in played or not isinstance(value, dict):
            continue
        for name, spec in value.items():
            if (
                isinstance(name, str)
                and isinstance(spec, dict)
                and spec.get("state", "present") == "absent"
            ):
                found.setdefault(name, spec)
    return found


def workload_sources(spec: Any) -> list[str]:
    """The quadlet files a workload names, as the inventory writes them."""
    value = spec.get("quadlets") if isinstance(spec, dict) else None
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def hosts_of(document: str | dict[str, Any], name: str) -> list[str]:
    """The machines an inventory sends this quadlet to."""
    return [
        quadlet.host
        for quadlet in [*declared(document), *workloads(document)]
        if quadlet.name == name
    ]


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


def names_in(content: str) -> set[str]:
    """The other quadlet files this one names, by the name they take on disk.

    `Network=web.network`, `Pod=web.pod`, `Volume=data.volume:/srv` and the
    `source=` of a `Mount=` are how a container is joined to the files podman
    turns into its dependencies. A name without a quadlet extension is a
    network or volume podman already has, which no file here describes.
    """
    found: set[str] = set()
    for match in _NAMING.finditer(content):
        found.update(_QUADLET_NAME.findall(match.group(1)))
    return found


def workload_config(spec: Any) -> list[str]:
    """The configuration files `deploy_containers_cluster` writes to every
    node of the cluster for a workload."""
    value = spec.get("config") if isinstance(spec, dict) else None
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def workload_files(spec: Any) -> list[tuple[str, str]]:
    """The text files `deploy_containers_cluster` writes onto a workload's RBD
    image, as `(src, dest)`, `dest` being relative to the image."""
    rbd = spec.get("rbd") if isinstance(spec, dict) else None
    files = rbd.get("files") if isinstance(rbd, dict) else None
    found: list[tuple[str, str]] = []
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict):
            continue
        source, dest = item.get("src"), item.get("dest")
        if isinstance(source, str) and source.strip():
            name = dest if isinstance(dest, str) and dest.strip() else source
            found.append((source.strip(), name.strip()))
    return found


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


def _workload(host: str, name: Any, spec: Any) -> Quadlet | None:
    if not isinstance(name, str) or not NAME.match(name) or not isinstance(spec, dict):
        return None
    if spec.get("state", "present") != "present":
        return None
    unit = spec.get("unit") or f"{name}.service"
    if not isinstance(unit, str) or "{{" in unit:
        return None
    # The quadlet the unit comes from is the one the page shows: the pod of a
    # workload made of a pod and its containers, the container of a single one.
    sources = workload_sources(spec)
    files = {source: on_machine(source) for source in sources}
    source = next(
        (item for item in sources if unit_for(files[item]) == unit),
        sources[0] if sources else "",
    )
    file_name = files.get(source) or f"{name}.container"
    kind = _split(file_name)[1] or ".container"
    preferred = spec.get("preferred_host")
    return Quadlet(
        host=host,
        name=name,
        kind=kind,
        unit=unit,
        src=source,
        dest=f"{QUADLET_DIR}/{file_name}",
        mode="0644",
        workload=True,
        preferred_host=preferred if isinstance(preferred, str) else "",
        rbd=isinstance(spec.get("rbd"), dict),
    )


def on_machine(source: str) -> str:
    """The name a quadlet takes on the machines: `.j2` is rendered away."""
    base = source.rsplit("/", 1)[-1]
    return base[: -len(".j2")] if base.endswith(".j2") else base


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
