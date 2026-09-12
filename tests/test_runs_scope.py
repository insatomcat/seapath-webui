# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Which machines a run plays: the guests it leaves out, and the narrowing.

Two claims are tested here. A convergence never reaches into the `VMs` group
unless it was aimed at it, and a run can be limited to one group or one machine
of the inventory. Both end up on the same command line, so most of these read
it back.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.runs import scope as scoping
from app.runs.scope import RunScope, ScopeRefused

CLUSTER = """
all:
  hosts:
    node1:
      ansible_host: 192.168.200.1
    node2:
      ansible_host: 192.168.200.2
    node3:
      ansible_host: 192.168.200.3
  children:
    cluster_machines:
      hosts:
        node1:
        node2:
        node3:
    hypervisors:
      hosts:
        node1:
        node2:
    observers:
      hosts:
        node3:
    VMs:
      hosts:
        guest1:
        guest2:
"""

# The same file with nothing in `VMs`, which is what a machine commissioned an
# hour ago looks like.
NO_GUESTS = CLUSTER.split("    VMs:")[0]

# Every group `seapath_setup_main` plays, copied from its own `hosts:` lines.
MAIN = ["cluster_machines", "standalone_machine", "VMs", "hypervisors"]


def table(document: str):
    return scoping.table(document)


# 1. The guests, left out.


def test_a_playbook_that_names_the_guests_is_limited_away_from_them() -> None:
    plan = scoping.plan(MAIN, table(CLUSTER))

    # The subtraction is a limit rather than a catalogue edited to say the
    # playbook plays something else than it plays.
    assert plan.limit == "all:!VMs"
    assert plan.hosts == ["node1", "node2", "node3"]
    assert plan.excluded == ["guest1", "guest2"]


def test_a_playbook_aimed_at_the_guests_and_nothing_else_keeps_them() -> None:
    # `test_run_cyclictest_vms` measures inside a guest, so the group is what it
    # plays on purpose. Subtracting it would leave the run nothing to play, and
    # it would end green having measured nothing.
    plan = scoping.plan(["VMs"], table(CLUSTER))

    assert plan.limit is None
    assert plan.hosts == ["guest1", "guest2"]
    assert plan.excluded == []


def test_a_playbook_that_names_no_guest_carries_no_limit() -> None:
    plan = scoping.plan(["cluster_machines"], table(CLUSTER))

    assert plan.limit is None
    assert plan.hosts == ["node1", "node2", "node3"]
    assert plan.excluded == []


def test_an_inventory_with_no_guest_needs_no_limit_either() -> None:
    """`--limit all:!VMs` against a file with no such group warns for nothing."""
    plan = scoping.plan(MAIN, table(NO_GUESTS))

    assert plan.limit is None
    assert plan.excluded == []


# 2. The patterns the catalogue actually uses.


def test_an_intersection_resolves_to_the_machines_in_both_groups() -> None:
    plan = scoping.plan(["hypervisors:&cluster_machines"], table(CLUSTER))

    assert plan.hosts == ["node1", "node2"]


def test_a_subscript_names_the_first_machine_the_file_declares() -> None:
    # `cluster_setup_cephadm` bootstraps from `cluster_machines[0]`, and which
    # machine that is has to be the file's answer rather than an alphabetical
    # one: the confirmation names it.
    plan = scoping.plan(["cluster_machines[0]"], table(CLUSTER))

    assert plan.hosts == ["node1"]


def test_a_pattern_this_service_does_not_read_is_reported_as_unread() -> None:
    # A derived entry can carry anything the collection ships. A wildcard is
    # not resolved here, and the answer says so rather than naming no machine,
    # which would read as a run that plays none.
    plan = scoping.plan(["node*"], table(CLUSTER))

    assert plan.hosts is None
    # The group is still subtracted, because the playbook does not name it.
    assert plan.limit is None


def test_the_guests_are_subtracted_from_an_unread_pattern_that_names_them() -> None:
    plan = scoping.plan(["VMs:node*"], table(CLUSTER))

    assert plan.hosts is None
    assert plan.limit == "all:!VMs"


# 3. Narrowing.


def test_a_run_can_be_narrowed_to_one_group() -> None:
    plan = scoping.plan(MAIN, table(CLUSTER), RunScope(groups=["hypervisors"]))

    assert plan.limit == "hypervisors"
    assert plan.hosts == ["node1", "node2"]


