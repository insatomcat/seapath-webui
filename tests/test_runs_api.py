# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The run API, including the event stream a browser reads."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from app.runs import fake
from tests.fakes import write_fake_collection


def wait_for(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/v1/runs/{run_id}").json()
        if record["state"] not in ("pending", "running"):
            return record
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish")


def test_the_catalogue_says_what_each_playbook_disrupts(
    signed_in: TestClient,
) -> None:
    catalogue = {
        item["entry"]["id"]: item for item in signed_in.get("/api/v1/playbooks").json()
    }

    main = catalogue["seapath_setup_main"]
    assert main["available"] is True
    assert main["entry"]["preview"] == "partial"
    assert main["entry"]["reboots"] == "gated"
    # Both switches, since main reboots in two places: the network playbook it
    # imports, and its own last play.
    assert main["entry"]["reboot_variables"] == [
        "skip_reboot_setup",
        "skip_reboot_setup_network",
    ]
    assert "restarts whatever the roles decide" in main["entry"]["disruption"]

    # Listed so an operator can see what exists, and unavailable with the
    # reason rather than silently missing.
    assert catalogue["cluster_setup_ha"]["available"] is False
    assert "not part of a cluster" in catalogue["cluster_setup_ha"]["unmet"][0]
    # A `none` playbook offers no preview button at all.
    assert catalogue["cluster_setup_ha"]["entry"]["preview"] == "none"


EXTRA_PLAYBOOK = """---
- name: Configure the thing
  hosts:
    - cluster_machines
    - standalone_machine
  become: true
  tasks:
    - name: Write the configuration
      ansible.builtin.template:
        src: thing.conf.j2
        dest: /etc/thing.conf
"""

EXTRA_NEEDING_A_VARIABLE = """---
- name: Update one machine
  hosts: "{{ machine_to_update }}"
  tasks:
    - name: Write the configuration
      ansible.builtin.template:
        src: thing.conf.j2
        dest: /etc/thing.conf
"""


def test_a_playbook_the_catalogue_never_heard_of_is_still_offered(
    tmp_path,
    signed_in_with,
) -> None:
    # The catalogue was written against one version of a collection that moves
    # without it. A playbook this service has never heard of is one an operator
    # can still see, read and launch, described by what the reader counted in
    # it rather than by a sentence nobody wrote.
    client = signed_in_with(
        write_fake_collection(
            tmp_path / "extra-collection", extras={"site_extra": EXTRA_PLAYBOOK}
        )
    )
    catalogue = {
        item["entry"]["id"]: item for item in client.get("/api/v1/playbooks").json()
    }

    extra = catalogue["site_extra"]
    assert extra["entry"]["reviewed"] is False
    assert extra["entry"]["title"] == "Site extra"
    assert extra["entry"]["targets"] == ["cluster_machines", "standalone_machine"]
    assert extra["entry"]["preview"] == "full"
    assert extra["entry"]["derivation"]["tasks"] == 1
    assert extra["available"] is True

    # And the reviewed entries keep every word a human wrote for them.
    assert catalogue["seapath_setup_snmp"]["entry"]["reviewed"] is True
    assert catalogue["seapath_setup_snmp"]["entry"]["disruption"] == "Restarts snmpd."

    started = client.post("/api/v1/runs", json={"playbook": "site_extra"})
    assert started.status_code == 202
    record = wait_for(client, started.json()["run_id"])
    assert record["playbook"] == "seapath.ansible.site_extra"


def test_a_playbook_needing_a_variable_with_no_field_is_refused(
    tmp_path,
    signed_in_with,
) -> None:
    client = signed_in_with(
        write_fake_collection(
            tmp_path / "variable-collection",
            extras={"site_update": EXTRA_NEEDING_A_VARIABLE},
        )
    )
    catalogue = {
        item["entry"]["id"]: item for item in client.get("/api/v1/playbooks").json()
    }

    entry = catalogue["site_update"]
    assert entry["available"] is False
    assert "machine_to_update" in entry["unmet"][0]
    assert "variables_supported" in entry["unmet_codes"]

    # Refused where it counts as well, since an API client reads the same
    # catalogue and can ask for the same run.
    refused = client.post("/api/v1/runs", json={"playbook": "site_update"})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "precondition_failed"


def test_launching_returns_the_run_and_its_preview_quality(
    signed_in: TestClient,
) -> None:
    response = signed_in.post("/api/v1/runs", json={"playbook": "seapath_setup_main"})

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"]
    # Carried back so the UI can refuse to present a partial check as a
    # guarantee.
    assert body["preview"] == "partial"

    record = wait_for(signed_in, body["run_id"])
    assert record["state"] == "success"
    assert record["launched_by"] == "admin"
    assert record["inventory_commit"]
    # The installed collection, and the build label when it says something the
    # fingerprint cannot.
    assert record["collection_version"].startswith("2.0.0+")
    assert record["collection_version"].endswith("(build test)")


def test_the_reboot_can_be_declined_and_the_variables_reach_the_run(
    signed_in: TestClient, run_adapter
) -> None:
    # Both switches, which is what declining a reboot on this playbook means:
    # one holds back its own last play, the other the reboot of the network
    # playbook it imports.
    declined = {"skip_reboot_setup": True, "skip_reboot_setup_network": True}
    run_id = signed_in.post(
        "/api/v1/runs",
        json={"playbook": "seapath_setup_main", "variables": declined},
    ).json()["run_id"]
    record = wait_for(signed_in, run_id)

    assert run_adapter.requests[0].extra_vars == declined
    # And the record keeps them, so relaunching repeats this run rather than
    # the one that reboots.
    assert record["variables"] == declined


def test_an_undeclared_variable_is_refused(signed_in: TestClient) -> None:
    response = signed_in.post(
        "/api/v1/runs",
        json={"playbook": "seapath_setup_main", "variables": {"ansible_user": "root"}},
    )

    # A free form extra vars field is a tag selector wearing a different hat.
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_variable"


def test_a_cluster_playbook_names_the_condition_it_needs(
    signed_in: TestClient,
) -> None:
    response = signed_in.post("/api/v1/runs", json={"playbook": "cluster_setup_ha"})

    # Never a bare 400: the operator has to know which condition to satisfy.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "precondition_failed"


def test_a_playbook_that_cannot_be_previewed_refuses_check_mode(
    signed_in: TestClient,
) -> None:
    response = signed_in.post(
        "/api/v1/runs", json={"playbook": "cluster_setup_cephadm", "check": True}
    )

    assert response.status_code == 409


def test_the_event_stream_carries_the_tasks_then_the_verdict(
    signed_in: TestClient,
) -> None:
    run_id = signed_in.post(
        "/api/v1/runs", json={"playbook": "seapath_setup_main"}
    ).json()["run_id"]
    wait_for(signed_in, run_id)

    with signed_in.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
        body = "".join(response.iter_text())

    payloads = [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    kinds = [payload.get("kind") for payload in payloads]

    assert "task" in kinds
    assert "result" in kinds
    # The last thing a client receives is the verdict, so a browser that missed
    # the state change still learns how it ended.
    assert payloads[-1]["state"] == "success"


def test_the_stream_is_resumable_by_index(signed_in: TestClient) -> None:
    run_id = signed_in.post(
        "/api/v1/runs", json={"playbook": "seapath_setup_main"}
    ).json()["run_id"]
    wait_for(signed_in, run_id)

    with signed_in.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
        everything = "".join(response.iter_text())
    with signed_in.stream("GET", f"/api/v1/runs/{run_id}/events?offset=3") as response:
        resumed = "".join(response.iter_text())

    # A browser that reconnects after the machine rebooted asks for what it has
    # not seen, rather than replaying a whole convergence.
    assert everything.count("data: ") > resumed.count("data: ")


def test_an_interrupted_run_is_presented_as_relaunchable(
    settings, reader, authenticator, directory
) -> None:
    from app.main import create_app
    from app.runs.fake import FakeRunAdapter

    application = create_app(
        settings=settings,
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=FakeRunAdapter(events=fake.interrupted_run(), return_code=4),
    )
    with TestClient(application, base_url="https://testserver") as client:
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "secret"}
        )
        client.headers["X-CSRF-Token"] = client.cookies["seapath_csrf"]
        run_id = client.post(
            "/api/v1/runs", json={"playbook": "seapath_setup_main"}
        ).json()["run_id"]
        record = wait_for(client, run_id)

    # The playbook rebooted the machine it was running from, which is what
    # seapath_setup_hardening.yaml does by design. Not a failure.
    assert record["state"] == "interrupted"
    assert "Relaunching is safe" in record["message"]


