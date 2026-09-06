# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Placing a resource on a named node, and emptying a machine.

The acts D34 adds, and the whole point of testing them here is that they write
to a live CIB. What each one is allowed to be is narrow: one `crm` command per
task, in a generated play, run on a cluster member over the SSH path a
convergence uses, with both names checked against what the cluster reported
first. None of them touches the inventory, and the inventory is what a
convergence still reads.

The fake cluster is the fixture, and it carries both shapes a placement takes:
`cli-prefer-vm-guest1`, which is what `preferred_host` and a move both write,
and `pin-vm-guest2-onelabo1`, which is what `pinned_host` writes and what makes
a resource one this service refuses to place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

# The seeded machine, put into `cluster_machines`. The address has to stay the
# seeded one, because that is what the fake exporters answer for, and the group
# has to exist, because the generated play targets `cluster_machines[0]`.
_CLUSTER_GROUP = """
cluster_machines:
  hosts:
    seapath-machine:
"""

# A guest the inventory declares a placement for, which is what a return writes
# back, and one it declares nothing about, which is what a return leaves to
# Pacemaker.
_GUESTS = """
VMs:
  hosts:
    vm-guest3:
      vm_template: "../templates/vm/guest.xml.j2"
      vm_disk: "../files/guest3.qcow2"
      preferred_host: seapath-machine
"""

UNREACHABLE = Path(__file__).parent / "golden" / "adopted-cluster.yaml"


def _cluster(client: TestClient, guests: str = "") -> TestClient:
    document = client.get("/api/v1/inventory/raw").text
    response = client.post(
        "/api/v1/inventory/import",
        json={"document": document + _CLUSTER_GROUP + guests},
    )
    assert response.status_code == 200, response.text
    return client


def _play(settings, run: dict, name: str) -> dict:
    written = list((settings.runs_dir / run["run_id"]).rglob(f"{name}.yaml"))
    assert len(written) == 1, written
    document = yaml.safe_load(written[0].read_text())
    assert len(document) == 1
    return document[0]


# 1. Moving a resource, which writes the constraint `preferred_host` writes.


