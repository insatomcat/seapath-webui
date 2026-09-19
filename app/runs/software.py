# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The software of the machines: what an upgrade would bring, and the upgrade.

Two acts, and they are different shapes.

**Updating** is `seapath_update_debian.yaml`, a playbook of the collection,
unchanged, launched from its reviewed catalogue entry. It snapshots the root
volume, runs `apt-get dist-upgrade`, and reboots a machine only when that
brings it a new kernel, with the GRUB boot counter armed first. A cluster
member is put in standby first, one machine at a time. Once its new system is
up, the machine itself removes the snapshot and leaves standby, so the run of
the machine serving this page can schedule its reboot and end. What it does is
the playbook's, and the page only chooses which machines it is sent to.

**Checking** has no playbook upstream, so it is a play generated here, the
shape D30 settles for acts no playbook covers: a handful of tasks, each an
ordinary module or a command given as `argv`, and the ordinary run path
around them. It refreshes the package lists, which is `apt-get update` and
changes nothing a machine runs, and asks apt what a `dist-upgrade` would do
with `-s`, which simulates. The kernel the machine booted and the kernels
it has installed are read beside it, because a kernel installed and not yet
booted is the case where the machine is behind without a single package
pending.

What each machine answered is written by the controller into the run's own
results directory, one JSON document per machine, and read back from there.
Nothing is written on a machine, and the answer is kept, listed and deleted
with the run that produced it, like a measurement.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.inventory.resolve import ROOT
from app.runs import catalogue
from app.runs.catalogue import (
    COLLECTION,
    COLLECTION_PLAYBOOKS,
    PlaybookEntry,
    Precondition,
    Preview,
    Reboots,
)

# The recorded playbook name, as `actions.py` records its own. Deliberately not
# `seapath.ansible.*`: this play was written here.
GENERATOR = "seapath-webui"

CHECK = "software_check"
"""What the check run is filed under, and the name the page finds it by."""

UPDATE = "seapath_update_debian"
"""The catalogue entry the update is launched from."""

RESULTS_VARIABLE = "software_results"
"""Where the controller writes what each machine answered."""

_FILE = re.compile(r"^software_(?P<host>.+)\.json$")

# `C`, so the lines are apt's own and not a translation of them. The parser
# below reads `Inst` and `Remv`, which a French locale would still print, but
# the error messages an operator reads would not be the ones a search finds.
_ENVIRONMENT = {"LC_ALL": "C"}

# The volume group `seapath_update_debian` snapshots root in, as it names it.
_VG = "{{ vg_name | default('vg1') }}"

PENDING = "/boot/efi/seapath_update"
"""Where the update leaves what the machine undoes itself after the reboot.

One empty file per thing to undo: `standby` and `noout`. `system_check`, of the
`debian_grub_bootcount` role, removes each once it is done. A playbook that
names this directory is one whose run may end with the reboot of the machine
driving it.
"""

DEFER_REBOOT = "defer_reboot"
"""What has the update stop short of the reboot it decided.

Given when the machine updated is the one driving the run, which cannot wait
for its own reboot. The playbook then leaves `update_debian_reboot` saying
whether the machine has to reboot, and the play that follows it here checks
the machines and schedules that reboot, a few seconds after the run ends.
"""

UPDATE_AND_CHECK = "software_update"
"""What an update launched from the Updates page is filed under.

The upstream playbook followed by the check, as one generated play, so the
table shows the machines as the update left them.
"""

REBOOT_DELAY = 15
"""Seconds between the end of the run and the reboot it scheduled.

Enough for the run to write its status before the machine goes down."""

SNAPSHOT_MIN_BYTES = 2 * 1024**3
"""The least room the update accepts for its snapshot.

The playbook's own default for `snapshot_min_size_gib`, repeated here so the
page can say before the run what the run would refuse. Below it the snapshot
would fill up during the upgrade and could not be rolled back to.
"""


