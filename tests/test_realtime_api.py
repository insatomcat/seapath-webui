# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The real time page over the API: conformance, and the measurement runs.

The measurement is a run and nothing else. What these tests hold is that it
went through the same door as a convergence: the same lock, the same record,
the same catalogue, and the admin role that launching one takes.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from tests.test_cyclictest import SMP


def wait_for(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/v1/runs/{run_id}").json()
        if record["state"] not in ("pending", "running"):
            return record
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish")


def test_the_conformance_report_says_which_checks_the_inventory_backs(
    signed_in: TestClient,
) -> None:
    report = signed_in.get("/api/v1/realtime").json()

    kinds = {check["id"]: check["kind"] for check in report["checks"]}
    # The inventory the fake node seeds carries isolcpus, so the isolation and
    # the tuned profile are compared against it. SMT is a site's own decision
    # and stays advice.
    assert kinds["cpu_isolation"] == "conformance"
    assert kinds["smt"] == "advice"
    assert report["hostname"] == "seapath-machine"


def test_the_raw_reading_is_served_for_an_automation_client(
    signed_in: TestClient,
) -> None:
    reading = signed_in.get("/api/v1/realtime/reading").json()

    assert reading["preemption"] == "PREEMPT_RT"
    assert reading["tuned_profile"] == "seapath-rt-host"


def test_a_viewer_may_read_the_conformance_and_not_launch_a_measurement(
    signed_in_viewer: TestClient,
) -> None:
    # Reading costs nothing. Measuring loads every machine of the inventory at
    # real time priority, which is a run, and launching one is an admin's act.
    assert signed_in_viewer.get("/api/v1/realtime").status_code == 200
    refused = signed_in_viewer.post(
        "/api/v1/runs", json={"playbook": "test_run_cyclictest"}
    )
    assert refused.status_code == 403


def test_the_measurement_is_launched_with_the_results_folder_of_its_own_run(
    signed_in: TestClient, run_adapter, settings
) -> None:
    run_id = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "test_run_cyclictest",
            "variables": {
                "cyclictest_duration": 30,
                "cyclictest_priority": 90,
                "cyclictest_affinity": "4-7",
            },
        },
    ).json()["run_id"]
    wait_for(signed_in, run_id)

    request = run_adapter.requests[0]
    assert request.playbook == "seapath.ansible.test_run_cyclictest"
    # Filled by the service, never by the caller: it is a path inside this
    # container. The role fetches the histogram into it, so a measurement is
    # kept and deleted with the run that produced it.
    assert request.extra_vars["cyclictest_result_folder"] == str(
        settings.runs_dir / run_id / "results"
    )
    assert request.extra_vars["cyclictest_duration"] == 30
    assert request.extra_vars["cyclictest_affinity"] == "4-7"


def test_the_results_folder_cannot_be_pointed_somewhere_else_by_the_caller(
    signed_in: TestClient,
) -> None:
    # It is not in `variables`, so it is refused like any other undeclared
    # name. A caller that could choose it would choose a path this service
    # writes to for other reasons.
    response = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "test_run_cyclictest",
            "variables": {"cyclictest_result_folder": "/etc/seapath/inventory"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_variable"


def test_a_priority_outside_the_sched_fifo_range_is_refused(
    signed_in: TestClient,
) -> None:
    # 99 sits above the kernel's own threads on a PREEMPT_RT machine, which is
    # how a measurement wedges the host it was measuring.
    response = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "test_run_cyclictest",
            "variables": {"cyclictest_priority": 99},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_variable"


def test_an_affinity_that_is_neither_smp_nor_a_cpu_list_is_refused(
    signed_in: TestClient,
) -> None:
    response = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "test_run_cyclictest",
            "variables": {"cyclictest_affinity": "$(reboot)"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_variable"


def test_smp_is_accepted_as_the_affinity_the_upstream_role_defaults_to(
    signed_in: TestClient,
) -> None:
    response = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "test_run_cyclictest",
            "variables": {"cyclictest_affinity": "smp"},
        },
    )

    assert response.status_code == 202


def test_a_measurement_lists_the_histogram_the_run_fetched(
    signed_in: TestClient, settings
) -> None:
    run_id = signed_in.post(
        "/api/v1/runs", json={"playbook": "test_run_cyclictest"}
    ).json()["run_id"]
    wait_for(signed_in, run_id)
    # What the role's `fetch` leaves behind, written here because the fake
    # adapter runs no Ansible.
    (settings.runs_dir / run_id / "results" / "cyclictest_node1.txt").write_text(SMP)

    measurements = signed_in.get("/api/v1/realtime/measurements").json()

    assert [item["run_id"] for item in measurements] == [run_id]
    result = measurements[0]["results"][0]
    assert result["host"] == "node1"
    assert [thread["max_us"] for thread in result["threads"]] == [15, 12, 9, 11]
    # The pair that makes a latency figure worth keeping: which desired state
    # the machines were carrying when it was taken.
    assert measurements[0]["inventory_commit"]
    # The injected path means nothing to a reader of the page, so the
    # operator's own parameters are what comes back.
    assert "cyclictest_result_folder" not in measurements[0]["variables"]


def test_a_convergence_run_is_absent_from_the_measurement_history(
    signed_in: TestClient,
) -> None:
    signed_in.post("/api/v1/runs", json={"playbook": "seapath_setup_main"})

    assert signed_in.get("/api/v1/realtime/measurements").json() == []
