# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The cluster's journal: what may be asked, what is sent, and what comes back.

Three things carry the value here, and they are the three D63 rests on.

The **refusals**, because they are what keeps this affordable on a substation
hypervisor. A query with no match on a field is a regex over every entry of a
517 MB journal, which measured at 2.9 seconds of a machine's CPU against 9 ms
for the same search behind a unit match. The endpoint refuses it rather than
discouraging it in the page.

The **command**, because it crosses two shells to reach root on another
machine. Every value in it came from a browser, and the patterns in
`app/logs/journal.py` are the only thing standing between the two.

The **merge**, because a machine that does not answer has to be a row on the
page rather than a page that fails, and because an operator reading three
machines interleaved is reading a cluster.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.hosts.remote import RemoteRefused, RemoteRequest
from app.logs import journal
from app.logs.journal import Query, Refused
from app.runs.service import RunPaths
from app.services.logs import LogService
from tests.conftest import sign_in


class _NoInventory:
    """An inventory service on a machine where nothing has been written."""

    def state(self):
        from app.inventory.service import InventoryState

        return InventoryState(inventory=None, commit=None)


def _paths() -> RunPaths:
    return RunPaths(
        collections_root=Path("/nowhere"),
        private_key_file=Path("/nowhere/id"),
        known_hosts_file=Path("/nowhere/known_hosts"),
        ssh_config_file=Path("/nowhere/config"),
    )


CLUSTER = """
all:
  hosts:
    seapath-machine:
      ansible_host: 192.168.200.125
      network_interface: eno1
    elabo1:
      ansible_host: 192.168.200.126
      network_interface: eno1
    elabo2:
      ansible_host: 192.168.200.127
      network_interface: eno1
  children:
    cluster_machines:
      hosts:
        seapath-machine:
        elabo1:
        elabo2:
    hypervisors:
      hosts:
        seapath-machine:
        elabo1:
        elabo2:
    VMs:
      hosts:
        guest1:
          ansible_host: 192.168.200.10
"""


def entry(
    *,
    stamp: datetime,
    message: str = "a line",
    unit: str = "pacemaker.service",
    priority: str = "6",
    host: str = "seapath-machine",
) -> str:
    """One line of `journalctl -o json`, as the far end writes it."""
    return json.dumps(
        {
            "__CURSOR": f"s=abc;i={int(stamp.timestamp() * 1e6)}",
            "__REALTIME_TIMESTAMP": str(int(stamp.timestamp() * 1_000_000)),
            "__MONOTONIC_TIMESTAMP": "123456",
            "_BOOT_ID": "342f8522cf08444aa21371cf19f245f1",
            "PRIORITY": priority,
            "MESSAGE": message,
            "_SYSTEMD_UNIT": unit,
            "SYSLOG_IDENTIFIER": unit.partition(".")[0],
            "_PID": "5463",
            "_HOSTNAME": host,
        }
    )


NOW = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)


def a_query(**changes) -> Query:
    base = {
        "since": NOW - timedelta(minutes=15),
        "units": ("pacemaker.service",),
    }
    base.update(changes)
    return Query(**base)


class Answers:
    """The machines, each with its own answer, keyed by address.

    `FakeRemoteRunner` matches on the command, and every machine of a fan out
    is sent the same one. What a test of a fan out needs to vary is the
    machine, so this keys on the address and records what each was asked.
    """

    def __init__(self, by_address: dict[str, str | Exception]) -> None:
        self.by_address = by_address
        self.requests: list[RemoteRequest] = []

    def run(self, request: RemoteRequest) -> str:
        self.requests.append(request)
        answer = self.by_address.get(request.address, "")
        if isinstance(answer, Exception):
            raise answer
        return answer


def build(client: TestClient, answers: Answers) -> TestClient:
    """A signed in client whose log service answers from `answers`."""
    sign_in(client, "admin")
    response = client.post("/api/v1/inventory/import", json={"document": CLUSTER})
    assert response.status_code == 200, response.text
    client.app.state.log_service._remote = answers  # noqa: SLF001 - the seam
    return client


# What may be asked


def test_a_query_with_no_match_on_a_field_is_refused() -> None:
    # The rule the whole design rests on. Without a match, journalctl scans the
    # journal: 2.9 seconds measured against 9 ms for the same search behind a
    # unit match.
    with pytest.raises(Refused, match="at least one match on a field"):
        journal.checked(Query(since=NOW - timedelta(minutes=15)))