def test_a_move_writes_the_constraint_preferred_host_writes(
    signed_in: TestClient, settings
) -> None:
    """One task, and the command an operator would type on the machine.

    `crm resource move` is what `vm_manager` calls to honour `preferred_host`,
    so a deliberate move introduces no kind of rule the cluster did not already
    carry. The node is always named: a bare `crm resource move` bans the
    resource from the node it is on, which is a different act with the same
    words.
    """
    response = _cluster(signed_in).post(
        "/api/v1/cluster/resources/vm-guest3/move", json={"node": "elabo1"}
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["resource"] == "vm-guest3"
    assert body["node"] == "elabo1"
    # Watched on the Runs page, with the same event stream and the same record
    # a convergence has.
    assert signed_in.get(f"/api/v1/runs/{body['run_id']}").status_code == 200

    play = _play(settings, body, "resource_move")
    assert play["hosts"] == "{{ groups['cluster_machines'][0] }}"
    assert len(play["tasks"]) == 1
    assert play["tasks"][0]["ansible.builtin.command"] == {
        "argv": ["crm", "resource", "move", "vm-guest3", "elabo1"]
    }


def test_the_run_is_titled_with_both_names(signed_in: TestClient, settings) -> None:
    # An operator scanning the run list reads the title before anything else,
    # and a move is only legible with the destination in it.
    run = (
        _cluster(signed_in)
        .post("/api/v1/cluster/resources/vm-guest3/move", json={"node": "elabo1"})
        .json()
    )

    assert _play(settings, run, "resource_move")["name"] == "Move vm-guest3 to elabo1"


def test_a_pinned_resource_is_not_moved(signed_in: TestClient) -> None:
    """`pinned_host` says the guest runs there or nowhere.

    A `cli-prefer` on another node would leave two mandatory rules pulling
    against each other, and a return would not remove the pin. Changing where a
    pinned guest lives is its inventory entry and a redeployment.
    """
    refused = _cluster(signed_in).post(
        "/api/v1/cluster/resources/vm-guest2/move", json={"node": "elabo1"}
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "resource_pinned"
    assert "pin-vm-guest2-onelabo1" in refused.json()["error"]["message"]


def test_a_clone_instance_is_not_moved(signed_in: TestClient) -> None:
    # A clone runs one instance per member and Pacemaker places them. There is
    # no single node to send one to, and `crm` would say so three minutes into
    # a run instead of here.
    refused = _cluster(signed_in).post(
        "/api/v1/cluster/resources/ping/move", json={"node": "elabo1"}
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "resource_is_cloned"
    assert "ping-clone" in refused.json()["error"]["message"]


def test_a_node_in_standby_is_refused_as_a_destination(signed_in: TestClient) -> None:
    # Pacemaker places nothing on a node in standby, so the resource would stay
    # where it is while carrying a constraint saying otherwise.
    refused = _cluster(signed_in).post(
        "/api/v1/cluster/resources/vm-guest3/move", json={"node": "elabo2"}
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "node_in_standby"


def test_a_machine_the_cluster_does_not_report_is_refused_as_a_destination(
    signed_in: TestClient,
) -> None:
    # The name reaches a command argument, so it is one the cluster answered
    # for rather than whatever was typed into a form.
    refused = _cluster(signed_in).post(
        "/api/v1/cluster/resources/vm-guest3/move", json={"node": "elabo9"}
    )

    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "unknown_node"


def test_a_resource_the_cluster_does_not_report_is_not_placed(
    signed_in: TestClient,
) -> None:
    for path, body in (
        ("/api/v1/cluster/resources/not-a-resource/move", {"node": "elabo1"}),
        ("/api/v1/cluster/resources/not-a-resource/clear", None),
    ):
        refused = _cluster(signed_in).post(path, json=body)
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "unknown_resource"


# 2. Returning a placement, which is the half that keeps the inventory true.


def test_a_return_writes_back_what_the_inventory_declares(
    signed_in: TestClient, settings
) -> None:
    """The reason a bare clear is the wrong command.

    `crm resource clear` removes the `cli-prefer` constraint, and that is the
    same constraint `preferred_host` had put there. Clearing alone would drop a
    declared placement with nothing anywhere saying it had gone, until somebody
    rebuilt the guest's Pacemaker resource.
    """
    response = _cluster(signed_in, _GUESTS).post(
        "/api/v1/cluster/resources/vm-guest3/clear"
    )

    assert response.status_code == 202, response.text
    assert response.json()["restored"] == "seapath-machine"

    tasks = _play(settings, response.json(), "resource_clear")["tasks"]
    assert [task["ansible.builtin.command"]["argv"] for task in tasks] == [
        ["crm", "resource", "clear", "vm-guest3"],
        ["crm", "resource", "move", "vm-guest3", "seapath-machine"],
    ]


def test_a_return_on_a_resource_the_inventory_says_nothing_about_is_one_task(
    signed_in: TestClient, settings
) -> None:
    # A fencing device, or a guest somebody deployed by hand. There is nothing
    # declared to write back, and Pacemaker places it by its own rules.
    response = _cluster(signed_in).post("/api/v1/cluster/resources/vm-guest1/clear")

    assert response.json()["restored"] == ""
    tasks = _play(settings, response.json(), "resource_clear")["tasks"]
    assert [task["ansible.builtin.command"]["argv"] for task in tasks] == [
        ["crm", "resource", "clear", "vm-guest1"]
    ]


def test_a_declared_placement_the_cluster_cannot_honour_is_not_written_back(
    signed_in: TestClient, settings
) -> None:
    """The inventory naming a machine this cluster does not report.

    A finding the Inventory page owns, and writing it into the CIB would end
    the run on a `crm` error three minutes later instead.
    """
    guests = _GUESTS.replace("preferred_host: seapath-machine", "preferred_host: node2")
    document = signed_in.get("/api/v1/inventory/raw").text
    signed_in.post(
        "/api/v1/inventory/import",
        json={"document": document + _CLUSTER_GROUP + guests},
    )

    response = signed_in.post("/api/v1/cluster/resources/vm-guest3/clear")

    assert response.json()["restored"] == ""
    tasks = _play(settings, response.json(), "resource_clear")["tasks"]
    assert len(tasks) == 1


def test_a_pinned_resource_has_no_placement_to_return(signed_in: TestClient) -> None:
    # `crm resource clear` does not remove a `pin-` constraint, so the button
    # would report success and change nothing.
    refused = _cluster(signed_in).post("/api/v1/cluster/resources/vm-guest2/clear")

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "resource_pinned"


# 3. Emptying a machine, which is the act with no per resource residue.


def test_standby_is_one_crm_command_on_a_cluster_member(
    signed_in: TestClient, settings
) -> None:
    response = _cluster(signed_in).post("/api/v1/cluster/nodes/elabo1/standby")

    assert response.status_code == 202, response.text
    assert response.json()["standby"] is True

    play = _play(settings, response.json(), "node_standby")
    assert play["hosts"] == "{{ groups['cluster_machines'][0] }}"
    assert play["name"] == "Put elabo1 in standby"
    assert play["tasks"][0]["ansible.builtin.command"] == {
        "argv": ["crm", "node", "standby", "elabo1"]
    }


def test_bringing_a_node_online_is_its_inverse(signed_in: TestClient, settings) -> None:
    # `elabo2` is the member the fake cluster reports in standby.
    response = _cluster(signed_in).post("/api/v1/cluster/nodes/elabo2/online")

    assert response.json()["standby"] is False
    play = _play(settings, response.json(), "node_online")
    assert play["tasks"][0]["ansible.builtin.command"] == {
        "argv": ["crm", "node", "online", "elabo2"]
    }


def test_the_act_that_would_change_nothing_is_refused(signed_in: TestClient) -> None:
    # A `crm node standby` on a node already in standby writes nothing, and a
    # run that does nothing is noise in a history somebody audits.
    for path in (
        "/api/v1/cluster/nodes/elabo2/standby",
        "/api/v1/cluster/nodes/elabo1/online",
    ):
        refused = _cluster(signed_in).post(path)
        assert refused.status_code == 409, path
        assert refused.json()["error"]["code"] == "already_there"


def test_a_machine_that_is_not_a_member_is_refused(signed_in: TestClient) -> None:
    refused = _cluster(signed_in).post("/api/v1/cluster/nodes/elabo9/standby")

    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "unknown_node"


# 4. What holds around all four.


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/cluster/resources/vm-guest3/move", {"node": "elabo1"}),
        ("/api/v1/cluster/resources/vm-guest3/clear", None),
        ("/api/v1/cluster/nodes/elabo1/standby", None),
        ("/api/v1/cluster/nodes/elabo2/online", None),
    ],
)
def test_none_of_them_is_offered_where_no_cluster_answered(
    signed_in: TestClient, path: str, body: dict | None
) -> None:
    """A cluster inventory whose machines are not there.

    Launching one would reach the run three minutes later as an Ansible error,
    rather than here as a sentence naming what to look at.
    """
    signed_in.post(
        "/api/v1/inventory/import", json={"document": UNREACHABLE.read_text()}
    )

    refused = signed_in.post(path, json=body)

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "no_cluster"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/cluster/resources/vm-guest3/move", {"node": "elabo1"}),
        ("/api/v1/cluster/resources/vm-guest3/clear", None),
        ("/api/v1/cluster/nodes/elabo1/standby", None),
        ("/api/v1/cluster/nodes/elabo2/online", None),
    ],
)
def test_a_viewer_reads_the_cluster_and_places_nothing_in_it(
    signed_in_viewer: TestClient, path: str, body: dict | None
) -> None:
    # Placement writes to the CIB of a live cluster, which is an operator's
    # act, exactly as starting a guest is.
    assert signed_in_viewer.get("/api/v1/cluster").status_code == 200
    assert signed_in_viewer.post(path, json=body).status_code == 403