# What the check keeps of each machine, as one Jinja mapping serialised by
# `to_json`, which keeps a boolean a boolean and a list a list. Every value
# has a default, because the tasks that read it may have failed: a machine
# whose mirror is unreachable is an ordinary answer and has to reach the page.
_READING = (
    "{"
    "'refresh_failed': (software_refresh is failed), "
    "'refresh_message': (software_refresh.msg | default('')), "
    "'simulation': (software_simulation.stdout | default('')), "
    "'simulation_error': (software_simulation.stderr | default('')), "
    "'simulation_message': (software_simulation.msg | default('')), "
    "'simulation_rc': (software_simulation.rc | default(-1)), "
    "'running_kernel': (software_kernel.stdout | default('')), "
    "'kernels': (software_kernels.stdout_lines | default([])), "
    "'reboot_required': "
    "((software_reboot.stat | default({})).exists | default(false)), "
    "'vg': (vg_name | default('vg1')), "
    "'volumes': (software_volumes.stdout_lines | default([])), "
    "'vg_free': (software_vg.stdout | default('')), "
    "'vg_message': (software_vg.stderr | default(software_vg.msg | default(''))), "
    "'bootcount': (software_bootcount.stdout | default('')), "
    "'pending': (software_pending.stdout_lines | default([])), "
    "'awaiting_reboot': ((update_debian_reboot | default(false) | bool) "
    "and (" + DEFER_REBOOT + " | default(false) | bool))"
    "}"
)