def test_a_priority_alone_is_a_match() -> None:
    # PRIORITY is indexed like any other field, which is what lets "everything
    # at error and above" be offered with no unit beside it.
    assert journal.checked(Query(since=NOW, priority=3)).matches


def test_a_pattern_alone_is_refused() -> None:
    with pytest.raises(Refused, match="at least one match on a field"):
        journal.checked(Query(since=NOW, grep="migrat"))


@pytest.mark.parametrize(
    "unit",
    [
        "pacemaker.service; rm -rf /",
        "$(id).service",
        "`id`.service",
        "unit with a space.service",
        "'quoted'.service",
        "",
    ],
)
def test_a_unit_that_is_not_a_unit_name_is_refused(unit: str) -> None:
    with pytest.raises(Refused, match="not a unit name"):
        journal.checked(a_query(units=(unit,)))


def test_the_units_a_seapath_machine_actually_runs_are_accepted() -> None:
    # `ceph-<fsid>@mon.ccv1.service` is why the pattern carries `@` and a long
    # run of hexadecimal.
    journal.checked(
        a_query(
            units=(
                "libvirtd.service",
                "pacemaker.service",
                "ceph-46613678-1b54-42ab-b3ac-5b2c1d285802@mon.ccv1.service",
                "seapath-webui.service",
            )
        )
    )


def test_an_identifier_that_is_not_one_is_refused() -> None:
    with pytest.raises(Refused, match="not a syslog identifier"):
        journal.checked(a_query(identifiers=("kernel; id",)))


@pytest.mark.parametrize("priority", [-1, 8, 99])
def test_a_priority_outside_the_syslog_range_is_refused(priority: int) -> None:
    with pytest.raises(Refused, match="not a syslog priority"):
        journal.checked(a_query(priority=priority))


@pytest.mark.parametrize("lines", [0, -1, journal.MAX_LINES + 1])
def test_the_number_of_entries_is_bounded(lines: int) -> None:
    with pytest.raises(Refused, match="1 to"):
        journal.checked(a_query(lines=lines))


def test_an_empty_pattern_is_refused() -> None:
    with pytest.raises(Refused, match="matches everything"):
        journal.checked(a_query(grep="   "))


def test_a_pattern_longer_than_the_bound_is_refused() -> None:
    with pytest.raises(Refused, match="at most"):
        journal.checked(a_query(grep="x" * (journal.MAX_GREP + 1)))


def test_a_pattern_that_does_not_compile_is_refused() -> None:
    with pytest.raises(Refused, match="not a valid pattern"):
        journal.checked(a_query(grep="migrat("))


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(Refused, match="ends before it starts"):
        journal.checked(a_query(since=NOW, until=NOW - timedelta(hours=1)))


def test_a_window_wider_than_the_bound_is_refused() -> None:
    with pytest.raises(Refused, match="at most"):
        journal.checked(a_query(since=NOW - timedelta(days=30), until=NOW))


def test_an_unchecked_query_never_reaches_a_command_line() -> None:
    # The flag is the belt to the patterns' braces: a Query built by hand in a
    # test, or by a caller that skipped `checked`, produces no argv at all.
    with pytest.raises(Refused, match="only once it is checked"):
        journal.argv(a_query())


# What is sent


def test_the_command_runs_as_root_through_the_one_rule_the_iso_grants() -> None:
    # `ansible` is in neither `adm` nor `systemd-journal`, so it sees nothing
    # of the system journal, and `/bin/sh` is the whole of its sudo rule. `-n`
    # so a machine missing the rule answers with an error rather than waiting
    # on a password prompt nothing will type into.
    command = journal.command(journal.checked(a_query()))
    assert command.startswith("sudo -n /bin/sh -c ")
    assert "journalctl" in command


def test_the_bounds_are_absolute_and_carry_their_timezone() -> None:
    # A bare timestamp is read in the machine's local time, and the machines of
    # one inventory need not share a timezone.
    built = journal.argv(
        journal.checked(a_query(since=NOW - timedelta(hours=1), until=NOW))
    )
    assert "--since=2026-09-20 09:00:00 UTC" in built
    assert "--until=2026-09-20 10:00:00 UTC" in built