def test_a_second_run_is_refused_while_one_is_going(
    signed_in: TestClient, settings
) -> None:
    from app.runs.store import RunStore

    RunStore(settings.runs_dir).acquire("an-earlier-run")

    response = signed_in.post("/api/v1/runs", json={"playbook": "seapath_setup_main"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_in_progress"
    assert "an-earlier-run" in response.json()["error"]["message"]


def test_a_viewer_may_watch_a_run_and_not_launch_one(
    signed_in_viewer: TestClient,
) -> None:
    assert signed_in_viewer.get("/api/v1/runs").status_code == 200
    assert signed_in_viewer.get("/api/v1/playbooks").status_code == 200

    response = signed_in_viewer.post(
        "/api/v1/runs", json={"playbook": "seapath_setup_main"}
    )

    assert response.status_code == 403


def test_the_trust_view_shows_the_relation_a_run_depends_on(
    signed_in: TestClient,
) -> None:
    relations = signed_in.get("/api/v1/trust/relations").json()

    assert len(relations) == 1
    assert relations[0]["kind"] == "self"
    assert relations[0]["installed"] is True
    assert relations[0]["fingerprint"].startswith("SHA256:")


def test_revoking_the_self_relation_stops_the_node_converging(
    signed_in: TestClient,
) -> None:
    comment = signed_in.get("/api/v1/trust/relations").json()[0]["comment"]

    assert signed_in.delete(f"/api/v1/trust/relations/{comment}").status_code == 204

    response = signed_in.post("/api/v1/runs", json={"playbook": "seapath_setup_main"})
    assert response.status_code == 409
    assert "no SSH trust with itself" in response.json()["error"]["message"]


def test_only_the_prerequisites_of_this_distribution_can_be_launched(
    signed_in: TestClient,
) -> None:
    # The fake machine runs Debian. None of the five prerequisites playbooks
    # checks what it landed on, and a run plays every machine the inventory
    # declares, this one among them, so four of the five are wrong for at
    # least this machine before they start.
    catalogue = {
        item["entry"]["id"]: item for item in signed_in.get("/api/v1/playbooks").json()
    }

    assert catalogue["seapath_setup_prerequisitesdebian"]["available"] is True

    for other in ("centos", "oraclelinux", "sles", "yocto"):
        entry = catalogue[f"seapath_setup_prerequisites{other}"]
        assert entry["available"] is False, other
        assert "distribution_matches" in entry["unmet_codes"], other
        assert "This machine runs Debian" in entry["unmet"][0], other

    # And the commissioning path is never filtered: it picks between the five
    # per machine, which is the whole reason it exists.
    assert catalogue["seapath_setup_main"]["available"] is True
    assert catalogue["seapath_setup_main"]["entry"]["distribution"] is None


def test_the_wrong_prerequisites_playbook_is_refused_at_launch(
    signed_in: TestClient,
) -> None:
    # The list is the courtesy; this is the guarantee. An API client reads the
    # same catalogue and can still ask for the run.
    refused = signed_in.post(
        "/api/v1/runs", json={"playbook": "seapath_setup_prerequisitesyocto"}
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "precondition_failed"
    assert "This machine runs Debian" in refused.json()["error"]["message"]


def test_a_yocto_machine_is_offered_the_yocto_prerequisites(
    settings, reader, authenticator, directory, run_adapter, console_adapter
) -> None:
    from app.main import create_app

    reader.seapath_distro = "Yocto"
    application = create_app(
        settings=settings,
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=console_adapter,
    )
    with TestClient(application, base_url="https://testserver") as client:
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "secret"}
        )
        catalogue = {
            item["entry"]["id"]: item for item in client.get("/api/v1/playbooks").json()
        }

    assert catalogue["seapath_setup_prerequisitesyocto"]["available"] is True
    assert catalogue["seapath_setup_prerequisitesdebian"]["available"] is False


def test_an_unreadable_os_release_blocks_no_prerequisites(
    settings, reader, authenticator, directory, run_adapter, console_adapter
) -> None:
    # Refusing all five because the container was mounted without
    # /etc/os-release is worse than the risk it guards against.
    from app.main import create_app

    reader.seapath_distro = None
    application = create_app(
        settings=settings,
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=console_adapter,
    )
    with TestClient(application, base_url="https://testserver") as client:
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "secret"}
        )
        catalogue = {
            item["entry"]["id"]: item for item in client.get("/api/v1/playbooks").json()
        }

    for distro in ("debian", "centos", "oraclelinux", "sles", "yocto"):
        assert catalogue[f"seapath_setup_prerequisites{distro}"]["available"] is True