def check_entry() -> PlaybookEntry:
    """The catalogue shape of the check.

    Never in the catalogue and never offered on the Deployment page, for the
    reason `actions.entry` gives: what the shape is for is the preconditions,
    the lock and the record, which a check wants exactly as a convergence
    does. `targets` is every machine, and the default scope then leaves the
    guests out the way it does for every run.
    """
    return PlaybookEntry(
        id=CHECK,
        playbook=f"{GENERATOR}.{CHECK}",
        title="Check the machines for software updates",
        targets=[ROOT],
        # Nothing to preview. The play refreshes the package lists and asks apt
        # to simulate, which is already the preview of the update.
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=(
            "Refreshes the package lists of every machine from the sources it "
            "is configured with, and asks apt what an upgrade would install, "
            "replace and remove. Nothing is installed and nothing restarts."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        results_variable=RESULTS_VARIABLE,
        reviewed=True,
    )


def _check(name: str) -> dict:
    """The check, as one play.

    Every task that reads the machine carries `ignore_errors`, and the last
    one writes whatever was gathered. A machine whose mirror is unreachable is
    an ordinary answer, and it is the answer the page has to show: dropping the
    machine from the results would read as a machine with nothing to install.
    """
    return {
        "name": name,
        "hosts": ROOT,
        "gather_facts": False,
        "become": True,
        "tasks": [
            {
                "name": "Refresh the package lists",
                "ansible.builtin.apt": {"update_cache": True},
                "environment": _ENVIRONMENT,
                "register": "software_refresh",
                "ignore_errors": True,
                # The lists are a cache. Refreshing them changes nothing a
                # machine runs, and a check that reported every machine as
                # changed would say the opposite.
                "changed_when": False,
            },
            {
                "name": "Ask apt what an upgrade would do",
                "ansible.builtin.command": {
                    # What `seapath_update_debian` runs, simulated.
                    "argv": ["apt-get", "--simulate", "dist-upgrade"],
                },
                "environment": _ENVIRONMENT,
                "register": "software_simulation",
                "ignore_errors": True,
                "changed_when": False,
            },
            {
                "name": "Read the kernel the machine booted",
                "ansible.builtin.command": {"argv": ["uname", "-r"]},
                "register": "software_kernel",
                "ignore_errors": True,
                "changed_when": False,
            },
            {
                "name": "List the kernels installed",
                "ansible.builtin.command": {
                    "argv": [
                        "find",
                        "/boot",
                        "-maxdepth",
                        "1",
                        "-name",
                        "vmlinuz-*",
                    ],
                },
                "register": "software_kernels",
                "ignore_errors": True,
                "changed_when": False,
            },
            {
                # Written by the packages that need a reboot to take effect,
                # on a machine that has the hook installed. A hint, never
                # the only reason given.
                "name": "Look for a pending reboot",
                "ansible.builtin.stat": {"path": "/run/reboot-required"},
                "register": "software_reboot",
                "ignore_errors": True,
            },
            {
                # What the update's snapshot is sized from, and a snapshot an
                # update left behind, which it refuses.
                "name": "Read the root volume group",
                "ansible.builtin.command": {
                    "argv": [
                        "lvs",
                        "--noheadings",
                        "--nosuffix",
                        "--units",
                        "b",
                        "--separator",
                        ",",
                        "-o",
                        "lv_name,lv_size",
                        _VG,
                    ],
                },
                "register": "software_volumes",
                "ignore_errors": True,
                "changed_when": False,
            },
            {
                # The room the snapshot is taken from.
                "name": "Read the room left in the root volume group",
                "ansible.builtin.command": {
                    "argv": [
                        "vgs",
                        "--noheadings",
                        "--nosuffix",
                        "--units",
                        "b",
                        "--separator",
                        ",",
                        "-o",
                        "vg_free",
                        _VG,
                    ],
                },
                "register": "software_vg",
                "ignore_errors": True,
                "changed_when": False,
            },
            {
                # Armed by the update and disabled by the machine once its
                # new system is up. Still armed afterwards, the machine
                # did not finish its update.
                "name": "Read the boot counter",
                "ansible.builtin.command": {
                    "argv": ["grub-editenv", "/boot/efi/bootcountenv", "list"],
                },
                "register": "software_bootcount",
                "ignore_errors": True,
                "changed_when": False,
            },
            {
                # Absent on a machine no update has touched, which `find`
                # reports as an error and the reading as nothing left.
                "name": "List what an update left the machine to undo",
                "ansible.builtin.command": {
                    "argv": ["find", PENDING, "-type", "f"],
                },
                "register": "software_pending",
                "ignore_errors": True,
                "changed_when": False,
            },
            {
                "name": "Keep what the machine answered",
                "ansible.builtin.copy": {
                    "content": "{{ " + _READING + " | to_json }}",
                    "dest": (
                        "{{ " + RESULTS_VARIABLE + " }}/"
                        "software_{{ inventory_hostname }}.json"
                    ),
                    "mode": "0644",
                },
                # Written by the controller, into this run's own directory.
                # Nothing about the check is left on the machine.
                "delegate_to": "localhost",
                "become": False,
            },
        ],
    }


def check_play() -> str:
    """The check, as YAML."""
    document = [_check(check_entry().title)]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def update_entry() -> PlaybookEntry:
    """The catalogue shape of an update launched from the Updates page.

    The reviewed entry of the upstream playbook, filed under its own id: the
    generated play is written into the run's `playbooks/` under that id, beside
    the upstream file it imports. What the entry says about the preconditions,
    the disruption and the machine driving the run is the playbook's.
    """
    entry = catalogue.get(UPDATE)
    assert entry is not None
    return entry.model_copy(
        update={
            "id": UPDATE_AND_CHECK,
            "playbook": f"{GENERATOR}.{UPDATE_AND_CHECK}",
            "results_variable": RESULTS_VARIABLE,
        }
    )


def update_play(this_host: str | None) -> str:
    """The upstream update, then the check, as YAML.

    With `this_host`, the machine driving the run and then the only one in it,
    the update stops short of the reboot it decides, since the run could not
    outlive it. The check reads the machine before that reboot, and the last
    play schedules it, so the run ends first and with its status. The decision
    to reboot stays the playbook's: the play only reads `update_debian_reboot`.

    The check plays the machines the run was narrowed to, and a machine the
    update never reached is not checked, since a failure stops the run.
    """
    upgrade: dict = {"import_playbook": f"{COLLECTION}.{UPDATE}"}
    if this_host is not None:
        upgrade["vars"] = {DEFER_REBOOT: True}
    document = [upgrade, _check("Check the machines the update left")]
    if this_host is not None:
        document.append(
            {
                "name": f"Reboot {this_host} into its new kernel",
                "hosts": this_host,
                "gather_facts": False,
                "become": True,
                "tasks": [
                    {
                        "name": "Schedule the reboot and end the run",
                        "ansible.builtin.command": {
                            "argv": [
                                "systemd-run",
                                f"--on-active={REBOOT_DELAY}",
                                "systemctl",
                                "reboot",
                            ],
                        },
                        "changed_when": True,
                        "when": "update_debian_reboot | default(false) | bool",
                    }
                ],
            }
        )
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def _installed(collections_path: Path) -> Path:
    return Path(collections_path).joinpath(*COLLECTION_PLAYBOOKS, f"{UPDATE}.yaml")


def updates_itself(collections_path: Path) -> bool:
    """Whether the installed update playbook lets a machine finish at boot.

    Only then can the machine serving this page be updated from it: the run
    ends with the reboot, and what used to follow on the controller, the
    snapshot removed, the GRUB password back, the member online and `noout`
    cleared, is done by the machine once its new system is up. A playbook from
    before that finishes on the controller, and would leave all of it undone.
    Read off the file, like `one_at_a_time`.
    """
    try:
        return PENDING in _installed(collections_path).read_text()
    except OSError:
        return False


def reboots_for_kernel(collections_path: Path) -> bool:
    """Whether the installed update playbook reboots a machine only for a kernel.

    Such a playbook reboots a machine the upgrade gives a new kernel, or that
    has one installed and not booted, and leaves the others running. It can
    also stop short of the reboot of the machine driving the run, which the
    play generated here then schedules. One from before that reboots every
    machine. Read off the file, like `one_at_a_time`.
    """
    try:
        return DEFER_REBOOT in _installed(collections_path).read_text()
    except OSError:
        return False


def one_at_a_time(collections_path: Path) -> bool:
    """Whether the installed update playbook takes the machines one by one.

    `seapath_update_debian` gained `serial: 1` and the standby of a cluster
    member in the same change, and a collection from before it upgrades and
    reboots every machine it is sent to at once, with their guests still on
    them. The page sends such a playbook one machine at a time. Read off the
    file rather than assumed from a version: a site builds the image from the
    branch it likes, and the version in `galaxy.yml` does not follow branches.
    """
    try:
        plays = yaml.safe_load(_installed(collections_path).read_text())
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(plays, list):
        return False
    return any(
        isinstance(play, dict) and "tasks" in play and play.get("serial") == 1
        for play in plays
    )


class Package(BaseModel):
    name: str
    current: str | None = Field(
        default=None, description="The version installed, absent for a new package"
    )
    candidate: str | None = Field(
        default=None, description="The version the upgrade installs"
    )
    origin: str = Field(default="", description="Where apt takes it from")
    kernel: bool = Field(
        default=False, description="Whether this package is a kernel image"
    )


class Simulation(BaseModel):
    upgrades: list[Package] = Field(default_factory=list)
    installs: list[Package] = Field(default_factory=list)
    removals: list[Package] = Field(default_factory=list)


class SnapshotRoom(BaseModel):
    """What the update's snapshot of root will find in the volume group."""

    vg: str
    free_bytes: int | None = None
    root_bytes: int | None = None
    leftover: bool = Field(
        default=False,
        description="A root-snap an earlier update left, which the update refuses",
    )
    enough: bool = Field(
        default=True, description="Whether the update would accept the room"
    )
    note: str | None = None


class MachineReading(BaseModel):
    """What one machine answered to the check."""

    host: str
    refresh_error: str | None = Field(
        default=None,
        description=(
            "Why the package lists could not be refreshed. What follows is then "
            "measured against the lists the machine already had."
        ),
    )
    error: str | None = Field(
        default=None, description="Why apt could not say what an upgrade would do"
    )
    simulation: Simulation = Field(default_factory=Simulation)
    running_kernel: str | None = None
    newest_kernel: str | None = None
    kernel_pending: bool = Field(
        default=False,
        description="A newer kernel is installed than the one the machine booted",
    )
    reboot_required: bool = False
    snapshot: SnapshotRoom | None = Field(
        default=None,
        description="Whether the update has room for its snapshot of root",
    )
    unfinished: str | None = Field(
        default=None,
        description=(
            "What an earlier update left undone on the machine after its "
            "reboot, absent when it finished"
        ),
    )
    awaiting_reboot: bool = Field(
        default=False,
        description=(
            "Read by the update of the machine driving it, just before the "
            "reboot that run scheduled, which finishes the update"
        ),
    )


# `Inst <name> [<installed>] (<candidate> <origins> [<arch>])`, the installed
# version absent when the package is new. The origins are a comma separated
# list, `Debian:13.1/stable, Debian-Security:13/stable-security` when a version
# is in both.
_INST = re.compile(
    r"^Inst (?P<name>\S+)(?: \[(?P<current>[^\]]+)\])? "
    r"\((?P<candidate>\S+)(?: (?P<origin>.*?))?(?: \[[^\]]+\])?\)"
)
# `Remv <name> [<installed>]`.
_REMV = re.compile(r"^Remv (?P<name>\S+)(?: \[(?P<current>[^\]]+)\])?")

_KERNEL_PACKAGE = re.compile(r"^linux-image-")


def parse_simulation(text: str) -> Simulation:
    """What `apt-get --simulate dist-upgrade` says it would do.

    `Conf` lines are dropped: every installed package is configured once, and
    listing them again says nothing the `Inst` lines did not.
    """
    simulation = Simulation()
    for line in text.splitlines():
        installed = _INST.match(line)
        if installed:
            package = Package(
                name=installed.group("name"),
                current=installed.group("current"),
                candidate=installed.group("candidate"),
                origin=(installed.group("origin") or "").strip(),
                kernel=bool(_KERNEL_PACKAGE.match(installed.group("name"))),
            )
            if package.current is None:
                simulation.installs.append(package)
            else:
                simulation.upgrades.append(package)
            continue
        removed = _REMV.match(line)
        if removed:
            simulation.removals.append(
                Package(
                    name=removed.group("name"),
                    current=removed.group("current"),
                    kernel=bool(_KERNEL_PACKAGE.match(removed.group("name"))),
                )
            )
    return simulation


def kernel_key(release: str) -> tuple:
    """A sort key for kernel releases, `6.12.43+deb13-rt-amd64` and the like.

    Numbers compared as numbers, so 6.12.9 sorts before 6.12.43, and the rest
    as text. Enough to find the newest of the kernels one machine holds, which
    are the same flavour from the same archive, and not a Debian version
    comparison.
    """
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.findall(r"\d+|[^\d]+", release)
    )


