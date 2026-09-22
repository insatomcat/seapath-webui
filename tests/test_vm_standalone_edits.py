# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What a standalone guest has in place of the Metadata window.

Its domain, read with `virsh dumpxml` and defined again by a run of the module
the standalone role defines with; its pinning profile, which is an entry of
the inventory the role writes out on every run; and the shut down and start
that makes either take effect. See D66.
"""

from __future__ import annotations

import shlex

import yaml
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.hosts.remote import FakeRemoteRunner
from tests.test_vms import STANDALONE_WITH_DOMAINS, _declare_cluster, wait_for

DOMAIN = """<domain type='kvm'>
  <name>ABBICT</name>
  <uuid>6a1f6c1e-0b8e-4c6e-9a53-2f1f0f7f0a01</uuid>
  <memory unit='KiB'>2097152</memory>
  <vcpu placement='static'>2</vcpu>
</domain>
"""

PROFILE = "version: 1\nvcpus:\n  - pool: rt\n"


def _standalone(client: TestClient, remote_runner: FakeRemoteRunner) -> None:
    response = client.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_DOMAINS}
    )
    assert response.status_code == 200, response.text
    remote_runner.answers["dumpxml"] = DOMAIN


def _written(settings: Settings, run_id: str, name: str) -> list[dict]:
    found = list((settings.runs_dir / run_id).rglob(f"{name}.yaml"))
    assert len(found) == 1
    return yaml.safe_load(found[0].read_text())


# The domain.


def test_the_domain_is_read_inactive_with_virsh_on_its_machine(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    _standalone(signed_in, remote_runner)

    response = signed_in.get("/api/v1/vms/ABBICT/xml")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "guest": "ABBICT",
        "host": "seapath-machine",
        "xml": DOMAIN,
    }
    request = remote_runner.requests[-1]
    assert request.address == "192.168.200.125"
    # What `virsh edit` edits, and no password a display carries.
    assert shlex.split(shlex.split(request.command)[4]) == [
        "exec",
        "virsh",
        "-c",
        "qemu:///system",
        "dumpxml",
        "--inactive",
        "ABBICT",
    ]


def test_an_edited_domain_is_defined_by_a_run_of_the_upstream_module(
    signed_in: TestClient, remote_runner: FakeRemoteRunner, settings: Settings
) -> None:
    _standalone(signed_in, remote_runner)
    edited = DOMAIN.replace("<vcpu placement='static'>2", "<vcpu placement='static'>4")

    response = signed_in.put("/api/v1/vms/ABBICT/xml", json={"xml": edited})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True
    wait_for(signed_in, body["run_id"])
    play = _written(settings, body["run_id"], "vm_define")
    assert play[0]["hosts"] == "seapath-machine"
    assert play[0]["tasks"] == [
        {
            "name": "Define ABBICT from the edited XML",
            "community.libvirt.virt": {"command": "define", "xml": edited},
        }
    ]
    # Nothing about the guest reaches the inventory: it is an act on one
    # domain, recorded with the run that made it.
    assert "vcpu" not in signed_in.get("/api/v1/inventory/raw").text


def test_a_domain_that_did_not_change_launches_nothing(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    _standalone(signed_in, remote_runner)

    response = signed_in.put("/api/v1/vms/ABBICT/xml", json={"xml": DOMAIN + "\n\n"})

    assert response.json() == {
        "guest": "ABBICT",
        "changed": False,
        "run_id": None,
        "state": None,
    }


def test_a_domain_named_otherwise_is_refused(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    # Defining it would add a guest beside this one, not change it.
    _standalone(signed_in, remote_runner)

    response = signed_in.put(
        "/api/v1/vms/ABBICT/xml",
        json={"xml": DOMAIN.replace("<name>ABBICT", "<name>OTHER")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_domain"


def test_a_domain_under_another_uuid_is_refused(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    _standalone(signed_in, remote_runner)

    response = signed_in.put(
        "/api/v1/vms/ABBICT/xml",
        json={"xml": DOMAIN.replace("0a01</uuid>", "0a02</uuid>")},
    )

    assert response.status_code == 400
    assert "Keep the one it holds" in response.json()["error"]["message"]


def test_a_document_that_is_not_a_domain_is_refused(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    _standalone(signed_in, remote_runner)

    for xml in (
        "<domain><name>ABBICT</name>",
        "<network><name>ABBICT</name></network>",
    ):
        response = signed_in.put("/api/v1/vms/ABBICT/xml", json={"xml": xml})
        assert response.status_code == 400, xml


def test_a_cluster_guests_domain_is_its_metadata(signed_in: TestClient) -> None:
    _declare_cluster(signed_in)

    response = signed_in.get("/api/v1/vms/vm-guest1/xml")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_standalone"


def test_only_an_administrator_reads_or_defines_a_domain(
    signed_in_viewer: TestClient,
) -> None:
    assert signed_in_viewer.get("/api/v1/vms/ABBICT/xml").status_code == 403
    assert (
        signed_in_viewer.put("/api/v1/vms/ABBICT/xml", json={"xml": DOMAIN}).status_code
        == 403
    )


# Shutting down and starting.


def test_restarting_waits_for_the_guest_to_be_shut_off_before_starting_it(
    signed_in: TestClient, remote_runner: FakeRemoteRunner, settings: Settings
) -> None:
    # `shutdown` only asks the guest, so a start right behind it would find it
    # still running and do nothing.
    _standalone(signed_in, remote_runner)

    response = signed_in.post("/api/v1/vms/ABBICT/restart")

    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    wait_for(signed_in, run_id)
    tasks = _written(settings, run_id, "vm_restart")[0]["tasks"]
    assert [task["community.libvirt.virt"] for task in tasks] == [
        {"name": "ABBICT", "state": "shutdown"},
        {"name": "ABBICT", "command": "status"},
        {"name": "ABBICT", "state": "running"},
    ]
    assert tasks[1]["until"] == "seapath_webui_domain.status == 'shutdown'"
    assert tasks[1]["retries"] * tasks[1]["delay"] == 300


def test_a_cluster_guest_is_not_restarted_behind_pacemaker(
    signed_in: TestClient,
) -> None:
    _declare_cluster(signed_in)

    response = signed_in.post("/api/v1/vms/vm-guest1/restart")

    assert response.status_code == 409


# The pinning profile.


def _entries(client: TestClient) -> dict:
    return yaml.safe_load(client.get("/api/v1/inventory/raw").text)["all"]["children"][
        "VMs"
    ]["hosts"]


def test_a_pinning_profile_is_a_commit_on_the_guests_entry(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    _standalone(signed_in, remote_runner)

    response = signed_in.put(
        "/api/v1/vms/ABBICT/pinning-profile", json={"profile": PROFILE}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit"]
    assert body["message"].startswith("vms: pinning profile of ABBICT")
    # And the run that puts it on the machine, launched by the same request.
    assert body["run_id"]
    assert _entries(signed_in)["ABBICT"]["vm_pinning_profile"] == PROFILE
    guests = {g["name"]: g for g in signed_in.get("/api/v1/vms").json()["guests"]}
    assert guests["ABBICT"]["pinning_profile"] == PROFILE
    # Only that entry moved.
    assert _entries(signed_in)["EITCS"] is None


def test_the_same_profile_again_is_no_commit(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    _standalone(signed_in, remote_runner)
    signed_in.put("/api/v1/vms/ABBICT/pinning-profile", json={"profile": PROFILE})

    again = signed_in.put(
        "/api/v1/vms/ABBICT/pinning-profile", json={"profile": PROFILE}
    )

    assert again.json()["commit"] is None
    assert again.json()["run_id"] is None


def test_an_empty_profile_takes_the_variable_out(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    # The role removes the file of a guest that no longer names one.
    _standalone(signed_in, remote_runner)
    signed_in.put("/api/v1/vms/ABBICT/pinning-profile", json={"profile": PROFILE})

    response = signed_in.put("/api/v1/vms/ABBICT/pinning-profile", json={"profile": ""})

    assert response.status_code == 200, response.text
    assert response.json()["commit"]
    assert not (_entries(signed_in)["ABBICT"] or {}).get("vm_pinning_profile")


def test_a_profile_the_hook_could_not_read_is_refused(
    signed_in: TestClient, remote_runner: FakeRemoteRunner
) -> None:
    _standalone(signed_in, remote_runner)

    for profile in ("version: [1", "- a list"):
        response = signed_in.put(
            "/api/v1/vms/ABBICT/pinning-profile", json={"profile": profile}
        )
        assert response.status_code == 400, profile


def test_a_cluster_guests_profile_is_its_metadata(signed_in: TestClient) -> None:
    # `deploy_vms_cluster` hands it to `vm_manager` at creation only.
    _declare_cluster(signed_in)

    response = signed_in.put(
        "/api/v1/vms/vm-guest1/pinning-profile", json={"profile": PROFILE}
    )

    assert response.status_code == 400
    assert "Metadata window" in response.json()["error"]["message"]


def test_the_profile_run_writes_the_committed_value_and_nothing_else(
    signed_in: TestClient, remote_runner: FakeRemoteRunner, settings: Settings
) -> None:
    # The two tasks the standalone role writes the file with, on one guest,
    # reading the value from the inventory the run stages: what lands on the
    # machine is what the role would write, so a convergence changes nothing.
    _standalone(signed_in, remote_runner)

    body = signed_in.put(
        "/api/v1/vms/ABBICT/pinning-profile", json={"profile": PROFILE}
    ).json()

    wait_for(signed_in, body["run_id"])
    play = _written(settings, body["run_id"], "vm_pinning_profile")[0]
    assert play["hosts"] == "seapath-machine"
    assert play["vars"] == {"seapath_webui_guest": "ABBICT"}
    copy, remove = play["tasks"]
    assert copy["ansible.builtin.copy"]["content"] == (
        "{{ hostvars[seapath_webui_guest].vm_pinning_profile }}"
    )
    assert copy["ansible.builtin.copy"]["dest"] == (
        "/etc/seapath/alloc.d/{{ seapath_webui_guest }}.yaml"
    )
    assert remove["ansible.builtin.file"]["state"] == "absent"
    assert remove["when"] == (
        "hostvars[seapath_webui_guest].vm_pinning_profile is not defined"
    )


def test_asking_for_a_restart_ends_the_same_run_with_it(
    signed_in: TestClient, remote_runner: FakeRemoteRunner, settings: Settings
) -> None:
    # One run whatever was chosen, so it goes on in the background and nobody
    # has to come back to launch a second one.
    _standalone(signed_in, remote_runner)
    edited = DOMAIN.replace("<vcpu placement='static'>2", "<vcpu placement='static'>4")

    defined = signed_in.put(
        "/api/v1/vms/ABBICT/xml", json={"xml": edited, "restart": True}
    ).json()
    profiled = signed_in.put(
        "/api/v1/vms/ABBICT/pinning-profile",
        json={"profile": PROFILE, "restart": True},
    ).json()

    for run_id, record in (
        (defined["run_id"], "vm_define"),
        (profiled["run_id"], "vm_pinning_profile"),
    ):
        wait_for(signed_in, run_id)
        tasks = _written(settings, run_id, record)[0]["tasks"]
        assert [task["name"] for task in tasks[-3:]] == [
            "Shut ABBICT down",
            "Wait for ABBICT to be shut off",
            "Start ABBICT",
        ]


def test_without_a_restart_the_guest_is_left_running(
    signed_in: TestClient, remote_runner: FakeRemoteRunner, settings: Settings
) -> None:
    _standalone(signed_in, remote_runner)

    body = signed_in.put(
        "/api/v1/vms/ABBICT/pinning-profile", json={"profile": PROFILE}
    ).json()

    wait_for(signed_in, body["run_id"])
    tasks = _written(settings, body["run_id"], "vm_pinning_profile")[0]["tasks"]
    assert not any("community.libvirt.virt" in task for task in tasks)
