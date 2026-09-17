# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The backups: where a site sends them, what they would weigh, what is there.

The upstream `backup_restore` role answers all three questions from a whiptail
menu on a machine. This page answers them from the inventory, from Ceph and
from the backup server, and `app/runs/backup.py` is what turns an answer into a
run. The division is the one the rest of this service already holds to:

- **Where a site sends its backups is desired state**, so it lives in the
  inventory, as seven variables named after the seven keys of
  `/etc/backup-restore.conf`. Editing them is a commit like any other. This
  service never writes that conf file: the file is the menu's, it is written on
  a host, and writing on a host is the one thing this service does not do. What
  a run carries instead is the seven values on the command line, which is the
  interface the scripts were already written against.

- **What a full backup would weigh is a reading of Ceph**, so it is asked of
  Ceph directly, with `rbd du`, over the client D31 already established. The
  upstream `backup_du.py` runs the same command on a machine and parses its
  human readable table; this asks for JSON and sums the same numbers here. It
  reaches no machine and costs no run.

- **What the backup server holds can only be read from a machine.** The SSH
  trust that reaches the backup server belongs to the cluster members and is
  the one the backups are pushed with, and nothing in this container has it. So
  that reading is a run: a short one that asks the server and brings the
  listing back into its own results directory, where this module parses it. It
  is in the history with everything else, which is the right record to keep of
  a service having been asked what it holds.

The layout the listing is parsed against is the scripts' own. One directory per
full backup, named for the minute it started, holding for each guest the qcow2
of every image, the libvirt XML, the metadata, and the diffs of every
incremental backup taken since. Which is to say:

    202603110733/system_vm1_202603110733.qcow2      the full backup
    202603110733/data_vm1_0_202603110733.qcow2      an additional disk
    202603110733/system_vm1-202603110733.xml        the libvirt XML
    202603110733/system_vm1-meta-_priority-...txt   one metadata key
    202603110733/system_vm1_202603110733_202603110836.diff   an increment
    202603110733/system_vm1-202603110836.xml        and its XML

A restore names the full backup directory, the guest and one of the dates whose
XML is there, which is exactly what `restore_vm.sh` takes and what the menu
asks for in three screens.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.errors import ApiError
from app.inventory.editor import Scope
from app.inventory.model import Mode
from app.inventory.repository import Commit
from app.inventory.resolve import groups, members, resolve
from app.inventory.service import ImportRefused, InventoryService
from app.runs import backup as plays
from app.runs.backup import BackupAction, BackupTarget
from app.runs.catalogue import role_present
from app.runs.models import RunRecord, RunState
from app.runs.service import RunService

logger = logging.getLogger(__name__)

# The group the seven variables are written on. The role is installed on every
# machine the prerequisites playbook configures, and the tool only works where
# there is an RBD pool, so the cluster is both where it runs and the smallest
# scope that describes the site rather than one machine.
GROUP = "cluster_machines"

# What the inventory calls each of them: the key of `/etc/backup-restore.conf`,
# prefixed. The prefix is what keeps `remote_dir` and `local_dir` from reading
# as variables about something else in a file that configures a whole site.
PREFIX = "backup_"

# A guest name, as the scripts and `vm_manager` allow. Checked before it
# reaches a command line, which is the second lock: the first is that it came
# out of a listing this service read.
GUEST = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# The minute a backup started, which is what every file and directory is named
# after.
DATE = re.compile(r"^[0-9]{12}$")

# What a path may hold. Deliberately narrow: these become arguments of a shell
# script that expands some of them unquoted, and a directory holding a space is
# not worth the risk on a machine where the alternative is `rm -rf` reaching
# somewhere else.
_PATH = re.compile(r"^(/[A-Za-z0-9._-]+)+/$")

# `[user@]host`, which is what rsync and ssh take before the colon. An address
# is allowed, and so is a name out of the site's `ssh_config`.
_SERVER = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._:-]+$")

# The remote shell, which is `ssh` and the options a site adds to it. It is
# expanded unquoted by the scripts, so it is words, and every word here is one
# an ssh command line has.
_SHELL = re.compile(r"^ssh(?: [A-Za-z0-9@=:,./_-]+)*$")


