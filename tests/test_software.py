# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The software updates: the check, what it brings back, and the update.

The check is a play written here, so its tasks are asserted as the whole of
what reaches a machine: a refresh of the package lists, a simulation, and
three readings. What each machine answered is a JSON document the controller
writes into the run's results directory, and the parser of apt's simulation is
tested on the lines apt prints.

The update is the upstream playbook, and what this service decides about it is
which machines it is sent to. The machine driving the run is refused whatever
launched it, because the playbook finishes its work after the reboot and the
controller would not be there to do it.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.runs import software
from app.trust import known_hosts
from tests.conftest import sign_in

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
    hypervisors:
      children:
        cluster_machines:
"""

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

# What `apt-get --simulate dist-upgrade` prints on a trixie machine, trimmed.
SIMULATION = """Reading package lists...
Building dependency tree...
Calculating upgrade...
The following NEW packages will be installed:
  linux-image-6.12.48+deb13-rt-amd64
The following packages will be upgraded:
  libc6 linux-image-rt-amd64 openssh-server
3 upgraded, 1 newly installed, 1 to remove and 0 not upgraded.
Remv linux-image-6.12.38+deb13-rt-amd64 [6.12.38-1]
Inst libc6 [2.41-12] (2.41-12+deb13u1 Debian:13.1/stable, Debian-Security:13/stable-security [amd64])
Inst linux-image-6.12.48+deb13-rt-amd64 (6.12.48-1 Debian-Security:13/stable-security [amd64])
Inst linux-image-rt-amd64 [6.12.43-1] (6.12.48-1 Debian-Security:13/stable-security [amd64])
Inst openssh-server [1:10.0p1-7] (1:10.0p1-7+deb13u1 Debian:13.1/stable [amd64])
Conf libc6 (2.41-12+deb13u1 Debian:13.1/stable, Debian-Security:13/stable-security [amd64])
Conf linux-image-6.12.48+deb13-rt-amd64 (6.12.48-1 Debian-Security:13/stable-security [amd64])
"""  # noqa: E501


def _answer(**overrides) -> str:
    """One machine's document, as the check's last task writes it."""
    answer = {
        "refresh_failed": False,
        "refresh_message": "",
        "simulation": SIMULATION,
        "simulation_error": "",
        "simulation_message": "",
        "simulation_rc": 0,
        "running_kernel": "6.12.43+deb13-rt-amd64",
        "kernels": [
            "/boot/vmlinuz-6.12.43+deb13-rt-amd64",
            "/boot/vmlinuz-6.12.9+deb13-rt-amd64",
        ],
        "reboot_required": False,
        "vg": "vg1",
        "volumes": ["root,16106127360", "swap,524288000", "varlog,5368709120"],
        "vg_free": "30064771072",
        "vg_message": "",
    }
    answer.update(overrides)
    return json.dumps(answer)


@pytest.fixture
def key_pair(tmp_path: Path) -> Path:
    path = tmp_path / "site_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True
    )
    return path


def _import(client: TestClient, document: str) -> None:
    response = client.post("/api/v1/inventory/import", json={"document": document})
    assert response.status_code == 200, response.text


def _cluster(client: TestClient, settings: Settings, key_pair: Path) -> None:
    """Three members, every one of them reachable from this one."""
    _import(client, CLUSTER)
    response = client.put(
        "/api/v1/trust/site-key", json={"material": key_pair.read_text()}
    )
    assert response.status_code == 200, response.text
    known_hosts.accept_peers(
        settings.known_hosts_file,
        {
            "192.168.200.126": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIelabo1"],
            "192.168.200.127": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIelabo2"],
        },
    )


def _wait(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/v1/runs/{run_id}").json()
        if record["state"] not in ("pending", "running"):
            return record
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish")


def _played(settings: Settings, run_id: str) -> dict:
    root = (
        settings.runs_dir
        / run_id
        / "collections/ansible_collections/seapath/ansible/playbooks"
    )
    documents = list(root.glob(f"{software.CHECK}.yaml"))
    assert len(documents) == 1, list(root.iterdir())
    return yaml.safe_load(documents[0].read_text())[0]


# What apt says it would do


def test_the_simulation_is_read_into_upgrades_installs_and_removals() -> None:
    simulation = software.parse_simulation(SIMULATION)

    assert [item.name for item in simulation.upgrades] == [
        "libc6",
        "linux-image-rt-amd64",
        "openssh-server",
    ]
    libc = simulation.upgrades[0]
    assert libc.current == "2.41-12"
    assert libc.candidate == "2.41-12+deb13u1"
    # Both origins, since the version is in both: that is how an operator
    # tells a security fix from a point release.
    assert libc.origin == "Debian:13.1/stable, Debian-Security:13/stable-security"
    assert simulation.upgrades[2].candidate == "1:10.0p1-7+deb13u1"

    assert [item.name for item in simulation.installs] == [
        "linux-image-6.12.48+deb13-rt-amd64"
    ]
    assert simulation.installs[0].current is None
    assert simulation.installs[0].kernel is True

    assert [item.name for item in simulation.removals] == [
        "linux-image-6.12.38+deb13-rt-amd64"
    ]
    assert simulation.removals[0].current == "6.12.38-1"


def test_a_machine_with_nothing_to_install_has_an_empty_simulation() -> None:
    simulation = software.parse_simulation(
        "Reading package lists...\n0 upgraded, 0 newly installed, 0 to remove "
        "and 0 not upgraded.\n"
    )

    assert simulation.upgrades == simulation.installs == simulation.removals == []


def test_a_kernel_installed_and_not_booted_is_pending() -> None:
    reading = software.parse_reading(
        "elabo1",
        _answer(
            running_kernel="6.12.9+deb13-rt-amd64",
            simulation="",
        ),
    )

    # 6.12.43 is newer than 6.12.9, which a comparison of text would get wrong.
    assert reading.newest_kernel == "6.12.43+deb13-rt-amd64"
    assert reading.kernel_pending is True
    assert reading.simulation.upgrades == []


def test_the_kernel_booted_being_the_newest_is_not_pending() -> None:
    reading = software.parse_reading("elabo1", _answer())

    assert reading.running_kernel == "6.12.43+deb13-rt-amd64"
    assert reading.kernel_pending is False


def test_a_mirror_that_cannot_be_reached_is_reported_beside_the_simulation() -> None:
    reading = software.parse_reading(
        "elabo1",
        _answer(
            refresh_failed=True,
            refresh_message="Failed to update apt cache: unknown reason",
        ),
    )

    assert reading.refresh_error == "Failed to update apt cache: unknown reason"
    assert len(reading.simulation.upgrades) == 3
    assert reading.error is None


def test_a_simulation_that_failed_says_why_and_lists_nothing() -> None:
    reading = software.parse_reading(
        "elabo1",
        _answer(
            simulation_rc=100,
            simulation_error="E: Could not get lock /var/lib/dpkg/lock-frontend.",
        ),
    )

    assert reading.error == "E: Could not get lock /var/lib/dpkg/lock-frontend."
    assert reading.simulation.upgrades == []


def test_a_volume_group_with_room_for_root_is_enough() -> None:
    room = software.parse_reading("elabo1", _answer()).snapshot

    assert room.enough is True
    assert room.note is None
    assert room.free_bytes == 30064771072
    assert room.root_bytes == 16106127360


def test_a_volume_group_with_too_little_room_is_refused_before_the_run() -> None:
    """What stopped the first real update: a local volume had taken the room."""
    room = software.parse_reading("elabo1", _answer(vg_free="1073741824")).snapshot

    assert room.enough is False
    assert "1.0 GiB free" in room.note


def test_less_room_than_root_is_accepted_and_said() -> None:
    room = software.parse_reading("elabo1", _answer(vg_free="8589934592")).snapshot

    assert room.enough is True
    assert "less than root" in room.note


def test_a_snapshot_left_by_an_earlier_update_is_refused() -> None:
    reading = software.parse_reading(
        "elabo1",
        _answer(volumes=["root,16106127360", "root-snap,16106127360"]),
    )

    assert reading.snapshot.leftover is True
    assert reading.snapshot.enough is False


def test_a_volume_group_that_could_not_be_read_says_why() -> None:
    room = software.parse_reading(
        "elabo1",
        _answer(vg_free="", vg_message='Volume group "vg1" not found'),
    ).snapshot

    assert room.enough is False
    assert room.note == 'Volume group "vg1" not found'


def test_an_unreadable_answer_is_an_error_and_not_a_crash() -> None:
    assert software.parse_reading("elabo1", "{not json").error


# The check


def test_the_check_refreshes_simulates_and_writes_nothing_on_a_machine(
    signed_in: TestClient, settings: Settings, key_pair: Path, run_adapter
) -> None:
    _cluster(signed_in, settings, key_pair)

    response = signed_in.post("/api/v1/software/check")

    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    play = _played(settings, run_id)
    assert play["hosts"] == "all"
    assert play["become"] is True
    tasks = play["tasks"]
    assert tasks[0]["ansible.builtin.apt"] == {"update_cache": True}
    assert tasks[1]["ansible.builtin.command"]["argv"] == [
        "apt-get",
        "--simulate",
        "dist-upgrade",
    ]
    assert tasks[2]["ansible.builtin.command"]["argv"] == ["uname", "-r"]
    # The volume group the update snapshots root in, named the way the
    # playbook names it.
    lvm = [task["ansible.builtin.command"]["argv"] for task in tasks[5:7]]
    assert [argv[0] for argv in lvm] == ["lvs", "vgs"]
    assert all(argv[-1] == "{{ vg_name | default('vg1') }}" for argv in lvm)
    # The one task that writes is the last, and it writes on the controller,
    # into this run's own results directory.
    writes = [task for task in tasks if "ansible.builtin.copy" in task]
    assert writes == [tasks[-1]]
    assert tasks[-1]["delegate_to"] == "localhost"
    assert tasks[-1]["become"] is False
    assert tasks[-1]["ansible.builtin.copy"]["dest"].startswith(
        "{{ software_results }}/"
    )

    request = run_adapter.requests[-1]
    assert request.extra_vars[software.RESULTS_VARIABLE] == str(
        settings.runs_dir / run_id / "results"
    )


def test_checking_is_an_operator_s_act(client: TestClient) -> None:
    viewer = sign_in(client, "viewer")

    assert viewer.post("/api/v1/software/check").status_code == 403


def test_the_page_draws_what_the_last_check_brought_back(
    signed_in: TestClient, settings: Settings, key_pair: Path
) -> None:
    _cluster(signed_in, settings, key_pair)
    run_id = signed_in.post("/api/v1/software/check").json()["run_id"]
    _wait(signed_in, run_id)
    results = settings.runs_dir / run_id / "results"
    (results / "software_elabo1.json").write_text(_answer())
    (results / "software_elabo2.json").write_text(_answer(simulation=""))

    view = signed_in.get("/api/v1/software").json()

    assert view["checked"]["id"] == run_id
    assert view["this_host"] == "seapath-machine"
    rows = {row["host"]: row for row in view["machines"]}
    assert sorted(rows) == ["elabo1", "elabo2", "seapath-machine"]
    assert len(rows["elabo1"]["reading"]["simulation"]["upgrades"]) == 3
    assert rows["elabo2"]["reading"]["simulation"]["upgrades"] == []
    # No answer from this one: said as absent, never as up to date.
    assert rows["seapath-machine"]["reading"] is None
    assert rows["seapath-machine"]["this_node"] is True


# The update


# The shape of the upstream playbook that matters here: the play that
# reboots takes the machines one at a time.
ROLLING = """---
- name: Make sure the debian_grub_bootcount is deployed
  hosts: all
  roles:
    - debian_grub_bootcount