def test_a_run_can_be_narrowed_to_one_machine() -> None:
    plan = scoping.plan(MAIN, table(CLUSTER), RunScope(hosts=["node2"]))

    assert plan.limit == "node2"
    assert plan.hosts == ["node2"]


def test_several_boxes_checked_join_as_the_union_ansible_reads() -> None:
    """`--limit a:b` is every host of either, which is what checking both means."""
    plan = scoping.plan(
        MAIN, table(CLUSTER), RunScope(groups=["observers"], hosts=["node1", "guest1"])
    )

    # Sorted, groups first, so the same selection is the same command line
    # twice and a run is comparable to the one before it.
    assert plan.limit == "observers:guest1:node1"
    assert plan.hosts == ["guest1", "node1", "node3"]


def test_a_guest_named_on_purpose_is_played() -> None:
    """The escape hatch the default needs: one guest that is a SEAPATH machine."""
    plan = scoping.plan(MAIN, table(CLUSTER), RunScope(hosts=["guest1"]))

    assert plan.limit == "guest1"
    assert plan.hosts == ["guest1"]
    assert plan.excluded == []


def test_the_single_choice_of_an_older_record_still_parses() -> None:
    """0.3.66 wrote `{"kind", "name"}`, and the run history is files."""
    older = RunScope.model_validate({"kind": "host", "name": "node2"})

    assert older.hosts == ["node2"]
    assert older.groups == []
    assert RunScope.model_validate({"kind": "default", "name": None}).narrowed is False


def test_a_group_the_inventory_does_not_declare_is_refused() -> None:
    with pytest.raises(ScopeRefused) as refused:
        scoping.plan(MAIN, table(CLUSTER), RunScope(groups=["mons"]))

    assert refused.value.code == "unknown_group"


def test_a_machine_the_inventory_does_not_declare_is_refused() -> None:
    with pytest.raises(ScopeRefused) as refused:
        scoping.plan(MAIN, table(CLUSTER), RunScope(hosts=["node9"]))

    assert refused.value.code == "unknown_host"


def test_a_narrowing_that_plays_nothing_is_refused_rather_than_run() -> None:
    # Ansible would accept this and end green having converged nothing, which
    # is the worst of the three possible answers.
    with pytest.raises(ScopeRefused) as refused:
        scoping.plan(
            ["hypervisors:&cluster_machines"],
            table(CLUSTER),
            RunScope(groups=["observers"]),
        )

    assert refused.value.code == "empty_scope"


def test_the_choices_are_the_groups_and_hosts_the_file_declares() -> None:
    choices = scoping.choices(table(CLUSTER))

    assert [group.name for group in choices.groups] == [
        "VMs",
        "cluster_machines",
        "hypervisors",
        "observers",
    ]
    assert choices.guests == ["guest1", "guest2"]
    assert "node1" in choices.hosts
    # An empty group is not offered: narrowing to it plays nothing.
    assert "standalone_machine" not in [group.name for group in choices.groups]


# 4. Through the API, on the command line a run records.

STANDALONE_WITH_GUESTS = """
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
"""


def wait_for(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/v1/runs/{run_id}").json()
        if record["state"] not in ("pending", "running"):
            return record
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish")


def launch(client: TestClient, **payload) -> dict:
    body = {"playbook": "seapath_setup_main", "check": False} | payload
    response = client.post("/api/v1/runs", json=body)
    assert response.status_code == 202, response.text
    return client.get(f"/api/v1/runs/{response.json()['run_id']}").json()


def test_a_convergence_of_the_machines_never_reaches_the_guests(
    signed_in: TestClient,
) -> None:
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_GUESTS}
    )

    record = launch(signed_in)

    command = record["command"]
    assert command[command.index("--limit") + 1] == "all:!VMs"
    assert record["machines"] == ["seapath-machine"]
    assert record["scope"] == {"groups": [], "hosts": []}
    wait_for(signed_in, record["id"])