def test_a_naive_moment_is_read_as_utc_by_the_endpoint(
    client: TestClient, remote_runner
) -> None:
    answers = Answers({})
    build(client, answers)
    response = client.get(
        "/api/v1/logs",
        params={"unit": "pacemaker.service", "since": "2026-09-20T09:00:00"},
    )
    assert response.status_code == 200, response.text
    assert "--since=2026-09-20 09:00:00 UTC" in answers.requests[0].command


def test_the_query_asks_for_the_fields_the_page_shows_and_no_more() -> None:
    # A full `-o json` entry is three times the size, and the answer crosses an
    # administration network once per machine.
    built = journal.argv(journal.checked(a_query()))
    fields = next(item for item in built if item.startswith("--output-fields="))
    assert fields == "--output-fields=" + ",".join(journal.FIELDS)


def test_every_match_reaches_the_command() -> None:
    built = journal.argv(
        journal.checked(
            a_query(
                units=("libvirtd.service", "pacemaker.service"),
                identifiers=("kernel",),
                priority=4,
                grep="migrat",
                lines=42,
            )
        )
    )
    assert built.count("--unit") == 2
    assert "--identifier" in built and "kernel" in built
    assert built[built.index("--priority") + 1] == "4"
    assert built[built.index("--grep") + 1] == "migrat"
    assert "--lines=42" in built


def test_a_pattern_carrying_shell_characters_stays_one_argument() -> None:
    # It crosses the remote shell and then `sh -c`, so it is quoted twice. A
    # pattern is a legitimate place for a quote or a dollar, and it must reach
    # journalctl as one argument whatever it holds, with nothing in it read as
    # shell syntax by either of the two.
    pattern = "a'b$c \"d; id"
    command = journal.command(journal.checked(a_query(grep=pattern)))
    outer = shlex.split(command)
    assert outer[:3] == ["sudo", "-n", "/bin/sh"]
    inner = shlex.split(outer[-1])
    assert inner[0] == "journalctl"
    assert inner[inner.index("--grep") + 1] == pattern


# What comes back


def test_an_entry_is_read_with_its_machine_and_its_time() -> None:
    entries, skipped = journal.parse(entry(stamp=NOW, message="hello"), "elabo1")
    assert skipped == []
    assert len(entries) == 1
    read = entries[0]
    assert read.host == "elabo1"
    assert read.timestamp == NOW
    assert read.message == "hello"
    assert read.priority == 6
    assert read.unit == "pacemaker.service"
    assert read.pid == 5463


def test_a_message_the_journal_kept_as_bytes_is_decoded() -> None:
    # journalctl writes a list of integers when a message is not valid UTF-8,
    # rather than losing it.
    line = json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1758362400000000",
            "MESSAGE": [104, 105, 255],
        }
    )
    entries, _ = journal.parse(line, "elabo1")
    assert entries[0].message.startswith("hi")


def test_a_field_the_journal_wrote_twice_is_joined() -> None:
    line = json.dumps(
        {"__REALTIME_TIMESTAMP": "1758362400000000", "MESSAGE": ["one", "two"]}
    )
    entries, _ = journal.parse(line, "elabo1")
    assert entries[0].message == "one two"


def test_a_field_left_out_of_the_answer_is_said_so_rather_than_guessed() -> None:
    line = json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1758362400000000",
            "MESSAGE": {"type": "data", "size": 9000},
        }
    )
    entries, _ = journal.parse(line, "elabo1")
    assert "not included" in entries[0].message


def test_a_line_that_cannot_be_read_costs_that_line_and_not_the_answer() -> None:
    # The far end may run a different systemd than this image was built
    # against. Four hundred and ninety nine entries and a warning beat a blank
    # page.
    text = "\n".join(
        [entry(stamp=NOW), "{not json", entry(stamp=NOW + timedelta(seconds=1))]
    )
    entries, skipped = journal.parse(text, "elabo1")
    assert len(entries) == 2
    assert len(skipped) == 1


def test_an_entry_with_no_usable_timestamp_is_skipped() -> None:
    entries, skipped = journal.parse(json.dumps({"MESSAGE": "no time"}), "elabo1")
    assert entries == []
    assert skipped[0].reason == "no usable timestamp"


# The fan out


