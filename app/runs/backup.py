# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The backup acts, as the plays that call the scripts the role installed.

`seapath-ansible` ships a `backup_restore` role, and the prerequisites
playbooks of Debian and Oracle Linux install it on every machine they
configure. What it puts in `/usr/local/bin` is four programs and a menu over
them: `backup_full.sh` exports every guest's RBD images as qcow2 and rsyncs
them to a backup server, `backup_inc.sh` exports RBD diffs against the latest
snapshot into the same directory, `restore_vm.sh` brings one guest back from a
chosen date, and `backup_du.py` estimates what a full backup would weigh.
`backup-restore.sh` is a whiptail menu that asks for the arguments and calls
them.

This service calls the four programs and leaves the menu alone, because the
menu is the part that has no place here. It reads and writes
`/etc/backup-restore.conf`, and writing a file on a host is the one thing this
service does not do. The scripts themselves take every value as an argument,
which is the interface a port can stand on: a run built here names the local
directory, the remote server, the remote shell and the two filters on the
command line, so the run record carries the whole of what was asked for and
nothing on the machine had to be edited first. The values come from the
inventory, which is where a site's desired state lives, and
`app/services/backup.py` is what reads and writes them.

The shape is D30's, the one the runtime actions already use: a play generated
into the run's own staged tree, one or two tasks, `argv` so no shell parses a
value, and everything else the ordinary run path with the same lock, the same
event stream and the same record. Two things are worth stating about it.

**The confirmation is answered here, once, by the page.** Each script pauses on
a bare `read -r` before it deletes anything, which is the whiptail menu's
confirmation and an operator's last chance at a terminal. A task's standard
input is `/dev/null`, so the read would return at once and the script would
carry on having asked nobody: the prompt would be answered by accident. The
plays below answer it on purpose, with `stdin`, and the question it was asking
is asked by the window on the page, which names the machines and what is about
to be erased. That is where this service puts ceremony everywhere else.

**A backup holds the run lock for as long as it takes.** One run at a time per
cluster is what keeps two operators from converging the same machines at once,
and a full backup of a dozen guests is an hour of `qemu-img convert` and rsync
under that same lock. It is not carved out. A backup reads every image of the
pool and pushes the result off the cluster, which is exactly the kind of act
that should not overlap a convergence, and an operator who needs the cluster
back has Cancel on the run.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum

import yaml

from app.runs.catalogue import PlaybookEntry, Precondition, Preview, Reboots

# The recorded playbook name, as `actions.py` records its own. Deliberately not
# `seapath.ansible.*`: the play was written here. What it calls belongs to the
# collection, and the command line in the record says so.
GENERATOR = "seapath-webui"

# The role whose files these are. Absent from the installed collection, the
# scripts are on no machine and every act below would fail on the first task.
ROLE = "backup_restore"

# Where the role's `synchronize` puts them.
SCRIPTS = "/usr/local/bin"

# What answers the `read -r` each script pauses on. A newline, which is what an
# operator types at that prompt.
CONFIRMATION = "\n"

# Where the listing of the backup server lands, on the controller, which for a
# run launched here is this container. Filled by the run service with the run's
# own results directory, exactly as a cyclictest is told where to fetch its
# histogram, so a listing is kept, listed and deleted with the run that read
# it.
LISTING_VARIABLE = "backup_listing_dir"
LISTING_FILE = "listing.txt"