def test_a_run_is_narrowed_to_what_the_caller_named(signed_in: TestClient) -> None:
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_GUESTS}
    )

    record = launch(signed_in, scope={"groups": [], "hosts": ["ABBICT"]})

    command = record["command"]
    assert command[command.index("--limit") + 1] == "ABBICT"
    # Kept on the record, so a relaunch repeats this run rather than a wider
    # one.
    assert record["scope"] == {"groups": [], "hosts": ["ABBICT"]}
    assert record["machines"] == ["ABBICT"]
    wait_for(signed_in, record["id"])


def test_a_scope_the_inventory_does_not_declare_is_refused_by_the_api(
    signed_in: TestClient,
) -> None:
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_GUESTS}
    )

    response = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "seapath_setup_main",
            "scope": {"groups": ["webservers"]},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_group"


def test_the_catalogue_names_the_machines_each_entry_plays(
    signed_in: TestClient,
) -> None:
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_GUESTS}
    )

    catalogue = {
        item["entry"]["id"]: item for item in signed_in.get("/api/v1/playbooks").json()
    }

    main = catalogue["seapath_setup_main"]
    # The scope line on the card says `VMs`, because the playbook does. The
    # machines say what this service will actually play.
    assert "VMs" in main["entry"]["targets"]
    assert main["machines"] == ["seapath-machine"]
    assert main["excluded"] == ["ABBICT", "EITCS"]


def test_the_scopes_offered_are_read_from_the_inventory(
    signed_in: TestClient,
) -> None:
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_GUESTS}
    )

    choices = signed_in.get("/api/v1/playbooks/scopes").json()

    assert [group["name"] for group in choices["groups"]] == [
        "VMs",
        "hypervisors",
        "standalone_machine",
    ]
    assert choices["guests"] == ["ABBICT", "EITCS"]
    assert choices["guest_group"] == "VMs"


def test_the_generated_action_plays_carry_no_limit(
    signed_in: TestClient,
) -> None:
    """A start or a stop names one guest in its own play, and nothing else."""
    signed_in.post(
        "/api/v1/inventory/import", json={"document": STANDALONE_WITH_GUESTS}
    )

    response = signed_in.post("/api/v1/vms/ABBICT/start")

    assert response.status_code == 202, response.text
    record = signed_in.get(f"/api/v1/runs/{response.json()['run_id']}").json()
    assert "--limit" not in record["command"]
    wait_for(signed_in, record["id"])


# 5. The preconditions follow the scope.

# A cluster of two, which is what a node that has just been joined sees. This
# node holds a trust with itself and with nothing else yet.
TWO_MACHINES = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      network_interface: eno1
      admin_user: admin
      ptp_interface: eno12419
      isolcpus: 4-7
    node2:
      ansible_host: 192.168.200.126
      network_interface: eno1
      admin_user: admin
      ptp_interface: eno12419
      isolcpus: 4-7
  children:
    cluster_machines:
      hosts:
        seapath-machine:
        node2:
    hypervisors:
      hosts:
        seapath-machine:
        node2:
"""


def test_the_chooser_names_the_machines_this_node_cannot_reach(
    signed_in: TestClient,
) -> None:
    """The one precondition a narrowing lifts travels with the choices.

    Without it the deployment page draws an entry as unavailable, with the way
    out on the far side of a button it has no reason to draw.
    """
    signed_in.post("/api/v1/inventory/import", json={"document": TWO_MACHINES})

    choices = signed_in.get("/api/v1/playbooks/scopes").json()

    assert choices["unreachable"] == ["node2"]
    assert "seapath-machine" in choices["hosts"]


def test_a_run_narrowed_to_a_machine_that_answers_is_not_refused_for_one_that_does_not(
    signed_in: TestClient,
) -> None:
    """The other half of what narrowing is for.

    This node has a trust with itself and with nothing else, so a convergence
    of both machines is refused before it starts. Narrowed to the machine that
    answers, it is the ordinary run it has always been.
    """
    imported = signed_in.post(
        "/api/v1/inventory/import", json={"document": TWO_MACHINES}
    )
    assert imported.status_code == 200, imported.text

    refused = signed_in.post(
        "/api/v1/runs", json={"playbook": "seapath_setup_timemaster"}
    )
    assert refused.status_code == 409
    assert "node2" in refused.json()["error"]["message"]

    record = launch(
        signed_in,
        playbook="seapath_setup_timemaster",
        scope={"hosts": ["seapath-machine"]},
    )

    assert record["machines"] == ["seapath-machine"]
    wait_for(signed_in, record["id"])
