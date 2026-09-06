# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The containers: a quadlet, its unit on each machine, and its resource.

A container in SEAPATH is `upload_extra_files` putting a `.container` file in
`/etc/containers/systemd`, so everything here is held against an inventory that
does exactly that. `tests/golden/adopted-cluster.yaml` is a real one, and the
two quadlets it uploads are the reason this page exists.

What the tests hold, beyond the reading: a write goes where the machines
actually read the variable from, because Ansible replaces a variable rather
than merging it, and an entry appended in the wrong place silently stops the
site's other uploads. And a start is a run of one task, through Pacemaker where
the cluster holds the resource and through systemd where it does not.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.cluster import metrics, systemd
from app.core.settings import Settings
from app.inventory import quadlets

REAL = Path(__file__).parent / "golden" / "adopted-cluster.yaml"

# A cluster of the three machines the fakes answer for. The addresses are what
# decides which exposition each host gets: .125 runs its containers, .126 has
# the exporter's own quadlet failed, and .127 answers nothing at all, which is
# the machine being built that every one of these pages has to render beside
# the ones that answered.
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
        upload_extra_files_upload_files:
          - src: ../files/nginxquadlet.container
            dest: /etc/containers/systemd/nginxquadlet.container
            mode: "0644"
        upload_extra_files_commands_to_run_after_upload:
          - /usr/bin/systemctl daemon-reload
        extra_crm_cmd_to_run: |
          primitive nginxquadlet systemd:nginxquadlet.service op monitor interval=30s
    hypervisors:
      children:
        cluster_machines:
"""


def _import(client: TestClient, document: str) -> None:
    response = client.post("/api/v1/inventory/import", json={"document": document})
    assert response.status_code == 200, response.text


def _containers(client: TestClient) -> dict:
    response = client.get("/api/v1/containers")
    assert response.status_code == 200, response.text
    return response.json()


def _by_name(payload: dict) -> dict:
    return {item["name"]: item for item in payload["containers"]}


# The reading


def test_a_node_with_no_container_says_what_one_is(signed_in: TestClient) -> None:
    payload = _containers(signed_in)

    assert payload["containers"] == []
    assert "quadlet" in payload["note"]
    # And which run puts one on the machines, which is where
    # `upload_extra_files` actually is: the prerequisites playbook of this
    # node's own distribution.
    assert payload["upload_playbook"] == "seapath_setup_prerequisitesdebian"


def test_the_real_inventory_is_read_as_the_two_containers_it_uploads(
    signed_in: TestClient,
) -> None:
    # The file D17 was written against. Nothing in it was added for this page:
    # two quadlets, uploaded to three machines by one entry on `all`.
    _import(signed_in, REAL.read_text())

    containers = _by_name(_containers(signed_in))

    assert sorted(containers) == ["nginxquadlet", "node-exporter"]
    exporter = containers["node-exporter"]
    assert exporter["unit"] == "node-exporter.service"
    assert exporter["hosts"] == ["node1", "node2", "node3"]
    # Written once on `all`, which is where a second one has to go too.
    assert (exporter["scope_kind"], exporter["scope_name"]) == ("group", "all")
    assert exporter["src"].endswith("node-exporter.container.j2")


def test_a_container_joins_its_declaration_its_unit_and_its_resource(
    signed_in: TestClient,
) -> None:
    _import(signed_in, CLUSTER)

    container = _by_name(_containers(signed_in))["nginxquadlet"]

    # Pacemaker holds it, so the cluster owns starting and stopping it.
    assert container["managed"] == "pacemaker"
    assert container["resource"]["id"] == "nginxquadlet"
    assert container["resource"]["node"] == "seapath-machine"
    # And the unit is read per machine, which is what says where it is up.
    units = {unit["host"]: unit for unit in container["units"]}
    assert units["seapath-machine"]["state"] == "active"
    assert units["seapath-machine"]["started_at"]
    assert units["elabo1"]["state"] == "inactive"
    # A machine that answers nothing is a row carrying the reason it gave.
    assert units["elabo2"]["reachable"] is False
    assert units["elabo2"]["error"]


def test_a_container_whose_file_is_missing_says_so_before_the_run(
    signed_in: TestClient,
) -> None:
    # With `any_errors_fatal`, a `copy` that cannot find its source ends the
    # convergence on every host at once, three minutes in.
    _import(signed_in, CLUSTER)

    container = _by_name(_containers(signed_in))["nginxquadlet"]

    assert container["file"]["found"] is False
    assert container["file"]["expected"] == "files/nginxquadlet.container"
    assert any("not in the inventory" in warning for warning in container["warnings"])


def test_a_systemd_resource_no_quadlet_explains_is_listed_on_its_own(
    signed_in: TestClient,
) -> None:
    # A container the cluster runs and this inventory does not describe. It
    # keeps running and it keeps a name a later declaration would collide with,
    # which is worth a line rather than a silent omission.
    payload = _containers(signed_in)

    assert [item["id"] for item in payload["undeclared"]] == ["nginxquadlet"]


def test_a_quadlet_that_starts_itself_under_pacemaker_is_a_finding(
    signed_in: TestClient,
) -> None:
    # systemd starts it at boot and the cluster starts it too, so the resource
    # Pacemaker could not stop is the one that comes back at every boot.
    _import(signed_in, CLUSTER)
    signed_in.put(
        "/api/v1/inventory/files/files/nginxquadlet.container",
        content=b"[Container]\nImage=nginx\n\n[Install]\nWantedBy=multi-user.target\n",
        headers={"Content-Type": "application/octet-stream"},
    )

    container = _by_name(_containers(signed_in))["nginxquadlet"]

    assert container["file"]["found"] is True
    assert any("[Install]" in warning for warning in container["warnings"])


# What a quadlet is, read off the inventory


def test_only_a_file_landing_in_the_quadlet_directory_is_a_container() -> None:
    document = {
        "all": {
            "hosts": {"node1": {}},
            "vars": {
                "upload_extra_files_upload_files": [
                    {
                        "src": "../files/a.container",
                        "dest": "/etc/containers/systemd/a.container",
                    },
                    # An ordinary upload that happens to be named like one.
                    {"src": "../files/b.container", "dest": "/etc/b.container"},
                    # An archive: what comes out of it is the site's business.
                    {
                        "src": "../files/c.tar",
                        "dest": "/etc/containers/systemd",
                        "extract": True,
                    },
                ]
            },
        }
    }

    assert [item.name for item in quadlets.declared(document)] == ["a"]


def test_the_unit_name_follows_the_kind_of_quadlet() -> None:
    # podman's generator names the unit after the file, and the rule differs
    # per extension. It is what Pacemaker is told and what the exporter
    # publishes, so getting it wrong is a page reporting a unit nothing runs.
    assert quadlets.unit_for("/etc/containers/systemd/web.container") == "web.service"
    assert (
        quadlets.unit_for("/etc/containers/systemd/web.volume") == "web-volume.service"
    )
    assert (
        quadlets.unit_for("/etc/containers/systemd/web.network")
        == "web-network.service"
    )
    # A templated destination is Ansible's business at run time.
    assert quadlets.unit_for("/etc/containers/systemd/{{ name }}.container") == ""


def test_the_unit_state_is_the_one_series_the_collector_set_to_one() -> None:
    text = (
        'node_systemd_unit_state{name="web.service",state="active"} 0\n'
        'node_systemd_unit_state{name="web.service",state="failed"} 1\n'
        'node_systemd_unit_state{name="web.service",state="inactive"} 0\n'
        'node_systemd_unit_state{name="other.service",state="active"} 1\n'
    )

    states = systemd.read(metrics.parse(text), {"web.service", "ghost.service"})

    assert states["web.service"].state == "failed"
    assert states["web.service"].failed is True
    # A unit the exporter does not publish is absent from the answer: the
    # machine has never received the file, and calling it inactive would
    # describe it as deployed and down.
    assert "ghost.service" not in states
    # And a unit nobody asked about is left where it was.
    assert "other.service" not in states


# Declaring one


def test_declaring_a_container_appends_to_the_list_the_machines_receive(
    signed_in: TestClient,
) -> None:
    _import(signed_in, REAL.read_text())

    response = signed_in.post(
        "/api/v1/containers",
        json={"name": "mosquitto", "scope_kind": "group", "scope_name": "all"},
    )

    assert response.status_code == 201, response.text
    document = yaml.safe_load(signed_in.get("/api/v1/inventory/raw").text)
    uploads = document["all"]["vars"]["upload_extra_files_upload_files"]
    # The two the site already had, then this one. Appended, and the entries
    # that were there keep every key they had.
    assert [item["dest"] for item in uploads] == [
        "/etc/containers/systemd/node-exporter.container",
        "/etc/containers/systemd/nginxquadlet.container",
        "/etc/containers/systemd/mosquitto.container",
    ]
    assert uploads[-1]["src"] == "../files/mosquitto.container"
    # The site's own `daemon-reload` already does the job, so nothing is added
    # beside it.
    assert document["all"]["vars"][
        "upload_extra_files_commands_to_run_after_upload"
    ] == ["/usr/bin/systemctl daemon-reload", "/usr/bin/systemctl start node-exporter"]


def test_declaring_it_where_the_machines_do_not_read_the_list_is_refused(
    signed_in: TestClient,
) -> None:
    # Ansible replaces a variable rather than merging it: an entry written on
    # `cluster_machines` would leave those three machines with this container
    # alone, and the site's two quadlets would stop being uploaded to them.
    _import(signed_in, REAL.read_text())

    response = signed_in.post(
        "/api/v1/containers",
        json={
            "name": "mosquitto",
            "scope_kind": "group",
            "scope_name": "cluster_machines",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_container"
    assert "group all" in response.json()["error"]["message"]


def test_a_scope_that_would_shadow_the_list_is_offered_as_unavailable(
    signed_in: TestClient,
) -> None:
    # The same refusal, before the operator picks it rather than after.
    _import(signed_in, REAL.read_text())

    scopes = {
        (item["kind"], item["name"]): item for item in _containers(signed_in)["scopes"]
    }

    assert scopes[("group", "all")]["available"] is True
    assert scopes[("group", "all")]["machines"] == ["node1", "node2", "node3"]
    assert scopes[("group", "cluster_machines")]["available"] is False
    assert "replaces a variable" in scopes[("group", "cluster_machines")]["reason"]


def test_a_cluster_container_gets_its_primitive_where_the_file_keeps_them(
    signed_in: TestClient,
) -> None:
    # `configure_ha` loads `extra_crm_cmd_to_run` with `run_once`, so the value
    # that counts is the one the member Ansible plays first. Writing the
    # primitive anywhere but where the cluster already reads it would be a coin
    # toss between two values.
    _import(signed_in, REAL.read_text())

    response = signed_in.post(
        "/api/v1/containers",
        json={
            "name": "mosquitto",
            "scope_kind": "group",
            "scope_name": "all",
            "pacemaker": True,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["cluster_playbook"] == "cluster_setup_ha"
    document = yaml.safe_load(signed_in.get("/api/v1/inventory/raw").text)
    crm = document["all"]["vars"]["extra_crm_cmd_to_run"].splitlines()
    # The site's own primitive is untouched and the new one sits under it.
    assert crm[0].startswith("primitive nginxquadlet systemd:nginxquadlet.service")
    assert crm[1] == (
        "primitive mosquitto systemd:mosquitto.service "
        "op monitor interval=30s "
        "op start timeout=60s interval=0s "
        "op stop timeout=60s interval=0s"
    )


def test_the_declaration_names_the_run_that_makes_it_so(
    signed_in: TestClient,
) -> None:
    _import(signed_in, REAL.read_text())

    body = signed_in.post(
        "/api/v1/containers",
        json={"name": "mosquitto", "scope_kind": "group", "scope_name": "all"},
    ).json()

    assert body["commit"]
    assert body["message"] == "containers: declare mosquitto"
    assert body["playbook"] == "seapath_setup_prerequisitesdebian"
    # No primitive was asked for, so no cluster run is named.
    assert body["cluster_playbook"] is None


def test_a_name_that_cannot_be_a_unit_is_refused(signed_in: TestClient) -> None:
    _import(signed_in, REAL.read_text())

    response = signed_in.post(
        "/api/v1/containers",
        json={"name": "my container", "scope_kind": "group", "scope_name": "all"},
    )

    assert response.status_code == 400
    assert "cannot be a container name" in response.json()["error"]["message"]


def test_declaring_a_container_twice_is_refused(signed_in: TestClient) -> None:
    _import(signed_in, REAL.read_text())

    response = signed_in.post(
        "/api/v1/containers",
        json={"name": "nginxquadlet", "scope_kind": "group", "scope_name": "all"},
    )

    assert response.status_code == 400
    assert "already declared" in response.json()["error"]["message"]


# Starting and stopping


def test_starting_a_container_the_cluster_holds_goes_through_pacemaker(
    signed_in: TestClient, settings: Settings
) -> None:
    _import(signed_in, CLUSTER)

    response = signed_in.post("/api/v1/containers/nginxquadlet/start")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["managed"] == "pacemaker"
    assert body["target"] == "nginxquadlet"
    # One task, `crm resource start`, on a member. The same shape the refresh
    # already had: no module covers a resource that is not a guest.
    written = list((settings.runs_dir / body["run_id"]).rglob("resource_start.yaml"))
    assert len(written) == 1
    document = yaml.safe_load(written[0].read_text())
    assert document[0]["hosts"] == "{{ groups['cluster_machines'][0] }}"
    tasks = document[0]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["ansible.builtin.command"] == {
        "argv": ["crm", "resource", "start", "nginxquadlet"]
    }


def test_stopping_a_container_systemd_owns_names_the_machine(
    signed_in: TestClient, settings: Settings
) -> None:
    _import(signed_in, REAL.read_text())

    response = signed_in.post(
        "/api/v1/containers/node-exporter/stop", params={"host": "node2"}
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["managed"] == "systemd"
    assert body["host"] == "node2"
    assert body["target"] == "node-exporter.service"
    written = list((settings.runs_dir / body["run_id"]).rglob("unit_stop.yaml"))
    document = yaml.safe_load(written[0].read_text())
    # The play names the one machine, because the same quadlet is a unit on
    # each of the three and stopping one says nothing about the others.
    assert document[0]["hosts"] == "node2"
    tasks = document[0]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["ansible.builtin.systemd_service"] == {
        "name": "node-exporter.service",
        "state": "stopped",
    }


def test_a_unit_act_with_no_machine_named_is_refused(
    signed_in: TestClient,
) -> None:
    # A quadlet on three machines is three units, and picking one on the
    # caller's behalf would start something else than what was asked for.
    _import(signed_in, REAL.read_text())

    response = signed_in.post("/api/v1/containers/node-exporter/start")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "host_required"
    assert "node1, node2, node3" in response.json()["error"]["message"]


def test_a_machine_the_inventory_does_not_send_it_to_is_refused(
    signed_in: TestClient,
) -> None:
    _import(signed_in, REAL.read_text())

    response = signed_in.post(
        "/api/v1/containers/node-exporter/start", params={"host": "elabo9"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_host"


def test_a_container_nobody_has_heard_of_is_refused(signed_in: TestClient) -> None:
    # The name reaches a module argument, so it is one this node has seen
    # rather than whatever was typed into a URL.
    _import(signed_in, REAL.read_text())

    response = signed_in.post("/api/v1/containers/not-a-container/start")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_container"


def test_the_run_record_does_not_claim_the_collection_wrote_the_play(
    signed_in: TestClient,
) -> None:
    _import(signed_in, CLUSTER)

    run = signed_in.post("/api/v1/containers/nginxquadlet/stop").json()
    record = signed_in.get(f"/api/v1/runs/{run['run_id']}").json()

    assert record["playbook"] == "seapath-webui.resource_stop"
    assert record["command"][-1].endswith("/playbooks/resource_stop.yaml")
