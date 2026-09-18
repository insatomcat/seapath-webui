# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The local volumes a machine carries beyond what its installer laid out.

The SEAPATH ISO writes a 50 GiB system partition whatever the size of the disk,
so a machine installed on a 1 TB disk has most of it unallocated, and the root
file system it does have is a few tens of gigabytes. Anything that needs local
room, a backup staging directory first of all, used to mean partitioning,
formatting and mounting by hand, on each machine, with nothing in the inventory
saying it had been done.

`configure_local_storage` is the upstream role that does it from the
inventory, and this module is the two halves around it that belong here:

- **Reading what a machine has**, over the same one SSH connection the backup
  listing uses: its disks, the free space after the last partition of each,
  its volume groups, and the stable name of every disk. A read, like
  [D54](../../docs/decisions.md)'s, and for the same reason: nothing about it
  changes the machine.
- **Declaring a new volume**, which is one entry appended to that machine's
  `configure_local_storage_volumes`, as one commit. What creates the partition
  is a run of `seapath_setup_local_storage` narrowed to that machine, through
  the ordinary run path. This service writes no partition table, and the rule
  it would be breaking is the first one it has.

The checks below mirror the role's, so a value the role would refuse is
refused here with a sentence, before a commit and a run. The role checks again
on the machine, against the disk as it is at that moment, and its refusals are
the ones that count.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.hosts.remote import RemoteRefused, RemoteRequest, RemoteRunner
from app.inventory.editor import Scope
from app.inventory.repository import Commit
from app.inventory.resolve import resolve
from app.inventory.service import ImportRefused, InventoryService
from app.runs.service import RunPaths

logger = logging.getLogger(__name__)

# The role's own variable.
VARIABLE = "configure_local_storage_volumes"

# The playbook that applies it, on every machine of the inventory unless the
# run is narrowed. The page always narrows it to the machine it declared on.
PLAYBOOK = "seapath_setup_local_storage"

# The role refuses a partition smaller than this, and a disk with less free
# space than this after its last partition is not offered.
MINIMUM_BYTES = 1 << 30

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
_DISK = re.compile(r"^/dev/[A-Za-z0-9_.:/+-]+$")
_MOUNTPOINT = re.compile(r"^(/[A-Za-z0-9_.-]+)+$")
_SIZE = re.compile(r"^(100%|[1-9][0-9]*[MGT])$")
_LVM_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
_FSTYPES = ("ext4", "xfs")

# Where a mount would hide the system. The role refuses a directory that is
# not empty, which catches these on any real machine; naming them here says why
# before a run is spent finding out.
_RESERVED = frozenset(
    {
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/proc",
        "/root",
        "/run",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
        "/var/lib",
        "/var/log",
    }
)

# What the machine is asked, as root: `parted` and `vgs` need it. Four sections,
# each marked, so one tool missing or failing leaves the others readable.
#
# `lsblk` gives the tree, which is what says a disk holds the running system.
# `parted -m print free` gives the free space after the last partition, which
# no other tool reports. `vgs` gives the groups a new partition can join. The
# by-path links are the stable names the inventory uses for disks, the way
# `ceph_osd_disks` already names them.
_SCRIPT = (
    "echo @@lsblk; "
    "lsblk -J -b -o NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,MODEL 2>/dev/null; "
    "echo @@parted; "
    "lsblk -dnpo NAME,TYPE | while read -r n t; do "
    '[ "$t" = disk ] || continue; '
    'echo "@disk $n"; '
    'parted -m -s "$n" unit B print free 2>/dev/null; '
    "done; "
    "echo @@vgs; "
    "vgs --reportformat json --units b --nosuffix "
    "-o vg_name,vg_size,vg_free 2>/dev/null; "
    "echo @@links; "
    "for l in /dev/disk/by-path/*; do "
    '[ -e "$l" ] || continue; '
    'case "$l" in *-part[0-9]*) continue ;; esac; '
    'printf "%s %s\\n" "$l" "$(readlink -f "$l")"; '
    "done"
)


def reading_command() -> str:
    """The one command a machine is asked, in one reviewable place."""
    return "sudo -n /bin/sh -c " + shlex.quote(_SCRIPT)


class InvalidVolume(Exception):
    """A volume cannot be declared, and the message says which rule refused."""


