# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Adoption: reading a file somebody else wrote, and editing it in place.

The claim this service rests on is that its inventory is equivalent to a hand
written one. Adoption is the same claim read backwards, and it is the harder
direction: a hand written inventory carries group variables, groups this
service has never heard of, and a `hostname` that deliberately differs from the
host key. Rendering one of those from a model that holds a dozen fields
destroys the rest.

So a save against such a file is an **edit**: the lines that change are the
lines the form changed, and every write is checked against what Ansible
resolves before it becomes a commit. The fixture all of this is proved against
is a real inventory from a real cluster, with its secrets replaced and nothing
else changed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.inventory.editor import UneditableInventory, edit
from app.inventory.fidelity import unintended_changes
from app.inventory.resolve import resolve
from tests.conftest import ANSIBLE_INVENTORY

GOLDEN = Path(__file__).parent / "golden"
ADOPTED = GOLDEN / "adopted-cluster.yaml"
OURS = GOLDEN / "standalone.yaml"

REFERENCE = Path.home() / "dev/seapath-ansible/inventories/examples"


def _hostvars(path: Path) -> dict[str, dict]:
    completed = subprocess.run(
        [str(ANSIBLE_INVENTORY), "--list", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["_meta"]["hostvars"]


# 1. The resolver, against the only authority on what an inventory means.


@pytest.mark.skipif(
    ANSIBLE_INVENTORY is None,
    reason="ansible-core is not installed in this environment",
)
@pytest.mark.parametrize("fixture", [ADOPTED, OURS])
def test_the_resolver_agrees_with_ansible_itself(fixture: Path) -> None:
    # Everything else in this module compares one resolution to another, so a
    # resolver that was wrong in the same way twice would prove nothing. This
    # is the test that anchors it: the group ordering, the nesting under
    # `all.children`, and host variables winning over group ones are Ansible's
    # rules, and this asserts we reproduce them variable for variable.
    assert resolve(fixture.read_text()) == _hostvars(fixture)


@pytest.mark.skipif(
    ANSIBLE_INVENTORY is None or not REFERENCE.is_dir(),
    reason="the seapath-ansible checkout is not next to this one",
)
@pytest.mark.parametrize("name", ["seapath-cluster.yaml", "seapath-standalone.yaml"])
def test_the_resolver_agrees_on_the_reference_inventories(name: str) -> None:
    # The two files the whole design is written against, in the layout upstream
    # actually uses, which is groups at the top level.
    assert resolve((REFERENCE / name).read_text()) == _hostvars(REFERENCE / name)


def test_groups_are_found_under_all_children_and_at_the_top_level() -> None:
    # Both shapes are valid Ansible, a hand written file is as likely to use
    # one as the other, and reading only one of them silently ignores half of
    # a real inventory.
    nested = """
    all:
      hosts:
        node1:
      children:
        cluster_machines:
          hosts:
            node1:
          vars:
            network_interface: eno8303
    """
    flat = """
    all:
      hosts:
        node1:
    cluster_machines:
      hosts:
        node1:
      vars:
        network_interface: eno8303
    """
    assert resolve(nested) == resolve(flat)
    assert resolve(nested)["node1"]["network_interface"] == "eno8303"


def test_a_host_variable_wins_over_the_group_it_comes_from() -> None:
    document = """
    all:
      hosts:
        node1:
          ptp_interface: eno1
      vars:
        ptp_interface: eno99
    """
    assert resolve(document)["node1"]["ptp_interface"] == "eno1"


# 2. Editing a file this service did not write.


def test_an_edit_touches_only_the_lines_it_changes() -> None:
    document = ADOPTED.read_text()

    edited = edit(document, {"node1": {"ansible_host": "10.132.159.70"}})

    before, after = document.splitlines(), edited.splitlines()
    assert len(before) == len(after)
    changed = [(b, a) for b, a in zip(before, after, strict=False) if b != a]
    assert changed == [
        ("      ansible_host: 10.132.159.60", "      ansible_host: 10.132.159.70")
    ]


def test_the_comments_and_the_rest_of_the_file_survive() -> None:
    document = ADOPTED.read_text()

    edited = edit(document, {"node1": {"ansible_host": "10.132.159.70"}})

    # The variables this service has never heard of, the Ceph groups, and the
    # comments an engineer wrote for the next engineer.
    assert "#node 2 cluster network ip" in edited
    assert "cephadm_install_release_name: tentacle" in edited
    assert "primitive nginxquadlet systemd:nginxquadlet.service" in edited
    assert edited.count("mons:") == 1


def test_a_comment_on_the_edited_line_survives_the_edit() -> None:
    document = ADOPTED.read_text()

    edited = edit(document, {"node1": {"cluster_next_ip_addr": "192.168.55.9"}})

    assert "192.168.55.9  #node 2 cluster network ip" in edited
    # The line below the edited one is a favourite casualty of line splicing.
    assert 'cluster_previous_ip_addr : "192.168.55.3"' in edited


def test_a_group_variable_is_overridden_on_the_host_it_was_edited_for() -> None:
    # The form edits one machine. Writing to `cluster_machines` instead would
    # change the other two, which nobody asked for.
    document = ADOPTED.read_text()

    edited = edit(document, {"node1": {"ptp_interface": "eno5"}})

    resolved = resolve(edited)
    assert resolved["node1"]["ptp_interface"] == "eno5"
    assert resolved["node2"]["ptp_interface"] == "eno12429"
    assert resolved["node3"]["ptp_interface"] == "eno12429"
    assert "network_interface: eno8303" in edited


def test_a_variable_the_file_never_had_is_added_to_the_host() -> None:
    document = ADOPTED.read_text()

    edited = edit(document, {"node2": {"subnet": 22}})

    assert resolve(edited)["node2"]["subnet"] == 22
    # And nowhere else, because the file never set it.
    assert "subnet" not in resolve(edited)["node1"]


def test_a_list_is_written_as_a_block_the_way_the_file_writes_them() -> None:
    document = ADOPTED.read_text()

    servers = ["ntp1.example", "ntp2.example"]

    edited = edit(document, {"node3": {"ntp_servers": servers}})

    assert resolve(edited)["node3"]["ntp_servers"] == servers
    assert resolve(edited)["node1"]["ntp_servers"] == [
        "ntp.example.org",
        "51.145.123.29",
    ]


def test_an_edit_to_a_host_with_no_entry_of_its_own_is_refused() -> None:
    document = """
    all:
      hosts: {}
    cluster_machines:
      hosts:
        node1:
    """
    with pytest.raises(UneditableInventory, match="nowhere to write"):
        edit(document, {"node1": {"ansible_host": "10.0.0.1"}})


# 3. The check that runs on every write.


def test_a_write_that_changes_only_what_was_asked_passes() -> None:
    document = ADOPTED.read_text()
    intended = {"node1": {"ptp_interface": "eno5"}}

    edited = edit(document, intended)

    assert unintended_changes(document, edited, intended) == []


def test_a_write_that_loses_a_neighbour_is_caught() -> None:
    # The failure mode of line splicing, forged here by hand: this is what the
    # check exists to refuse, and it was a real bug before it existed.
    document = ADOPTED.read_text()
    mangled = document.replace(
        '      cluster_previous_ip_addr : "192.168.55.3" #node 3 cluster network ip\n',
        "",
    )

    found = unintended_changes(document, mangled, {"node1": {"ptp_interface": "eno5"}})

    kinds = {divergence.kind for divergence in found}
    assert "lost" in kinds
    assert any(d.variable == "cluster_previous_ip_addr" for d in found)


def test_a_change_that_did_not_happen_is_caught() -> None:
    # The quietest failure: the commit lands, the diff is empty, and the
    # operator believes the machine is about to be configured differently.
    document = ADOPTED.read_text()

    found = unintended_changes(document, document, {"node1": {"ptp_interface": "eno5"}})

    assert [d.kind for d in found] == ["not_applied"]
    assert "stayed 'eno12429'" in found[0].message


# 4. The same rule, seen from the API and the machine it protects.


def _adopt(settings: Settings, fixture: Path) -> None:
    """Put a foreign inventory in place, the way a site clone would."""
    (settings.inventory_dir / "inventory.yaml").write_text(fixture.read_text())


def test_the_api_reads_an_adopted_cluster_inventory_whole(
    signed_in: TestClient, settings: Settings
) -> None:
    _adopt(settings, ADOPTED)

    state = signed_in.get("/api/v1/inventory").json()

    assert state["adopted"] is True
    assert state["inventory"]["mode"] == "cluster"
    assert list(state["inventory"]["hosts"]) == ["node1", "node2", "node3"]
    # Everything below lives on a group in that file, and the form needs it.
    node = state["inventory"]["hosts"]["node1"]
    assert node["network_interface"] == "eno8303"
    assert node["ptp_interface"] == "eno12429"
    assert node["gateway_addr"] == "10.132.159.1"
    assert node["isolcpus"] == "3-11,15-23"
    # And it has nothing to complain about, which it did when it read a third
    # of the file.
    assert state["validation"]["findings"] == []


def test_a_form_save_against_an_adopted_inventory_edits_it_in_place(
    signed_in: TestClient, settings: Settings
) -> None:
    _adopt(settings, ADOPTED)
    path = settings.inventory_dir / "inventory.yaml"
    before = path.read_text()

    response = signed_in.patch(
        "/api/v1/inventory/hosts/node1",
        json={"changes": {"ptp_interface": "eno5"}},
    )

    assert response.status_code == 200
    after = path.read_text()
    assert len(after.splitlines()) == len(before.splitlines()) + 1
    # What Ansible resolves changed by exactly one variable on one host.
    assert unintended_changes(before, after, {"node1": {"ptp_interface": "eno5"}}) == []
    assert resolve(after)["node2"]["ptp_interface"] == "eno12429"


def test_the_commit_message_names_what_changed(
    signed_in: TestClient, settings: Settings
) -> None:
    _adopt(settings, ADOPTED)

    signed_in.patch(
        "/api/v1/inventory/hosts/node2", json={"changes": {"ptp_interface": "eno5"}}
    )

    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"] == "time: set ptp_interface on node2"
    assert history[0]["author"] == "admin"


def test_adding_a_machine_to_an_adopted_inventory_is_refused(
    signed_in: TestClient, settings: Settings
) -> None:
    _adopt(settings, ADOPTED)
    state = signed_in.get("/api/v1/inventory").json()
    candidate = state["inventory"]
    fourth = dict(candidate["hosts"]["node1"])
    # A distinct address, so what refuses this is the writer rather than the
    # duplicate address rule.
    fourth["ansible_host"] = "10.132.159.63"
    candidate["hosts"]["node4"] = fourth

    response = signed_in.put("/api/v1/inventory", json={"inventory": candidate})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "refused_write"


def test_the_seeded_inventory_is_still_rendered_from_the_model(
    signed_in: TestClient, settings: Settings
) -> None:
    # A freshly installed machine keeps the canonical shape, and the editor
    # never touches it.
    state = signed_in.get("/api/v1/inventory").json()
    assert state["adopted"] is False

    assert (
        signed_in.patch(
            "/api/v1/inventory/hosts/seapath-machine",
            json={"changes": {"ptp_interface": "eno5"}},
        ).status_code
        == 200
    )
    written = (settings.inventory_dir / "inventory.yaml").read_text()
    assert "managed by seapath-webui" in written
    assert "ptp_interface: eno5" in written


# 5. The property the whole design is for.


def test_a_save_leaves_everything_ansible_resolves_alone_except_the_change(
    signed_in: TestClient, settings: Settings
) -> None:
    _adopt(settings, ADOPTED)
    path = settings.inventory_dir / "inventory.yaml"
    before = resolve(path.read_text())

    signed_in.patch(
        "/api/v1/inventory/hosts/node1",
        json={"changes": {"ansible_host": "10.132.159.70"}},
    )

    after = resolve(path.read_text())
    assert set(before) == set(after)
    for host in before:
        expected = dict(before[host])
        if host == "node1":
            expected["ansible_host"] = "10.132.159.70"
        assert after[host] == expected


# 6. Bringing an inventory in.


def test_an_imported_inventory_is_committed_exactly_as_it_arrived(
    signed_in: TestClient, settings: Settings
) -> None:
    document = ADOPTED.read_text()

    response = signed_in.post("/api/v1/inventory/import", json={"document": document})

    assert response.status_code == 200
    assert response.json()["hosts"] == ["node1", "node2", "node3"]
    # Byte for byte. The operator brought a file, and a file is what the
    # repository holds.
    assert (settings.inventory_dir / "inventory.yaml").read_text() == document
    history = signed_in.get("/api/v1/inventory/history").json()
    assert history[0]["message"] == (
        "inventory: import a cluster inventory of node1, node2, node3"
    )


def test_the_replaced_inventory_stays_one_revert_away(
    signed_in: TestClient, settings: Settings
) -> None:
    signed_in.post("/api/v1/inventory/import", json={"document": ADOPTED.read_text()})

    history = signed_in.get("/api/v1/inventory/history").json()

    # The seed is still in the history, so importing over it destroys nothing.
    assert history[-1]["message"].startswith("discovery: seed")


def test_an_inventory_that_is_not_yaml_is_refused(signed_in: TestClient) -> None:
    response = signed_in.post(
        "/api/v1/inventory/import", json={"document": "all: {hosts: {node1: {"}
    )

    assert response.status_code == 400
    assert "YAML" in response.json()["error"]["message"]


def test_an_inventory_that_breaks_a_rule_is_refused_with_the_rule(
    signed_in: TestClient, settings: Settings
) -> None:
    before = (settings.inventory_dir / "inventory.yaml").read_text()
    broken = ADOPTED.read_text().replace(
        "gateway_addr: 10.132.159.1", "gateway_addr: 10.99.99.1"
    )

    response = signed_in.post("/api/v1/inventory/import", json={"document": broken})

    assert response.status_code == 422
    rules = {f["rule"] for f in response.json()["error"]["detail"]["findings"]}
    assert "gateway_is_reachable" in rules
    # And nothing was written.
    assert (settings.inventory_dir / "inventory.yaml").read_text() == before


def test_the_machine_finds_its_own_entry_when_the_key_is_not_its_name(
    signed_in: TestClient,
) -> None:
    # The fixture keys its hosts node1..node3 and carries the real names in
    # `hostname`, which is what `network_buildhosts` honours. This machine is
    # `seapath-machine`, so it is node2 here.
    document = ADOPTED.read_text().replace(
        'hostname: "elabo2"', 'hostname: "seapath-machine"'
    )
    signed_in.post("/api/v1/inventory/import", json={"document": document})

    state = signed_in.get("/api/v1/inventory").json()

    assert state["this_host"] == "node2"


def test_a_machine_that_recognises_no_entry_says_so(signed_in: TestClient) -> None:
    # Guessing would put the operator in front of another machine's
    # configuration, which is worse than admitting the file does not describe
    # this one.
    signed_in.post("/api/v1/inventory/import", json={"document": ADOPTED.read_text()})

    assert signed_in.get("/api/v1/inventory").json()["this_host"] is None