class InvalidBackupSetting(Exception):
    """One value cannot be written, and the message says which rule refused."""


class BackupSetting(BaseModel):
    """One of the seven, with what it is and what it holds."""

    name: str
    """The inventory variable, `backup_` and the conf file's own key."""
    key: str
    """The key of `/etc/backup-restore.conf` it corresponds to."""
    label: str
    value: str = ""
    required: bool = True
    placeholder: str = ""
    help: str = ""
    source: str | None = None
    """Where the value comes from: `group cluster_machines`, or a machine."""


class GuestVolume(BaseModel):
    """What one guest would weigh in a full backup."""

    guest: str
    images: list[str] = Field(default_factory=list)
    used_bytes: int = 0
    provisioned_bytes: int = 0


class Estimate(BaseModel):
    """`rbd du`, summed per guest, which is the port of `backup_du.py`."""

    guests: list[GuestVolume] = Field(default_factory=list)
    used_bytes: int = 0
    included: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    """The guests the two filters leave out, named so a surprise is visible."""
    error: str | None = None


class GuestBackup(BaseModel):
    """One guest inside one full backup, and the dates it can be restored to."""

    guest: str
    dates: list[str] = Field(default_factory=list)
    """Every date whose libvirt XML is there, newest last.

    The menu asks for one of these and so does this page: `restore_vm.sh`
    replays the diffs up to the date it is given, and the XML of that date is
    what the guest is recreated from, so a date with no XML is not a date the
    guest can be restored to.
    """
    disks: int = 0
    """How many images the full backup carries for it, the system disk included."""


class FullBackup(BaseModel):
    date: str
    guests: list[GuestBackup] = Field(default_factory=list)
    files: int = 0


class BackupCatalogue(BaseModel):
    """What the backup server held, when it was last asked."""

    backups: list[FullBackup] = Field(default_factory=list)
    run_id: str | None = None
    read_at: str | None = None
    state: str | None = None
    note: str = ""


class BackupView(BaseModel):
    """The whole page, in one answer."""

    mode: str = Mode.STANDALONE.value
    configured: bool = False
    settings: list[BackupSetting] = Field(default_factory=list)
    target: str = ""
    """`<server>:<directory>`, the one string that says where backups go."""
    estimate: Estimate = Field(default_factory=Estimate)
    catalogue: BackupCatalogue = Field(default_factory=BackupCatalogue)
    warnings: list[str] = Field(default_factory=list)
    note: str = ""
    commit: str | None = None


# The seven, in the order the form asks for them, which is the order the menu
# lists them in with the two that matter most brought to the front.
_SETTINGS: tuple[tuple[str, str, bool, str, str], ...] = (
    (
        "remote_serv",
        "Backup server",
        True,
        "backup@backup.example.org",
        "The account the backups are pushed to, as ssh and rsync take it. The "
        "cluster members reach it with root's own SSH key, which this service "
        "neither holds nor installs: the trust to the backup server is the "
        "site's to provision, once, on each member.",
    ),
    (
        "remote_dir",
        "Directory on the server",
        True,
        "/srv/seapath-backups/",
        "Where the full backup directories live on that server. It ends with a "
        "slash: the scripts glue the date onto it.",
    ),
    (
        "local_dir",
        "Staging directory on the machines",
        True,
        "/var/lib/seapath-backup/",
        "Where a full backup is assembled before it is sent. A full backup "
        "empties it first, with `rm -rf`, so it has to be a directory of its "
        "own and it has to end with a slash.",
    ),
    (
        "local_tmp_dir",
        "Staging directory for a restore",
        True,
        "/var/lib/seapath-restore/",
        "Where a restore downloads what it needs before applying it. Emptied "
        "at the start of every restore, by the same `rm -rf`.",
    ),
    (
        "remote_shell",
        "Remote shell",
        True,
        "ssh",
        "How the machines reach the backup server. `ssh`, with the options a "
        "site needs, such as `ssh -p 2222`.",
    ),
    (
        "include_vm",
        "Guests to back up",
        False,
        ".*",
        "An extended regular expression matched against guest names. Empty "
        "means every guest.",
    ),
    (
        "exclude_vm",
        "Guests to leave out",
        False,
        "",
        "An extended regular expression, applied after the one above. Empty "
        "means nothing is left out.",
    ),
)

