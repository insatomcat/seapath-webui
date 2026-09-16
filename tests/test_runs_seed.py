# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The root password: in the run's own copy of the inventory, and nowhere else.

The property every test here is about is the same one: the repository never
holds the hash, at any moment, committed or in the working tree. What holds it
is the copy `app.runs.staging` freezes into the run directory, from the moment
it is staged to the moment the run ends.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.errors import ApiError
from app.runs import fake, seed
from app.runs.adapter import (
    CancelCheck,
    EventHandler,
    OutputHandler,
    RunOutcome,
    RunRequest,
)
from tests.test_runs import (  # noqa: F401
    build,
    store,
    trust,
    wait_for,
    wait_for_the_lock,
)
from tests.test_runs import inventory as inventory_fixture  # noqa: F401

PASSWORD = "an office chair"

INVENTORY = """\
all:
  children:
    VMs:
      hosts:
        guest1:
          vm_disk: ../files/guest1.qcow2
          ansible_host: 10.0.0.42
          cloud_init:
            hostname: guest1
            users:
              - name: ansible
                ssh_authorized_keys:
                  - ssh-ed25519 AAAAkey ansible@node
        guest2:
          vm_disk: ../files/guest2.qcow2
"""


# The splice


def test_the_password_becomes_a_hash_beside_the_account_the_entry_names() -> None:
    written = seed.carrying_passwords(INVENTORY, {"guest1": PASSWORD})
    users = yaml.safe_load(written)["all"]["children"]["VMs"]["hosts"]["guest1"][
        "cloud_init"
    ]["users"]

    # The trust account is untouched and root is added under it: one seed, two
    # accounts, one reached by key over the network and one at the console.
    assert [user["name"] for user in users] == ["ansible", "root"]
    assert users[1]["lock_passwd"] is False
    assert users[1]["hashed_passwd"].startswith("$6$rounds=656000$")
    assert PASSWORD not in written


def test_a_guest_with_no_seed_at_all_gains_one() -> None:
    # The guest whose image carries its own network: the form asked for a
    # password and nothing else, so the entry has no `cloud_init` for the
    # member to go in and the splice writes the key.
    written = seed.carrying_passwords(INVENTORY, {"guest2": PASSWORD})
    entry = yaml.safe_load(written)["all"]["children"]["VMs"]["hosts"]["guest2"]

    assert [user["name"] for user in entry["cloud_init"]["users"]] == ["root"]


def test_the_other_guests_of_the_file_are_left_alone() -> None:
    written = seed.carrying_passwords(INVENTORY, {"guest1": PASSWORD})
    hosts = yaml.safe_load(written)["all"]["children"]["VMs"]["hosts"]

    assert hosts["guest2"] == {"vm_disk": "../files/guest2.qcow2"}
    assert hosts["guest1"]["ansible_host"] == "10.0.0.42"
    assert hosts["guest1"]["cloud_init"]["hostname"] == "guest1"


def test_a_password_for_a_guest_the_file_does_not_declare_is_refused() -> None:
    # Written into nothing otherwise, and the run would create the guest with
    # no password and say nothing about why the console refuses it.
    with pytest.raises(seed.SeedRefused) as failure:
        seed.carrying_passwords(INVENTORY, {"ghost": PASSWORD})

    assert "ghost" in str(failure.value)
    assert PASSWORD not in str(failure.value)


def test_the_same_guest_spliced_twice_carries_one_root_member() -> None:
    once = seed.carrying_passwords(INVENTORY, {"guest1": PASSWORD})
    twice = seed.carrying_passwords(once, {"guest1": "another one"})
    users = yaml.safe_load(twice)["all"]["children"]["VMs"]["hosts"]["guest1"][
        "cloud_init"
    ]["users"]

    assert [user["name"] for user in users] == ["ansible", "root"]


# The wipe


def test_the_wipe_takes_the_hash_out_and_leaves_the_key_account() -> None:
    written = seed.carrying_passwords(INVENTORY, {"guest1": PASSWORD})

    cleaned = seed.without_passwords(written)
    users = yaml.safe_load(cleaned)["all"]["children"]["VMs"]["hosts"]["guest1"][
        "cloud_init"
    ]["users"]

    assert "hashed_passwd" not in cleaned
    assert "$6$" not in cleaned
    assert [user["name"] for user in users] == ["ansible"]


def test_a_seed_the_splice_created_loses_its_users_list() -> None:
    # What the entry carried before: `cloud_init` with no `users` at all,
    # rather than an empty list cloud-init would read as an instruction.
    written = seed.carrying_passwords(INVENTORY, {"guest2": PASSWORD})

    cleaned = yaml.safe_load(seed.without_passwords(written))
    entry = cleaned["all"]["children"]["VMs"]["hosts"]["guest2"]

    assert "users" not in (entry.get("cloud_init") or {})


def test_a_file_that_never_carried_a_hash_is_left_byte_for_byte() -> None:
    assert seed.without_passwords(INVENTORY) == INVENTORY


def test_wiping_a_file_that_cannot_be_read_is_logged_and_not_raised(
    tmp_path: Path,
) -> None:
    # This runs as a run ends, where the operator is waiting for the run's own
    # outcome. A copy that could not be rewritten is a log line.
    assert seed.wipe(tmp_path / "nothing.yaml") is False


# Through a run


class WatchingAdapter(fake.FakeRunAdapter):
    """A fake run that records what the inventory said while it was going."""

    def __init__(self) -> None:
        super().__init__()
        self.inventory_during_the_run = ""

    def execute(
        self,
        request: RunRequest,
        on_event: EventHandler,
        on_output: OutputHandler,
        should_cancel: CancelCheck,
    ) -> RunOutcome:
        self.inventory_during_the_run = request.inventory_file.read_text()
        return super().execute(request, on_event, on_output, should_cancel)