def test_every_machine_of_the_inventory_is_asked_at_its_administration_address(
    client: TestClient,
) -> None:
    # The relations carry `from=` bound to the administration address, so a
    # read that went out over the cluster network would be refused by
    # authorized_keys before sshd looked at the key.
    answers = Answers({})
    build(client, answers)
    response = client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    assert response.status_code == 200, response.text
    asked = {request.address for request in answers.requests}
    assert asked == {"192.168.200.125", "192.168.200.126", "192.168.200.127"}


def test_this_machine_is_asked_over_the_same_ssh_as_the_others(
    client: TestClient,
) -> None:
    # The container has no route to the host's journal and must not grow one,
    # so the node the operator is browsing is one entry of the fan out.
    answers = Answers({})
    build(client, answers)
    client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    assert "192.168.200.125" in {request.address for request in answers.requests}


def test_the_guests_are_not_asked(client: TestClient) -> None:
    # The `VMs` group is guests. This service holds no trust into them.
    answers = Answers({})
    build(client, answers)
    client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    assert "192.168.200.10" not in {request.address for request in answers.requests}


def test_a_machine_that_does_not_answer_is_a_row_and_not_a_failure(
    client: TestClient,
) -> None:
    answers = Answers(
        {
            "192.168.200.125": entry(stamp=NOW, message="from this one"),
            "192.168.200.126": RemoteRefused("192.168.200.126 did not answer."),
            "192.168.200.127": entry(stamp=NOW, message="and this one"),
        }
    )
    build(client, answers)
    response = client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["entries"]) == 2
    silent = [row for row in body["machines"] if not row["answered"]]
    assert len(silent) == 1
    assert silent[0]["host"] == "elabo1"
    assert any("elabo1" in warning for warning in body["warnings"])


def test_the_machines_are_asked_with_the_shorter_connect_timeout(
    client: TestClient,
) -> None:
    # Ten seconds is right for a reading an operator waits on alone. This one
    # fans out, and the page waits on the slowest machine.
    answers = Answers({})
    build(client, answers)
    client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    assert answers.requests[0].connect_timeout == 5


def test_the_entries_of_every_machine_are_merged_in_time_order(
    client: TestClient,
) -> None:
    answers = Answers(
        {
            "192.168.200.125": entry(stamp=NOW, message="first"),
            "192.168.200.126": entry(stamp=NOW + timedelta(seconds=2), message="third"),
            "192.168.200.127": entry(
                stamp=NOW + timedelta(seconds=1), message="second"
            ),
        }
    )
    build(client, answers)
    body = client.get("/api/v1/logs", params={"unit": "pacemaker.service"}).json()
    assert [item["message"] for item in body["entries"]] == [
        "first",
        "second",
        "third",
    ]
    assert [item["host"] for item in body["entries"]] == [
        "seapath-machine",
        "elabo2",
        "elabo1",
    ]


def test_the_answer_is_bounded_whatever_the_cluster_holds(client: TestClient) -> None:
    # Every machine is asked for `lines` and the merge keeps the most recent of
    # them, so a five node cluster costs what a one node cluster costs.
    many = "\n".join(
        entry(stamp=NOW + timedelta(seconds=index), message=f"line {index}")
        for index in range(5)
    )
    answers = Answers(
        dict.fromkeys(["192.168.200.125", "192.168.200.126", "192.168.200.127"], many)
    )
    build(client, answers)
    body = client.get(
        "/api/v1/logs", params={"unit": "pacemaker.service", "lines": 4}
    ).json()
    assert len(body["entries"]) == 4
    assert body["truncated"] is True
    assert any("Older entries" in warning for warning in body["warnings"])


def test_a_machine_that_filled_its_answer_says_it_holds_older_entries(
    client: TestClient,
) -> None:
    answers = Answers({"192.168.200.125": entry(stamp=NOW)})
    build(client, answers)
    body = client.get(
        "/api/v1/logs", params={"unit": "pacemaker.service", "lines": 1}
    ).json()
    capped = {row["host"]: row["capped"] for row in body["machines"]}
    assert capped["seapath-machine"] is True
    assert capped["elabo1"] is False


def test_a_reading_can_be_narrowed_to_the_machines_that_matter(
    client: TestClient,
) -> None:
    answers = Answers({})
    build(client, answers)
    response = client.get(
        "/api/v1/logs", params={"unit": "pacemaker.service", "host": ["elabo1"]}
    )
    assert response.status_code == 200, response.text
    assert [request.address for request in answers.requests] == ["192.168.200.126"]