_NO_INVENTORY = (
    "There is no inventory on this node yet, so there is nowhere to say where "
    "the backups go. The Inventory page is where the machines are described."
)
_NOT_A_CLUSTER = (
    "This inventory declares no cluster machine. The backup tool exports RBD "
    "images, and there is no RBD pool outside a cluster: `backup_full.sh` "
    "opens with `rbd list`. Form a cluster first."
)
_NOT_CONFIGURED = (
    "This inventory does not say where the backups go. Fill in the settings "
    "below and they are committed with the rest of the desired state, so a "
    "control machine running the same playbooks reads the same values."
)


class BackupService:
    def __init__(
        self,
        inventory: InventoryService,
        runs: RunService,
        rbd,
        collections_path,
    ) -> None:
        self._inventory = inventory
        self._runs = runs
        self._rbd = rbd
        self._collections_path = collections_path

    # Reading

    def view(self) -> BackupView:
        """Everything the page shows, in one answer.

        Three readings of three different things, and none of them reaches a
        machine: the inventory off the disk, Ceph over its own client, and the
        listing the last listing run brought back.
        """
        state = self._inventory.state()
        if state.inventory is None:
            return BackupView(note=_NO_INVENTORY, settings=self._settings(""))

        document = self._inventory.raw()
        target, sources = self._read(document)
        settings = self._settings(document)
        view = BackupView(
            mode=state.inventory.mode.value,
            configured=self.complete(target),
            settings=settings,
            target=target.destination if target.remote_serv else "",
            catalogue=self.catalogue(),
            commit=state.commit,
        )
        if not state.inventory.cluster_members:
            view.note = _NOT_A_CLUSTER
            return view
        if not view.configured:
            view.note = _NOT_CONFIGURED
        view.estimate = self.estimate(target)
        view.warnings = self._warnings(target, sources)
        return view

    def estimate(self, target: BackupTarget | None = None) -> Estimate:
        """What a full backup would weigh, per guest, from `rbd du`.

        The two filters are applied here exactly as the scripts apply them, to
        the guest name and never to the image name, so a guest excluded on this
        page is a guest the run will skip. An additional disk is counted with
        the guest it belongs to, which is what makes the total the size of a
        backup rather than the size of a pool.
        """
        if target is None:
            target, _ = self._read(self._inventory.raw())
        try:
            usage = self._rbd.disk_usage()
        except Exception as error:
            # Ceph not answering is an ordinary state on a node whose cluster
            # is down, and it must not take the rest of the page with it.
            logger.warning("The backup estimate could not read Ceph: %s", error)
            return Estimate(error=str(error))

        include, exclude = _filters(target)
        if include is None:
            return Estimate(
                error=(
                    f"{target.include_vm!r} is not an extended regular "
                    "expression, so no guest can be matched against it."
                )
            )

        volumes: dict[str, GuestVolume] = {}
        excluded: set[str] = set()
        for image in usage:
            guest = _guest_of(image.image)
            if guest is None:
                continue
            if not include.search(guest) or (exclude and exclude.search(guest)):
                excluded.add(guest)
                continue
            volume = volumes.setdefault(guest, GuestVolume(guest=guest))
            volume.images.append(image.image)
            volume.used_bytes += image.used_bytes
            volume.provisioned_bytes += image.provisioned_bytes

        guests = sorted(volumes.values(), key=lambda item: item.guest)
        for volume in guests:
            volume.images.sort()
        return Estimate(
            guests=guests,
            used_bytes=sum(volume.used_bytes for volume in guests),
            included=[volume.guest for volume in guests],
            excluded=sorted(excluded),
        )

    def catalogue(self) -> BackupCatalogue:
        """What the last listing run found on the backup server.

        Read from the run's own results directory at each request rather than
        remembered, for the reason `latency_results` gives: the record is what
        the run did and this is what it brought back, so a directory reclaimed
        for space reads as a listing that is no longer there, which is what it
        is.
        """
        record = self._last_listing()
        if record is None:
            return BackupCatalogue(
                note=(
                    "The backup server has not been asked what it holds. "
                    "Reading it is a short run from a cluster member, over the "
                    "same SSH the backups are pushed with."
                )
            )
        listing = self._listing_text(record)
        if listing is None:
            return BackupCatalogue(
                run_id=record.id,
                state=record.state.value,
                read_at=record.started_at.isoformat() if record.started_at else None,
                note=(
                    "The last reading of the backup server brought nothing "
                    "back. Its log is on the Runs page."
                ),
            )
        backups = parse_listing(listing)
        return BackupCatalogue(
            backups=backups,
            run_id=record.id,
            state=record.state.value,
            read_at=record.started_at.isoformat() if record.started_at else None,
            note=(
                ""
                if backups
                else (
                    "The backup server answered, and its directory holds no "
                    "backup: nothing has been pushed there yet."
                )
            ),
        )

    # Writing

    def save(
        self, values: dict[str, str], author: str, expected_head: str | None = None
    ) -> Commit | None:
        """Write the seven variables on `cluster_machines`, as one commit.

        Every value is checked first, and the two directories are checked
        hardest: `backup_full.sh` runs `rm -rf "${local_dir}"*`, so a value
        without its trailing slash removes every sibling whose name starts the
        same way, and a short one removes a great deal more than that. The
        sentence a refusal carries says which rule it is and why it exists.
        """
        state = self._inventory.state()
        if state.inventory is None:
            raise InvalidBackupSetting(_NO_INVENTORY)
        if not state.inventory.cluster_members:
            raise InvalidBackupSetting(_NOT_A_CLUSTER)

        document = self._inventory.raw()
        if GROUP not in groups(document):
            raise InvalidBackupSetting(
                f"This inventory declares no `{GROUP}` group, so there is "
                "nowhere to write the backup settings."
            )

        variables: dict[str, object] = {}
        for key, label, required, _placeholder, _help in _SETTINGS:
            value = (values.get(_variable(key)) or values.get(key) or "").strip()
            _check(key, label, value, required)
            variables[_variable(key)] = value

        scope = Scope("group", GROUP)
        hosts = sorted(members(groups(document), GROUP))
        intended = {
            host: {name: value for name, value in variables.items()} for host in hosts
        }
        try:
            return self._inventory.write_variables(
                writes=[(scope, variables)],
                intended=intended,
                message=(
                    "webui: back up "
                    + (variables[_variable("include_vm")] or ".*")
                    + " to "
                    + str(variables[_variable("remote_serv")])
                ),
                author=author,
                expected_head=expected_head,
            )
        except ImportRefused as error:
            raise InvalidBackupSetting(str(error)) from error

    # Launching

    def launch(
        self,
        action: BackupAction,
        author: str,
        guest: str = "",
        full_date: str = "",
        incremental_date: str = "",
    ) -> RunRecord:
        """One backup act, as a run like any other.

        Everything the run needs is checked here rather than by the play: the
        settings are complete, the collection this image ships has the role
        whose scripts the play calls, and a restore names a guest and a date
        that the listing actually carries. A run launched without those fails
        on its first task, minutes after an operator confirmed something
        destructive, which is a late and expensive way to learn it.
        """
        target, _ = self._read(self._inventory.raw())
        missing = self._missing(target)
        if missing:
            raise ApiError(
                "backup_not_configured",
                "This inventory does not say "
                + _list(missing)
                + ". Fill in the backup settings first: they are committed "
                "with the rest of the desired state and passed to the scripts "
                "on the command line.",
                409,
                {"missing": missing},
            )
        divided = self._divided()
        if divided:
            raise ApiError(
                "backup_ambiguous",
                "This inventory writes "
                + _list(divided)
                + " in more than one place, so which value a run would use "
                "depends on the member it is sent to. A backup passes these "
                "on the command line, so the two have to be reconciled on the "
                "Inventory page first.",
                409,
                {"variables": divided},
            )
        if not role_present(self._collections_path(), plays.ROLE):
            raise ApiError(
                "role_missing",
                f"The SEAPATH collection this image ships has no `{plays.ROLE}` "
                "role, so the scripts this run calls are on no machine. They "
                "are installed by the prerequisites playbook of the machine's "
                "distribution.",
                409,
            )

        if action is BackupAction.RESTORE:
            self._check_restore(guest, full_date, incremental_date)

        return self._runs.launch_generated(
            plays.entry(action, guest, incremental_date or full_date),
            author,
            plays.play(action, target, guest, full_date, incremental_date),
            guest=guest or None,
        )

    # Internals

    def _read(self, document: str) -> tuple[BackupTarget, dict[str, str]]:
        """The seven values as the cluster members resolve them.

        Read off the resolved variables of a member rather than off the group's
        own mapping, because a site may have written one of them on a machine
        and Ansible would hand that machine the other value. What the page shows
        is what a run would use, and `sources` says where each came from so a
        value written in two places is visible rather than silently one of them.
        """
        if not document.strip():
            return BackupTarget(), {}
        table = groups(document)
        hosts = sorted(members(table, GROUP))
        if not hosts:
            return BackupTarget(), {}
        resolved = resolve(document)
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        for key, *_ in _SETTINGS:
            name = _variable(key)
            found = {str(resolved.get(host, {}).get(name, "") or "") for host in hosts}
            # The largest of them where they disagree, deliberately arbitrary:
            # the page warns about the disagreement and `launch` refuses to
            # act on it, so the only job left for this value is to be shown.
            values[key] = sorted(found)[-1] if found else ""
            sources[key] = (
                "every machine of " + GROUP
                if len(found) == 1
                else "different values on " + ", ".join(hosts)
            )
        return (
            BackupTarget(
                local_dir=values["local_dir"],
                local_tmp_dir=values["local_tmp_dir"],
                remote_serv=values["remote_serv"],
                remote_dir=values["remote_dir"],
                remote_shell=values["remote_shell"] or "ssh",
                include_vm=values["include_vm"] or ".*",
                exclude_vm=values["exclude_vm"],
            ),
            sources,
        )

    def _settings(self, document: str) -> list[BackupSetting]:
        """The seven fields the form is drawn from.

        A file that says nothing still fills two of them, with `ssh` and `.*`:
        those are the defaults the role writes into the conf file and the ones
        the scripts behave as if they had, so a form that left them blank would
        be asking an operator to invent a value that already exists.
        """
        target, sources = self._read(document)
        held = {key: getattr(target, key, "") for key, *_ in _SETTINGS}
        return [
            BackupSetting(
                name=_variable(key),
                key=key,
                label=label,
                value=held.get(key, ""),
                required=required,
                placeholder=placeholder,
                help=help_text,
                source=sources.get(key),
            )
            for key, label, required, placeholder, help_text in _SETTINGS
        ]

    def _warnings(self, target: BackupTarget, sources: dict[str, str]) -> list[str]:
        warnings = []
        for key, label, required, *_ in _SETTINGS:
            value = getattr(target, key, "")
            if not required or not value:
                continue
            source = sources.get(key, "")
            if source.startswith("different values"):
                warnings.append(
                    f"{label} is written more than once in this inventory: "
                    f"{source}. A run uses the value of the member it is sent "
                    "to, so the two have to be reconciled on the Inventory "
                    "page before this page can say where the backups go."
                )
        if target.exclude_vm and target.include_vm == ".*":
            warnings.append(
                "Every guest is backed up except those matching "
                f"{target.exclude_vm!r}. A guest added later is included by "
                "default, which is usually what a site wants and is worth "
                "knowing."
            )
        return warnings

    def _missing(self, target: BackupTarget) -> list[str]:
        return [
            label
            for key, label, required, *_ in _SETTINGS
            if required and not getattr(target, key, "")
        ]

    def complete(self, target: BackupTarget) -> bool:
        return not self._missing(target)

    def _divided(self) -> list[str]:
        """The settings this inventory writes in more than one place.

        The page warns about them; an act is refused outright. The value goes
        on the command line of a run sent to one member, so a file that holds
        two of them would back up to whichever this service happened to read.
        """
        _, sources = self._read(self._inventory.raw())
        return [
            label
            for key, label, *_ in _SETTINGS
            if sources.get(key, "").startswith("different values")
        ]

    def _check_restore(self, guest: str, full_date: str, date: str) -> None:
        if not GUEST.match(guest or ""):
            raise ApiError(
                "invalid_guest",
                f"{guest!r} is not a guest name this service restores.",
                400,
            )
        if not DATE.match(full_date or "") or not DATE.match(date or ""):
            raise ApiError(
                "invalid_date",
                "A restore names the full backup it comes from and the date "
                "inside it to replay up to. Both are the twelve digit stamps "
                "the backup scripts write.",
                400,
            )
        catalogue = self.catalogue()
        backup = next(
            (item for item in catalogue.backups if item.date == full_date), None
        )
        if backup is None:
            raise ApiError(
                "unknown_backup",
                f"The last reading of the backup server found no backup taken "
                f"at {full_date}. Read the server again: what is offered here "
                "is what it held when it was last asked.",
                409,
            )
        held = next((item for item in backup.guests if item.guest == guest), None)
        if held is None:
            raise ApiError(
                "unknown_guest",
                f"That backup holds no guest called {guest}.",
                409,
            )
        if date not in held.dates:
            raise ApiError(
                "unknown_date",
                f"{guest} cannot be restored to {date} out of that backup. "
                "A restore replays the diffs up to a date whose libvirt XML "
                "is there, and that one has none.",
                409,
                {"dates": held.dates},
            )

    def _last_listing(self) -> RunRecord | None:
        """The newest listing run that finished, whatever it found."""
        for record in self._runs.list(limit=200):
            if record.playbook_id != plays.record(BackupAction.LIST):
                continue
            if record.state in (RunState.RUNNING, RunState.PENDING):
                continue
            return record
        return None

    def _listing_text(self, record: RunRecord) -> str | None:
        for path in self._runs.results(record.id):
            if path.name == plays.LISTING_FILE:
                try:
                    return path.read_text()
                except OSError as error:
                    logger.warning(
                        "The listing of run %s is unreadable: %s", record.id, error
                    )
                    return None
        return None


