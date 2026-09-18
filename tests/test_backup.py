# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The backups: the seven variables, the estimate, the listing and four runs.

The upstream `backup_restore` role is a whiptail menu over four programs, and
what is held here is the port of the four rather than of the menu. Three things
carry most of the value.

The refusals around the two staging directories, because `backup_full.sh`
opens with `rm -rf "${local_dir}"*`: a value without its trailing slash removes
every sibling whose name starts the same way, and a short one removes a great
deal more than that. Those are tested with the accepting and the refusing case,
as every validation rule here is.

The exact command line each act runs, because that command line is the whole of
what reaches a machine. A backup that passed its arguments in the wrong order
would export the right images to the wrong place.

And the `read -r` each script pauses on. A task's standard input is
`/dev/null`, so the prompt would be answered by accident and nobody would ever
see it fail. The plays answer it on purpose, and a test says so, because the
day that `stdin` is removed as noise is the day a backup silently stops
deleting its staging directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.cluster.rbd import CommandRbdClient
from app.core.settings import Settings
from app.hosts.reader import CommandResult
from app.runs import backup as plays
from app.runs.backup import BackupAction, BackupTarget
from app.services.backup import parse_listing, parse_staging
from tests.conftest import sign_in
from tests.fakes import FakeCommandRunner, write_fake_collection

# A cluster of three machines, with the backup settings already on
# `cluster_machines`. The addresses are the ones the fake exporters answer for,
# so this file is read the same way every other cluster page reads it.
CLUSTER = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      cluster_ip_addr: 192.168.55.1
      cluster_next_ip_addr: 192.168.55.2
      cluster_previous_ip_addr: 192.168.55.3
      br_rstp_priority: 12288
    elabo1:
      ansible_host: 192.168.200.126
      cluster_ip_addr: 192.168.55.2
      cluster_next_ip_addr: 192.168.55.3
      cluster_previous_ip_addr: 192.168.55.1
      br_rstp_priority: 16384
    elabo2:
      ansible_host: 192.168.200.127
      cluster_ip_addr: 192.168.55.3
      cluster_next_ip_addr: 192.168.55.1
      cluster_previous_ip_addr: 192.168.55.2
      br_rstp_priority: 16384
  children:
    cluster_machines:
      hosts:
        seapath-machine:
        elabo1:
        elabo2:
      vars:
        network_interface: eno1
        team0_0: eno2
        team0_1: eno3
        admin_user: admin
{settings}
    hypervisors:
      children:
        cluster_machines:
"""

CONFIGURED = """        backup_restore_remote_serv: backup@backup.example.org
        backup_restore_remote_dir: /srv/seapath-backups/
        backup_restore_local_dir: /var/lib/seapath-backup/
        backup_restore_local_tmp_dir: /var/lib/seapath-restore/
        backup_restore_remote_shell: ssh
        backup_restore_include_vm: .*
        backup_restore_exclude_vm: ''
"""

# One standalone machine, which is what the backup tool cannot work on: there
# is no RBD pool outside a cluster.
STANDALONE = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
  children:
    standalone_machine:
      hosts:
        seapath-machine:
      vars:
        network_interface: eno1
        admin_user: admin
"""

# What the backup server answers, in the shape the listing command prints. One
# full backup holding two guests, one of which has an additional disk, and one
# incremental backup taken an hour later.
LISTING = """dir 202603110733
file 202603110733/system_vm-guest1_202603110733.qcow2
file 202603110733/data_vm-guest1_0_202603110733.qcow2
file 202603110733/system_vm-guest1-202603110733.xml
file 202603110733/system_vm-guest1-metaall-202603110733.txt
file 202603110733/system_vm-guest1-meta-_priority-202603110733.txt
file 202603110733/system_vm-guest1_202603110733_202603110836.diff
file 202603110733/data_vm-guest1_0_202603110733_202603110836.diff
file 202603110733/system_vm-guest1-202603110836.xml
file 202603110733/system_vm-guest2_202603110733.qcow2
file 202603110733/system_vm-guest2-202603110733.xml
dir 202602010900
file 202602010900/system_vm-guest3_202602010900.qcow2
file 202602010900/system_vm-guest3-202602010900.xml
"""


def _import(client: TestClient, document: str) -> None:
    response = client.post("/api/v1/inventory/import", json={"document": document})
    assert response.status_code == 200, response.text


def _configured(client: TestClient) -> None:
    _import(client, CLUSTER.format(settings=CONFIGURED))


def _backup(client: TestClient) -> dict:
    response = client.get("/api/v1/backup")
    assert response.status_code == 200, response.text
    return response.json()


def _played(settings: Settings, run_id: str) -> dict:
    """The play the run staged, read back off the disk it was written to."""
    root = (
        settings.runs_dir
        / run_id
        / "collections/ansible_collections/seapath/ansible/playbooks"
    )
    documents = list(root.glob("backup_*.yaml"))
    assert len(documents) == 1, documents
    return yaml.safe_load(documents[0].read_text())[0]


def _argv(play: dict, task: int = 0) -> list[str]:
    return play["tasks"][task]["ansible.builtin.command"]["argv"]


def settings_of(client: TestClient) -> Settings:
    return client.app.state.settings


