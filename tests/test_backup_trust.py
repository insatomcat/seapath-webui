# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The connection from the cluster members to the backup server.

Three things carry the value. What each member is asked, because it decides
whether the page says a backup would connect, and it has to connect exactly the
way `rsync -e` will. What is committed, because a `remote_shell` that lost the
site's port, or never named the dedicated key, pushes with the wrong key or to
the wrong place. And the one write this service makes on a machine that is not
a SEAPATH machine: the exact command, the append that never rewrites, the host
key that has to be the confirmed one, and a password that appears nowhere but
in the one connection it was typed for.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.inventory.resolve import resolve
from app.runs.backup import BackupTarget
from app.services.backup_trust import (
    KEY_PATH,
    parse_space,
    probe_command,
    with_identity,
)
from app.trust.backup_server import (
    InstallRefused,
    InstallRequest,
    append_script,
    check,
    install_command,
)
from app.trust.keyscan import ScanFailed, ScannedKey
from tests.conftest import sign_in
from tests.test_backup import CLUSTER, CONFIGURED

HOST_KEY_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIGb0mq8zvWm1sZ6s0xZsYt1vJ6Hn5sJwqKx0L3g0H1aB"
HOST_KEY = f"backup.example.org ssh-ed25519 {HOST_KEY_BLOB}"
KEY1 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPbWUaCiTNdA3Clj542jYa0rsy0NhPE8yknBcKVnCDuO"
    " backup_restore@elabo1"
)
KEY2 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILhJWlRFSQvB4eDF/FXWd4itfNI6SOhPJtn+sbGiJt3q"
    " backup_restore@elabo2"
)
PASSWORD = "c0rrect-h0rse-battery"

PREPARED = CONFIGURED.replace(
    "        backup_restore_remote_shell: ssh\n",
    "        backup_restore_remote_shell: ssh -i /root/.ssh/backup_restore_ed25519\n"
    "        backup_restore_ssh_key: /root/.ssh/backup_restore_ed25519\n"
    "        backup_restore_remote_host_keys:\n"
    f"          - {HOST_KEY}\n",
)


def _import(client: TestClient, settings: str = CONFIGURED) -> None:
    response = client.post(
        "/api/v1/inventory/import", json={"document": CLUSTER.format(settings=settings)}
    )
    assert response.status_code == 200, response.text


def _members_answer(remote_runner, answers: dict[str, str]) -> None:
    """Each member answers for itself, by the address it is asked at."""
    by_address = {
        "192.168.200.125": answers.get("seapath-machine", ""),
        "192.168.200.126": answers.get("elabo1", ""),
        "192.168.200.127": answers.get("elabo2", ""),
    }

    def run(request):
        remote_runner.requests.append(request)
        return by_address[request.address]

    remote_runner.run = run


def _scanner(found: list[ScannedKey] | None = None, failure: str | None = None):
    asked = []

    def scan(addresses, port=22):
        asked.append((addresses, port))
        if failure:
            raise ScanFailed(failure)
        return found or []

    scan.asked = asked
    return scan


# Reading


def test_every_member_is_asked_for_its_key_and_to_reach_the_server(
    signed_in: TestClient, remote_runner
) -> None:
    _import(signed_in, PREPARED)
    _members_answer(
        remote_runner,
        {
            "elabo1": f"pub {KEY1}\nreach ok\n"
            "space /dev/sdb1 976284628 12 931000000 1% /srv\n",
            "elabo2": f"pub {KEY2}\nreach failed Permission denied (publickey).\n",
            "seapath-machine": "reach failed No such file or directory\n",
        },
    )

    view = signed_in.get("/api/v1/backup/connection").json()

    assert sorted(request.address for request in remote_runner.requests) == [
        "192.168.200.125",
        "192.168.200.126",
        "192.168.200.127",
    ]
    command = remote_runner.requests[0].command
    assert command.startswith("sudo -n /bin/sh -c ")
    assert f"{KEY_PATH}.pub" in command
    assert "BatchMode=yes" in command
    # The connection a backup makes, measuring where the backups land.
    script = shlex.split(command)[-1]
    assert "backup@backup.example.org 'df -Pk /srv/seapath-backups/ 2>&1" in script
    members = {member["host"]: member for member in view["members"]}
    assert members["elabo1"] == {
        "host": "elabo1",
        "key": KEY1,
        "reaches": True,
        "message": "",
    }
    assert members["elabo2"]["reaches"] is False
    assert "Permission denied" in members["elabo2"]["message"]
    assert members["seapath-machine"]["key"] is None
    assert view["key_path"] == KEY_PATH
    assert view["uses_key"] is True
    assert view["host_keys"][0]["line"] == HOST_KEY
    assert view["host_keys"][0]["fingerprint"].startswith("SHA256:")
    assert view["space"] == {
        "read_from": "elabo1",
        "directory": "/srv/seapath-backups/",
        "mountpoint": "/srv",
        "size_bytes": 976284628 * 1024,
        "free_bytes": 931000000 * 1024,
        "note": "",
    }