def parse_reading(host: str, raw: str) -> MachineReading:
    """One machine's JSON document, as the check's last task wrote it."""
    try:
        data = json.loads(raw)
    except ValueError as error:
        return MachineReading(host=host, error=f"The answer is unreadable: {error}")
    if not isinstance(data, dict):
        return MachineReading(host=host, error="The answer is unreadable.")

    reading = MachineReading(host=host)
    if data.get("refresh_failed"):
        reading.refresh_error = (
            str(data.get("refresh_message") or "").strip() or "apt-get update failed."
        )

    rc = data.get("simulation_rc")
    if rc not in (0, "0"):
        reading.error = (
            str(data.get("simulation_error") or "").strip()
            or str(data.get("simulation_message") or "").strip()
            or "apt-get could not simulate the upgrade."
        )
    else:
        reading.simulation = parse_simulation(str(data.get("simulation") or ""))

    running = str(data.get("running_kernel") or "").strip()
    reading.running_kernel = running or None
    releases = [
        Path(str(path)).name.removeprefix("vmlinuz-")
        for path in data.get("kernels") or []
        if Path(str(path)).name.startswith("vmlinuz-")
    ]
    if releases:
        reading.newest_kernel = max(releases, key=kernel_key)
    reading.kernel_pending = bool(
        running and reading.newest_kernel and reading.newest_kernel != running
    )
    reading.reboot_required = bool(data.get("reboot_required"))
    reading.awaiting_reboot = bool(data.get("awaiting_reboot"))
    if reading.awaiting_reboot:
        # The snapshot, the armed counter and the standby are the update's
        # own, which the machine clears at the reboot that follows. Said as an
        # update left undone, or as a snapshot the next update refuses, they
        # would read as a failure.
        return reading
    reading.snapshot = parse_room(data)
    reading.unfinished = parse_unfinished(data)
    return reading