def _wait(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/v1/runs/{run_id}").json()
        if record["state"] not in ("pending", "running"):
            return record
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish")


def _listed(remote_runner) -> None:
    """The backup server, with what it holds.

    One hop away from a cluster member and two from here, so the suite answers
    for it rather than reaching it. The command the service sends is asserted
    separately; this is the answer that comes back.
    """
    remote_runner.answers = {"cd ": LISTING}


# What this machine's own /etc/backup-restore.conf contributes


def test_the_form_is_offered_the_values_this_machine_already_has(
    signed_in: TestClient,
) -> None:
    """A site that has been driving the whiptail menu has them in that file.

    Asking for them again is asking an operator to retype what the machine is
    already saying, which is how a `local_dir` loses its trailing slash.
    """
    _import(signed_in, CLUSTER.format(settings=""))

    payload = _backup(signed_in)

    assert payload["conf_found"] is True
    assert payload["conf_path"] == "/etc/backup-restore.conf"
    held = {setting["key"]: setting["on_machine"] for setting in payload["settings"]}
    assert held["remote_serv"] == "backup@backup.example.org"
    assert held["local_dir"] == "/var/lib/seapath-backup/"
    # The inventory is silent, so the note sends the operator to the form the
    # file has already filled in.
    assert "on this machine does" in payload["note"]


def test_a_default_this_service_invents_never_hides_the_machine_s_value(
    signed_in: TestClient, reader
) -> None:
    """The scripts' own defaults belong to a command line, not to the form.

    `remote_shell` used to be read off the target a run is built from, which
    carries `ssh` wherever the inventory is silent. That invented value won
    over the machine's own, so a site running `ssh -p 22` was shown `ssh`, and
    committing the form as shown would have written `ssh` into the inventory
    and dropped the port from every machine at the next convergence.
    """
    from app.hosts.models import BackupConf

    reader.conf = BackupConf(
        found=True,
        values={"remote_shell": "ssh -p 22", "include_vm": "vm-.*"},
    )
    _import(signed_in, CLUSTER.format(settings=""))

    held = {setting["key"]: setting for setting in _backup(signed_in)["settings"]}

    # Empty, because the inventory says nothing. That emptiness is what lets
    # the form fall back to the machine.
    assert held["remote_shell"]["value"] == ""
    assert held["remote_shell"]["on_machine"] == "ssh -p 22"
    assert held["include_vm"]["value"] == ""
    assert held["include_vm"]["on_machine"] == "vm-.*"
    # And the default is still offered, in the placeholder, where it says
    # what happens to an empty box rather than filling it in.
    assert held["remote_shell"]["placeholder"] == "ssh"


def test_the_scripts_defaults_still_reach_the_command_line(
    signed_in: TestClient, settings: Settings
) -> None:
    """Keeping them out of the form does not take them out of a run.

    An inventory that names the six that matter and leaves the shell alone
    launches with `ssh`, which is what the role writes into the conf file and
    what `backup-restore.sh` would have used.
    """
    _import(
        signed_in,
        CLUSTER.format(
            settings=CONFIGURED.replace(
                "        backup_restore_remote_shell: ssh\n", ""
            )
        ),
    )

    run_id = signed_in.post("/api/v1/backup/full").json()["run_id"]

    assert _argv(_played(settings, run_id))[2] == "ssh"


def test_a_silent_inventory_is_not_reported_as_a_disagreement(
    signed_in: TestClient,
) -> None:
    """A variable the inventory says nothing about has not diverged from anything.

    Listing all seven as differences on a node whose inventory has never
    mentioned backups buried the one case that matters, and the note at the top
    of the page already offers to adopt the file's values.
    """
    _import(signed_in, CLUSTER.format(settings=""))

    payload = _backup(signed_in)

    assert payload["conf_found"] is True
    assert not any("differs from the inventory" in w for w in payload["warnings"])


def test_the_inventory_wins_and_a_difference_is_named(signed_in: TestClient) -> None:
    """Neither is silently preferred.

    The file is what the menu on that machine uses; the inventory is what a run
    passes and what the role renders the file from at the next convergence. An
    operator who can see both decides which is right.
    """
    _import(
        signed_in,
        CLUSTER.format(
            settings=CONFIGURED.replace(
                "backup_restore_remote_dir: /srv/seapath-backups/",
                "backup_restore_remote_dir: /srv/elsewhere/",
            )
        ),
    )

    payload = _backup(signed_in)

    held = {setting["key"]: setting for setting in payload["settings"]}
    assert held["remote_dir"]["value"] == "/srv/elsewhere/"
    assert held["remote_dir"]["on_machine"] == "/srv/seapath-backups/"
    assert any(
        "differs from the inventory" in warning and "/srv/elsewhere/" in warning
        for warning in payload["warnings"]
    )
    # And it is the inventory's value that a run carries.
    run_id = signed_in.post("/api/v1/backup/full").json()["run_id"]
    assert "/srv/elsewhere/" in " ".join(_argv(_played(settings_of(signed_in), run_id)))


def test_a_machine_with_no_such_file_says_so_rather_than_failing(
    signed_in: TestClient, reader
) -> None:
    from app.hosts.models import BackupConf

    reader.conf = BackupConf()
    _import(signed_in, CLUSTER.format(settings=""))

    payload = _backup(signed_in)

    assert payload["conf_found"] is False
    assert all(setting["on_machine"] is None for setting in payload["settings"])
    assert "does not say where the backups go" in payload["note"]


def test_the_conf_parser_takes_the_seven_keys_and_unquotes_the_one_that_is_quoted(
    tmp_path: Path,
) -> None:
    """`writeVar` quotes `remote_shell` and nothing else.

    The quotes are the shell's, so they are not part of the value, and the menu
    also lets a site keep its own lines in that file. Those are left alone.
    """
    from app.hosts.local import LocalHostReader

    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "backup-restore.conf").write_text(
        'remote_shell="ssh -p 2222"\n'
        "local_dir=/var/lib/seapath-backup/\n"
        "exclude_vm=vm-test\n"
        "# a comment the menu never writes\n"
        "SITE_OWN_SETTING=whatever\n"
        "not a key value line\n"
    )

    conf = LocalHostReader(root=tmp_path, etc_root=etc).backup_conf()

    assert conf.found is True
    assert conf.values == {
        "remote_shell": "ssh -p 2222",
        "local_dir": "/var/lib/seapath-backup/",
        "exclude_vm": "vm-test",
    }