def test_a_machine_the_inventory_does_not_declare_is_refused(
    client: TestClient,
) -> None:
    # Quietly asking two machines when the caller named three would report an
    # incident as absent from a machine nobody looked at.
    answers = Answers({})
    build(client, answers)
    response = client.get(
        "/api/v1/logs", params={"unit": "pacemaker.service", "host": ["elabo9"]}
    )
    assert response.status_code == 409
    assert "elabo9" in response.json()["error"]["message"]


def test_a_node_with_no_inventory_says_so_rather_than_answering_empty() -> None:
    # A machine on which nothing has been written yet. The page has to say
    # where to go rather than render an empty log and let an operator conclude
    # the cluster is quiet.
    service = LogService(
        inventory=_NoInventory(),
        remote=Answers({}),
        keys=_paths(),
        ansible_user="ansible",
    )
    reading = service.read(a_query())
    assert reading.entries == []
    assert "no inventory" in reading.warnings[0]
    assert reading.machines == []


# The endpoint


def test_a_query_with_no_match_is_refused_with_the_condition_named(
    client: TestClient,
) -> None:
    build(client, Answers({}))
    response = client.get("/api/v1/logs", params={"grep": "migrat"})
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "precondition_failed"
    assert "match on a field" in body["message"]


def test_a_scope_supplies_the_matches(client: TestClient) -> None:
    answers = Answers({})
    build(client, answers)
    response = client.get("/api/v1/logs", params={"scope": "guests"})
    assert response.status_code == 200, response.text
    command = answers.requests[0].command
    assert "libvirtd.service" in command
    assert "pacemaker.service" in command
    assert "corosync.service" in command


def test_the_errors_scope_is_a_priority_and_needs_no_unit(client: TestClient) -> None:
    answers = Answers({})
    build(client, answers)
    response = client.get("/api/v1/logs", params={"scope": "errors"})
    assert response.status_code == 200, response.text
    assert "--priority 3" in answers.requests[0].command


def test_a_scope_and_a_pattern_compose(client: TestClient) -> None:
    answers = Answers({})
    build(client, answers)
    response = client.get(
        "/api/v1/logs", params={"scope": "guests", "grep": "migrat", "host": ["elabo1"]}
    )
    assert response.status_code == 200, response.text
    assert len(answers.requests) == 1
    assert "--grep migrat" in answers.requests[0].command


def test_a_scope_that_does_not_exist_is_refused_with_the_ones_that_do(
    client: TestClient,
) -> None:
    build(client, Answers({}))
    response = client.get("/api/v1/logs", params={"scope": "everything"})
    assert response.status_code == 409
    assert "guests" in response.json()["error"]["message"]


def test_a_caller_that_names_no_window_gets_the_last_fifteen_minutes(
    client: TestClient,
) -> None:
    answers = Answers({})
    build(client, answers)
    client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    stamp = next(
        part
        for part in answers.requests[0].command.split("--since=")[1:]
        for part in [part[:19]]
    )
    asked = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    waited = datetime.now(UTC) - asked
    assert timedelta(minutes=14) < waited < timedelta(minutes=17)


def test_the_page_reads_the_scopes_and_the_machines_from_the_service(
    client: TestClient,
) -> None:
    build(client, Answers({}))
    body = client.get("/api/v1/logs/sources").json()
    assert [scope["name"] for scope in body["scopes"]] == [
        "guests",
        "cluster",
        "errors",
        "service",
    ]
    assert {machine["host"] for machine in body["machines"]} == {
        "seapath-machine",
        "elabo1",
        "elabo2",
    }
    assert body["max_lines"] == journal.MAX_LINES


def test_a_viewer_reads_the_journal(client: TestClient) -> None:
    answers = Answers({})
    build(client, answers)
    sign_in(client, "viewer")
    response = client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    assert response.status_code == 200, response.text


def test_a_site_can_raise_the_role_the_journal_needs(client: TestClient) -> None:
    # A journal carries command lines and whatever a service chose to print, so
    # a site that treats it as more than a reading raises this.
    from app.core.auth import Role

    answers = Answers({})
    build(client, answers)
    client.app.state.log_service.required_role = Role.ADMIN
    sign_in(client, "viewer")
    response = client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    assert response.status_code == 403
    assert "admin" in response.json()["error"]["message"]


def test_signing_in_is_required(client: TestClient) -> None:
    response = client.get("/api/v1/logs", params={"unit": "pacemaker.service"})
    assert response.status_code == 401