- name: Update Debian Systems
  hosts: all
  serial: 1
  tasks:
    - name: Upgrade the OS (apt-get dist-upgrade)
      ansible.builtin.apt:
        upgrade: dist
"""


def _install_playbook(settings: Settings, content: str) -> None:
    path = settings.collections_path.joinpath(
        "ansible_collections/seapath/ansible/playbooks", "seapath_update_debian.yaml"
    )
    path.write_text(content)


def test_the_update_is_the_upstream_playbook_narrowed_to_the_machines_chosen(
    signed_in: TestClient, settings: Settings, key_pair: Path, run_adapter
) -> None:
    _cluster(signed_in, settings, key_pair)
    _install_playbook(settings, ROLLING)

    response = signed_in.post(
        "/api/v1/software/update", json={"hosts": ["elabo2", "elabo1"]}
    )

    assert response.status_code == 202, response.text
    request = run_adapter.requests[-1]
    assert request.playbook == "seapath.ansible.seapath_update_debian"
    assert request.limit == "elabo1:elabo2"
    assert request.extra_vars == {}


def test_an_older_playbook_is_sent_one_machine_at_a_time(
    signed_in: TestClient, settings: Settings, key_pair: Path, run_adapter
) -> None:
    """Without `serial: 1` it reboots every machine it is sent to at once."""
    _cluster(signed_in, settings, key_pair)
    _install_playbook(settings, ROLLING.replace("  serial: 1\n", ""))

    assert signed_in.get("/api/v1/software").json()["one_at_a_time"] is False
    refused = signed_in.post(
        "/api/v1/software/update", json={"hosts": ["elabo1", "elabo2"]}
    )
    alone = signed_in.post("/api/v1/software/update", json={"hosts": ["elabo1"]})

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "one_machine_at_a_time"
    assert alone.status_code == 202, alone.text
    assert run_adapter.requests[-1].limit == "elabo1"


def test_the_machine_driving_the_run_is_refused_and_the_others_named(
    signed_in: TestClient, settings: Settings, key_pair: Path, run_adapter
) -> None:
    _cluster(signed_in, settings, key_pair)
    _install_playbook(settings, ROLLING)

    response = signed_in.post(
        "/api/v1/software/update", json={"hosts": ["seapath-machine", "elabo1"]}
    )

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "controller_in_scope"
    assert "seapath-machine" in error["message"]
    assert "elabo1" in error["message"]
    assert run_adapter.requests == []


def test_the_deployment_page_cannot_send_the_update_to_this_machine_either(
    signed_in: TestClient, settings: Settings, key_pair: Path, run_adapter
) -> None:
    """The refusal belongs to the entry, whatever page launches it."""
    _cluster(signed_in, settings, key_pair)

    response = signed_in.post(
        "/api/v1/runs", json={"playbook": "seapath_update_debian"}
    )

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "controller_in_scope"
    assert error["detail"]["others"] == ["elabo1", "elabo2"]
    assert run_adapter.requests == []


def test_a_standalone_machine_is_told_it_cannot_update_itself_from_here(
    signed_in: TestClient,
) -> None:
    _import(signed_in, STANDALONE)

    response = signed_in.post(
        "/api/v1/software/update", json={"hosts": ["seapath-machine"]}
    )

    assert response.status_code == 409, response.text
    assert "another machine" in response.json()["error"]["message"]


def test_a_name_the_inventory_does_not_declare_is_refused(
    signed_in: TestClient, settings: Settings, key_pair: Path
) -> None:
    _cluster(signed_in, settings, key_pair)

    response = signed_in.post("/api/v1/software/update", json={"hosts": ["elabo9"]})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_machine"


def test_an_update_naming_no_machine_is_refused(signed_in: TestClient) -> None:
    _import(signed_in, CLUSTER)

    response = signed_in.post("/api/v1/software/update", json={"hosts": []})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_machine"


def test_updating_is_an_administrator_s_act(client: TestClient) -> None:
    operator = sign_in(client, "operator")

    response = operator.post("/api/v1/software/update", json={"hosts": ["elabo1"]})

    assert response.status_code == 403


def test_a_check_older_than_an_update_is_marked_stale(
    signed_in: TestClient, settings: Settings, key_pair: Path
) -> None:
    _cluster(signed_in, settings, key_pair)
    check = signed_in.post("/api/v1/software/check").json()["run_id"]
    _wait(signed_in, check)
    (settings.runs_dir / check / "results" / "software_elabo1.json").write_text(
        _answer()
    )
    update = signed_in.post(
        "/api/v1/software/update", json={"hosts": ["elabo1"]}
    ).json()["run_id"]
    _wait(signed_in, update)
    # The fake run reports the host it plays as `seapath-machine`, so the
    # record is told which machine it reached, as a real run's events would.
    record_file = settings.runs_dir / update / "run.json"
    record = json.loads(record_file.read_text())
    record["progress"]["hosts"] = {"elabo1": {"ok": 12, "changed": 5}}
    record_file.write_text(json.dumps(record))

    rows = {
        row["host"]: row for row in signed_in.get("/api/v1/software").json()["machines"]
    }

    assert rows["elabo1"]["stale"] is True
    assert rows["elabo1"]["last_update"]["id"] == update
    assert rows["elabo2"]["stale"] is False
    assert rows["elabo2"]["last_update"] is None


def test_the_page_is_served(signed_in: TestClient) -> None:
    response = signed_in.get("/updates")

    assert response.status_code == 200
    assert "updates.js" in response.text