class BackupAction(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    LIST = "list"
    RESTORE = "restore"


@dataclass(frozen=True)
class BackupTarget:
    """Everything the scripts are told, in the words the conf file uses.

    The names are `/etc/backup-restore.conf`'s own, prefixed in the inventory
    with `backup_restore_`, so the two descriptions of one site's backup can be read
    side by side. `app/services/backup.py` is what refuses a value before it
    gets here: a `local_dir` without its trailing slash makes `backup_full.sh`
    remove `/var/lib/seapath-backup*` rather than the contents of a directory,
    and that check belongs where the sentence explaining it can be written.
    """

    local_dir: str = ""
    local_tmp_dir: str = ""
    remote_serv: str = ""
    remote_dir: str = ""
    remote_shell: str = "ssh"
    include_vm: str = ".*"
    exclude_vm: str = ""

    @property
    def destination(self) -> str:
        """`<server>:<directory>`, which is the one argument the scripts take.

        They pass it to rsync as it stands, and `restore_vm.sh` glues the date
        directory onto it, which is why the directory keeps its trailing
        slash.
        """
        return f"{self.remote_serv}:{self.remote_dir}"

    @property
    def shell_argv(self) -> list[str]:
        """The remote shell as a command line.

        `ssh`, or `ssh` with the options a site needs. The scripts hand it to
        `rsync -e` and expand it unquoted, so it is several words there and it
        is several arguments here.
        """
        return shlex.split(self.remote_shell or "ssh")


# What each act is called, and what an operator has to be told before it runs.
# The sentences are long because they are the confirmation: an apply on these
# machines restarts services under running VMs, and a restore recreates a guest
# over the one that is there.
_TITLES = {
    BackupAction.FULL: "Back up every guest, in full",
    BackupAction.INCREMENTAL: "Back up what changed since the last full backup",
    BackupAction.LIST: "Read what the backup server holds",
    BackupAction.RESTORE: "Restore {guest} from {date}",
}

_DISRUPTIONS = {
    BackupAction.FULL: (
        "Exports every guest's disks from Ceph and sends them to the backup "
        "server. On each image it runs `rbd sparsify`, then removes every "
        "snapshot the image carries with `rbd snap purge`, then takes the base "
        "snapshot this backup and the incremental ones after it are made "
        "against. The guests keep running throughout, and the deleted "
        "snapshots do not come back: an incremental backup taken against one "
        "of them can no longer be applied. It also empties the staging "
        "directory on the machine before it starts, which is where the "
        "previous full backup was kept. Expect an hour or more, and the "
        "cluster takes no other run until it ends."
    ),
    BackupAction.INCREMENTAL: (
        "Exports what each image changed since its latest snapshot, as an RBD "
        "diff beside the full backup it belongs to, and sends the directory to "
        "the backup server again. The guests keep running. A disk added since "
        "the last full backup has no snapshot to diff against: it is skipped "
        "with a warning in the log and stays out of every incremental backup "
        "until a new full one is made."
    ),
    BackupAction.LIST: (
        "Asks the backup server what it holds, over the same SSH the backups "
        "are pushed with, and brings the listing back. It reads and changes "
        "nothing, on the machines or on the server."
    ),
    BackupAction.RESTORE: (
        "Recreates the guest from the backup and starts it. `vm-mgr create "
        "--force` replaces whatever is there under that name: the disks the "
        "guest has now, the Pacemaker resource and the metadata on its image "
        "are all overwritten by what the backup carries, and the data written "
        "since the chosen date is gone. The staging directory on the machine "
        "is emptied first. Restore over a guest that is running, and the "
        "running one is destroyed."
    ),
}


def record(action: BackupAction) -> str:
    """What the run is filed under, which is also the generated play's name."""
    return f"backup_{action.value}"


def entry(
    action: BackupAction,
    guest: str = "",
    date: str = "",
) -> PlaybookEntry:
    """The catalogue shape of one backup act.

    Never in the catalogue and never offered on the Deployment page, for the
    reason `actions.entry` gives: these name a directory on a backup server
    rather than a machine of the inventory, and a run that plays every machine
    is a different act. What the shape is for is the preconditions, the lock
    and the record, which a backup wants exactly as a convergence does.
    """
    identifier = record(action)
    requires = [
        Precondition.INVENTORY_VALID,
        Precondition.SELF_TRUST,
        # The backup tool works on RBD images, and there are none outside a
        # cluster: `backup_full.sh` opens with `rbd list`. The role's own
        # README says cluster mode only.
        Precondition.CLUSTER,
    ]
    return PlaybookEntry(
        id=identifier,
        playbook=f"{GENERATOR}.{identifier}",
        title=_TITLES[action].format(guest=guest, date=_readable(date)),
        targets=["cluster_machines[0]"],
        # There is nothing to preview. The play runs a shell script, and what
        # it does is what the script does.
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=_DISRUPTIONS[action],
        requires=requires,
        results_variable=LISTING_VARIABLE if action is BackupAction.LIST else None,
        reviewed=True,
    )


def play(
    action: BackupAction,
    target: BackupTarget,
    guest: str = "",
    full_date: str = "",
    incremental_date: str = "",
) -> str:
    """The play, as YAML.

    Dumped rather than templated, so no value a form carried can become YAML of
    its own. Every one of them has been checked by the service before it gets
    here, and this is the second lock on the same door.
    """
    document = [
        {
            "name": entry(action, guest, incremental_date or full_date).title,
            # `cluster_vm` is called on one member and answers for the cluster;
            # the backup scripts read the pool from one member and answer for
            # all of it, so they run in the same place and for the same reason.
            "hosts": "{{ groups['cluster_machines'][0] }}",
            "gather_facts": False,
            "become": True,
            "tasks": _tasks(action, target, guest, full_date, incremental_date),
        }
    ]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def _tasks(
    action: BackupAction,
    target: BackupTarget,
    guest: str,
    full_date: str,
    incremental_date: str,
) -> list[dict]:
    if action is BackupAction.LIST:
        return _listing_tasks(target)

    if action is BackupAction.RESTORE:
        argv = [
            f"{SCRIPTS}/restore_vm.sh",
            target.local_tmp_dir,
            target.remote_shell,
            target.destination,
            full_date,
            guest,
            incremental_date,
        ]
    else:
        script = "backup_full.sh" if action is BackupAction.FULL else "backup_inc.sh"
        argv = [
            f"{SCRIPTS}/{script}",
            target.local_dir,
            target.remote_shell,
            target.destination,
            target.include_vm,
            # The script matches it with `grep -E -v`, and an empty pattern
            # there excludes everything. The menu's own default is a name no
            # guest has, and this keeps it rather than inventing another.
            target.exclude_vm or _NOTHING,
        ]
    return [
        {
            "name": entry(action, guest, incremental_date or full_date).title,
            "ansible.builtin.command": {
                "argv": argv,
                # The `read -r` the script pauses on, answered here rather than
                # by the empty standard input a task would otherwise have. The
                # question it asks was asked by the window on the page.
                "stdin": CONFIRMATION,
            },
            # It writes to Ceph and to the backup server whatever the state
            # was, so there is nothing here for Ansible to call unchanged.
            "changed_when": True,
        }
    ]


# What `backup-restore.sh` puts in `exclude_vm` when a site has set nothing: a
# guest name nobody has, because the pattern is matched with `grep -E -v` and
# an empty one would exclude every guest.
_NOTHING = "NonExistingGuestNameForDefault"

# What the backup server is asked, in one POSIX shell command. The directory
# layout is the scripts' own: one directory per full backup, named for the
# minute it started, holding the qcow2 of every image, the libvirt XML and the
# metadata of each guest, and the diffs of the incremental backups made since.
#
# It prints one line per entry rather than a recursive listing, because that is
# what a remote shell can be relied on to have: `find -printf` is GNU's, and a
# backup server is whatever the site already had.
_LISTING_SCRIPT = (
    "cd {directory} || exit 1; "
    "for d in */; do "
    '[ -d "$d" ] || continue; '
    "printf 'dir %s\\n' \"${{d%/}}\"; "
    'for f in "$d"*; do '
    '[ -e "$f" ] || continue; '
    "printf 'file %s\\n' \"$f\"; "
    "done; "
    "done"
)


def listing_command(target: BackupTarget) -> list[str]:
    """The exact command the listing runs, which is also what a test asserts."""
    return [
        *target.shell_argv,
        target.remote_serv,
        _LISTING_SCRIPT.format(directory=shlex.quote(target.remote_dir)),
    ]


def _listing_tasks(target: BackupTarget) -> list[dict]:
    """Ask the server, then put the answer where this service can read it.

    Two tasks, and the second is why the listing is a run at all. Nothing in
    this container can reach the backup server: the SSH trust that reaches it
    belongs to the cluster machines, and is the one the backups are pushed
    with. So the machine is asked, and the answer comes back the way a
    measurement's results come back, into the run's own directory, through the
    variable the run service filled with it.
    """
    return [
        {
            "name": "Ask the backup server what it holds",
            "ansible.builtin.command": {"argv": listing_command(target)},
            "register": "backup_listing",
            # It reads. Saying so keeps a listing out of the changed count of
            # the run record, where it would read as a backup having written
            # something.
            "changed_when": False,
        },
        {
            "name": "Bring the listing back",
            "ansible.builtin.copy": {
                "content": "{{ backup_listing.stdout }}\n",
                "dest": f"{{{{ {LISTING_VARIABLE} }}}}/{LISTING_FILE}",
                "mode": "0644",
            },
            # The controller, which for a run launched here is this container,
            # and as this service rather than as root: the play becomes root on
            # the machines it plays and there is nothing to become here.
            "delegate_to": "localhost",
            "become": False,
            "changed_when": False,
        },
    ]


def _readable(date: str) -> str:
    """`202203110836` as `2022-03-11 08:36`, which is what a title carries.

    The scripts name everything after the minute a backup started, and the menu
    reformats it for the same reason: twelve digits is a string an operator has
    to decode before deciding whether it is the right one.
    """
    if len(date) != 12 or not date.isdigit():
        return date
    return f"{date[0:4]}-{date[4:6]}-{date[6:8]} {date[8:10]}:{date[10:12]}"


__all__ = [
    "CONFIRMATION",
    "GENERATOR",
    "LISTING_FILE",
    "LISTING_VARIABLE",
    "ROLE",
    "SCRIPTS",
    "BackupAction",
    "BackupTarget",
    "entry",
    "listing_command",
    "play",
    "record",
]