class LocalDisk(BaseModel):
    """One whole disk of the machine, and whether a volume can go on it."""

    path: str
    """The name the inventory will hold: the by-path link where there is one."""
    device: str
    size_bytes: int = 0
    model: str = ""
    table: str = "unknown"
    """`gpt`, `msdos`, `none` for an empty disk, `whole` for a file system
    written on the disk itself."""
    free_bytes: int = 0
    """The free space after the last partition, which is all the role uses."""
    system: bool = False
    """Something on it is mounted: on a machine from the ISO, the system."""
    ceph: bool = False
    usable: bool = False
    reason: str = ""
    """Why it is not usable, in a sentence."""


class VolumeGroup(BaseModel):
    name: str
    size_bytes: int = 0
    free_bytes: int = 0


class LocalVolume(BaseModel):
    """One entry of `configure_local_storage_volumes`, as the role takes it."""

    name: str
    disk: str
    mountpoint: str
    size: str = "100%"
    fstype: str = "ext4"
    lvm_vg: str = ""
    lvm_lv: str = ""
    mounted: bool | None = None
    """Whether the machine has it mounted there now. `None` before a reading."""


class LocalStorage(BaseModel):
    """What one machine has, and what the inventory declares for it."""

    host: str | None = None
    hosts: list[str] = Field(default_factory=list)
    """The machines a volume can be declared on."""
    disks: list[LocalDisk] = Field(default_factory=list)
    volume_groups: list[VolumeGroup] = Field(default_factory=list)
    declared: list[LocalVolume] = Field(default_factory=list)
    read_at: str | None = None
    note: str = ""


class LocalStorageService:
    def __init__(
        self,
        inventory: InventoryService,
        remote: RemoteRunner,
        keys: RunPaths,
        ansible_user: str,
    ) -> None:
        self._inventory = inventory
        self._remote = remote
        self._keys = keys
        self._ansible_user = ansible_user

    # Reading

    def hosts(self) -> list[str]:
        """The machines of the inventory, which are where a volume can go."""
        state = self._inventory.state()
        if state.inventory is None:
            return []
        return sorted(state.inventory.hosts)

    def read(self, host: str | None = None) -> LocalStorage:
        """The disks and volume groups of one machine, asked now.

        This node when no machine is named and the inventory declares it, the
        first machine otherwise.
        """
        state = self._inventory.state()
        hosts = self.hosts()
        if state.inventory is None or not hosts:
            return LocalStorage(
                note=(
                    "There is no inventory on this node yet, so there is no "
                    "machine to declare a volume on."
                )
            )
        if host is None:
            host = state.this_host if state.this_host in hosts else hosts[0]
        if host not in hosts:
            raise InvalidVolume(f"{host} is not a machine of this inventory.")

        declared = self._declared(host)
        view = LocalStorage(host=host, hosts=hosts, declared=declared)
        node = state.inventory.hosts.get(host)
        address = getattr(node, "ansible_host", None) if node else None
        if not address:
            view.note = f"{host} carries no `ansible_host`, so it cannot be asked."
            return view
        try:
            answer = self._remote.run(
                RemoteRequest(
                    address=str(address),
                    user=self._ansible_user,
                    command=reading_command(),
                    private_key_file=self._keys.private_key_file,
                    known_hosts_file=self._keys.known_hosts_file,
                    extra_key_files=self._keys.extra_key_files(),
                )
            )
        except RemoteRefused as error:
            view.note = f"{host} could not be asked about its disks: {error}"
            return view

        ceph = [str(item) for item in self._variable(host, "ceph_osd_disks") or []]
        disks, groups, mounts = parse_reading(answer, ceph)
        view.disks = disks
        view.volume_groups = groups
        view.read_at = datetime.now(tz=UTC).isoformat()
        for volume in view.declared:
            volume.mounted = volume.mountpoint in mounts
        if not any(disk.usable for disk in disks):
            view.note = (
                f"No disk of {host} has room for a new partition: every one is "
                "full after its last partition, given to Ceph, or carries a "
                "table the role would have to rewrite."
            )
        return view

    # Writing

    def declare(
        self,
        host: str,
        volume: LocalVolume,
        author: str,
        expected_head: str | None = None,
    ) -> Commit | None:
        """Append one volume to the machine's list, as one commit.

        On the machine and never on a group: disks differ from one machine to
        the next, and a by-path name written on a group would partition
        whatever answers to that name on every member.
        """
        if host not in self.hosts():
            raise InvalidVolume(f"{host} is not a machine of this inventory.")
        check(volume)
        existing = self._variable(host, VARIABLE) or []
        if not isinstance(existing, list):
            raise InvalidVolume(
                f"{VARIABLE} on {host} is not a list, so there is no entry to "
                "add this one beside. The Inventory page shows what it holds."
            )
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") == volume.name:
                raise InvalidVolume(
                    f"{host} already declares a volume called {volume.name}. "
                    "The name is how the role finds its partition again, so "
                    "two entries cannot share it."
                )
            if entry.get("mountpoint") == volume.mountpoint:
                raise InvalidVolume(
                    f"{host} already mounts a volume on {volume.mountpoint}."
                )
        entries = [*existing, entry_of(volume)]
        try:
            return self._inventory.write_variables(
                writes=[(Scope("host", host), {VARIABLE: entries})],
                intended={host: {VARIABLE: entries}},
                message=(
                    f"webui: mount a local volume on {volume.mountpoint} " f"of {host}"
                ),
                author=author,
                expected_head=expected_head,
            )
        except ImportRefused as error:
            raise InvalidVolume(str(error)) from error

    # Internals

    def _variable(self, host: str, name: str) -> Any:
        document = self._inventory.raw()
        if not document.strip():
            return None
        return resolve(document).get(host, {}).get(name)

    def _declared(self, host: str) -> list[LocalVolume]:
        volumes = []
        for entry in self._variable(host, VARIABLE) or []:
            if not isinstance(entry, dict):
                continue
            lvm = entry.get("lvm") if isinstance(entry.get("lvm"), dict) else {}
            volumes.append(
                LocalVolume(
                    name=str(entry.get("name", "")),
                    disk=str(entry.get("disk", "")),
                    mountpoint=str(entry.get("mountpoint", "")),
                    size=str(entry.get("size", "100%")),
                    fstype=str(entry.get("fstype", "ext4")),
                    lvm_vg=str(lvm.get("vg", "")),
                    lvm_lv=str(lvm.get("lv", "")),
                )
            )
        return volumes


