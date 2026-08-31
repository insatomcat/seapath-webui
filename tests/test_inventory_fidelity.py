# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Adoption: reading a file somebody else wrote, and refusing to ruin it.

The claim this service rests on is that its inventory is equivalent to a hand
written one. Adoption is the same claim read backwards, and it is the harder
direction: a hand written inventory carries group variables, groups this
service has never heard of, and a `hostname` that deliberately differs from the
host key. Rewriting one of those from a model that holds a dozen fields
destroys the rest.

So the service proves it can reproduce a file before it is allowed to write it,
and the fixture it is proved against is a real inventory from a real cluster,
with its secrets replaced and nothing else changed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.inventory.fidelity import divergences
from app.inventory.parser import parse
from app.inventory.renderer import render
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


# 2. What the service may write, and what it may not.


def test_an_inventory_this_service_wrote_can_be_written_again() -> None:
    # The round trip is exact for our own files, which is what keeps the check
    # from turning into a service that can never write anything.
    assert divergences(OURS.read_text()) == []


def test_the_adopted_inventory_is_refused_and_says_what_it_protected() -> None:
    found = divergences(ADOPTED.read_text())
    messages = [divergence.message for divergence in found]
    variables = {divergence.variable for divergence in found}

    assert found

    # The rename comes first, because it is the one that changes three running
    # machines rather than a file. The host key is `node1` and the machine is
    # called `elabo1`.
    assert found[0].variable == "hostname"
    assert "elabo1" in found[0].message

    # Group variables, which the model reads on the host and nowhere else.
    assert {"cephadm_network", "admin_ssh_keys", "admin_passwd"} <= variables
    # There are more of them than a page should list, and the count of what is
    # elided is itself the finding.
    assert "further variables" in messages[-1]
    # A value nobody set, which the renderer writes on every host.
    assert any(
        divergence.kind == "invented" and divergence.variable == "subnet"
        for divergence in found
    )
    # And the three hosts named once rather than the same loss repeated.
    assert any("node1, node2, node3" in message for message in messages)


def test_a_cluster_inventory_is_refused_before_the_renderer_raises() -> None:
    # `render` raises NotImplementedError for a cluster, which reached the API
    # as a 500 on the preview button. It is a refusal with a reason now.
    cluster = OURS.read_text().replace(
        "cluster_machines: null", "cluster_machines:\n  hosts:\n    seapath-machine:"
    )
    found = divergences(cluster)

    assert [divergence.kind for divergence in found] == ["unsupported"]
    assert "M3" in found[0].message


def test_the_dns_and_ntp_lists_are_not_reported_as_a_change() -> None:
    # The roles take either a list or a string, the model normalises to a list,
    # and reporting that as a divergence would make every reference inventory
    # unwritable for a difference the roles cannot see.
    document = OURS.read_text().replace(
        "      dns_servers:\n        - 192.168.200.1",
        "      dns_servers: 192.168.200.1",
    )
    assert "dns_servers: 192.168.200.1" in document
    assert divergences(document) == []


# 3. The same rule, seen from the API and the machine it protects.


def _adopt(settings: Settings, fixture: Path) -> None:
    """Put a foreign inventory in place, the way a site clone would."""
    (settings.inventory_dir / "inventory.yaml").write_text(fixture.read_text())


def test_the_api_reports_the_inventory_as_read_only(
    signed_in: TestClient, settings: Settings
) -> None:
    _adopt(settings, ADOPTED)

    state = signed_in.get("/api/v1/inventory").json()

    assert state["writable"] is False
    assert "read only" in state["read_only_reason"]
    assert state["divergences"]
    # Reading still works, which is the point of refusing to write rather than
    # refusing to open.
    assert list(state["inventory"]["hosts"]) == ["node1", "node2", "node3"]


def test_a_form_save_against_an_adopted_inventory_is_refused(
    signed_in: TestClient, settings: Settings
) -> None:
    _adopt(settings, ADOPTED)
    before = (settings.inventory_dir / "inventory.yaml").read_text()

    response = signed_in.patch(
        "/api/v1/inventory/hosts/node1",
        json={"changes": {"ptp_interface": "eno5"}},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "read_only_inventory"
    assert response.json()["error"]["detail"]["divergences"]
    # The file is untouched, which is the whole claim.
    assert (settings.inventory_dir / "inventory.yaml").read_text() == before


def test_the_preview_of_an_adopted_inventory_is_refused_rather_than_crashing(
    signed_in: TestClient, settings: Settings
) -> None:
    _adopt(settings, ADOPTED)
    state = signed_in.get("/api/v1/inventory").json()

    response = signed_in.post(
        "/api/v1/inventory/preview", json={"inventory": state["inventory"]}
    )

    assert response.status_code == 409


def test_the_seeded_inventory_stays_writable(signed_in: TestClient) -> None:
    # The first boot seed is our own render, so nothing about this check makes
    # a freshly installed machine harder to configure.
    state = signed_in.get("/api/v1/inventory").json()

    assert state["writable"] is True
    assert state["divergences"] == []
    assert (
        signed_in.patch(
            "/api/v1/inventory/hosts/seapath-machine",
            json={"changes": {"ptp_interface": "eno5"}},
        ).status_code
        == 200
    )


# 4. Where this is going.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The writer keeps neither group variables nor unknown groups yet. "
        "When it does, this passes and the read only rule stops applying to "
        "inventories like this one."
    ),
)
def test_a_rewrite_of_the_adopted_inventory_changes_nothing_ansible_can_see() -> None:
    document = ADOPTED.read_text()
    assert resolve(render(parse(document))) == resolve(document)
