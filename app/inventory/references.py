# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The files an inventory names, and whether a run would find them.

A dozen SEAPATH roles take a path to a file the control machine holds:
`upload_extra_files_upload_files`, `iptables_rules_path`, the syslog
certificates, the cephadm spec, `vm_disk` and `vm_template`. The path is
written in the inventory as an ordinary variable, and Ansible resolves it the
way it resolves any `src`: against the directory the playbook sits in, never
against the directory the inventory sits in.

That resolution is reproduced here, so the page can say "this file is missing,
and here is the name to upload it under" while an operator is still looking at
the inventory. The alternative is finding out three minutes into a convergence,
from a task that failed on every host at once because `any_errors_fatal` is on.

The list of variables is curated rather than guessed, the way the playbook
catalogue is. A heuristic over every string that contains a slash would call
`/dev/disk/by-path/...` a missing file on every hypervisor in the inventory.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

from app.inventory.resolve import resolve


class Shape(str, Enum):
    """How the variable carries its paths."""

    SCALAR = "scalar"
    LIST = "list"
    SRC_LIST = "src_list"
    """A list of mappings, each with a `src`, which is `upload_extra_files`."""
    MAPPING = "mapping"
    """A mapping with one key that is a path, which is `cloud_init`."""


@dataclass(frozen=True)
class Variable:
    name: str
    shape: Shape
    role: str
    key: str = ""
    """The key holding the path, for `MAPPING`."""


# Read off the roles of the collection, task by task. Each entry is a variable
# a site writes in its inventory and a role hands to `copy`, `template` or
# `unarchive` with the control machine's filesystem behind it.
KNOWN: tuple[Variable, ...] = (
    Variable("upload_extra_files_upload_files", Shape.SRC_LIST, "upload_extra_files"),
    Variable("iptables_rules_path", Shape.SCALAR, "iptables"),
    Variable("iptables_rules_template_path", Shape.SCALAR, "iptables"),
    Variable("syslog_conf_template", Shape.SCALAR, "syslog_ng_client"),
    Variable("syslog_tls_ca", Shape.SCALAR, "syslog_ng_client"),
    Variable("syslog_tls_key", Shape.SCALAR, "syslog_ng_client"),
    Variable("syslog_tls_server_ca", Shape.SCALAR, "syslog_ng_client"),
    Variable("cephadm_spec_path", Shape.SCALAR, "cephadm"),
    Variable("configure_hypervisor_tuned_path", Shape.SCALAR, "configure_hypervisor"),
    Variable("hosts_path", Shape.SCALAR, "network_buildhosts"),
    Variable("update_swu_image_path", Shape.SCALAR, "update"),
    Variable("vm_disk", Shape.SCALAR, "deploy_vms"),
    Variable("vm_template", Shape.SCALAR, "deploy_vms"),
    # The libvirt XML a guest names when it is not rendered from a template.
    # `deploy_vms_cluster` reads it with `lookup('file')` rather than through
    # `copy`, which resolves the same way and fails just as hard.
    Variable("xml_path", Shape.SCALAR, "deploy_vms"),
    Variable("additional_disk", Shape.LIST, "deploy_vms"),
    Variable("cloud_init", Shape.MAPPING, "cloud_init_seed", key="user_data_file"),
)

# Where a relative `src` is looked for, in the order `path_dwim_relative_stack`
# looks. `dirname` is the subdirectory the action plugin prepends: `files` for
# `copy` and `unarchive`, `templates` for `template` and the template lookup.
# Both are tried here because the variable does not say which module will
# receive it.
_DIRNAMES = ("files", "templates")
_PLAYBOOKS = "playbooks"


class Where(str, Enum):
    INVENTORY = "inventory"
    ARTEFACTS = "artefacts"
    COLLECTION = "collection"
    NODE = "node"
    """An absolute path, found on this machine's own filesystem."""


class Reference(BaseModel):
    """One path an inventory names, and what a run would make of it."""

    host: str
    variable: str
    value: str
    found: bool
    where: Where | None = None
    resolved: str | None = None
    expected: str | None = None
    """Where to upload it, when it is missing and can be named."""


class Roots(BaseModel):
    """The trees a run overlays, in the order the overlay applies."""

    inventory: Path
    artefacts: Path | None = None
    collection: Path | None = None
    """The installed `seapath.ansible`, which ships files of its own."""