# What the page reads


def test_a_freshly_seeded_node_offers_the_seven_fields_and_no_act(
    signed_in: TestClient,
) -> None:
    """A node out of the ISO describes itself and is standalone.

    So the page has the form and nothing to launch: the backup tool exports
    RBD images and there is no pool here yet. The seven fields are offered
    anyway, because filling them in is a commit and a commit needs no cluster.
    """
    payload = _backup(signed_in)

    assert payload["configured"] is False
    assert "no cluster machine" in payload["note"]
    assert [setting["key"] for setting in payload["settings"]] == [
        "remote_serv",
        "remote_dir",
        "local_dir",
        "local_tmp_dir",
        "remote_shell",
        "include_vm",
        "exclude_vm",
    ]


def test_a_standalone_machine_is_told_the_tool_needs_a_cluster(
    signed_in: TestClient,
) -> None:
    # `backup_full.sh` opens with `rbd list`. There is no pool here.
    _import(signed_in, STANDALONE)

    payload = _backup(signed_in)

    assert payload["configured"] is False
    assert "no cluster machine" in payload["note"]


def test_an_inventory_that_says_nothing_about_backups_says_so(
    signed_in: TestClient,
) -> None:
    _import(signed_in, CLUSTER.format(settings=""))

    payload = _backup(signed_in)

    assert payload["configured"] is False
    assert "does not say where the backups go" in payload["note"]


def test_the_settings_are_read_as_the_cluster_members_resolve_them(
    signed_in: TestClient,
) -> None:
    _configured(signed_in)

    payload = _backup(signed_in)

    assert payload["configured"] is True
    # The one string that says where the backups go, in the shape the scripts
    # take it: `<server>:<directory>`.
    assert payload["target"] == "backup@backup.example.org:/srv/seapath-backups/"
    held = {setting["key"]: setting["value"] for setting in payload["settings"]}
    assert held["local_dir"] == "/var/lib/seapath-backup/"
    assert held["remote_shell"] == "ssh"
    # And the variable name each one is written under, which is the conf file's
    # own key with the prefix the inventory needs.
    names = {setting["key"]: setting["name"] for setting in payload["settings"]}
    assert names["remote_serv"] == "backup_restore_remote_serv"


def test_a_value_written_twice_is_reported_and_no_act_is_offered(
    signed_in: TestClient,
) -> None:
    """Ansible hands each machine the value nearest to it.

    So two values mean two different backups depending on which member the run
    is sent to, and a run passes the value on the command line: whichever this
    service read is the one that would be used. The page says so and the act is
    refused rather than resolved here.
    """
    document = CLUSTER.format(settings=CONFIGURED).replace(
        "    elabo1:\n      ansible_host: 192.168.200.126",
        "    elabo1:\n      backup_restore_local_dir: /srv/other/\n"
        "      ansible_host: 192.168.200.126",
    )
    _import(signed_in, document)

    payload = _backup(signed_in)

    assert any("written more than once" in warning for warning in payload["warnings"])
    response = signed_in.post("/api/v1/backup/full")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "backup_ambiguous"


# The estimate, which is the port of backup_du.py


def _estimate(client: TestClient) -> dict:
    response = client.get("/api/v1/backup/estimate")
    assert response.status_code == 200, response.text
    return response.json()


def test_the_estimate_is_asked_for_rather_than_read_with_the_page(
    signed_in: TestClient,
) -> None:
    """`rbd du` adds up the objects of every image in the pool.

    That is minutes on a real cluster, and a page that asked for it on every
    visit held the page up and then reported the client's timeout. It has an
    endpoint of its own and a button that says what it costs.
    """
    _configured(signed_in)

    assert "estimate" not in _backup(signed_in)


# `rbd du --format json` as a cluster holding snapshotted guests answers it.
# Recorded rather than invented: `system_ABB15` is an image whose backup
# snapshot holds all but three hundred megabytes of it, `system_ABB15SSH` has
# had nothing written to it since its snapshot, `system_ABB` has two snapshots
# whose rows add up past the disk, and `system_debian14` has no snapshot at
# all. Sizes in MiB below, bytes in the document.
DU_WITH_SNAPSHOTS = {
    "system_ABB15": [("202609171030", 30 * 1024, 18 * 1024), (None, 30 * 1024, 320)],
    "system_ABB15SSH": [
        ("202609171030", 30 * 1024, 4915),
        (None, 30 * 1024, 0),
    ],
    "system_ABB": [
        ("snapshot", 32 * 1024, 16 * 1024),
        ("202512182144", 32 * 1024, 20 * 1024),
        (None, 32 * 1024, 0),
    ],
    "system_debian14": [(None, 3 * 1024, 1331)],
}


