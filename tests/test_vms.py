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

import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.cluster.fake import FakeRbdClient
from app.cluster.rbd import RbdUnavailable
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


def wait_for(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/v1/runs/{run_id}").json()
        if record["state"] not in ("pending", "running"):
            return record
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish")


# The guests the recorded libvirt exposition reports, declared on the machine
# that runs them. `ghost` is declared and absent from the exposition, which is
# the case the page has to tell from a domain reported as down.
STANDALONE_WITH_DOMAINS = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      network_interface: eno1
      admin_user: admin
  children:
    standalone_machine:
      hosts:
        seapath-machine:
    hypervisors:
      hosts:
        seapath-machine:
    VMs:
      hosts:
        ABBICT:
        EITCS:
        ghost:
"""

# The metadata of a guest lives on an RBD image, so every operation on it is a
# cluster act. The fixture is the real cluster inventory the fidelity tests use,
# with the guests added to it.
CLUSTER = Path(__file__).parent / "golden" / "adopted-cluster.yaml"


def _declare_cluster(client: TestClient) -> None:
    response = client.post(
        "/api/v1/inventory/import",
        json={"document": CLUSTER.read_text() + GUESTS},
    )
    assert response.status_code == 200, response.text


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
    assert "vm_manager" not in payload["runtime_note"]
    # And what it does not answer, said rather than left to be inferred from
    # an empty cell: a standalone guest has no Pacemaker resource, and what
    # would know about it is an exporter this page does not ask.
    assert "libvirt-exporter" in payload["runtime_note"]


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
    assert again.json()["error"]["code"] == "guest_exists"
    assert machine.status_code == 409
    # Nothing replaces a machine, so its refusal is the ordinary one and the
    # page offers no replacement for it.
    assert machine.json()["error"]["code"] == "refused_write"


def test_a_declared_guest_is_replaced_on_request(
    signed_in: TestClient, settings: Settings
) -> None:
    # An attempt that failed after declaring the guest, then the retry.
    signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "vm_disk": "../files/old.qcow2", "enable": False},
    )

    response = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "vm_disk": "../files/newvm.qcow2", "replace": True},
    )

    assert response.status_code == 201, response.text
    written = (settings.inventory_dir / "inventory.yaml").read_text()
    assert "../files/newvm.qcow2" in written
    # The old entry goes whole, including what the new form leaves out.
    assert "old.qcow2" not in written
    assert "enable: false" not in written
    guests = signed_in.get("/api/v1/vms").json()["guests"]
    assert [guest["name"] for guest in guests] == ["newvm"]
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"] == "vms: replace the declaration of newvm"


def test_a_replacement_of_a_name_nobody_declared_is_a_declaration(
    signed_in: TestClient,
) -> None:
    response = signed_in.post("/api/v1/vms", json={"name": "fresh", "replace": True})

    assert response.status_code == 201
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"] == "vms: declare fresh"


def test_a_machine_is_never_replaced_by_a_guest(signed_in: TestClient) -> None:
    response = signed_in.post(
        "/api/v1/vms", json={"name": "seapath-machine", "replace": True}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "refused_write"
    hosts = signed_in.get("/api/v1/inventory").json()["inventory"]["hosts"]
    assert "seapath-machine" in hosts


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


# 5. The RBD image metadata, which is where a guest's Pacemaker configuration
# actually lives. Read from Ceph as the request is served, the way the cluster
# view reads the exporters. See D31.


def test_the_metadata_of_a_guest_is_what_its_image_carries(
    signed_in: TestClient,
) -> None:
    _declare_cluster(signed_in)

    view = signed_in.get("/api/v1/vms/vm-guest1/metadata").json()

    assert view["image"] == "system_vm-guest1"
    assert view["entries"]["_preferred_host"] == "seapath-machine"
    assert view["entries"]["_priority"] == "10"
    # A read moves nothing, so there is nothing to apply.
    assert view["changes"] == []


def test_a_guest_the_cluster_has_no_image_for_reads_as_empty(
    signed_in: TestClient,
) -> None:
    # A guest declared and never deployed. An ordinary state, and not a
    # failure to report.
    _declare_cluster(signed_in)

    view = signed_in.get("/api/v1/vms/vm-guest3/metadata").json()

    assert view["entries"] == {"vm_name": "vm-guest3"}


def test_setting_a_key_says_what_moved_on_the_image(
    signed_in: TestClient, rbd_client: FakeRbdClient
) -> None:
    _declare_cluster(signed_in)

    view = signed_in.put(
        "/api/v1/vms/vm-guest1/metadata",
        json={"key": "_preferred_host", "value": "elabo1"},
    ).json()

    assert view["changes"] == [
        {"key": "_preferred_host", "before": "seapath-machine", "after": "elabo1"}
    ]
    assert view["entries"]["_preferred_host"] == "elabo1"
    assert rbd_client.images["system_vm-guest1"]["_preferred_host"] == "elabo1"


def test_a_key_the_image_did_not_have_is_added(
    signed_in: TestClient, rbd_client: FakeRbdClient
) -> None:
    _declare_cluster(signed_in)

    view = signed_in.put(
        "/api/v1/vms/vm-guest1/metadata",
        json={"key": "_stop_timeout", "value": "60"},
    ).json()

    assert view["changes"] == [{"key": "_stop_timeout", "before": None, "after": "60"}]


def test_a_write_with_no_value_removes_the_key(
    signed_in: TestClient, rbd_client: FakeRbdClient
) -> None:
    _declare_cluster(signed_in)

    view = signed_in.put(
        "/api/v1/vms/vm-guest1/metadata", json={"key": "_priority"}
    ).json()

    assert view["changes"] == [{"key": "_priority", "before": "10", "after": None}]
    assert "_priority" not in rbd_client.images["system_vm-guest1"]


def test_a_write_that_set_the_value_already_there_changed_nothing(
    signed_in: TestClient,
) -> None:
    # The image is read before and after, so this is answered by the image
    # rather than by what the browser believed a minute ago. The page offers
    # the outage that applies a change only when there is one, and applying a
    # change that moved nothing would be an outage for nothing.
    _declare_cluster(signed_in)

    view = signed_in.put(
        "/api/v1/vms/vm-guest1/metadata",
        json={"key": "_priority", "value": "10"},
    ).json()

    assert view["changes"] == []


def test_the_libvirt_xml_is_a_key_like_any_other(
    signed_in: TestClient, rbd_client: FakeRbdClient
) -> None:
    # `vm_manager` stores the domain XML as metadata, so editing it is editing
    # a value that happens to be several kilobytes of markup.
    _declare_cluster(signed_in)
    document = "<domain type='kvm'>\n  <name>vm-guest1</name>\n</domain>\n"

    view = signed_in.put(
        "/api/v1/vms/vm-guest1/metadata",
        json={"key": "xml", "value": document},
    ).json()

    assert view["entries"]["xml"] == document


def test_a_key_this_service_will_not_write_is_refused(signed_in: TestClient) -> None:
    _declare_cluster(signed_in)

    response = signed_in.put(
        "/api/v1/vms/vm-guest1/metadata",
        json={"key": "a key with spaces", "value": "x"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_metadata"


def test_a_value_too_large_to_be_a_setting_is_refused(signed_in: TestClient) -> None:
    # 64 KB takes a pinning profile and refuses a file.
    _declare_cluster(signed_in)

    response = signed_in.put(
        "/api/v1/vms/vm-guest1/metadata",
        json={"key": "big", "value": "x" * (64 * 1024 + 1)},
    )

    assert response.status_code == 400


def test_ceph_not_answering_is_said_as_what_it_is(
    signed_in: TestClient, rbd_client: FakeRbdClient
) -> None:
    # A standalone machine has no Ceph at all, and a cluster whose monitors are
    # down is the other reason. Either way the request was legitimate and the
    # store it needs is what did not answer.
    _declare_cluster(signed_in)

    def refuse(image: str) -> dict[str, str]:
        raise RbdUnavailable("rbd: not found in this image")

    rbd_client.list_metadata = refuse

    response = signed_in.get("/api/v1/vms/vm-guest1/metadata")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ceph_unavailable"


def test_writing_metadata_is_an_administrator_s_act(
    signed_in_viewer: TestClient,
) -> None:
    assert (
        signed_in_viewer.put(
            "/api/v1/vms/vm-guest1/metadata", json={"key": "k", "value": "v"}
        ).status_code
        == 403
    )


def test_applying_a_metadata_change_is_disable_then_enable(
    signed_in: TestClient, settings: Settings
) -> None:
    # `enable_vm` reads the `_` keys only when the guest is not already a
    # Pacemaker resource, so there is no way to apply one without the guest
    # going down and coming back.
    _declare_cluster(signed_in)

    run = signed_in.post("/api/v1/vms/vm-guest1/reconfigure").json()

    written = list((settings.runs_dir / run["run_id"]).rglob("vm_reconfigure.yaml"))
    tasks = yaml.safe_load(written[0].read_text())[0]["tasks"]
    assert [task["seapath.ansible.cluster_vm"]["command"] for task in tasks] == [
        "disable",
        "enable",
    ]


# 6. What a guest is created with, which is what its image then carries.


def test_the_options_a_creation_bakes_in_are_written_into_the_entry(
    signed_in: TestClient, settings: Settings
) -> None:
    # Every one of these reaches `cluster_vm create` and is written once into
    # the metadata of the guest's image. Changing one afterwards is the
    # metadata window and an outage, so the form asks while it is still cheap.
    _declare_cluster(signed_in)

    response = signed_in.post(
        "/api/v1/vms",
        json={
            "name": "newvm",
            "vm_disk": "../files/newvm.qcow2",
            "preferred_host": "node2",
            "priority": 20,
            "live_migration": True,
            "migrate_to_timeout": 300,
            "migration_downtime": 50,
            "disk_bus": "scsi",
            "colocated_vms": ["vm-guest1"],
            "strong_colocation": True,
            "nostart": True,
        },
    )

    assert response.status_code == 201, response.text
    written = (settings.inventory_dir / "inventory.yaml").read_text()
    for line in (
        "preferred_host: node2",
        "priority: 20",
        "live_migration: true",
        "migrate_to_timeout: 300",
        "migration_downtime: 50",
        "disk_bus: scsi",
        "strong_colocation: true",
        "nostart: true",
    ):
        assert line in written


def test_a_placement_naming_a_machine_the_inventory_lacks_is_refused(
    signed_in: TestClient,
) -> None:
    # `preferred_host: nod2` is a guest Pacemaker places nowhere, reported as
    # a constraint nobody can read. Caught here it is a typo in a form.
    _declare_cluster(signed_in)

    response = signed_in.post(
        "/api/v1/vms", json={"name": "newvm", "preferred_host": "nod2"}
    )

    assert response.status_code == 400
    assert "not a machine a guest can be placed on" in (
        response.json()["error"]["message"]
    )


def test_a_guest_is_pinned_or_preferred_and_not_both(signed_in: TestClient) -> None:
    # `cluster_vm` reads `pinned_host` first and ignores the other, so writing
    # the pair would hide one of the two decisions.
    _declare_cluster(signed_in)

    response = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "preferred_host": "node2", "pinned_host": "node3"},
    )

    assert response.status_code == 400


def test_a_colocation_with_a_guest_nobody_declared_is_refused(
    signed_in: TestClient,
) -> None:
    _declare_cluster(signed_in)

    response = signed_in.post(
        "/api/v1/vms", json={"name": "newvm", "colocated_vms": ["ghost"]}
    )

    assert response.status_code == 400


def test_a_pinning_profile_that_is_not_yaml_is_refused(signed_in: TestClient) -> None:
    # It is read by the seapath-alloc hook at every start, so a broken one is a
    # guest that fails to start long after the form was submitted.
    _declare_cluster(signed_in)

    response = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "vm_pinning_profile": "version: [1"},
    )

    assert response.status_code == 400
    assert "not YAML" in response.json()["error"]["message"]


def test_the_defaults_the_roles_already_have_are_not_written(
    signed_in: TestClient, settings: Settings
) -> None:
    # An entry spelling out `force: false`, `enable: true` and
    # `live_migration: false` on every guest says nothing and reads as if it
    # did, and the file is somebody's audit trail.
    _declare_cluster(signed_in)

    signed_in.post("/api/v1/vms", json={"name": "newvm"})

    written = (settings.inventory_dir / "inventory.yaml").read_text()
    # The entry it wrote is the name and nothing else, which is also the shape
    # a guest already running is adopted with.
    assert written.endswith("    newvm:\n")


def test_the_form_is_offered_the_machines_a_guest_can_be_placed_on(
    signed_in: TestClient,
) -> None:
    _declare_cluster(signed_in)

    view = signed_in.get("/api/v1/vms").json()

    assert view["machines"] == ["node1", "node2", "node3"]


def test_only_a_cluster_member_that_runs_libvirt_is_offered_for_placement(
    signed_in: TestClient,
) -> None:
    # A standalone machine has no Pacemaker to hear the constraint and an
    # observer has no libvirt to run the guest. Offering either is offering a
    # guest that never starts and a constraint nobody can read.
    document = CLUSTER.read_text() + "\nobservers:\n  hosts:\n    node3:\n"
    signed_in.post("/api/v1/inventory/import", json={"document": document + GUESTS})

    view = signed_in.get("/api/v1/vms").json()

    assert view["machines"] == ["node1", "node2"]


def test_a_file_with_both_a_cluster_and_a_standalone_machine_says_so(
    signed_in: TestClient,
) -> None:
    # Both deployment playbooks loop over the whole `VMs` group and neither
    # takes a guest to deploy, so the file has no way of saying which
    # deployment a guest belongs to. Said rather than resolved: inventing an
    # answer here would be a variable the roles do not read.
    document = (
        CLUSTER.read_text()
        + """
standalone_machine:
  hosts:
    ccv-admin:
      ansible_host: 10.132.159.74
      network_interface: eno8303
      admin_user: admin
"""
    )
    signed_in.post("/api/v1/inventory/import", json={"document": document + GUESTS})

    view = signed_in.get("/api/v1/vms").json()

    assert len(view["warnings"]) == 1
    assert "ccv-admin" in view["warnings"][0]
    assert "a second time" in view["warnings"][0]
    # And that machine is still not somewhere Pacemaker can place a guest.
    assert "ccv-admin" not in view["machines"]


def test_the_real_time_profile_is_offered_on_a_standalone_machine_too(
    signed_in: TestClient, settings: Settings
) -> None:
    # `deploy_vms_standalone` writes it to /etc/seapath/alloc.d/<vm>.yaml and
    # the same libvirt hook reads it there, so it is not a cluster feature.
    _declare(signed_in)

    response = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "vm_pinning_profile": "version: 1\n"},
    )

    assert response.status_code == 201, response.text
    assert (
        "vm_pinning_profile:" in (settings.inventory_dir / "inventory.yaml").read_text()
    )


def test_a_variable_the_other_role_reads_is_refused_rather_than_written(
    signed_in: TestClient,
) -> None:
    # A variable written for the wrong mode is silently inert, which is the
    # worst outcome of the three: the operator asked, the file says they got
    # it, and nothing anywhere does it.
    _declare(signed_in)

    response = signed_in.post("/api/v1/vms", json={"name": "newvm", "disk_bus": "scsi"})

    assert response.status_code == 400
    assert "deploy_vms_standalone, which creates newvm, would ignore it" in (
        response.json()["error"]["message"]
    )


def test_the_libvirt_autostart_flag_is_standalone_only(
    signed_in: TestClient,
) -> None:
    # In a cluster, whether a guest comes back is Pacemaker's and not
    # libvirt's, and the cluster role never reads this.
    _declare_cluster(signed_in)

    response = signed_in.post("/api/v1/vms", json={"name": "newvm", "autostart": False})

    assert response.status_code == 400


# 6bis. The network one form section gives a guest, which is what takes the
# address out of the image it is created from.


NETWORK = {
    "bridge": "br0",
    "mac_address": "52:54:00:e4:ff:02",
    "address": "10.0.0.42/24",
    "gateway": "10.0.0.1",
    "dns": ["9.9.9.9"],
}


def test_a_guest_is_given_an_interface_a_seed_and_an_address(
    signed_in: TestClient, settings: Settings
) -> None:
    # One section, three variables, three readers: `guest.xml.j2` renders the
    # interface, `cloud_init_seed` builds the seed from the mapping, and
    # Ansible reads the address when a later run has to reach inside.
    response = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "vm_disk": "../files/newvm.qcow2", "network": NETWORK},
    )

    assert response.status_code == 201, response.text
    assert response.json()["mac_address"] == "52:54:00:e4:ff:02"

    written = (settings.inventory_dir / "inventory.yaml").read_text()
    entry = yaml.safe_load(written)["VMs"]["hosts"]["newvm"]
    assert entry["ansible_host"] == "10.0.0.42"
    assert entry["bridges"] == [{"name": "br0", "mac_address": "52:54:00:e4:ff:02"}]
    assert entry["cloud_init"] == {
        "network": {
            "ethernets": {
                "primary": {
                    "match": {"macaddress": "52:54:00:e4:ff:02"},
                    "addresses": ["10.0.0.42/24"],
                    "routes": [{"to": "default", "via": "10.0.0.1"}],
                    "nameservers": {"addresses": ["9.9.9.9"]},
                }
            }
        }
    }
    # And the network sits directly under the files in the entry, because
    # where the disk is and where the guest is are what an operator looks for.
    block = written[written.index("newvm:") :]
    assert block.index("vm_disk") < block.index("ansible_host")
    assert block.index("ansible_host") < block.index("bridges")
    assert block.index("bridges") < block.index("cloud_init")


def test_the_mac_is_generated_where_a_bridge_is_named_without_one(
    signed_in: TestClient, settings: Settings
) -> None:
    # `guest.xml.j2` requires a MAC on every bridge it renders, and the seed
    # matches the interface on it, so a guest cannot be declared without one.
    # The answer carries it because it is the guest's identity on that bridge.
    response = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "network": {"bridge": "br0", "dhcp": True}},
    )

    assert response.status_code == 201, response.text
    generated = response.json()["mac_address"]
    assert generated.startswith("52:54:00:")

    entry = yaml.safe_load((settings.inventory_dir / "inventory.yaml").read_text())[
        "VMs"
    ]["hosts"]["newvm"]
    assert entry["bridges"][0]["mac_address"] == generated
    # DHCP, so nothing here claims to know the address it will be given.
    assert "ansible_host" not in entry
    assert entry["cloud_init"]["network"]["ethernets"]["primary"]["dhcp4"] is True


def test_a_guest_with_no_network_section_is_declared_as_before(
    signed_in: TestClient, settings: Settings
) -> None:
    # The guest whose image carries its own configuration. Nothing is written,
    # rather than an empty interface and an empty seed.
    response = signed_in.post(
        "/api/v1/vms", json={"name": "newvm", "vm_disk": "../files/newvm.qcow2"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["mac_address"] is None

    entry = yaml.safe_load((settings.inventory_dir / "inventory.yaml").read_text())[
        "VMs"
    ]["hosts"]["newvm"]
    assert "cloud_init" not in entry
    assert "bridges" not in entry
    assert "ansible_host" not in entry


def test_an_address_a_machine_already_answers_on_is_refused(
    signed_in: TestClient,
) -> None:
    # The seed inventory gives this node 192.168.200.125. A guest on the same
    # address is a network where neither is reliably reachable.
    response = signed_in.post(
        "/api/v1/vms",
        json={
            "name": "newvm",
            "network": {"bridge": "br0", "address": "192.168.200.125/24"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_guest"
    assert "seapath-machine" in response.json()["error"]["message"]


def test_an_address_another_guest_already_has_is_refused(
    signed_in: TestClient,
) -> None:
    first = signed_in.post(
        "/api/v1/vms", json={"name": "first", "network": dict(NETWORK)}
    )
    assert first.status_code == 201, first.text

    response = signed_in.post(
        "/api/v1/vms",
        json={
            "name": "second",
            "network": {"bridge": "br0", "address": "10.0.0.42/24"},
        },
    )

    assert response.status_code == 400
    assert "first" in response.json()["error"]["message"]


def test_a_mac_another_guest_already_carries_is_refused(
    signed_in: TestClient,
) -> None:
    first = signed_in.post(
        "/api/v1/vms", json={"name": "first", "network": dict(NETWORK)}
    )
    assert first.status_code == 201, first.text

    response = signed_in.post(
        "/api/v1/vms",
        json={
            "name": "second",
            "network": {"bridge": "br0", "mac_address": NETWORK["mac_address"]},
        },
    )

    assert response.status_code == 400
    assert "first" in response.json()["error"]["message"]


def test_a_network_that_could_not_work_is_refused_before_it_is_written(
    signed_in: TestClient, settings: Settings
) -> None:
    before = (settings.inventory_dir / "inventory.yaml").read_text()

    response = signed_in.post(
        "/api/v1/vms",
        json={
            "name": "newvm",
            "network": {
                "bridge": "br0",
                "address": "10.0.0.42/24",
                "gateway": "10.9.9.1",
            },
        },
    )

    assert response.status_code == 400
    assert "outside" in response.json()["error"]["message"]
    # Nothing was committed, which is what a refusal has to mean here: the
    # file is the audit trail and the guest is declared once.
    assert (settings.inventory_dir / "inventory.yaml").read_text() == before


def test_a_declaration_replaced_keeps_its_own_address(
    signed_in: TestClient, settings: Settings
) -> None:
    # A second attempt at the same guest, which is what `replace` is for. Its
    # own address is not a collision with itself.
    first = signed_in.post(
        "/api/v1/vms", json={"name": "newvm", "network": dict(NETWORK)}
    )
    assert first.status_code == 201, first.text

    again = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "network": dict(NETWORK), "replace": True},
    )

    assert again.status_code == 201, again.text
    # The entry already said exactly this, so there is no second commit: an
    # empty one in the audit trail would say a change happened.
    assert again.json()["commit"] is None
    entry = yaml.safe_load((settings.inventory_dir / "inventory.yaml").read_text())[
        "VMs"
    ]["hosts"]["newvm"]
    assert entry["ansible_host"] == "10.0.0.42"


# 7. A file that says which deployment each guest belongs to.


SPLIT = """
VMs:
  children:
    cluster_VMs:
      hosts:
        vm-guest1:
        vm-guest3:
    standalone_VMs:
      hosts:
        localvm:
"""


def _declare_split(client: TestClient) -> None:
    """The cluster inventory, plus a standalone machine, plus both groups."""
    document = (
        CLUSTER.read_text()
        + """
standalone_machine:
  hosts:
    ccv-admin:
      ansible_host: 10.132.159.74
      network_interface: eno8303
      admin_user: admin
"""
        + SPLIT
    )
    response = client.post("/api/v1/inventory/import", json={"document": document})
    assert response.status_code == 200, response.text


def test_a_guest_says_which_playbook_creates_it(signed_in: TestClient) -> None:
    _declare_split(signed_in)

    guests = {
        item["name"]: item for item in signed_in.get("/api/v1/vms").json()["guests"]
    }

    assert guests["vm-guest1"]["deployment"] == "cluster"
    assert guests["vm-guest1"]["playbook"] == "deploy_vms_cluster"
    assert guests["localvm"]["deployment"] == "standalone"
    assert guests["localvm"]["playbook"] == "deploy_vms_standalone"
    assert all(item["declared"] for item in guests.values())


def test_a_flat_group_takes_the_file_s_mode_and_says_it_assumed(
    signed_in: TestClient,
) -> None:
    # Every inventory written before the two groups existed, and every one
    # that needs a single deployment.
    _declare_cluster(signed_in)

    view = signed_in.get("/api/v1/vms").json()

    assert view["split"] is False
    assert all(item["deployment"] == "cluster" for item in view["guests"])
    assert not any(item["declared"] for item in view["guests"])


def test_the_mixed_warning_goes_when_the_file_answers_the_question(
    signed_in: TestClient,
) -> None:
    # The warning exists because one flat group cannot say which deployment a
    # guest belongs to. A file that says it has nothing to be warned about.
    _declare_split(signed_in)

    assert signed_in.get("/api/v1/vms").json()["warnings"] == []


def test_a_guest_in_neither_group_is_refused(signed_in: TestClient) -> None:
    # Both playbooks loop over what is left of `VMs`, so it would be created
    # twice, once in the Ceph pool and once in the local one.
    document = CLUSTER.read_text() + SPLIT.replace(
        "VMs:\n  children:", "VMs:\n  hosts:\n    stray:\n  children:"
    )

    response = signed_in.post("/api/v1/inventory/import", json={"document": document})

    assert response.status_code == 422
    rules = {f["rule"] for f in response.json()["error"]["detail"]["findings"]}
    assert "guest_belongs_to_one_deployment" in rules


def test_adding_a_guest_writes_it_into_the_deployment_group(
    signed_in: TestClient, settings: Settings
) -> None:
    _declare_split(signed_in)

    response = signed_in.post(
        "/api/v1/vms", json={"name": "newvm", "deployment": "standalone"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["playbook"] == "deploy_vms_standalone"
    written = (settings.inventory_dir / "inventory.yaml").read_text()
    assert written.endswith("        newvm:\n")
    guests = {
        item["name"]: item for item in signed_in.get("/api/v1/vms").json()["guests"]
    }
    assert guests["newvm"]["deployment"] == "standalone"


def test_the_options_a_guest_may_carry_follow_its_deployment(
    signed_in: TestClient,
) -> None:
    # The file holds both kinds, so the file's mode answers nothing. A
    # standalone guest in a cluster inventory takes the standalone role's
    # variables and refuses Pacemaker's.
    _declare_split(signed_in)

    placed = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "deployment": "standalone", "priority": 10},
    )
    autostarted = signed_in.post(
        "/api/v1/vms",
        json={"name": "newvm", "deployment": "cluster", "autostart": False},
    )

    assert placed.status_code == 400
    assert "deploy_vms_standalone, which creates newvm" in (
        placed.json()["error"]["message"]
    )
    assert autostarted.status_code == 400


def test_a_standalone_guest_has_no_rbd_image_to_carry_metadata(
    signed_in: TestClient,
) -> None:
    # Its disk is a file in the local libvirt pool, and it has no Pacemaker
    # either, so there is nothing for this window to read or to apply.
    _declare_split(signed_in)

    response = signed_in.get("/api/v1/vms/localvm/metadata")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_image"


def test_a_standalone_guest_is_started_through_libvirt(
    signed_in: TestClient, settings: Settings
) -> None:
    # In a file that holds both, the guest decides the module rather than the
    # file: `cluster_vm` for a Pacemaker guest, `community.libvirt.virt` for
    # one libvirt owns alone.
    _declare_split(signed_in)

    run = signed_in.post("/api/v1/vms/localvm/start").json()

    written = list((settings.runs_dir / run["run_id"]).rglob("vm_start.yaml"))
    document = yaml.safe_load(written[0].read_text())
    assert document[0]["hosts"] == "standalone_machine"
    assert "community.libvirt.virt" in document[0]["tasks"][0]


def test_a_standalone_guest_is_reported_by_nothing_this_page_asks(
    signed_in: TestClient,
) -> None:
    # Pacemaker does not know it, and "not deployed" about a guest that may
    # well be running is a claim rather than a reading. The page says which of
    # the two it is.
    _declare_split(signed_in)

    guests = {
        item["name"]: item for item in signed_in.get("/api/v1/vms").json()["guests"]
    }

    assert guests["localvm"]["resource"] is None


# 8. What libvirt says, for the guests Pacemaker does not answer for.


def test_a_standalone_guest_carries_what_libvirt_says_about_it(
    signed_in: TestClient,
) -> None:
    # Its only reading: it has no Pacemaker resource at all, and the machine
    # running it publishes libvirt-exporter like every other hypervisor.
    document = STANDALONE_WITH_DOMAINS
    signed_in.post("/api/v1/inventory/import", json={"document": document})

    guests = {
        item["name"]: item for item in signed_in.get("/api/v1/vms").json()["guests"]
    }

    assert guests["ABBICT"]["resource"] is None
    assert guests["ABBICT"]["domain"]["running"] is True
    # The word the state column uses for a Pacemaker resource, so a standalone
    # guest and a cluster one read the same. libvirt's own sentence is kept
    # beside it.
    assert guests["ABBICT"]["domain"]["state"] == "running"
    assert guests["ABBICT"]["domain"]["description"] == "the domain is running"
    assert guests["ABBICT"]["domain"]["host"] == "seapath-machine"
    assert guests["ABBICT"]["domain"]["vcpus"] == 2


def test_a_domain_that_is_shut_off_is_told_from_one_nothing_reported(
    signed_in: TestClient,
) -> None:
    # The distinction the page was missing: a guest libvirt knows and reports
    # as down, against a guest nothing here has heard of.
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_DOMAINS}
    )

    guests = {
        item["name"]: item for item in signed_in.get("/api/v1/vms").json()["guests"]
    }

    assert guests["ghost"]["domain"] is None
    assert guests["EITCS"]["domain"]["running"] is True


def test_a_domain_the_machine_runs_and_the_inventory_ignores_is_named(
    signed_in: TestClient,
) -> None:
    # `VMUADMIN` runs on the machine and no inventory declares it. The same
    # finding as an undeclared Pacemaker resource, for the machines Pacemaker
    # does not answer for.
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_DOMAINS}
    )

    payload = signed_in.get("/api/v1/vms").json()

    reported = {item["name"]: item for item in payload["undeclared_domains"]}

    # Every domain the machine runs that this inventory leaves out, and the
    # two declared ones are absent from the list.
    assert "VMUADMIN" in reported
    assert reported["VMUADMIN"]["running"] is False
    # libvirt calls it `shut off` and Pacemaker calls it `stopped`. The column
    # holding both says `stopped`, whatever answered for the guest.
    assert reported["VMUADMIN"]["state"] == "stopped"
    assert reported["VMUADMIN"]["description"] == "the domain is shut off"
    assert "ABBICT" not in reported
    assert "EITCS" not in reported


# 7. Taking a guest out of the cluster, and putting it back. `cluster_vm
# disable` removes the Pacemaker resource and keeps the RBD group and image,
# which is what `cluster_vm status` then calls Disabled.

# vm-guest4 is held by the fake Ceph and reported by no Pacemaker resource.
DISABLED_GUEST = """
    vm-guest4:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest4.qcow2"
"""


def _declare_cluster_with_disabled(client: TestClient) -> None:
    response = client.post(
        "/api/v1/inventory/import",
        json={"document": CLUSTER.read_text() + GUESTS + DISABLED_GUEST},
    )
    assert response.status_code == 200, response.text


def _pacemaker_answers(monkeypatch) -> None:
    """The fake cluster, served on the first member of the cluster fixture.

    The fixture's machines live at addresses the fake exporters do not
    answer on, and a disabled guest is only said when Pacemaker did answer.
    """
    from app.cluster import fake

    monkeypatch.setitem(fake.HA_EXPORTERS, "10.132.159.60", fake._pacemaker())


def test_a_guest_ceph_holds_and_pacemaker_does_not_reads_as_disabled(
    signed_in: TestClient, monkeypatch
) -> None:
    _pacemaker_answers(monkeypatch)
    _declare_cluster_with_disabled(signed_in)

    guests = {
        item["name"]: item for item in signed_in.get("/api/v1/vms").json()["guests"]
    }

    assert guests["vm-guest4"]["disabled"] is True
    # A guest Pacemaker holds, even a failed one, is in the cluster.
    assert guests["vm-guest1"]["disabled"] is False
    assert guests["vm-guest3"]["disabled"] is False


def test_a_guest_ceph_does_not_hold_is_never_deployed_rather_than_disabled(
    signed_in: TestClient, rbd_client: FakeRbdClient, monkeypatch
) -> None:
    _pacemaker_answers(monkeypatch)
    rbd_client.images.pop("system_vm-guest4")
    _declare_cluster_with_disabled(signed_in)

    guests = {
        item["name"]: item for item in signed_in.get("/api/v1/vms").json()["guests"]
    }

    assert guests["vm-guest4"]["disabled"] is False


def test_ceph_not_answering_costs_the_word_and_nothing_else(
    signed_in: TestClient, rbd_client: FakeRbdClient, monkeypatch
) -> None:
    def refuse() -> list[str]:
        raise RbdUnavailable("no monitor")

    _pacemaker_answers(monkeypatch)
    monkeypatch.setattr(rbd_client, "list_groups", refuse)
    _declare_cluster_with_disabled(signed_in)

    response = signed_in.get("/api/v1/vms")

    assert response.status_code == 200
    guests = {item["name"]: item for item in response.json()["guests"]}
    assert guests["vm-guest4"]["disabled"] is False


def test_disabling_a_guest_is_the_upstream_module_on_one_member(
    signed_in: TestClient, settings: Settings
) -> None:
    _declare_cluster(signed_in)

    response = signed_in.post("/api/v1/vms/vm-guest1/disable")

    assert response.status_code == 202, response.text
    run = response.json()
    assert run["action"] == "disable"
    written = list((settings.runs_dir / run["run_id"]).rglob("vm_disable.yaml"))
    document = yaml.safe_load(written[0].read_text())
    assert document[0]["hosts"] == "{{ groups['cluster_machines'][0] }}"
    assert document[0]["tasks"] == [
        {
            "name": "Take vm-guest1 out of the cluster",
            "seapath.ansible.cluster_vm": {"name": "vm-guest1", "command": "disable"},
        }
    ]


def test_enabling_a_guest_is_the_upstream_module_on_one_member(
    signed_in: TestClient, settings: Settings
) -> None:
    _declare_cluster_with_disabled(signed_in)

    run = signed_in.post("/api/v1/vms/vm-guest4/enable").json()

    written = list((settings.runs_dir / run["run_id"]).rglob("vm_enable.yaml"))
    tasks = yaml.safe_load(written[0].read_text())[0]["tasks"]
    assert tasks[0]["seapath.ansible.cluster_vm"] == {
        "name": "vm-guest4",
        "command": "enable",
    }


def test_a_guest_the_cluster_runs_and_the_inventory_ignores_can_be_disabled(
    signed_in: TestClient, monkeypatch
) -> None:
    # Pacemaker reports vm-guest2 and the file declares vm-guest1 and
    # vm-guest3: the guest an operator most wants out of the cluster.
    _pacemaker_answers(monkeypatch)
    _declare_cluster(signed_in)

    assert signed_in.post("/api/v1/vms/vm-guest2/disable").status_code == 202


def test_a_standalone_guest_has_no_resource_to_disable(signed_in: TestClient) -> None:
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_DOMAINS}
    )

    response = signed_in.post("/api/v1/vms/ABBICT/disable")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_in_cluster"


def test_a_viewer_may_not_take_a_guest_out_of_the_cluster(
    signed_in_viewer: TestClient,
) -> None:
    assert signed_in_viewer.post("/api/v1/vms/vm-guest1/disable").status_code == 403
    assert signed_in_viewer.post("/api/v1/vms/vm-guest1/enable").status_code == 403