def references(document: str | dict[str, Any]) -> list[tuple[str, Variable, str]]:
    """Every (host, variable, path) the inventory declares.

    Resolved the way Ansible resolves variables, groups included, because a
    site that sets `upload_extra_files_upload_files` once on `all` sets it on
    every machine and the answer has to be the same.
    """
    found: list[tuple[str, Variable, str]] = []
    for host, variables in resolve(document).items():
        for variable in KNOWN:
            if variable.name not in variables:
                continue
            for value in _paths(variable, variables[variable.name]):
                found.append((host, variable, value))
    return found


def check(document: str | dict[str, Any], roots: Roots) -> list[Reference]:
    """What a run would find, one line per path the inventory names."""
    return [
        _locate(host, variable, value, roots)
        for host, variable, value in references(document)
    ]


def missing(document: str | dict[str, Any], roots: Roots) -> list[Reference]:
    return [reference for reference in check(document, roots) if not reference.found]


def _paths(variable: Variable, value: Any) -> Iterator[str]:
    if variable.shape is Shape.SCALAR:
        if isinstance(value, str) and value.strip():
            yield value.strip()
    elif variable.shape is Shape.LIST:
        for item in value if isinstance(value, list) else []:
            if isinstance(item, str) and item.strip():
                yield item.strip()
    elif variable.shape is Shape.SRC_LIST:
        for item in value if isinstance(value, list) else []:
            source = item.get("src") if isinstance(item, dict) else None
            if isinstance(source, str) and source.strip():
                yield source.strip()
    elif variable.shape is Shape.MAPPING and isinstance(value, dict):
        source = value.get(variable.key)
        if isinstance(source, str) and source.strip():
            yield source.strip()


def _locate(host: str, variable: Variable, value: str, roots: Roots) -> Reference:
    reference = Reference(host=host, variable=variable.name, value=value, found=False)

    if "{{" in value:
        # A templated path is a question for Ansible at run time, and guessing
        # at its value would produce a confident wrong answer.
        reference.found = True
        reference.where = None
        return reference

    if value.startswith("/") or value.startswith("~"):
        path = Path(os.path.expanduser(value))
        reference.found = path.exists()
        reference.where = Where.NODE if reference.found else None
        reference.resolved = str(path)
        return reference

    relative = _within_site_root(value)
    reference.expected = relative.as_posix() if relative is not None else None

    for root, where in (
        (roots.inventory, Where.INVENTORY),
        (roots.artefacts, Where.ARTEFACTS),
        (roots.collection, Where.COLLECTION),
    ):
        if root is None:
            continue
        hit = _first_existing(root, value)
        if hit is not None:
            reference.found = True
            reference.where = where
            reference.resolved = str(hit)
            return reference

    if roots.collection is not None:
        hit = _in_a_role(roots.collection, value)
        if hit is not None:
            reference.found = True
            reference.where = Where.COLLECTION
            reference.resolved = str(hit)
    return reference


def _candidates(value: str) -> Iterator[PurePosixPath]:
    """The paths Ansible tries, relative to the root a run mounts.

    The basedir of a play is the directory its playbook sits in, so every
    candidate starts from `playbooks/`. That is the whole reason the inventory
    folder is mounted where a checkout of `seapath-ansible` would be: it makes
    `../files/guest.qcow2` mean the same thing here as it does there.
    """
    for dirname in _DIRNAMES:
        yield PurePosixPath(os.path.normpath(f"{_PLAYBOOKS}/{dirname}/{value}"))
    yield PurePosixPath(os.path.normpath(f"{_PLAYBOOKS}/{value}"))


def _within_site_root(value: str) -> PurePosixPath | None:
    """The name to store the file under, or None when it escapes the root."""
    candidate = PurePosixPath(os.path.normpath(f"{_PLAYBOOKS}/{value}"))
    if candidate.parts and candidate.parts[0] == "..":
        return None
    return candidate


def _first_existing(root: Path, value: str) -> Path | None:
    for candidate in _candidates(value):
        if candidate.parts and candidate.parts[0] == "..":
            continue
        path = root / candidate
        if path.exists():
            return path
    return None


def _in_a_role(collection: Path, value: str) -> Path | None:
    """A file the collection's own roles ship, which several defaults name.

    `syslog_conf_template` defaults to `syslog-ng.conf.j2`, which lives in the
    role's `templates/`. Reporting that as missing would be wrong and loud.
    """
    roles = collection / "roles"
    if not roles.is_dir():
        return None
    for dirname in _DIRNAMES:
        for path in sorted(roles.glob(f"*/{dirname}/{value}")):
            return path
    return None