def test_all_four_are_in_the_openapi_document(signed_in: TestClient) -> None:
    document = signed_in.get("/api/v1/openapi.json").json()

    for path in (
        "/api/v1/cluster/resources/{name}/move",
        "/api/v1/cluster/resources/{name}/clear",
        "/api/v1/cluster/nodes/{name}/standby",
        "/api/v1/cluster/nodes/{name}/online",
    ):
        assert set(document["paths"][path]) == {"post"}, path


# 5. What the VMs page reads, which is how a placement becomes visible.


def test_a_guest_carries_the_constraint_holding_it_and_what_it_declares(
    signed_in: TestClient,
) -> None:
    """The comparison the page draws, and the reason it needs both.

    `preferred_host` and a move write the same `cli-prefer` object, so the CIB
    cannot say who asked for it. Held against the entry it can say whether
    anybody declared it, which is what makes an operator's override visible
    rather than silent.
    """
    guests = _GUESTS.replace("vm-guest3", "vm-guest1")
    view = _cluster(signed_in, guests).get("/api/v1/vms").json()
    guest = next(item for item in view["guests"] if item["name"] == "vm-guest1")

    assert guest["preferred_host"] == "seapath-machine"
    assert [item["id"] for item in guest["constraints"]] == ["cli-prefer-vm-guest1"]
    assert guest["constraints"][0]["node"] == "seapath-machine"


def test_the_page_is_offered_the_members_a_guest_may_be_sent_to(
    signed_in: TestClient,
) -> None:
    # The inventory's own placement hosts, held against what the cluster
    # reports: a member in standby is one Pacemaker will place nothing on.
    view = _cluster(signed_in).get("/api/v1/vms").json()

    assert view["placement_nodes"] == ["seapath-machine"]
    assert "elabo2" not in view["placement_nodes"]