def entry_of(volume: LocalVolume) -> dict[str, Any]:
    """The entry the role reads, with nothing it would default written out."""
    entry: dict[str, Any] = {"name": volume.name, "disk": volume.disk}
    if volume.size != "100%":
        entry["size"] = volume.size
    if volume.lvm_vg:
        entry["lvm"] = {"vg": volume.lvm_vg, "lv": volume.lvm_lv}
    if volume.fstype != "ext4":
        entry["fstype"] = volume.fstype
    entry["mountpoint"] = volume.mountpoint
    return entry


def check(volume: LocalVolume) -> None:
    """The role's own checks, refused here with the sentence that says why."""
    if not _NAME.match(volume.name):
        raise InvalidVolume(
            "The name is at most 36 letters, digits, dashes or underscores. It "
            "becomes the GPT name of the partition, which is how the role finds "
            "it again on the next run."
        )
    if not _DISK.match(volume.disk):
        raise InvalidVolume("The disk is a path under /dev.")
    if not _MOUNTPOINT.match(volume.mountpoint) or any(
        part in (".", "..") for part in volume.mountpoint.split("/")
    ):
        raise InvalidVolume(
            "The mount point is an absolute path of letters, digits, dots, "
            "dashes and underscores, such as /data."
        )
    if volume.mountpoint in _RESERVED:
        raise InvalidVolume(
            f"{volume.mountpoint} belongs to the system. Mounting a volume "
            "there would hide what the machine runs from, so choose a "
            "directory of its own, such as /data."
        )
    if not _SIZE.match(volume.size):
        raise InvalidVolume(
            "The size is 100% for all the free space, or a number followed by "
            "M, G or T, such as 500G."
        )
    if volume.fstype not in _FSTYPES:
        raise InvalidVolume("The file system is ext4 or xfs.")
    if bool(volume.lvm_vg) != bool(volume.lvm_lv):
        raise InvalidVolume(
            "An LVM volume names both its volume group and its logical volume."
        )
    for value in (volume.lvm_vg, volume.lvm_lv):
        if value and not _LVM_NAME.match(value):
            raise InvalidVolume(
                f"{value} is not a name LVM takes: letters, digits, and "
                "`_.+-`, not starting with a dash."
            )


