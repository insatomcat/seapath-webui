# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The VMs view: the definition, its files, and Pacemaker's line for it.

Three readings that live on three other pages, joined on the name. What makes
the join legitimate is that `vm_manager` names the Pacemaker resource after the
VM, and the VM after its host key in the inventory, so the same string is the
entry, the domain and the resource.

Read only, and the tests hold that: there is no route here that changes a
guest. Its definition is a commit on `/inventory` and its deployment is a run
on `/runs`.
"""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.runs.store import RunStore

# The fake cluster runs `vm-guest1` and `vm-guest2`, and `vm-guest3` failed
# where it last ran. Declaring two of the three is what lets one row be a guest
# the cluster runs and the inventory does not.
GUESTS = """
VMs:
  hosts:
    vm-guest1:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest1.qcow2"
    vm-guest3:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest3.qcow2"
      force: true
      enable: false
"""


def _declare(client: TestClient) -> None:
    document = client.get("/api/v1/inventory/raw").text
    response = client.post(
        "/api/v1/inventory/import", json={"document": document + GUESTS}
    )
    assert response.status_code == 200, response.text


def test_a_node_with_no_guest_says_where_one_is_written(
    signed_in: TestClient,
) -> None:
    payload = signed_in.get("/api/v1/vms").json()

    assert payload["guests"] == []
    assert "`VMs` group" in payload["note"]
    # And which playbook would deploy them, which is the seeded machine's mode.
    assert payload["playbook"] == "deploy_vms_standalone"


def test_the_guests_are_the_members_of_the_vms_group(signed_in: TestClient) -> None:
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()

    assert [item["name"] for item in payload["guests"]] == ["vm-guest1", "vm-guest3"]
    guest = payload["guests"][1]
    assert guest["vm_disk"] == "../files/guest3.qcow2"
    # `force` destroys and recreates the guest on the next deployment run, and
    # `enable` decides whether it is started. Both are on the row an operator
    # reads before launching one.
    assert guest["force"] is True
    assert guest["enable"] is False


def test_a_guest_carries_pacemakers_line_for_it(signed_in: TestClient) -> None:
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()
    running, failed = payload["guests"]

    assert running["resource"]["role"] == "started"
    assert running["resource"]["node"] == "seapath-machine"
    assert failed["resource"]["failed"] is True
    assert failed["resource"]["fail_count"] == 3


def test_a_guest_the_cluster_runs_and_the_inventory_ignores_is_named(
    signed_in: TestClient,
) -> None:
    # It keeps running, a convergence will not touch it, and a guest added
    # under that name later would collide with what is already there.
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()

    assert [item["id"] for item in payload["undeclared"]] == ["vm-guest2"]


def test_a_fencing_device_is_not_a_guest(signed_in: TestClient) -> None:
    # The exporter reports every resource of the cluster. Only the ones
    # `vm_manager` created are VMs, and a stonith device listed among them
    # would be the page saying something false about the inventory.
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()

    names = [item["id"] for item in payload["undeclared"]]
    assert not [name for name in names if name.startswith("fence-")]


def test_a_file_no_run_would_find_is_flagged_where_the_guest_is(
    signed_in: TestClient, settings: Settings
) -> None:
    # With `any_errors_fatal`, a copy that cannot find its source ends the
    # deployment on every host at once, three minutes in. The answer belongs
    # before the run.
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()
    missing = {
        reference["value"]
        for guest in payload["guests"]
        for reference in guest["files"]
        if not reference["found"]
    }
    assert "../files/guest1.qcow2" in missing

    # Uploaded to the store that holds the large files, and the same page says
    # so without a commit anywhere.
    (settings.artefacts_dir / "files").mkdir(parents=True, exist_ok=True)
    (settings.artefacts_dir / "files/guest1.qcow2").write_bytes(b"not really an image")

    payload = signed_in.get("/api/v1/vms").json()
    found = {
        reference["value"]: reference
        for guest in payload["guests"]
        for reference in guest["files"]
    }
    assert found["../files/guest1.qcow2"]["found"] is True
    assert found["../files/guest1.qcow2"]["where"] == "artefacts"


def test_the_page_says_where_the_runtime_column_comes_from(
    signed_in: TestClient,
) -> None:
    _declare(signed_in)

    payload = signed_in.get("/api/v1/vms").json()

    assert "ha_cluster_exporter" in payload["runtime_note"]
    assert "vm_manager" in payload["runtime_note"]


def test_a_viewer_may_read_the_guests(signed_in_viewer: TestClient) -> None:
    assert signed_in_viewer.get("/api/v1/vms").status_code == 200


def test_the_guests_need_a_session(client: TestClient) -> None:
    assert client.get("/api/v1/vms").status_code == 401


# 3. Adding one, which is the act this page performs.


def test_adding_a_guest_writes_it_into_the_vms_group(
    signed_in: TestClient, settings: Settings
) -> None:
    response = signed_in.post(
        "/api/v1/vms",
        json={
            "name": "newvm",
            "vm_disk": "../files/newvm.qcow2",
            "vm_template": "../files/newvm.xml.j2",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "newvm"
    # And the run to launch next, so the page does not have to work it out.
    assert body["playbook"] == "deploy_vms_standalone"

    written = (settings.inventory_dir / "inventory.yaml").read_text()
    assert "VMs:" in written
    assert "newvm:" in written
    assert signed_in.get("/api/v1/vms").json()["guests"][0]["name"] == "newvm"


def test_the_group_is_created_once_and_added_to_afterwards(
    signed_in: TestClient, settings: Settings
) -> None:
    signed_in.post("/api/v1/vms", json={"name": "first"})
    signed_in.post("/api/v1/vms", json={"name": "second"})

    written = (settings.inventory_dir / "inventory.yaml").read_text()

    assert written.count("VMs:") == 1
    assert [item["name"] for item in signed_in.get("/api/v1/vms").json()["guests"]] == [
        "first",
        "second",
    ]


def test_adding_a_guest_leaves_the_machines_alone(
    signed_in: TestClient, settings: Settings
) -> None:
    # The write is a splice and `fidelity` checks it: the whole point of doing
    # this through the inventory rather than around it is that the file keeps
    # meaning what it meant.
    before = signed_in.get("/api/v1/inventory").json()["inventory"]["hosts"]

    signed_in.post("/api/v1/vms", json={"name": "newvm"})

    assert signed_in.get("/api/v1/inventory").json()["inventory"]["hosts"] == before


def test_adding_a_guest_is_a_commit_that_names_it(signed_in: TestClient) -> None:
    signed_in.post("/api/v1/vms", json={"name": "newvm"})

    history = signed_in.get("/api/v1/inventory/history").json()

    assert history[0]["message"] == "vms: declare newvm"
    assert history[0]["author"] == "admin"


def test_a_guest_declined_at_start_says_so_and_the_others_say_nothing(
    signed_in: TestClient, settings: Settings
) -> None:
    # `enable` defaults to true in the roles, so an entry spelling it out says
    # nothing and reads as if it did.
    signed_in.post("/api/v1/vms", json={"name": "running"})
    signed_in.post("/api/v1/vms", json={"name": "stopped", "enable": False})

    written = (settings.inventory_dir / "inventory.yaml").read_text()

    assert "enable: false" in written
    assert "enable: true" not in written


def test_a_name_that_is_already_in_the_inventory_is_refused(
    signed_in: TestClient,
) -> None:
    # It becomes the libvirt domain and the Pacemaker resource, and a machine
    # of the inventory holds the name just as firmly as a guest does.
    signed_in.post("/api/v1/vms", json={"name": "newvm"})

    again = signed_in.post("/api/v1/vms", json={"name": "newvm"})
    machine = signed_in.post("/api/v1/vms", json={"name": "seapath-machine"})

    assert again.status_code == 409
    assert machine.status_code == 409


def test_a_name_that_could_not_be_a_domain_is_refused(signed_in: TestClient) -> None:
    response = signed_in.post("/api/v1/vms", json={"name": "not a name"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_guest"


def test_only_an_administrator_may_add_a_vm(signed_in_viewer: TestClient) -> None:
    # It commits the desired state and the run that follows creates a machine.
    assert (
        signed_in_viewer.post("/api/v1/vms", json={"name": "newvm"}).status_code == 403
    )


# 4. Starting and stopping one, which is the runtime plane.


def test_starting_a_guest_is_a_run_like_any_other(signed_in: TestClient) -> None:
    _declare(signed_in)

    response = signed_in.post("/api/v1/vms/vm-guest1/start")

    assert response.status_code == 202
    body = response.json()
    assert body["guest"] == "vm-guest1"
    assert body["action"] == "start"
    # Watched on the Runs page, with the same event stream and the same record
    # a convergence has.
    assert signed_in.get(f"/api/v1/runs/{body['run_id']}").status_code == 200


def test_the_generated_play_calls_the_upstream_module_and_nothing_else(
    signed_in: TestClient, settings: Settings
) -> None:
    # The one exception D30 makes, and its bounds: one task, one upstream
    # module, one command value. A play that grew a second task would be this
    # service writing Ansible, which is the thing it does not do.
    _declare(signed_in)

    run = signed_in.post("/api/v1/vms/vm-guest1/stop").json()

    written = list((settings.runs_dir / run["run_id"]).rglob("vm_stop.yaml"))
    assert len(written) == 1
    document = yaml.safe_load(written[0].read_text())
    assert len(document) == 1
    assert document[0]["hosts"] == "standalone_machine"
    tasks = document[0]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["community.libvirt.virt"] == {
        "name": "vm-guest1",
        "state": "shutdown",
    }


def test_the_command_line_runs_the_generated_play(signed_in: TestClient) -> None:
    # Recorded from the request, so a run says what it actually executed. The
    # path is inside the run directory, which is how an operator reading the
    # record can tell a generated play from one of the collection's.
    _declare(signed_in)

    run = signed_in.post("/api/v1/vms/vm-guest1/start").json()
    record = signed_in.get(f"/api/v1/runs/{run['run_id']}").json()

    assert record["command"][-1].endswith("/playbooks/vm_start.yaml")
    # And the record does not claim the collection wrote it.
    assert record["playbook"] == "seapath-webui.vm_start"


def test_a_guest_nobody_has_heard_of_is_refused(signed_in: TestClient) -> None:
    # The name reaches a module argument, so it is one this node has seen
    # rather than whatever was typed into a URL.
    _declare(signed_in)

    response = signed_in.post("/api/v1/vms/not-a-guest/start")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_guest"


def test_a_guest_the_cluster_runs_and_the_inventory_ignores_can_be_stopped(
    signed_in: TestClient,
) -> None:
    # It is the guest most likely to need stopping: a convergence will not
    # touch it and nothing else here can reach it.
    _declare(signed_in)

    assert signed_in.post("/api/v1/vms/vm-guest2/stop").status_code == 202


def test_an_action_takes_the_same_lock_a_convergence_does(
    signed_in: TestClient, settings: Settings
) -> None:
    # Two operators must not converge and restart the same machines at once,
    # and a start slipping in under a convergence is the same hazard. One lock
    # for both is what makes that true, so the action is refused by a
    # convergence exactly as a second convergence would be.
    _declare(signed_in)
    RunStore(settings.runs_dir).acquire("an-earlier-run")

    response = signed_in.post("/api/v1/vms/vm-guest1/start")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_in_progress"


def test_an_operator_may_act_on_a_guest_and_a_viewer_may_not(
    signed_in_viewer: TestClient,
) -> None:
    # Starting a VM changes nothing an inventory declares, so it is the
    # operator's act rather than the administrator's, the way cancelling a run
    # is.
    assert signed_in_viewer.post("/api/v1/vms/vm-guest1/start").status_code == 403