def _variable(key: str) -> str:
    return f"{PREFIX}{key}"


def _list(items: list[str]) -> str:
    if len(items) == 1:
        return items[0].lower()
    return ", ".join(item.lower() for item in items[:-1]) + " or " + items[-1].lower()


def _filters(target: BackupTarget) -> tuple[re.Pattern | None, re.Pattern | None]:
    """The two patterns, compiled as the scripts' `grep -E` would read them."""
    try:
        include = re.compile(target.include_vm or ".*")
    except re.error:
        return None, None
    try:
        exclude = re.compile(target.exclude_vm) if target.exclude_vm else None
    except re.error:
        # An unreadable exclusion excludes nothing here, and the run would
        # exclude nothing either: `grep -E -v` on a bad pattern fails open on
        # the guest list rather than on the guest.
        exclude = None
    return include, exclude


def _guest_of(image: str) -> str | None:
    """The guest an image belongs to, `backup_du.py`'s own mapping.

    `system_<guest>` is the system disk and `data_<guest>_<n>` an additional
    one. Anything else in the pool is not a guest's disk and is not backed up.
    """
    if image.startswith("system_"):
        return image[len("system_") :] or None
    if image.startswith("data_"):
        rest = image[len("data_") :]
        name, _, index = rest.rpartition("_")
        return name if name and index.isdigit() else None
    return None