def _du_client() -> tuple[CommandRbdClient, FakeCommandRunner]:
    mebibyte = 1024 * 1024
    images = [
        {
            "name": name,
            "provisioned_size": provisioned * mebibyte,
            "used_size": used * mebibyte,
            **({"snapshot": snapshot} if snapshot else {}),
        }
        for name, rows in DU_WITH_SNAPSHOTS.items()
        for snapshot, provisioned, used in rows
    ]
    document = json.dumps({"images": images, "total_used_size": 0})
    runner = FakeCommandRunner({"rbd -p rbd du": CommandResult(0, document, "")})
    return CommandRbdClient(runner=runner), runner


def test_an_image_is_worth_its_rows_added_up_rather_than_its_own() -> None:
    """The rows `rbd du` answers are deltas between snapshots.

    Ceph walks each snapshot from the one before it, so an image's own row
    carries what was written since the latest snapshot and nothing older. An
    image snapshotted by last night's backup and untouched since reports `0 B`
    while a full backup still exports it whole, because the blocks the
    snapshot holds are the blocks the image reads. Reading that row on its own
    put a thirty gigabyte guest on the Backup page as three hundred megabytes.
    """
    client, runner = _du_client()

    usage = {image.image: image for image in client.disk_usage()}

    mebibyte = 1024 * 1024
    # The snapshot's 18 GiB plus the 320 MiB written since it was taken.
    assert usage["system_ABB15"].used_bytes == (18 * 1024 + 320) * mebibyte
    # Nothing written since the snapshot, and an export that still writes it.
    assert usage["system_ABB15SSH"].used_bytes == 4915 * mebibyte
    # An image without a snapshot is its own row, which is the case that was
    # right before and has to stay right.
    assert usage["system_debian14"].used_bytes == 1331 * mebibyte
    assert usage["system_debian14"].provisioned_bytes == 3 * 1024 * mebibyte
    # One reading of the pool, whatever the snapshots.
    assert [argument for argument in runner.calls[0] if argument != "-p"] == [
        "rbd",
        "rbd",
        "du",
        "--format",
        "json",
    ]


def test_a_volume_is_bounded_by_what_the_disk_provisions() -> None:
    """The sum is a ceiling, and the disk is a harder one.

    A block rewritten since a snapshot is counted in both rows, so the rows of
    an image with a long snapshot history add up past what the image can
    possibly export. The provisioned size bounds it, because no export writes
    more than the disk holds.
    """
    client, _ = _du_client()

    usage = {image.image: image for image in client.disk_usage()}

    # 16 + 20 GiB of rows on a 32 GiB disk.
    assert usage["system_ABB"].used_bytes == 32 * 1024 * 1024 * 1024
    assert usage["system_ABB"].provisioned_bytes == 32 * 1024 * 1024 * 1024


def test_the_estimate_sums_a_guest_and_its_additional_disks(
    signed_in: TestClient,
) -> None:
    _configured(signed_in)

    estimate = _estimate(signed_in)

    volumes = {guest["guest"]: guest for guest in estimate["guests"]}
    # `vm-guest1` has a system disk and a data disk, and a backup exports both.
    assert volumes["vm-guest1"]["images"] == [
        "data_vm-guest1_0",
        "system_vm-guest1",
    ]
    gigabyte = 1024 * 1024 * 1024
    assert volumes["vm-guest1"]["used_bytes"] == 17 * gigabyte
    # `vm-guest2` has been snapshotted and not written to since, so its own
    # `rbd du` row is `0 B` and the four gigabytes sit in the snapshot. A
    # backup exports them all the same.
    assert volumes["vm-guest2"]["used_bytes"] == 4 * gigabyte
    # The total is what crosses the network, which is the used size and not
    # what the disks were provisioned at.
    assert estimate["used_bytes"] == sum(
        guest["used_bytes"] for guest in estimate["guests"]
    )


def test_the_filters_are_applied_to_guest_names_the_way_the_scripts_apply_them(
    signed_in: TestClient,
) -> None:
    _import(
        signed_in,
        CLUSTER.format(
            settings=CONFIGURED.replace(
                "backup_restore_exclude_vm: ''", "backup_restore_exclude_vm: guest[34]"
            )
        ),
    )

    estimate = _estimate(signed_in)

    assert estimate["included"] == ["vm-guest1", "vm-guest2"]
    # Named rather than silently absent: a guest missing from a backup because
    # of a pattern somebody wrote months ago is what this line exists for.
    assert estimate["excluded"] == ["vm-guest3", "vm-guest4"]


# Writing the settings