# What each file under `PENDING` says the machine has not done yet.
_UNDONE = {
    "standby": "leave the standby the update put it in",
    "noout": "clear the Ceph noout flag the update set",
}


def parse_unfinished(data: dict) -> str | None:
    """What the machine was left to do after the reboot and has not done.

    The boot counter still armed, and the files the update left under
    `PENDING`. The snapshot left behind is `SnapshotRoom.leftover`, said
    beside the room it takes. Absent from an answer written before the check
    read them, and from a machine whose last update finished.
    """
    undone = []
    counter = re.search(r"^bootcount=(\d+)$", str(data.get("bootcount") or ""), re.M)
    if counter:
        undone.append(f"disable its boot counter, still at {counter.group(1)}")
    for path in data.get("pending") or []:
        name = Path(str(path)).name
        undone.append(_UNDONE.get(name, f"undo {name}"))
    if not undone:
        return None
    return (
        "The last update rebooted this machine and it did not "
        + " or ".join(undone)
        + ". journalctl -t system_check on the machine says why."
    )


def parse_room(data: dict) -> SnapshotRoom | None:
    """The room for the snapshot, and whether the update would refuse it.

    The same two refusals the playbook makes before it touches anything, so
    the page can say them before the run: a `root-snap` left by an update that
    never finished, and less room than `SNAPSHOT_MIN_BYTES`. Absent from an
    answer written before the check read the volume group.
    """
    if "vg" not in data:
        return None
    room = SnapshotRoom(vg=str(data.get("vg") or "vg1"))
    free = str(data.get("vg_free") or "").strip()
    if not free.isdigit():
        message = str(data.get("vg_message") or "").strip()
        room.note = message or f"The volume group {room.vg} could not be read."
        room.enough = False
        return room
    room.free_bytes = int(free)
    for line in data.get("volumes") or []:
        name, _, size = str(line).strip().partition(",")
        if name == "root" and size.strip().isdigit():
            room.root_bytes = int(size)
        elif name == "root-snap":
            room.leftover = True
    if room.leftover:
        room.enough = False
        room.note = (
            f"{room.vg}/root-snap is left from an update that did not finish. "
            "The update refuses to run until it is removed."
        )
    elif room.free_bytes < SNAPSHOT_MIN_BYTES:
        room.enough = False
        room.note = (
            f"{room.vg} has {room.free_bytes / 1024**3:.1f} GiB free, and the "
            f"snapshot the update takes of root needs at least "
            f"{SNAPSHOT_MIN_BYTES // 1024**3} GiB."
        )
    elif room.root_bytes is not None and room.free_bytes < room.root_bytes:
        room.note = (
            f"{room.vg} has {room.free_bytes / 1024**3:.1f} GiB free, less than "
            f"root's {room.root_bytes / 1024**3:.1f} GiB. The snapshot takes "
            "what is free, and an upgrade writing more than that could not be "
            "rolled back."
        )
    return room


def read(files: list[Path]) -> dict[str, MachineReading]:
    """Every machine's answer a check run brought back, by host name."""
    readings: dict[str, MachineReading] = {}
    for path in files:
        match = _FILE.match(path.name)
        if match is None:
            continue
        host = match.group("host")
        try:
            raw = path.read_text(errors="replace")
        except OSError as error:
            readings[host] = MachineReading(host=host, error=str(error))
            continue
        readings[host] = parse_reading(host, raw)
    return readings
