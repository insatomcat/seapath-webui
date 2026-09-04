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
    assert measurements[0]["kind"] == "cyclictest"
    result = measurements[0]["latency"][0]
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


def test_the_two_measurements_are_one_history_told_apart_by_kind(
    signed_in: TestClient, settings
) -> None:
    """Both kinds in one list, each carrying only its own results.

    They answer complementary questions, so an operator reads them together:
    cyclictest reports what the scheduler delivered, hwlatdetect what the
    firmware took without telling the kernel.
    """
    from tests.test_hwlatdetect import CLEAN

    latency = signed_in.post(
        "/api/v1/runs", json={"playbook": "test_run_cyclictest"}
    ).json()["run_id"]
    wait_for(signed_in, latency)
    (settings.runs_dir / latency / "results" / "cyclictest_node1.txt").write_text(SMP)

    hardware = signed_in.post(
        "/api/v1/runs", json={"playbook": "test_run_hwlatdetect"}
    ).json()["run_id"]
    wait_for(signed_in, hardware)
    (settings.runs_dir / hardware / "results" / "hwlatdetect_node1.txt").write_text(
        CLEAN
    )

    both = signed_in.get("/api/v1/realtime/measurements").json()
    assert {item["kind"] for item in both} == {"cyclictest", "hwlatdetect"}

    only = signed_in.get("/api/v1/realtime/measurements?kind=hwlatdetect").json()
    assert [item["run_id"] for item in only] == [hardware]
    assert only[0]["interruptions"][0]["samples_recorded"] == 0
    # A cyclictest run carries no interruptions and the reverse, so a page
    # rendering one kind never has to guess which field to read.
    assert only[0]["latency"] == []


def test_the_hwlatdetect_results_folder_is_filled_by_the_service(
    signed_in: TestClient, run_adapter, settings
) -> None:
    run_id = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "test_run_hwlatdetect",
            "variables": {"hwlatdetect_duration": 300, "hwlatdetect_threshold": 20},
        },
    ).json()["run_id"]
    wait_for(signed_in, run_id)

    request = run_adapter.requests[0]
    assert request.playbook == "seapath.ansible.test_run_hwlatdetect"
    assert request.extra_vars["hwlatdetect_result_folder"] == str(
        settings.runs_dir / run_id / "results"
    )
    assert request.extra_vars["hwlatdetect_duration"] == 300


def test_a_sample_width_beyond_a_second_is_refused(signed_in: TestClient) -> None:
    # The width is the interval during which the machine's interrupts are held
    # off. Beyond a second that is a machine taken away from its guests rather
    # than measured.
    response = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "test_run_hwlatdetect",
            "variables": {"hwlatdetect_width": 5_000_000},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_variable"


def test_a_measurement_can_be_relaunched_from_its_own_record(
    signed_in: TestClient, run_adapter, settings
) -> None:
    """The Runs page replays a record's variables, so they must be replayable.

    The results folder is filled by the service and refused from a caller, and
    recording it among the operator's own variables made the two rules
    collide: the relaunch sent it back, the API refused it as undeclared, and
    a measurement could be launched but never relaunched.
    """
    first = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": "test_run_cyclictest",
            "variables": {"cyclictest_duration": 20, "cyclictest_affinity": "4-7"},
        },
    ).json()["run_id"]
    wait_for(signed_in, first)

    record = signed_in.get(f"/api/v1/runs/{first}").json()
    # Only what the operator chose. The injected path is not an operator's
    # decision and has no business being replayed.
    assert record["variables"] == {
        "cyclictest_duration": 20,
        "cyclictest_affinity": "4-7",
    }

    # Exactly what the Runs page posts when Relaunch is pressed.
    again = signed_in.post(
        "/api/v1/runs",
        json={
            "playbook": record["playbook_id"],
            "check": record["check"],
            "variables": record["variables"],
        },
    )
    assert again.status_code == 202
    second = again.json()["run_id"]
    wait_for(signed_in, second)

    # And the relaunch fetches into its own directory. Replaying the recorded
    # path would have written the second run's results over the first's.
    assert run_adapter.requests[1].extra_vars["cyclictest_result_folder"] == str(
        settings.runs_dir / second / "results"
    )


def test_the_recorded_command_still_names_the_results_folder(
    signed_in: TestClient, settings
) -> None:
    # Keeping the path out of `variables` must not cost the record its exact
    # invocation. `command` is built from the request, so it carries every
    # extra var the run was actually given.
    run_id = signed_in.post(
        "/api/v1/runs", json={"playbook": "test_run_hwlatdetect"}
    ).json()["run_id"]
    wait_for(signed_in, run_id)

    command = " ".join(signed_in.get(f"/api/v1/runs/{run_id}").json()["command"])

    assert str(settings.runs_dir / run_id / "results") in command
    assert "hwlatdetect_result_folder" in command