def test_a_blank_line_in_the_answer_is_not_an_entry() -> None:
    # journalctl ends its output with a newline, and a connection that carried
    # an extra one must not become an entry with no timestamp.
    entries, skipped = journal.parse(f"\n{entry(stamp=NOW)}\n\n", "elabo1")
    assert len(entries) == 1
    assert skipped == []


def test_a_line_that_is_json_but_not_an_entry_is_skipped() -> None:
    entries, skipped = journal.parse('"a bare string"', "elabo1")
    assert entries == []
    assert skipped[0].reason == "not a JSON object"


def test_a_field_written_as_a_number_is_read_all_the_same() -> None:
    # The journal writes its values as strings, and a reader that assumed so
    # would break on the day one of them arrives as a JSON number.
    line = json.dumps({"__REALTIME_TIMESTAMP": "1758362400000000", "PRIORITY": 4})
    entries, _ = journal.parse(line, "elabo1")
    assert entries[0].priority == 4


def test_a_machine_whose_answer_could_not_be_read_whole_says_so(
    client: TestClient,
) -> None:
    answers = Answers({"192.168.200.126": "\n".join([entry(stamp=NOW), "{not json"])})
    build(client, answers)
    body = client.get("/api/v1/logs", params={"unit": "pacemaker.service"}).json()
    assert len(body["entries"]) == 1
    assert any("could not be read" in warning for warning in body["warnings"])


def test_asking_about_no_machine_reaches_no_machine() -> None:
    answers = Answers({})
    service = LogService(
        inventory=_NoInventory(),
        remote=answers,
        keys=_paths(),
        ansible_user="ansible",
    )
    assert service._ask([], journal.checked(a_query())) == []  # noqa: SLF001
    assert answers.requests == []


def test_a_window_given_with_its_own_offset_is_converted(
    client: TestClient,
) -> None:
    answers = Answers({})
    build(client, answers)
    response = client.get(
        "/api/v1/logs",
        params={
            "unit": "pacemaker.service",
            "since": "2026-09-20T10:00:00+02:00",
            "until": "2026-09-20T11:00:00+02:00",
        },
    )
    assert response.status_code == 200, response.text
    command = answers.requests[0].command
    assert "--since=2026-09-20 08:00:00 UTC" in command
    assert "--until=2026-09-20 09:00:00 UTC" in command


def test_a_priority_the_caller_named_wins_over_the_scope(client: TestClient) -> None:
    # The scope contributes what the caller left out rather than replacing what
    # they sent, so a page can narrow a scope without leaving it.
    answers = Answers({})
    build(client, answers)
    response = client.get("/api/v1/logs", params={"scope": "errors", "priority": 1})
    assert response.status_code == 200, response.text
    assert "--priority 1" in answers.requests[0].command


def test_a_line_systemd_wrote_about_a_unit_is_labelled_with_that_unit() -> None:
    """The line that says a cluster daemon stopped comes from PID 1.

    `journalctl -u pacemaker.service` returns it, because it is about that
    unit, and it carries `_SYSTEMD_UNIT=init.scope` because `systemd` wrote it.
    A table drawn from that field alone labels the most important line of a
    failover with the scope of PID 1. Taken verbatim from a SEAPATH cluster
    member during a node shutdown.
    """
    line = json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1789805861074669",
            "MESSAGE": "pacemaker.service: Deactivated successfully.",
            "UNIT": "pacemaker.service",
            "SYSLOG_IDENTIFIER": "systemd",
            "_SYSTEMD_UNIT": "init.scope",
            "_PID": "1",
            "PRIORITY": "6",
        }
    )
    entries, _ = journal.parse(line, "ccv1")
    assert entries[0].unit == "pacemaker.service"


def test_a_line_a_daemon_wrote_itself_keeps_its_own_unit() -> None:
    entries, _ = journal.parse(entry(stamp=NOW, unit="corosync.service"), "ccv1")
    assert entries[0].unit == "corosync.service"


def test_the_unit_a_line_is_about_is_asked_for() -> None:
    assert "UNIT" in journal.FIELDS
    # The machine's own name is not: a machine is named here by the inventory's
    # name for it, so asking for `_HOSTNAME` would be bytes across an
    # administration network for a value nothing reads.
    assert "_HOSTNAME" not in journal.FIELDS