# What each file in a backup directory is, read off the name the script wrote.
_FULL = re.compile(r"^(?P<image>.+)_(?P<date>[0-9]{12})\.qcow2$")
_DIFF = re.compile(r"^(?P<image>.+)_(?P<from>[0-9]{12})_(?P<to>[0-9]{12})\.diff$")
_XML = re.compile(r"^system_(?P<guest>.+)-(?P<date>[0-9]{12})\.xml$")


def parse_listing(text: str) -> list[FullBackup]:
    """The backup server's directory, as the page shows it.

    The listing is `dir <name>` and `file <dir>/<name>` lines, which is what
    the one shell command in `app/runs/backup.py` prints. Parsed rather than
    trusted: a name that fits none of the three shapes above is counted and
    otherwise ignored, so a site that keeps something else in that directory
    gets a listing rather than an error.
    """
    backups: dict[str, FullBackup] = {}
    guests: dict[tuple[str, str], GuestBackup] = {}

    for line in text.splitlines():
        kind, _, rest = line.strip().partition(" ")
        if kind == "dir" and rest and DATE.match(rest):
            backups.setdefault(rest, FullBackup(date=rest))
            continue
        if kind != "file" or "/" not in rest:
            continue
        directory, _, name = rest.partition("/")
        if not DATE.match(directory):
            continue
        backup = backups.setdefault(directory, FullBackup(date=directory))
        backup.files += 1

        xml = _XML.match(name)
        if xml:
            guest = _guest(guests, backups, directory, xml.group("guest"))
            date = xml.group("date")
            if date not in guest.dates:
                guest.dates.append(date)
            continue
        full = _FULL.match(name)
        if full:
            owner = _guest_of(full.group("image"))
            if owner:
                _guest(guests, backups, directory, owner).disks += 1

    for backup in backups.values():
        backup.guests.sort(key=lambda item: item.guest)
        for guest in backup.guests:
            guest.dates.sort()
    return sorted(backups.values(), key=lambda item: item.date)