def test_the_room_on_the_server_is_read_from_the_member_the_backups_run_on(
    signed_in: TestClient, remote_runner
) -> None:
    _import(signed_in, PREPARED)
    _members_answer(
        remote_runner,
        {
            "seapath-machine": "reach ok\nspace /dev/x 100 0 7 0% /a\n",
            "elabo1": "reach ok\nspace /dev/x 100 0 42 0% /b\n",
        },
    )

    space = signed_in.get("/api/v1/backup/connection").json()["space"]

    assert space["read_from"] == "elabo1"
    assert space["free_bytes"] == 42 * 1024


def test_a_member_whose_reading_has_no_number_gives_way_to_one_that_has(
    signed_in: TestClient, remote_runner
) -> None:
    """The same directory on the same server: any member's measure will do."""
    _import(signed_in, PREPARED)
    _members_answer(
        remote_runner,
        {
            "seapath-machine": "reach ok\nspace /dev/x 100 0 7 0% /a\n",
            "elabo1": "reach ok\nspace \n",
        },
    )

    space = signed_in.get("/api/v1/backup/connection").json()["space"]

    assert space["read_from"] == "seapath-machine"
    assert space["free_bytes"] == 7 * 1024


def test_what_the_connection_prints_on_stderr_is_not_taken_for_df(
    tmp_path: Path,
) -> None:
    """A warning the server's shell prints after `df` answered, as sshd may order it.

    The generated command runs for real, against an `ssh` that runs the remote
    command locally and then complains, and a `sudo` that only steps aside.
    """
    for name, body in (
        (
            "ssh",
            'for a; do cmd=$a; done\nsh -c "$cmd"\n'
            "echo 'bash: warning: setlocale: LC_ALL: cannot change locale' >&2\n",
        ),
        ("sudo", 'shift\nexec "$@"\n'),
        (
            "df",
            "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
            "echo '/dev/sdb1 100 10 90 10% /srv'\n",
        ),
    ):
        script = tmp_path / name
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
    command = probe_command(
        BackupTarget(
            remote_serv="backup@server", remote_shell="ssh", remote_dir="/srv/"
        ),
        "",
    )

    answer = subprocess.run(
        ["/bin/sh", "-c", command],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert answer == "reach ok\nspace /dev/sdb1 100 10 90 10% /srv\n"


def test_a_directory_the_server_cannot_measure_is_said_with_df_s_words() -> None:
    space = parse_space(
        "df: /srv/seapath-backups/: No such file or directory",
        "elabo1",
        "/srv/seapath-backups/",
    )

    assert space.free_bytes is None
    assert "No such file or directory" in space.note


def test_df_answers_in_kibibytes() -> None:
    """`-k` is the one unit POSIX `df -P` guarantees, whatever the server is."""
    assert parse_space("/dev/sdb1 10 1 9 10% /srv", "h", "/srv/").free_bytes == 9216


def test_a_member_that_cannot_be_asked_is_not_reported_as_refused(
    signed_in: TestClient, remote_runner
) -> None:
    """Whether it reaches the server is unknown, which is not the same as no."""
    _import(signed_in)
    remote_runner.refusal = "Connection timed out"

    view = signed_in.get("/api/v1/backup/connection").json()

    assert {member["reaches"] for member in view["members"]} == {None}
    assert "Connection timed out" in view["members"][0]["message"]


def test_a_site_pushing_with_root_s_own_key_is_probed_as_it_pushes() -> None:
    """No dedicated key yet: the probe asks for no key file and uses the shell."""
    command = probe_command(
        BackupTarget(remote_serv="backup@server", remote_shell="ssh -p 2222"), ""
    )

    assert ".pub" not in command
    assert "ssh -o BatchMode=yes -o ConnectTimeout=10 -p 2222 backup@server" in (
        command
    )


# The server's host key


def test_the_server_s_host_keys_are_read_under_the_name_the_members_use(
    signed_in: TestClient,
) -> None:
    _import(
        signed_in,
        CONFIGURED.replace(
            "backup_restore_remote_shell: ssh\n",
            "backup_restore_remote_shell: ssh -p 2222\n",
        ),
    )
    scan = _scanner(
        [
            ScannedKey(
                address="backup.example.org",
                key_type="ssh-ed25519",
                key=f"ssh-ed25519 {HOST_KEY_BLOB}",
                fingerprint="SHA256:abc",
            )
        ]
    )
    signed_in.app.state.backup_trust_service.scanner = scan

    response = signed_in.post("/api/v1/backup/connection/scan")

    assert response.status_code == 200, response.text
    assert scan.asked == [(["backup.example.org"], 2222)]
    assert response.json() == [
        {
            "key_type": "ssh-ed25519",
            "fingerprint": "SHA256:abc",
            "line": f"[backup.example.org]:2222 ssh-ed25519 {HOST_KEY_BLOB}",
        }
    ]


def test_a_server_that_does_not_answer_the_scan_says_so(signed_in: TestClient) -> None:
    _import(signed_in)
    signed_in.app.state.backup_trust_service.scanner = _scanner(
        failure="No host key answered at backup.example.org."
    )

    response = signed_in.post("/api/v1/backup/connection/scan")

    assert response.status_code == 502
    assert "No host key answered" in response.json()["error"]["message"]


# Preparing


def _cluster_vars(client: TestClient) -> dict:
    document = client.get("/api/v1/inventory/raw").text
    return resolve(document)["elabo1"]


def test_the_confirmed_host_key_the_dedicated_key_and_the_shell_are_one_commit(
    signed_in: TestClient,
) -> None:
    _import(
        signed_in,
        CONFIGURED.replace(
            "backup_restore_remote_shell: ssh\n",
            "backup_restore_remote_shell: ssh -p 22 -o Compression=no\n",
        ),
    )

    response = signed_in.put(
        "/api/v1/backup/connection/key", json={"host_keys": [HOST_KEY]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["commit"]
    held = _cluster_vars(signed_in)
    assert held["backup_restore_ssh_key"] == KEY_PATH
    assert held["backup_restore_remote_host_keys"] == [HOST_KEY]
    # The site's own options stay, and the dedicated key is named, so the
    # backups are pushed with it rather than with root's default key.
    assert held["backup_restore_remote_shell"] == (
        f"ssh -i {KEY_PATH} -p 22 -o Compression=no"
    )


def test_a_host_key_of_another_server_is_refused(signed_in: TestClient) -> None:
    _import(signed_in)

    response = signed_in.put(
        "/api/v1/backup/connection/key",
        json={"host_keys": [f"elsewhere.example.org ssh-ed25519 {HOST_KEY_BLOB}"]},
    )

    assert response.status_code == 400
    assert "not a host key of backup.example.org" in (
        response.json()["error"]["message"]
    )


def test_no_host_key_is_refused(signed_in: TestClient) -> None:
    _import(signed_in)

    response = signed_in.put("/api/v1/backup/connection/key", json={"host_keys": []})

    assert response.status_code == 400
    assert "at least one host key" in response.json()["error"]["message"]


def test_a_shell_that_names_a_key_already_keeps_it() -> None:
    assert with_identity("ssh -i /root/.ssh/site_key", KEY_PATH) == (
        "ssh -i /root/.ssh/site_key"
    )
    assert with_identity("ssh", KEY_PATH) == f"ssh -i {KEY_PATH}"


# Installing the keys on the server


def test_the_members_keys_are_installed_with_the_password_typed_once(
    signed_in: TestClient, remote_runner, key_installer, caplog
) -> None:
    _import(signed_in, PREPARED)
    _members_answer(
        remote_runner,
        {
            "elabo1": f"pub {KEY1}\nreach failed Permission denied\n",
            "elabo2": f"pub {KEY2}\nreach failed Permission denied\n",
            "seapath-machine": "reach failed Permission denied\n",
        },
    )
    caplog.set_level(logging.DEBUG)

    response = signed_in.post(
        "/api/v1/backup/connection/install", json={"password": PASSWORD}
    )

    assert response.status_code == 200, response.text
    asked = key_installer.requests[0]
    assert asked.server == "backup@backup.example.org"
    assert asked.port == 22
    assert asked.known_hosts == (HOST_KEY,)
    assert sorted(asked.keys) == sorted([KEY1, KEY2])
    assert asked.password == PASSWORD
    # Never in the answer, never in a log line, never in the request's repr.
    assert PASSWORD not in response.text
    assert PASSWORD not in caplog.text
    assert PASSWORD not in repr(asked)
    # The member with no key yet is named rather than silently skipped.
    assert "seapath-machine had none yet" in response.json()["note"]


def test_no_password_is_sent_before_the_host_key_is_confirmed(
    signed_in: TestClient, remote_runner, key_installer
) -> None:
    _import(signed_in)
    _members_answer(remote_runner, {"elabo1": f"pub {KEY1}\n"})

    response = signed_in.post(
        "/api/v1/backup/connection/install", json={"password": PASSWORD}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "host_key_unconfirmed"
    assert key_installer.requests == []


def test_nothing_is_installed_before_the_role_generated_a_key(
    signed_in: TestClient, remote_runner, key_installer
) -> None:
    _import(signed_in, PREPARED)
    _members_answer(remote_runner, {})

    response = signed_in.post(
        "/api/v1/backup/connection/install", json={"password": PASSWORD}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_backup_key"
    assert key_installer.requests == []


def test_what_ssh_answered_is_what_the_page_is_told(
    signed_in: TestClient, remote_runner, key_installer
) -> None:
    _import(signed_in, PREPARED)
    _members_answer(remote_runner, {"elabo1": f"pub {KEY1}\n"})
    key_installer.refusal = "backup@backup.example.org: Permission denied."

    response = signed_in.post(
        "/api/v1/backup/connection/install", json={"password": PASSWORD}
    )

    assert response.status_code == 502
    assert "Permission denied" in response.json()["error"]["message"]
    assert PASSWORD not in response.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/backup/connection/scan", None),
        ("put", "/api/v1/backup/connection/key", {"host_keys": [HOST_KEY]}),
        ("post", "/api/v1/backup/connection/install", {"password": PASSWORD}),
    ],
)
def test_the_trust_to_the_backup_server_is_an_administrator_s_act(
    client: TestClient, method: str, path: str, body
) -> None:
    sign_in(client, "operator")

    response = getattr(client, method)(path, json=body)

    assert response.status_code == 403


# The write on the server, which is the whole of what reaches it


def _request(**fields) -> InstallRequest:
    base = {
        "server": "backup@backup.example.org",
        "port": 22,
        "known_hosts": (HOST_KEY,),
        "keys": (KEY1, KEY2),
        "password": PASSWORD,
    }
    base.update(fields)
    return InstallRequest(**base)


def test_the_connection_offers_a_password_once_and_trusts_one_host_key() -> None:
    argv = install_command(_request(port=2222), Path("/tmp/x/known_hosts"))

    assert argv[:-1] == [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "UserKnownHostsFile=/tmp/x/known_hosts",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "PreferredAuthentications=keyboard-interactive,password",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ConnectTimeout=10",
        "-p",
        "2222",
        "backup@backup.example.org",
    ]
    # The password is on no command line.
    assert PASSWORD not in " ".join(argv)


def test_the_keys_are_appended_and_nothing_is_rewritten() -> None:
    script = append_script((KEY1,))

    assert script == (
        "umask 077; "
        "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys || exit 1; "
        "f=~/.ssh/authorized_keys; "
        'if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then echo >> "$f"; fi; '
        f"for k in '{KEY1}'; do "
        'grep -qxF "$k" "$f" || printf \'%s\\n\' "$k" >> "$f" || exit 1; '
        "done"
    )
    # Every write to the file is an append: no `>` that would truncate it.
    assert '> "$f"' not in script.replace('>> "$f"', "")


def test_only_the_keys_the_role_generated_are_installed() -> None:
    check(_request())
    with pytest.raises(InstallRefused):
        check(_request(keys=("ssh-rsa AAAAB3Nza backup_restore@elabo1",)))
    with pytest.raises(InstallRefused):
        check(_request(keys=(KEY1 + "; rm -rf ~",)))
    with pytest.raises(InstallRefused):
        check(_request(keys=()))
    with pytest.raises(InstallRefused):
        check(_request(known_hosts=()))
