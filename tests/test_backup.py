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

from app.core.settings import Settings
from app.runs import backup as plays
from app.runs.backup import BackupAction, BackupTarget
from app.services.backup import parse_listing
from tests.conftest import sign_in
from tests.fakes import write_fake_collection

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

CONFIGURED = """        backup_remote_serv: backup@backup.example.org
        backup_remote_dir: /srv/seapath-backups/
        backup_local_dir: /var/lib/seapath-backup/
        backup_local_tmp_dir: /var/lib/seapath-restore/
        backup_remote_shell: ssh
        backup_include_vm: .*
        backup_exclude_vm: ''
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


def _listed(client: TestClient, settings: Settings) -> str:
    """A listing run that finished, with what it would have brought back.

    The fake adapter replays an event stream rather than reaching a backup
    server, so the file the second task would have written is written here. The
    run around it is real: the same lock, the same record, the same results
    directory the service reads from.
    """
    run_id = client.post("/api/v1/backup/listing").json()["run_id"]
    _wait(client, run_id)
    results = settings.runs_dir / run_id / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "listing.txt").write_text(LISTING)
    return run_id


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
                "backup_remote_dir: /srv/seapath-backups/",
                "backup_remote_dir: /srv/elsewhere/",
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
    assert names["remote_serv"] == "backup_remote_serv"


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
        "    elabo1:\n      backup_local_dir: /srv/other/\n"
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
                "backup_exclude_vm: ''", "backup_exclude_vm: guest[34]"
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
    assert "backup_remote_serv: backup@backup.example.org" in document
    assert "backup_remote_shell: ssh -p 2222" in document
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
    # One member drives, the way `cluster_vm` is called on one member and
    # answers for the cluster.
    assert play["hosts"] == "{{ groups['cluster_machines'][0] }}"
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


def test_the_listing_asks_the_server_and_brings_the_answer_back(
    signed_in: TestClient, settings: Settings
) -> None:
    _configured(signed_in)

    response = signed_in.post("/api/v1/backup/listing")

    assert response.status_code == 202, response.text
    play = _played(settings, response.json()["run_id"])
    argv = _argv(play)
    assert argv[0] == "ssh"
    assert argv[1] == "backup@backup.example.org"
    # One shell command the server runs, over the directory the settings name.
    # The listing is flat, so it needs no `find -printf`, which is GNU's and a
    # backup server is whatever the site already had.
    assert argv[2].startswith("cd /srv/seapath-backups/ || exit 1;")
    assert "printf 'file %s\\n'" in argv[2]
    # It reads, so it is not counted as a change of the cluster.
    assert play["tasks"][0]["changed_when"] is False
    # And the second task puts the answer in the run's own results directory,
    # which is the only place this container can read it from.
    copy = play["tasks"][1]["ansible.builtin.copy"]
    assert copy["content"] == "{{ backup_listing.stdout }}\n"
    assert copy["dest"] == "{{ backup_listing_dir }}/listing.txt"
    assert play["tasks"][1]["delegate_to"] == "localhost"
    assert play["tasks"][1]["become"] is False


def test_the_listing_run_is_told_where_to_put_what_it_brings_back(
    signed_in: TestClient, settings: Settings
) -> None:
    _configured(signed_in)

    run_id = signed_in.post("/api/v1/backup/listing").json()["run_id"]

    # Filled by the run service with the run's own directory, never by the
    # caller, exactly as a cyclictest is told where to fetch its histogram.
    command = json.loads((settings.runs_dir / run_id / "run.json").read_text())[
        "command"
    ]
    assert f"{run_id}/results" in " ".join(command)


def test_what_the_listing_found_is_read_back_off_the_run(
    signed_in: TestClient, settings: Settings
) -> None:
    _configured(signed_in)
    run_id = _listed(signed_in, settings)

    catalogue = _backup(signed_in)["catalogue"]

    assert catalogue["run_id"] == run_id
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
    # Two disks in the full backup, and two dates it can be restored to: the
    # full backup itself and the incremental one taken an hour later.
    assert guests["vm-guest1"]["disks"] == 2
    assert guests["vm-guest1"]["dates"] == ["202603110733", "202603110836"]
    assert guests["vm-guest2"]["dates"] == ["202603110733"]


def test_a_node_that_has_never_asked_says_so_rather_than_showing_nothing(
    signed_in: TestClient,
) -> None:
    _configured(signed_in)

    catalogue = _backup(signed_in)["catalogue"]

    assert catalogue["backups"] == []
    assert "has not been asked" in catalogue["note"]


# Restoring


def test_a_restore_names_the_guest_the_backup_and_the_date_to_replay_to(
    signed_in: TestClient, settings: Settings
) -> None:
    _configured(signed_in)
    _listed(signed_in, settings)

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
    signed_in: TestClient, settings: Settings
) -> None:
    """`restore_vm.sh` recreates the guest from the XML of the date it is given.

    A date with no XML there produces a guest created from a file that is not
    on the machine, after the confirmation that destroyed the running one.
    """
    _configured(signed_in)
    _listed(signed_in, settings)

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
    signed_in: TestClient, settings: Settings
) -> None:
    _configured(signed_in)
    _listed(signed_in, settings)

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

    assert command[:3] == ["ssh", "-p", "2222"]
    assert command[3] == "backup@server"


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
    signed_in: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """The plays are generated, so nothing but Ansible says they are plays.

    A YAML document this service dumped is syntactically fine and can still be
    a play Ansible refuses, and the place that would be found out is a run an
    operator launched after confirming something destructive.
    """
    _configured(signed_in)
    _listed(signed_in, settings)
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
        # Last, because a listing run that brought nothing back becomes the
        # newest one and a restore is offered out of what the newest found.
        ("/api/v1/backup/listing", None),
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
        entry = plays.entry(action, "vm-guest1", "202603110836")

        assert entry.targets == ["cluster_machines[0]"]
        assert entry.preview.value == "none"
        assert entry.reboots.value == "no"
        # A backup is a cluster act: `backup_full.sh` opens with `rbd list`.
        assert "cluster" in [item.value for item in entry.requires]