def _guest(
    guests: dict[tuple[str, str], GuestBackup],
    backups: dict[str, FullBackup],
    directory: str,
    name: str,
) -> GuestBackup:
    key = (directory, name)
    if key not in guests:
        guests[key] = GuestBackup(guest=name)
        backups[directory].guests.append(guests[key])
    return guests[key]


def _check(key: str, label: str, value: str, required: bool) -> None:
    """One value, refused with the sentence that says why the rule is there."""
    if not value:
        if required:
            raise InvalidBackupSetting(
                f"{label} is required: the scripts take it as an argument and "
                "refuse to start without it."
            )
        return

    if key in ("local_dir", "local_tmp_dir", "remote_dir"):
        if not _PATH.match(value):
            raise InvalidBackupSetting(
                f"{label} has to be an absolute path of at least two segments, "
                "ending with a slash, holding letters, digits, dots, dashes "
                "and underscores. The slash is not a formality: the scripts "
                "build a path by gluing the date onto this value, and "
                "`backup_full.sh` empties the staging directory with "
                f"`rm -rf {value}*`, which without the slash removes every "
                "sibling whose name starts the same way."
            )
        if len(Path(value).parts) < 3:
            raise InvalidBackupSetting(
                f"{label} is too close to the root of the filesystem. "
                f"`rm -rf {value}*` runs against it at the start of every "
                "backup, so it has to be a directory of its own, such as "
                "/var/lib/seapath-backup/."
            )
        return

    if key == "remote_serv":
        if not _SERVER.match(value):
            raise InvalidBackupSetting(
                f"{label} is `[user@]host`, as ssh and rsync take it before "
                "the colon."
            )
        return

    if key == "remote_shell":
        if not _SHELL.match(value):
            raise InvalidBackupSetting(
                f"{label} has to be `ssh`, optionally followed by ssh options "
                "such as `-p 2222` or `-o StrictHostKeyChecking=yes`. The "
                "scripts expand it unquoted and hand it to `rsync -e`, so it "
                "is a command line and not a shell command."
            )
        return

    if key in ("include_vm", "exclude_vm"):
        try:
            re.compile(value)
        except re.error as error:
            raise InvalidBackupSetting(
                f"{label} is not an extended regular expression: {error}. The "
                "scripts match guest names against it with `grep -E`."
            ) from error


__all__ = [
    "BackupCatalogue",
    "BackupService",
    "BackupSetting",
    "BackupView",
    "Estimate",
    "FullBackup",
    "GuestBackup",
    "GuestVolume",
    "InvalidBackupSetting",
    "parse_listing",
]