def test_the_settings_are_written_on_the_cluster_group_as_one_commit(
    signed_in: TestClient,
) -> None:
    _import(signed_in, CLUSTER.format(settings=""))

    response = signed_in.put(
        "/api/v1/backup/settings",
        json={
            "remote_serv": "backup@backup.example.org",
            "remote_dir": "/srv/seapath-backups/",
            "local_dir": "/var/lib/seapath-backup/",
            "local_tmp_dir": "/var/lib/seapath-restore/",
            "remote_shell": "ssh -p 2222",
            "include_vm": ".*",
            "exclude_vm": "",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["commit"]
    document = signed_in.get("/api/v1/inventory/raw").text
    assert "backup_restore_remote_serv: backup@backup.example.org" in document
    assert "backup_restore_remote_shell: ssh -p 2222" in document
    assert _backup(signed_in)["configured"] is True


def test_a_staging_directory_without_its_trailing_slash_is_refused(
    signed_in: TestClient,
) -> None:
    """The rule that stands between a backup and `rm -rf /var/lib/seapath*`.

    `backup_full.sh` empties the staging directory with `rm -rf
    "${local_dir}"*`. Without the slash that glob reaches every sibling whose
    name starts the same way, and the script has no check of its own.
    """
    _import(signed_in, CLUSTER.format(settings=""))

    response = signed_in.put(
        "/api/v1/backup/settings",
        json={
            "remote_serv": "backup@backup.example.org",
            "remote_dir": "/srv/seapath-backups/",
            "local_dir": "/var/lib/seapath-backup",
            "local_tmp_dir": "/var/lib/seapath-restore/",
            "remote_shell": "ssh",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_backup_setting"
    assert "rm -rf" in response.json()["error"]["message"]


def test_the_remote_directory_is_refused_for_its_own_reason(
    signed_in: TestClient,
) -> None:
    """Nothing in the scripts ever deletes anything on the backup server.

    The trailing slash is required there too, because `restore_vm.sh` writes
    the backup date straight after the value, so a refusal that borrowed the
    `rm -rf` sentence from the staging directories would be telling an operator
    something false about their backup server.
    """
    _import(signed_in, CLUSTER.format(settings=""))

    response = signed_in.put(
        "/api/v1/backup/settings",
        json={
            "remote_serv": "backup@backup.example.org",
            "remote_dir": "/data/backups",
            "local_dir": "/var/lib/seapath-backup/",
            "local_tmp_dir": "/var/lib/seapath-restore/",
            "remote_shell": "ssh",
        },
    )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "restore_vm.sh" in message
    assert "/data/backups202603110733/" in message
    assert "rm -rf" not in message


def test_a_directory_on_the_server_may_sit_at_the_root(signed_in: TestClient) -> None:
    """The two segment rule guards the `rm -rf`, which the server never sees.

    `/backups/` is a perfectly ordinary place to keep them, and refusing it
    would be this service inventing a rule out of a sentence it copied.
    """
    _import(signed_in, CLUSTER.format(settings=""))

    response = signed_in.put(
        "/api/v1/backup/settings",
        json={
            "remote_serv": "backup@backup.example.org",
            "remote_dir": "/backups/",
            "local_dir": "/var/lib/seapath-backup/",
            "local_tmp_dir": "/var/lib/seapath-restore/",
            "remote_shell": "ssh",
        },
    )

    assert response.status_code == 200, response.text


def test_a_staging_directory_at_the_root_of_the_filesystem_is_refused(
    signed_in: TestClient,
) -> None:
    _import(signed_in, CLUSTER.format(settings=""))

    response = signed_in.put(
        "/api/v1/backup/settings",
        json={
            "remote_serv": "backup@backup.example.org",
            "remote_dir": "/srv/seapath-backups/",
            "local_dir": "/var/",
            "local_tmp_dir": "/var/lib/seapath-restore/",
            "remote_shell": "ssh",
        },
    )

    assert response.status_code == 400
    assert "too close to the root" in response.json()["error"]["message"]


def test_a_remote_shell_that_is_not_ssh_is_refused(signed_in: TestClient) -> None:
    _import(signed_in, CLUSTER.format(settings=""))

    response = signed_in.put(
        "/api/v1/backup/settings",
        json={
            "remote_serv": "backup@backup.example.org",
            "remote_dir": "/srv/seapath-backups/",
            "local_dir": "/var/lib/seapath-backup/",
            "local_tmp_dir": "/var/lib/seapath-restore/",
            "remote_shell": "ssh; rm -rf /",
        },
    )

    assert response.status_code == 400
    assert "command line" in response.json()["error"]["message"]


def test_a_filter_that_is_not_a_regular_expression_is_refused(
    signed_in: TestClient,
) -> None:
    _import(signed_in, CLUSTER.format(settings=""))

    response = signed_in.put(
        "/api/v1/backup/settings",
        json={
            "remote_serv": "backup@backup.example.org",
            "remote_dir": "/srv/seapath-backups/",
            "local_dir": "/var/lib/seapath-backup/",
            "local_tmp_dir": "/var/lib/seapath-restore/",
            "remote_shell": "ssh",
            "include_vm": "vm-[",
        },
    )

    assert response.status_code == 400
    assert "regular expression" in response.json()["error"]["message"]


def test_writing_the_settings_is_an_administrator_s_act(client: TestClient) -> None:
    sign_in(client, "operator")

    response = client.put("/api/v1/backup/settings", json={"remote_serv": "a@b"})

    assert response.status_code == 403


# The runs


def test_a_full_backup_runs_the_script_with_the_settings_as_arguments(
    signed_in: TestClient, settings: Settings
) -> None:
    _configured(signed_in)

    response = signed_in.post("/api/v1/backup/full")

    assert response.status_code == 202, response.text
    play = _played(settings, response.json()["run_id"])
    # One member drives, named: the first hypervisor of the cluster by name,
    # which every node of the cluster agrees on.
    assert play["hosts"] == "elabo1"
    assert play["become"] is True
    assert _argv(play) == [
        "/usr/local/bin/backup_full.sh",
        "/var/lib/seapath-backup/",
        "ssh",
        "backup@backup.example.org:/srv/seapath-backups/",
        ".*",
        # What the menu puts there when a site has set nothing: an empty
        # pattern is matched with `grep -E -v` and would exclude every guest.
        "NonExistingGuestNameForDefault",
    ]


def test_a_backup_answers_the_prompt_the_script_pauses_on(
    signed_in: TestClient, settings: Settings
) -> None:
    """The `read -r` before the `rm -rf`, answered on purpose.

    A task's standard input is `/dev/null`, so the read would return at once
    and the script would carry on having asked nobody. The window on the page
    is where that question is asked now, and this is what says the answer is
    deliberate rather than an accident of how Ansible runs a command.
    """
    _configured(signed_in)

    response = signed_in.post("/api/v1/backup/full")

    play = _played(settings, response.json()["run_id"])
    assert play["tasks"][0]["ansible.builtin.command"]["stdin"] == "\n"


def test_an_incremental_backup_runs_the_other_script(
    signed_in: TestClient, settings: Settings
) -> None:
    _configured(signed_in)

    response = signed_in.post("/api/v1/backup/incremental")

    assert response.status_code == 202, response.text
    play = _played(settings, response.json()["run_id"])
    assert _argv(play)[0] == "/usr/local/bin/backup_inc.sh"


def test_a_backup_is_refused_where_the_inventory_says_nothing(
    signed_in: TestClient,
) -> None:
    _import(signed_in, CLUSTER.format(settings=""))

    response = signed_in.post("/api/v1/backup/full")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "backup_not_configured"
    assert response.json()["error"]["detail"]["missing"]


def test_a_backup_is_refused_where_the_collection_has_no_such_role(
    signed_in_with, tmp_path: Path
) -> None:
    """A collection without `backup_restore` has put no script on any machine.

    This service and the collection move independently, which is why the
    collection version is part of the image identity. An act whose scripts are
    not there fails on its first task, and this says so before the run.
    """
    collection = write_fake_collection(tmp_path / "older", roles=[])
    client = signed_in_with(collection)
    _configured(client)

    response = client.post("/api/v1/backup/full")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "role_missing"
    assert "backup_restore" in response.json()["error"]["message"]


def test_taking_a_backup_is_an_operator_s_act(client: TestClient) -> None:
    sign_in(client, "viewer")

    assert client.post("/api/v1/backup/full").status_code == 403


# Reading the backup server


def test_the_server_is_asked_from_a_member_over_one_connection(
    signed_in: TestClient, remote_runner
) -> None:
    """A read, so it reads: no run, no lock, no record.

    Two hops, because the trust that reaches the backup server is root's own
    key on each member: this node's key reaches the `ansible` account of a
    member, and `sudo` there reaches the server.
    """
    _configured(signed_in)
    _listed(remote_runner)

    response = signed_in.get("/api/v1/backup/catalogue")

    assert response.status_code == 200, response.text
    assert len(remote_runner.requests) == 1
    asked = remote_runner.requests[0]
    assert asked.user == "ansible"
    # The command run on the member: sudo, then ssh to the backup server.
    assert asked.command.startswith("sudo -n /bin/sh -c ")
    assert "backup@backup.example.org" in asked.command
    assert "cd /srv/seapath-backups/" in asked.command


def test_the_second_hop_can_never_sit_on_a_prompt(
    signed_in: TestClient, remote_runner
) -> None:
    """The defect that made an operator watch a page say nothing.

    The ssh from the member to the backup server carried neither `BatchMode`
    nor a connect timeout, so an unaccepted host key or a refused key ended in
    a prompt on a connection with no terminal, and the read waited for good.
    Both are added to whatever `remote_shell` the site wrote, leaving the port
    and the options it carries in place.
    """
    _configured(signed_in)
    _listed(remote_runner)

    signed_in.get("/api/v1/backup/catalogue")

    command = remote_runner.requests[0].command
    assert "BatchMode=yes" in command
    assert "ConnectTimeout=10" in command


def test_what_the_server_answered_is_parsed_into_the_backups_it_holds(
    signed_in: TestClient, remote_runner
) -> None:
    _configured(signed_in)
    _listed(remote_runner)

    catalogue = signed_in.get("/api/v1/backup/catalogue").json()

    assert catalogue["read_from"] == "elabo1"
    assert catalogue["read_at"]
    assert [backup["date"] for backup in catalogue["backups"]] == [
        "202602010900",
        "202603110733",
    ]
    guests = {
        guest["guest"]: guest
        for backup in catalogue["backups"]
        if backup["date"] == "202603110733"
        for guest in backup["guests"]
    }
    assert sorted(guests) == ["vm-guest1", "vm-guest2"]
    assert guests["vm-guest1"]["disks"] == 2
    assert guests["vm-guest1"]["dates"] == ["202603110733", "202603110836"]
    assert guests["vm-guest2"]["dates"] == ["202603110733"]


def test_the_backups_run_where_the_server_is_read_from(
    signed_in: TestClient, settings: Settings, remote_runner
) -> None:
    """One member for the backups and for every reading about them.

    The listing used to be read from this node and the backup to run on
    `groups['cluster_machines'][0]`, so a listing that worked said nothing
    about the key a backup would push with.
    """
    _configured(signed_in)
    _listed(remote_runner)

    assert _backup(signed_in)["runs_on"] == "elabo1"
    signed_in.get("/api/v1/backup/catalogue")
    response = signed_in.post("/api/v1/backup/full")

    assert remote_runner.requests[0].address == "192.168.200.126"
    assert _played(settings, response.json()["run_id"])["hosts"] == "elabo1"


def test_an_observer_is_not_where_the_backups_run(signed_in: TestClient) -> None:
    """An observer votes and holds no disk worth staging a backup on."""
    document = CLUSTER.format(settings=CONFIGURED).replace(
        """    hypervisors:
      children:
        cluster_machines:
""",
        """    hypervisors:
      hosts:
        seapath-machine:
        elabo2:
    observers:
      hosts:
        elabo1:
""",
    )
    _import(signed_in, document)

    assert _backup(signed_in)["runs_on"] == "elabo2"


def test_a_server_that_cannot_be_reached_says_what_ssh_answered(
    signed_in: TestClient, remote_runner
) -> None:
    """The trust to the backup server is the site's, installed on each member.

    So the repair is on that machine, and the message has to carry what ssh
    said.
    """
    _configured(signed_in)
    remote_runner.refusal = "Host key verification failed."

    catalogue = signed_in.get("/api/v1/backup/catalogue").json()

    assert catalogue["backups"] == []
    assert "Host key verification failed." in catalogue["note"]
    assert "root's own key" in catalogue["note"]


def test_the_catalogue_is_not_on_the_path_of_the_page(signed_in: TestClient) -> None:
    # Two hops and a directory listing. `GET /backup` answers off the disk.
    _configured(signed_in)

    assert "catalogue" not in _backup(signed_in)


def test_an_inventory_with_no_backup_server_is_told_so_rather_than_asked(
    signed_in: TestClient, remote_runner
) -> None:
    _import(signed_in, CLUSTER.format(settings=""))

    catalogue = signed_in.get("/api/v1/backup/catalogue").json()

    assert catalogue["backups"] == []
    assert "no backup server to ask" in catalogue["note"]
    assert remote_runner.requests == []


# The staging directories

STAGED = """dir present /var/lib/seapath-backup/
df /var/lib/seapath-backup/ /data 1000000000000 900000000000
dir absent /var/lib/seapath-restore/
df /var/lib/seapath-restore/ / 16000000000 9000000000
"""


def test_the_staging_directories_are_read_on_the_member_the_backups_run_on(
    signed_in: TestClient, remote_runner
) -> None:
    """The room that matters is where `backup_full.sh` writes the qcow2s."""
    _configured(signed_in)
    remote_runner.answers = {"df -B1": STAGED}

    reading = signed_in.get("/api/v1/backup/staging").json()

    asked = remote_runner.requests[0]
    assert asked.address == "192.168.200.126"
    assert asked.user == "ansible"
    # Root, because the role creates both directories readable by root only.
    assert asked.command.startswith("sudo -n /bin/sh -c ")
    assert "/var/lib/seapath-backup/ /var/lib/seapath-restore/" in asked.command
    assert reading["host"] == "elabo1"
    assert reading["read_at"]
    assert reading["directories"] == [
        {
            "path": "/var/lib/seapath-backup/",
            "purpose": "backup",
            "exists": True,
            "mountpoint": "/data",
            "size_bytes": 1000000000000,
            "free_bytes": 900000000000,
        },
        {
            "path": "/var/lib/seapath-restore/",
            "purpose": "restore",
            "exists": False,
            "mountpoint": "/",
            "size_bytes": 16000000000,
            "free_bytes": 9000000000,
        },
    ]


def test_a_directory_the_machine_said_nothing_about_is_reported_missing() -> None:
    directories = parse_staging(
        "dir present /a/b/\n", [("/a/b/", "backup"), ("/c/d/", "restore")]
    )

    assert [item.exists for item in directories] == [True, False]
    assert directories[1].free_bytes is None


def test_a_machine_that_cannot_be_asked_about_its_staging_says_why(
    signed_in: TestClient, remote_runner
) -> None:
    _configured(signed_in)
    remote_runner.refusal = "Connection timed out"

    reading = signed_in.get("/api/v1/backup/staging").json()

    assert reading["directories"] == []
    assert "Connection timed out" in reading["note"]
    assert reading["host"] == "elabo1"


def test_no_staging_directory_is_asked_about_before_one_is_named(
    signed_in: TestClient, remote_runner
) -> None:
    _import(signed_in, CLUSTER.format(settings=""))

    reading = signed_in.get("/api/v1/backup/staging").json()

    assert "names no staging directory" in reading["note"]
    assert remote_runner.requests == []


def test_the_staging_directories_are_created_by_the_role_alone(
    signed_in: TestClient,
) -> None:
    """Creating them is a convergence of `backup_restore`, never a mkdir here.

    The page launches `seapath_setup_backup_restore` through the ordinary run
    path, so the entry has to be in the catalogue, reviewed, and say what it
    does to the machines.
    """
    catalogue = {
        item["entry"]["id"]: item for item in signed_in.get("/api/v1/playbooks").json()
    }

    entry = catalogue["seapath_setup_backup_restore"]["entry"]
    assert entry["reviewed"] is True
    assert entry["targets"] == ["cluster_machines"]
    assert "staging directories" in entry["disruption"]
    assert "cluster" in entry["requires"]


# Restoring


def test_a_restore_names_the_guest_the_backup_and_the_date_to_replay_to(
    signed_in: TestClient, settings: Settings, remote_runner
) -> None:
    _configured(signed_in)
    _listed(remote_runner)

    response = signed_in.post(
        "/api/v1/backup/restore",
        json={
            "guest": "vm-guest1",
            "full_date": "202603110733",
            "date": "202603110836",
        },
    )

    assert response.status_code == 202, response.text
    play = _played(settings, response.json()["run_id"])
    assert _argv(play) == [
        "/usr/local/bin/restore_vm.sh",
        "/var/lib/seapath-restore/",
        "ssh",
        "backup@backup.example.org:/srv/seapath-backups/",
        "202603110733",
        "vm-guest1",
        "202603110836",
    ]
    # The title an operator reads in the run list, with the date spelled out.
    assert play["name"] == "Restore vm-guest1 from 2026-03-11 08:36"


def test_a_restore_to_a_date_the_backup_does_not_hold_is_refused(
    signed_in: TestClient, remote_runner
) -> None:
    """`restore_vm.sh` recreates the guest from the XML of the date it is given.

    A date with no XML there produces a guest created from a file that is not
    on the machine, after the confirmation that destroyed the running one.
    """
    _configured(signed_in)
    _listed(remote_runner)

    response = signed_in.post(
        "/api/v1/backup/restore",
        json={
            "guest": "vm-guest2",
            "full_date": "202603110733",
            "date": "202603110836",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unknown_date"
    assert response.json()["error"]["detail"]["dates"] == ["202603110733"]


def test_a_restore_of_a_guest_no_backup_holds_is_refused(
    signed_in: TestClient, remote_runner
) -> None:
    _configured(signed_in)
    _listed(remote_runner)

    response = signed_in.post(
        "/api/v1/backup/restore",
        json={
            "guest": "vm-guest9",
            "full_date": "202603110733",
            "date": "202603110733",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unknown_guest"


def test_restoring_is_an_administrator_s_act(client: TestClient) -> None:
    sign_in(client, "operator")

    response = client.post(
        "/api/v1/backup/restore",
        json={
            "guest": "vm-guest1",
            "full_date": "202603110733",
            "date": "202603110733",
        },
    )

    assert response.status_code == 403


# The listing parser, on its own


def test_the_listing_parser_ignores_what_it_does_not_recognise() -> None:
    """A backup server is a machine a site already had, and holds other things.

    Anything that fits none of the three shapes is counted and otherwise left
    alone, so a directory of notes beside the backups produces a listing rather
    than an error.
    """
    backups = parse_listing(
        LISTING + "dir notes\nfile notes/readme.txt\nfile 202603110733/README\n"
    )

    assert [backup.date for backup in backups] == ["202602010900", "202603110733"]
    recent = backups[1]
    assert recent.files == 11
    assert [guest.guest for guest in recent.guests] == ["vm-guest1", "vm-guest2"]


def test_an_empty_listing_is_a_server_holding_nothing() -> None:
    assert parse_listing("") == []


# The command lines, in one place


def test_the_remote_shell_is_several_words_where_a_site_made_it_several() -> None:
    """`ssh -p 2222` is one setting and three arguments.

    The scripts expand it unquoted and hand it to `rsync -e`, which is why it
    is a command line rather than a command, and the listing has to split it
    the same way.
    """
    target = BackupTarget(
        remote_serv="backup@server",
        remote_dir="/srv/backups/",
        remote_shell="ssh -p 2222",
    )

    command = plays.listing_command(target)

    # The site's own words are kept, after the two options this service adds
    # so the second hop can never sit on a prompt.
    assert command[0] == "ssh"
    assert "BatchMode=yes" in command
    assert command[-3:-2] == ["-p"] or "-p" in command
    assert command[command.index("-p") + 1] == "2222"
    assert command[-2] == "backup@server"


# Where this interpreter's own `ansible-playbook` is, the one the requirements
# pin. An unactivated virtualenv keeps it off PATH, which is the same lookup
# `conftest` makes for `ansible-inventory` and for the same reason: Ansible is
# the only authority on whether a play it is handed is a play.
ANSIBLE_PLAYBOOK = shutil.which(
    "ansible-playbook",
    path=os.pathsep.join(
        [str(Path(sys.executable).parent), os.environ.get("PATH", "")]
    ),
)


@pytest.mark.skipif(
    ANSIBLE_PLAYBOOK is None, reason="ansible-playbook is not installed"
)
def test_ansible_parses_every_play_this_service_writes(
    signed_in: TestClient, settings: Settings, tmp_path: Path, remote_runner
) -> None:
    """The plays are generated, so nothing but Ansible says they are plays.

    A YAML document this service dumped is syntactically fine and can still be
    a play Ansible refuses, and the place that would be found out is a run an
    operator launched after confirming something destructive.
    """
    _configured(signed_in)
    _listed(remote_runner)
    inventory = tmp_path / "inventory.yaml"
    inventory.write_text(CLUSTER.format(settings=CONFIGURED))

    # One at a time, because one run at a time per cluster is the rule these
    # obey like every other.
    acts = [
        ("/api/v1/backup/full", None),
        ("/api/v1/backup/incremental", None),
        (
            "/api/v1/backup/restore",
            {
                "guest": "vm-guest1",
                "full_date": "202603110733",
                "date": "202603110836",
            },
        ),
    ]
    for path, payload in acts:
        response = signed_in.post(path, json=payload)
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        _wait(signed_in, run_id)
        document = (
            settings.runs_dir
            / run_id
            / "collections/ansible_collections/seapath/ansible/playbooks"
        )
        for path in document.glob("backup_*.yaml"):
            result = subprocess.run(
                [
                    str(ANSIBLE_PLAYBOOK),
                    "--syntax-check",
                    "-i",
                    str(inventory),
                    str(path),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False"},
            )
            assert result.returncode == 0, result.stderr


def test_every_backup_act_plays_one_cluster_member_and_previews_nothing() -> None:
    for action in BackupAction:
        entry = plays.entry(action, "vm-guest1", "202603110836", "elabo1")

        assert entry.targets == ["elabo1"]
        assert entry.preview.value == "none"
        assert entry.reboots.value == "no"
        # A backup is a cluster act: `backup_full.sh` opens with `rbd list`.
        assert "cluster" in [item.value for item in entry.requires]
