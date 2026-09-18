# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The software of the machines: what an upgrade would bring, and the upgrade.

Two acts, and they are different shapes.

**Updating** is `seapath_update_debian.yaml`, a playbook of the collection,
unchanged, launched from its reviewed catalogue entry. It snapshots the root
volume, arms the GRUB boot counter, runs `apt-get dist-upgrade`, writes the
boot menu, reboots, and removes the snapshot once the machine has come back. A
cluster member is put in standby first, one machine at a time. What it does is
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
from app.runs.catalogue import (
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
    "((software_reboot.stat | default({})).exists | default(false))"
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


def check_play() -> str:
    """The check, as YAML.

    Every task that reads the machine carries `ignore_errors`, and the last
    one writes whatever was gathered. A machine whose mirror is unreachable is
    an ordinary answer, and it is the answer the page has to show: dropping the
    machine from the results would read as a machine with nothing to install.
    """
    document = [
        {
            "name": check_entry().title,
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
    ]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def one_at_a_time(collections_path: Path) -> bool:
    """Whether the installed update playbook takes the machines one by one.

    `seapath_update_debian` gained `serial: 1` and the standby of a cluster
    member in the same change, and a collection from before it upgrades and
    reboots every machine it is sent to at once, with their guests still on
    them. The page sends such a playbook one machine at a time. Read off the
    file rather than assumed from a version: a site builds the image from the
    branch it likes, and the version in `galaxy.yml` does not follow branches.
    """
    path = Path(collections_path).joinpath(*COLLECTION_PLAYBOOKS, f"{UPDATE}.yaml")
    try:
        plays = yaml.safe_load(path.read_text())
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
    return reading


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