@pytest.fixture
def declared(inventory_fixture):  # noqa: F811
    inventory_fixture.declare_guest(
        "guest1",
        {"vm_disk": "../files/guest1.qcow2"},
        author="alice",
    )
    return inventory_fixture


def committed(tmp_path: Path) -> str:
    """The inventory as the repository holds it, working tree included."""
    return (tmp_path / "inventory" / "inventory.yaml").read_text()


def test_the_run_reads_the_hash_and_the_repository_never_holds_it(
    store,  # noqa: F811
    declared,
    trust,  # noqa: F811
    tmp_path: Path,
) -> None:
    adapter = WatchingAdapter()
    service = build(store, declared, trust, adapter, tmp_path)
    before = committed(tmp_path)
    head = declared.state().commit

    record = service.launch(
        "deploy_vms_standalone", "alice", root_passwords={"guest1": PASSWORD}
    )
    wait_for(service, record.id)

    # The play read a seed carrying the hash.
    during = yaml.safe_load(adapter.inventory_during_the_run)
    users = _guest(during)["cloud_init"]["users"]
    assert [user["name"] for user in users] == ["root"]
    assert users[0]["hashed_passwd"].startswith("$6$rounds=")
    assert PASSWORD not in adapter.inventory_during_the_run

    # The copy is wiped when the run ends.
    staged = adapter.requests[0].inventory_file.read_text()
    assert "hashed_passwd" not in staged
    assert "$6$" not in staged

    # And the repository never moved. This is the property the whole design is
    # for: no commit, and no change in the working tree either.
    assert committed(tmp_path) == before
    assert declared.state().commit == head
    assert "hashed_passwd" not in before
    assert PASSWORD not in before


def test_a_password_is_refused_where_this_node_cannot_build_a_seed(
    store,  # noqa: F811
    declared,
    trust,  # noqa: F811
    tmp_path: Path,
) -> None:
    # The entry carries no seed, so the precondition that reads the inventory
    # says nothing about this guest. Without `cloud-localds` the role creates
    # it with no seed disk and reports success, and the console still refuses.
    service = build(
        store,
        declared,
        trust,
        fake.FakeRunAdapter(),
        tmp_path,
        seed_builder=lambda: None,
    )

    with pytest.raises(ApiError) as failure:
        service.launch(
            "deploy_vms_standalone", "alice", root_passwords={"guest1": PASSWORD}
        )

    assert failure.value.status_code == 409
    assert failure.value.detail["codes"] == ["seed_buildable"]
    assert "cloud-image-utils" in failure.value.message
    assert PASSWORD not in failure.value.message


def test_a_run_launched_without_a_password_stages_the_inventory_unchanged(
    store,  # noqa: F811
    declared,
    trust,  # noqa: F811
    tmp_path: Path,
) -> None:
    adapter = WatchingAdapter()
    service = build(store, declared, trust, adapter, tmp_path)

    record = service.launch("deploy_vms_standalone", "alice")
    wait_for(service, record.id)

    assert adapter.inventory_during_the_run == committed(tmp_path)


def test_the_copy_is_wiped_even_when_the_run_fails(
    store,  # noqa: F811
    declared,
    trust,  # noqa: F811
    tmp_path: Path,
) -> None:
    # The play has read the seed by the time anything fails, so what is left is
    # a file on this node holding a hash nothing reads again.
    adapter = WatchingAdapter()
    adapter.events = fake.failed_run()
    adapter.return_code = 2
    service = build(store, declared, trust, adapter, tmp_path)

    record = service.launch(
        "deploy_vms_standalone", "alice", root_passwords={"guest1": PASSWORD}
    )
    wait_for(service, record.id)

    assert "$6$" not in adapter.requests[0].inventory_file.read_text()


def test_the_copy_is_wiped_before_anything_reads_the_run_as_finished(
    store,  # noqa: F811
    declared,
    trust,  # noqa: F811
    tmp_path: Path,
) -> None:
    # The ordering inside the run's `finally`, pinned because getting it wrong
    # fails on a loaded machine and nowhere else. The record is what every
    # reader watches, this service's own listeners included, so the wipe comes
    # before the record says the run ended. A listener runs later still, and
    # what it sees here is what a reader polling the record sees too.
    seen: list[str] = []
    adapter = WatchingAdapter()
    service = build(store, declared, trust, adapter, tmp_path)
    service.when_finished(
        lambda record: seen.append(adapter.requests[0].inventory_file.read_text())
    )

    record = service.launch(
        "deploy_vms_standalone", "alice", root_passwords={"guest1": PASSWORD}
    )
    wait_for(service, record.id)
    assert wait_for_the_lock(store)

    assert seen and "hashed_passwd" not in seen[0]


def test_the_password_reaches_no_variable_of_the_record(
    store,  # noqa: F811
    declared,
    trust,  # noqa: F811
    tmp_path: Path,
) -> None:
    # `variables` is what a relaunch replays and what the run view shows, and
    # `command` is built from the extra vars: a password in either is a
    # password on a screen and in a file this service keeps.
    adapter = WatchingAdapter()
    service = build(store, declared, trust, adapter, tmp_path)

    record = service.launch(
        "deploy_vms_standalone", "alice", root_passwords={"guest1": PASSWORD}
    )
    finished = wait_for(service, record.id)

    assert PASSWORD not in repr(finished.model_dump())
    assert "hashed_passwd" not in repr(finished.model_dump())


def _guest(document: dict) -> dict:
    return document["VMs"]["hosts"]["guest1"]