def parse_reading(
    text: str, ceph_disks: list[str]
) -> tuple[list[LocalDisk], list[VolumeGroup], set[str]]:
    """The four sections of the answer: disks, groups, and what is mounted."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("@@"):
            current = line[2:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    links: dict[str, str] = {}
    for line in sections.get("links", []):
        link, _, device = line.strip().partition(" ")
        if link and device:
            links.setdefault(device, link)
    ceph = {links_target(item, links) for item in ceph_disks}

    tree = _json("\n".join(sections.get("lsblk", [])))
    mounts: set[str] = set()
    whole: dict[str, dict[str, Any]] = {}
    for device in tree.get("blockdevices", []) if isinstance(tree, dict) else []:
        if not isinstance(device, dict):
            continue
        found = set(_mountpoints(device))
        mounts |= found
        if device.get("type") == "disk":
            device["_mounted"] = bool(found)
            whole[str(device.get("path") or "/dev/" + str(device.get("name")))] = device

    tables, free = _parted(sections.get("parted", []))
    disks = []
    for path, device in sorted(whole.items()):
        size = _int(device.get("size"))
        # An empty slot, such as an `nbd` device nothing is attached to.
        if not size:
            continue
        table = tables.get(path, "unknown")
        children = device.get("children") or []
        if table == "unknown":
            table = "whole" if device.get("fstype") else "none"
        elif table == "loop":
            table = "whole"
        disk = LocalDisk(
            path=links.get(path, path),
            device=path,
            size_bytes=size,
            model=str(device.get("model") or "").strip(),
            table=table,
            free_bytes=(
                size if table == "none" and not children else free.get(path, 0)
            ),
            system=bool(device.get("_mounted")),
            ceph=path in ceph,
        )
        disk.usable, disk.reason = _usable(disk, children)
        disks.append(disk)

    groups = []
    report = _json("\n".join(sections.get("vgs", [])))
    for block in report.get("report", []) if isinstance(report, dict) else []:
        for group in block.get("vg", []) if isinstance(block, dict) else []:
            groups.append(
                VolumeGroup(
                    name=str(group.get("vg_name", "")),
                    size_bytes=_int(group.get("vg_size")),
                    free_bytes=_int(group.get("vg_free")),
                )
            )
    return disks, groups, mounts


def links_target(path: str, links: dict[str, str]) -> str:
    """A disk the inventory names by link, as the device the link points at."""
    for device, link in links.items():
        if link == path:
            return device
    return path


def _usable(disk: LocalDisk, children: list) -> tuple[bool, str]:
    if disk.ceph:
        return False, "Given to Ceph in ceph_osd_disks: an OSD owns all of it."
    if disk.table == "msdos":
        return False, (
            "It carries an MBR table, and the role only adds partitions to a "
            "GPT disk: writing one over it would erase what is there."
        )
    if disk.table == "whole":
        return False, "It carries a file system on the whole disk."
    if disk.table == "none" and children:
        return False, "It carries no partition table but is in use."
    if disk.free_bytes < MINIMUM_BYTES:
        return False, "It has no free space after its last partition."
    return True, ""


def _parted(lines: list[str]) -> tuple[dict[str, str], dict[str, int]]:
    """The table of each disk, and the free space after its last partition.

    `parted -m print free` answers one line for the disk, then one per
    partition and one per stretch of free space, in disk order. The last line
    counts only when it is free space and comes after every partition.
    """
    tables: dict[str, str] = {}
    free: dict[str, int] = {}
    disk = None
    last_end = 0
    trailing = 0
    for raw in lines:
        line = raw.strip().rstrip(";")
        if line.startswith("@disk "):
            if disk is not None:
                free[disk] = trailing
            disk = line[len("@disk ") :].strip()
            last_end, trailing = 0, 0
            continue
        if disk is None or not line or line == "BYT":
            continue
        fields = line.split(":")
        if fields[0] == disk and len(fields) > 5:
            tables[disk] = fields[5]
            continue
        if len(fields) < 5 or not fields[0].isdigit():
            continue
        start, end = _bytes(fields[1]), _bytes(fields[2])
        if fields[4] == "free":
            trailing = end - start + 1 if start > last_end else trailing
        else:
            last_end = max(last_end, end)
            trailing = 0
    if disk is not None:
        free[disk] = trailing
    return tables, free


def _mountpoints(device: dict[str, Any]):
    if device.get("mountpoint"):
        yield str(device["mountpoint"])
    for child in device.get("children") or []:
        if isinstance(child, dict):
            yield from _mountpoints(child)


def _json(text: str) -> Any:
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        return {}


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _bytes(value: str) -> int:
    return _int(value.rstrip("B"))


__all__ = [
    "PLAYBOOK",
    "VARIABLE",
    "InvalidVolume",
    "LocalDisk",
    "LocalStorage",
    "LocalStorageService",
    "LocalVolume",
    "VolumeGroup",
    "check",
    "entry_of",
    "parse_reading",
    "reading_command",
]
