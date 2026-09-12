# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Runs: the invocation, the event mapping, and how a run ends.

The interruption cases carry most of the weight here. A playbook can reboot the
machine running it, so a run that dies mid flight is the ordinary case, not the
exceptional one.
"""

from __future__ import annotations

import configparser
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.hosts.fake import FakeHostReader
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService
from app.runs import catalogue, fake
from app.runs.adapter import RunRequest, build_command, prepare, runner_arguments
from app.runs.models import RunProgress, RunRecord, RunState
from app.runs.progress import apply_event, summarise
from app.runs.scope import RunScope
from app.runs.service import RunPaths, RunService
from app.runs.store import RunLocked, RunStore
from app.trust.service import TrustService
from tests.fakes import write_fake_collection

SITE_KEY = "ssh-rsa AAAAsite ansible@control-machine"


@pytest.fixture
def trust(tmp_path: Path) -> TrustService:
    ssh_home = tmp_path / "home/ansible/.ssh"
    ssh_home.mkdir(parents=True)
    (ssh_home / "authorized_keys").write_text(SITE_KEY + "\n")
    service = TrustService(
        ssh_dir=tmp_path / "state/ssh",
        authorized_keys_file=ssh_home / "authorized_keys",
    )
    service.ensure_self_trust("seapath-machine", ["192.168.200.125"])
    return service


@pytest.fixture
def inventory(tmp_path: Path) -> InventoryService:
    service = InventoryService(
        InventoryRepository(tmp_path / "inventory"), FakeHostReader()
    )
    service.ensure_seed()
    return service


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


def build(
    store: RunStore,
    inventory: InventoryService,
    trust: TrustService,
    adapter,
    tmp_path: Path,
    collections: Path | None = None,
    seed_builder=lambda: "/usr/bin/cloud-localds",
) -> RunService:
    return RunService(
        store=store,
        adapter=adapter,
        inventory=inventory,
        trust=trust,
        paths=RunPaths(
            collections_root=collections
            or write_fake_collection(tmp_path / "collections"),
            private_key_file=tmp_path / "state/ssh/id_ed25519_self",
            known_hosts_file=tmp_path / "state/ssh/known_hosts",
            ssh_config_file=tmp_path / "root/.ssh/config",
        ),
        hostname="seapath-machine",
        collection_version="2.0.0",
        # The suite runs on a laptop, which has no `cloud-localds` and no
        # cluster either. The default here is the machine that does, so the
        # absence is asked for by the one test that is about it rather than
        # inherited from whoever is running the suite.
        seed_builder=seed_builder,
    )


def wait_for(service: RunService, run_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = service.get(run_id)
        if record is not None and record.finished:
            return record
        time.sleep(0.01)
    raise AssertionError(f"Run {run_id} did not finish")


# The invocation


def test_the_invocation_carries_the_settings_the_collection_does_not_ship(
    tmp_path: Path,
) -> None:
    # seapath-ansible lists ansible.cfg under galaxy.yml build_ignore, so the
    # installed collection carries none of it. Losing these would gather facts
    # everywhere, continue past a failed host, and install packages.
    preparation = prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.seapath_setup_main",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
        )
    )
    config = preparation.config_file.read_text()

    assert "gathering = explicit" in config
    assert "any_errors_fatal = True" in config
    assert "skip = package-install" in config
    assert "force_valid_group_names = ignore" in config
    # The host key is read from the machine's own filesystem, so checking stays
    # on rather than being waved through.
    assert "host_key_checking = True" in config
    assert f"UserKnownHostsFile={tmp_path / 'known_hosts'}" in config


def test_the_narrowing_reaches_ansible_and_not_only_the_recorded_command(
    tmp_path: Path,
) -> None:
    """The bug this holds shut: a limit that was displayed and never passed.

    `build_command` builds what the run records and shows, and the adapter
    built the call to `ansible_runner.run` separately, without the limit. A run
    narrowed to one machine therefore played every machine the playbook names,
    and the guest subtraction of D39 protected the screen and nothing else. On a
    substation that is a convergence reaching machines and guests nobody aimed
    it at.
    """
    request = RunRequest(
        run_id="r1",
        playbook="seapath.ansible.test_run_cyclictest_vms",
        inventory_file=tmp_path / "inventory.yaml",
        private_data_dir=tmp_path / "run",
        collections_path=tmp_path / "collections",
        private_key_file=tmp_path / "key",
        known_hosts_file=tmp_path / "known_hosts",
        limit="rtvm",
    )

    arguments = runner_arguments(request, prepare(request))
    command = build_command(request)

    assert arguments["limit"] == "rtvm"
    # The two are built from one request and say the same thing, which is the
    # property that was missing.
    assert command[command.index("--limit") + 1] == arguments["limit"]
    assert arguments["playbook"] == request.playbook
    assert arguments["cmdline"] is None


def test_a_run_with_no_narrowing_passes_no_limit_at_all(tmp_path: Path) -> None:
    # `None` rather than an empty string: ansible-runner appends `--limit` for
    # any value that is not None, and `--limit ''` selects no host at all.
    request = RunRequest(
        run_id="r1",
        playbook="seapath.ansible.cluster_setup_ha",
        inventory_file=tmp_path / "inventory.yaml",
        private_data_dir=tmp_path / "run",
        collections_path=tmp_path / "collections",
        private_key_file=tmp_path / "key",
        known_hosts_file=tmp_path / "known_hosts",
    )

    arguments = runner_arguments(request, prepare(request))

    assert arguments["limit"] is None
    assert "--limit" not in build_command(request)


def test_the_generated_config_is_readable_by_a_config_parser(
    tmp_path: Path,
) -> None:
    # Ansible reads this with configparser and hands ssh_args to ssh. A
    # continuation line would put a newline inside the value, which is the kind
    # of mistake that only shows up against a real machine.
    preparation = prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.seapath_setup_main",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
        )
    )

    parser = configparser.ConfigParser()
    parser.read(preparation.config_file)

    ssh_args = parser["ssh_connection"]["ssh_args"]
    assert "\n" not in ssh_args
    assert ssh_args.startswith(f"-o UserKnownHostsFile={tmp_path / 'known_hosts'}")
    assert parser["defaults"]["gathering"] == "explicit"
    assert parser["tags"]["skip"] == "package-install"


def test_a_run_with_no_scope_narrows_nothing(tmp_path: Path) -> None:
    preparation = prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.cluster_setup_ha",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
        )
    )

    # The playbook's own `hosts:` line, untouched. A limit appears only when
    # the guests are subtracted or an operator narrowed the run, and
    # `app.runs.scope` is the only thing that decides either.
    assert "--limit" not in preparation.command
    assert preparation.command[-1] == "seapath.ansible.cluster_setup_ha"


def test_the_scope_reaches_the_command_line(tmp_path: Path) -> None:
    preparation = prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.seapath_setup_main",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
            limit="all:!VMs",
        )
    )

    command = preparation.command
    assert command[command.index("--limit") + 1] == "all:!VMs"
    # Before the playbook, which is where ansible-playbook takes its options.
    assert command[-1] == "seapath.ansible.seapath_setup_main"


def test_check_mode_and_declared_variables_reach_the_command(tmp_path: Path) -> None:
    preparation = prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.seapath_setup_main",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
            extra_vars={"skip_reboot_setup": True},
            check=True,
        )
    )

    assert "--check" in preparation.command
    assert "skip_reboot_setup=true" in preparation.command


def test_the_private_key_is_a_fact_about_the_control_machine(tmp_path: Path) -> None:
    preparation = prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.seapath_setup_main",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
        )
    )

    # Not in the inventory, which is why the exported inventory works unchanged
    # on a conventional control machine that has its own key.
    assert preparation.environment["ANSIBLE_PRIVATE_KEY_FILE"] == str(tmp_path / "key")


def test_the_keys_reach_the_ssh_commands_a_role_spawns_itself(
    tmp_path: Path,
) -> None:
    # `ansible.posix.synchronize` builds its own ssh command line for rsync and
    # forwards only `private_key_file` unless the task sets `use_ssh_args`. A
    # task written without it offers the wrong identity to a machine driven
    # with the site key, ssh asks for a password, and the run hangs on a prompt
    # nobody can see. `deploy_seapath_alloc` was that task.
    config_file = tmp_path / "root/.ssh/config"
    prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.seapath_setup_deploy_seapath_alloc",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
            ssh_config_file=config_file,
            extra_key_files=(tmp_path / "id_site",),
        )
    )

    content = config_file.read_text()
    assert f"IdentityFile {tmp_path / 'key'}" in content
    assert f"IdentityFile {tmp_path / 'id_site'}" in content
    # The other half: a run that cannot authenticate says so rather than
    # waiting forever on a prompt, holding the run lock.
    assert "BatchMode yes" in content
    assert config_file.stat().st_mode & 0o777 == 0o600


def test_an_ssh_configuration_this_service_did_not_write_is_left_alone(
    tmp_path: Path,
) -> None:
    # On a node running from a source checkout this is root's own file.
    config_file = tmp_path / "root/.ssh/config"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("Host bastion\n    User someone\n")

    prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.seapath_setup_main",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
            ssh_config_file=config_file,
        )
    )

    assert config_file.read_text() == "Host bastion\n    User someone\n"


def test_the_ansible_that_runs_is_the_one_this_image_pins(tmp_path: Path) -> None:
    preparation = prepare(
        RunRequest(
            run_id="r1",
            playbook="seapath.ansible.seapath_setup_main",
            inventory_file=tmp_path / "inventory.yaml",
            private_data_dir=tmp_path / "run",
            collections_path=tmp_path / "collections",
            private_key_file=tmp_path / "key",
            known_hosts_file=tmp_path / "known_hosts",
        )
    )

    # ansible-runner finds ansible-playbook on PATH. An inherited PATH would
    # mean a distribution Ansible where there is one, and rc 127 where there is
    # none, so the interpreter's own directory comes first.
    interpreter_bin = str(Path(sys.executable).parent)
    assert preparation.environment["PATH"].split(os.pathsep)[0] == interpreter_bin


# The event mapping


def test_events_become_progress() -> None:
    run_progress = RunProgress()
    for event in fake.successful_run():
        apply_event(run_progress, event)

    assert run_progress.play == "Import seapath_setup_network playbook"
    assert run_progress.tasks_started == 2
    assert run_progress.final_status_seen is True
    host = run_progress.hosts["seapath-machine"]
    assert (host.ok, host.changed, host.failed) == (2, 1, 0)


def test_an_ignored_failure_is_not_counted_as_one() -> None:
    # Ansible counts a failure under `ignore_errors` in `ok` and in `ignored`,
    # and the recap says `failed=0`. The host table used to say `failed=1`
    # beside it, because the running tally counted the event as a failure and
    # the recap, carrying no `failures` entry for the host, never corrected it.
    run_progress = RunProgress()
    for event in fake.ignored_run():
        apply_event(run_progress, event)

    host = run_progress.hosts["seapath-machine"]
    assert (host.ok, host.failed, host.ignored) == (1, 0, 1)

    summaries = [summarise(event) for event in fake.ignored_run()]
    result = next(s for s in summaries if s and s["kind"] == "result")
    assert result["outcome"] == "ignored"
    # Still worth saying why, which is the whole reason to read the line.
    assert result["message"] == "eno1 does not exist"


def test_the_recap_overrules_the_running_tally() -> None:
    # Whatever the stream counted, the recap is what the run view prints, and
    # the host table has to agree with it counter by counter.
    run_progress = RunProgress()
    apply_event(run_progress, fake.failed("node1", "Apply the network", "no eno1"))
    assert run_progress.hosts["node1"].failed == 1

    apply_event(run_progress, fake.stats("node1", ok_count=286, changed=27))

    host = run_progress.hosts["node1"]
    assert (host.ok, host.changed, host.failed) == (286, 27, 0)


def test_a_failure_keeps_the_reason() -> None:
    summaries = [summarise(event) for event in fake.failed_run()]
    failure = next(s for s in summaries if s and s.get("outcome") == "failed")

    assert failure["message"] == "eno1 does not exist"
    assert failure["host"] == "seapath-machine"


def test_a_host_that_was_never_reached_keeps_what_ssh_answered() -> None:
    # The reason was in the event and reached neither the stream nor the page,
    # so an unreachable row was a red word with nothing beside it and the only
    # way to the cause was downloading the log.
    summaries = [summarise(event) for event in fake.unreachable_run()]
    result = next(s for s in summaries if s and s.get("outcome") == "unreachable")

    assert result["message"] == "Failed to connect to the host via ssh"


def test_the_stream_is_a_reduction_not_a_passthrough() -> None:
    # The raw stream carries the full result of every task on every host, which
    # is megabytes nobody reads and a place for a secret to leak into a browser.
    summary = summarise(
        {
            "event": "runner_on_ok",
            "event_data": {
                "host": "node1",
                "task": "Install the corosync authkey",
                "res": {"changed": True, "content": "a secret nobody should see"},
            },
        }
    )

    assert summary == {
        "kind": "result",
        "host": "node1",
        "task": "Install the corosync authkey",
        "outcome": "changed",
        "message": None,
        # What the host result already carries and the view now shows. The
        # payload it comes wrapped in stays out.
        "seconds": None,
        "output": None,
    }


def test_an_interrupted_stream_never_reports_a_final_status() -> None:
    run_progress = RunProgress()
    for event in fake.interrupted_run():
        apply_event(run_progress, event)

    assert run_progress.final_status_seen is False


# Running


def test_a_successful_run_is_recorded_with_its_reproducibility_pair(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)

    record = wait_for(service, service.launch("seapath_setup_main", "alice").id)

    assert record.state is RunState.SUCCESS
    assert record.launched_by == "alice"
    # "Which version of the desired state is this machine running, and which
    # version of the code read it" has an answer.
    assert record.inventory_commit == inventory.state().commit
    # The version galaxy.yml declares, and a fingerprint of the collection's
    # own FILES.json. Every branch declares 2.0.0, so the version alone answers
    # nothing for a site running one.
    assert record.collection_version.startswith("2.0.0+")
    assert len(record.collection_version) == len("2.0.0+") + 12


def test_the_inventory_used_is_frozen_with_the_run(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)
    record = wait_for(service, service.launch("seapath_setup_main", "alice").id)

    # The repository can move on without making the trace a lie.
    assert "seapath-machine" in store.inventory_of(record.id)


def test_a_failing_host_fails_the_run(store, inventory, trust, tmp_path) -> None:
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(events=fake.failed_run(), return_code=2),
        tmp_path,
    )

    record = wait_for(service, service.launch("seapath_setup_main", "alice").id)

    assert record.state is RunState.FAILED
    assert "any_errors_fatal" in record.message


def test_a_host_nothing_could_reach_is_not_reported_as_a_task_failure(
    store, inventory, trust, tmp_path
) -> None:
    # Unreachable is the connection failing, and it has a different answer from
    # a role that refused: saying "a host failed and any_errors_fatal stopped
    # everything" over it sends an operator reading a playbook for a fault that
    # is in the SSH path.
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(events=fake.unreachable_run(), return_code=4),
        tmp_path,
    )

    record = wait_for(service, service.launch("seapath_setup_main", "alice").id)

    assert record.state is RunState.FAILED
    assert "seapath-machine could not be reached" in record.message
    assert "nothing was changed" in record.message
    assert "any_errors_fatal" not in record.message
    # And what SSH answered, so the cause is on the page rather than in a file
    # the operator has to download to read one line.
    assert "Failed to connect to the host via ssh" in record.message


def test_a_guest_nothing_could_reach_is_told_what_a_guest_needs(
    store, inventory, trust, tmp_path
) -> None:
    # The run that reaches into a guest is the latency measurement, and a guest
    # is reached over SSH like any other host while this service installs
    # nothing inside one. The operator measuring a guest for the first time is
    # the one who has never had to think about that.
    inventory.declare_guest("rtvm", {"ansible_host": "192.168.200.140"}, author="alice")
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(events=fake.unreachable_run("rtvm"), return_code=4),
        tmp_path,
    )

    record = wait_for(
        service,
        service.launch(
            "test_run_cyclictest_vms",
            "alice",
            variables={
                "cyclictest_duration": 20,
                "cyclictest_priority": 90,
                "cyclictest_affinity": "smp",
            },
            scope=RunScope(hosts=["rtvm"]),
        ).id,
    )

    assert record.state is RunState.FAILED
    assert "rtvm could not be reached" in record.message
    assert "ansible_host" in record.message
    assert "sudo" in record.message


def test_a_run_without_a_final_status_is_interrupted_not_failed(
    store, inventory, trust, tmp_path
) -> None:
    # The machine rebooted under the playbook, which is what
    # seapath_setup_hardening.yaml does on every host by design.
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(events=fake.interrupted_run(), return_code=4),
        tmp_path,
    )

    record = wait_for(service, service.launch("seapath_setup_main", "alice").id)

    assert record.state is RunState.INTERRUPTED
    assert "Relaunching" in record.message
    assert "seapath-machine" in record.message


def test_the_command_is_recorded_before_the_run_ends(
    store, inventory, trust, tmp_path
) -> None:
    """The invocation is readable while the run is going.

    An operator watching a converge asks what was launched then, not once it
    is over, and a run whose machine reboots under it never gets an "over".
    """
    started = threading.Event()
    release = threading.Event()

    class BlockingAdapter(fake.FakeRunAdapter):
        def execute(self, request, on_event, on_output, should_cancel):
            started.set()
            release.wait(5.0)
            return super().execute(request, on_event, on_output, should_cancel)

    service = build(store, inventory, trust, BlockingAdapter(), tmp_path)
    record = service.launch("seapath_setup_main", "alice")
    started.wait(5.0)
    try:
        going = store.load(record.id)
        assert going.state is RunState.RUNNING
        assert going.command[0] == "ansible-playbook"
        assert going.command[-1] == "seapath.ansible.seapath_setup_main"
    finally:
        release.set()
    assert wait_for(service, record.id).command == going.command


def test_an_interrupted_run_still_says_what_was_launched(
    store, inventory, trust, tmp_path
) -> None:
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(events=fake.interrupted_run(), return_code=4),
        tmp_path,
    )

    record = wait_for(service, service.launch("seapath_setup_main", "alice").id)

    assert record.state is RunState.INTERRUPTED
    assert record.command[-1] == "seapath.ansible.seapath_setup_main"


def test_a_run_that_never_started_a_task_failed_rather_than_was_interrupted(
    store, inventory, trust, tmp_path
) -> None:
    # Found on a real node. Ansible refused the playbook before reaching any
    # machine, over a collection that was installed without its dependencies,
    # and the run was reported as interrupted with "relaunching is safe". It is
    # safe and it fails again in half a second, so the operator relaunches a
    # second time before reading the log.
    refusal = (
        "\x1b[0;31mERROR! couldn't resolve module/action "
        "'community.general.modprobe'. This often indicates a misspelling, "
        "missing collection, or incorrect module path.\x1b[0m\n"
        "\nThe error appears to be in '/opt/ansible/collections/"
        "ansible_collections/seapath/ansible/roles/network_configovs/tasks/"
        "main.yml': line 19, column 7\n"
    )
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(events=[], return_code=4, output=refusal),
        tmp_path,
    )

    record = wait_for(service, service.launch("seapath_setup_main", "alice").id)

    assert record.state is RunState.FAILED
    assert "before it reached any machine" in record.message
    # Ansible's own sentence, colours stripped, rather than anything this
    # service could infer about what went wrong.
    assert "community.general.modprobe" in record.message
    assert "\x1b[" not in record.message
    assert "Relaunching" not in record.message


def test_the_lock_serialises_two_operators(store, inventory, trust, tmp_path) -> None:
    store.acquire("an-earlier-run")
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)

    with pytest.raises(ApiError) as failure:
        service.launch("seapath_setup_main", "bob")

    assert failure.value.code == "run_in_progress"
    assert "an-earlier-run" in failure.value.message


def test_the_lock_is_released_when_a_run_ends(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)
    wait_for(service, service.launch("seapath_setup_main", "alice").id)

    # A lock nobody releases is a node that can never converge again.
    store.acquire("a-later-run")


def test_a_restart_closes_out_a_run_that_was_going(store) -> None:
    store.create(
        RunRecord(
            id="20260811T090000",
            playbook="seapath.ansible.seapath_setup_main",
            playbook_id="seapath_setup_main",
            state=RunState.RUNNING,
            launched_by="alice",
        )
    )
    store.acquire("20260811T090000")

    recovered = store.reconcile()

    assert [record.state for record in recovered] == [RunState.INTERRUPTED]
    assert "restarted" in recovered[0].message
    # And the lock is freed, or the node could never converge again.
    store.acquire("a-later-run")


def test_a_second_acquire_is_refused(store) -> None:
    store.acquire("one")

    with pytest.raises(RunLocked, match="one"):
        store.acquire("two")


# Preconditions and variables


def test_a_playbook_outside_the_catalogue_is_refused(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)

    with pytest.raises(ApiError) as failure:
        service.launch("rm_minus_rf", "alice")

    assert failure.value.status_code == 404


def test_a_cluster_playbook_is_refused_on_a_standalone_machine(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)

    with pytest.raises(ApiError) as failure:
        service.launch("cluster_setup_ha", "alice")

    # Named, never a bare 400: the operator has to know which condition to
    # satisfy.
    assert failure.value.status_code == 409
    assert "declares no cluster machine" in failure.value.message


def test_a_run_without_self_trust_is_refused(store, inventory, tmp_path) -> None:
    ssh_home = tmp_path / "empty/.ssh"
    ssh_home.mkdir(parents=True)
    (ssh_home / "authorized_keys").write_text("")
    trust = TrustService(
        ssh_dir=tmp_path / "unused/ssh",
        authorized_keys_file=ssh_home / "authorized_keys",
    )
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)

    with pytest.raises(ApiError) as failure:
        service.launch("seapath_setup_main", "alice")

    assert "no SSH trust with itself" in failure.value.message


def test_an_undeclared_variable_is_refused(store, inventory, trust, tmp_path) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)

    # A free form extra vars field is a tag selector wearing a different hat.
    with pytest.raises(ApiError) as failure:
        service.launch(
            "seapath_setup_main", "alice", variables={"ansible_user": "root"}
        )

    assert failure.value.code == "unknown_variable"


def test_a_required_variable_must_be_supplied(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)
    # Bypass the cluster precondition to reach the variable check.
    entry = catalogue.get("cluster_remove_machine")

    with pytest.raises(ApiError) as failure:
        service._accepted_variables(entry, {}, inventory.state())

    assert failure.value.code == "missing_variable"


def test_the_machine_to_remove_must_be_one_the_inventory_declares(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)
    entry = catalogue.get("cluster_remove_machine")

    # The playbook reads `hostvars[machine_to_remove]`, so a name the file does
    # not carry fails halfway through an eviction rather than before it.
    with pytest.raises(ApiError) as failure:
        service._accepted_variables(
            entry, {"machine_to_remove": "node-nine"}, inventory.state()
        )

    assert failure.value.code == "invalid_variable"
    assert "seapath-machine" in failure.value.message


def test_a_machine_cannot_evict_itself_from_the_cluster(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)
    entry = catalogue.get("cluster_remove_machine")

    # The eviction is sent to a surviving member, and this node is the one
    # driving the run.
    with pytest.raises(ApiError) as failure:
        service._accepted_variables(
            entry, {"machine_to_remove": "seapath-machine"}, inventory.state()
        )

    assert failure.value.code == "invalid_variable"
    assert "this machine" in failure.value.message


def test_every_preview_quality_matches_what_the_playbook_can_report(
    store, inventory, trust, tmp_path
) -> None:
    # The three values are read off the modules the roles use, so they are
    # checkable. `cluster_setup_libvirt` reads the `.stdout` of a shell check
    # mode skips, which is a crash rather than a partial answer.
    assert catalogue.get("seapath_setup_libvirt").preview.value == "full"
    assert catalogue.get("seapath_setup_network").preview.value == "partial"
    assert catalogue.get("cluster_setup_libvirt").preview.value == "none"
    assert catalogue.get("cluster_setup_users").preview.value == "none"


def test_a_playbook_that_cannot_be_previewed_offers_no_preview(
    store, inventory, trust, tmp_path
) -> None:
    assert catalogue.get("cluster_setup_ha").previewable is False
    assert catalogue.get("seapath_setup_timemaster").previewable is True


def test_the_catalogue_reports_why_an_entry_is_not_offered(
    store, inventory, trust, tmp_path
) -> None:
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)
    by_id = {item.entry.id: item for item in service.playbooks()}

    assert by_id["seapath_setup_main"].available is True
    assert by_id["cluster_setup_ha"].available is False
    assert by_id["cluster_setup_ha"].unmet


def test_an_entry_the_shipped_collection_lacks_is_explained_not_offered(
    store, inventory, trust, tmp_path
) -> None:
    # The catalogue and seapath-ansible are released separately, so a SEAPATH
    # release can add or rename a playbook under this service. An entry the
    # image does not carry must not be a button that fails at the first task.
    service = RunService(
        store=store,
        adapter=fake.FakeRunAdapter(),
        inventory=inventory,
        trust=trust,
        paths=RunPaths(
            collections_root=write_fake_collection(
                tmp_path / "partial", entries=["seapath_setup_network"]
            ),
            private_key_file=tmp_path / "state/ssh/id_ed25519_self",
            known_hosts_file=tmp_path / "state/ssh/known_hosts",
            ssh_config_file=tmp_path / "root/.ssh/config",
        ),
        hostname="seapath-machine",
        collection_version="1.9.0",
    )
    by_id = {item.entry.id: item for item in service.playbooks()}

    assert by_id["seapath_setup_network"].available is True
    assert by_id["seapath_setup_main"].available is False
    assert "not in the SEAPATH collection this image ships" in (
        by_id["seapath_setup_main"].unmet[0]
    )
    assert "1.9.0" in by_id["seapath_setup_main"].unmet[0]
    # The code behind the sentence, so a page can group thirteen entries that
    # are unavailable for one reason into one line. Comparing the sentences
    # cannot do it: each names its own playbook.
    assert by_id["seapath_setup_main"].unmet_codes == ["playbook_present"]
    assert by_id["seapath_setup_network"].unmet_codes == []

    with pytest.raises(ApiError) as failure:
        service.launch("seapath_setup_main", "alice")

    assert failure.value.status_code == 409


def test_a_guest_with_no_cloud_init_asks_nothing_of_the_seed_builder(
    store, inventory, trust, tmp_path
) -> None:
    # The precondition is about the guests that ask for a seed. An inventory
    # whose guests carry no `cloud_init` deploys from a node with no
    # `cloud-localds` on it, because nothing would call one.
    inventory.declare_guest("plainvm", {"vm_disk": "files/vm.qcow2"}, author="alice")
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(),
        tmp_path,
        seed_builder=lambda: None,
    )
    by_id = {item.entry.id: item for item in service.playbooks()}

    assert by_id["deploy_vms_standalone"].available is True
    assert "seed_buildable" not in by_id["deploy_vms_standalone"].unmet_codes


def test_a_seeded_guest_is_refused_while_the_tool_that_seeds_it_is_absent(
    store, inventory, trust, tmp_path
) -> None:
    # `cloud_init_seed` builds the seed image on the control machine, which for
    # a run launched here is this container. Without the tool the run dies on
    # that task, after the operator has confirmed a deployment: the package is
    # named here instead.
    inventory.declare_guest(
        "seededvm",
        {"vm_disk": "files/vm.qcow2", "cloud_init": {"hostname": "seededvm"}},
        author="alice",
    )
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(),
        tmp_path,
        seed_builder=lambda: None,
    )
    by_id = {item.entry.id: item for item in service.playbooks()}

    for entry in ("deploy_vms_standalone", "deploy_vms_cluster"):
        assert "seed_buildable" in by_id[entry].unmet_codes
    refusal = by_id["deploy_vms_standalone"].unmet[0]
    assert "seededvm" in refusal
    assert "cloud-localds" in refusal
    assert "cloud-image-utils" in refusal
    assert by_id["deploy_vms_standalone"].available is False

    # And the launch is refused, rather than only the card being dimmed: the
    # VMs page deploys by launching this entry, and a page that lost the
    # listing would otherwise start the run anyway.
    with pytest.raises(ApiError) as failure:
        service.launch("deploy_vms_standalone", "alice")

    assert failure.value.status_code == 409
    assert "cloud-image-utils" in failure.value.message
    assert failure.value.detail["codes"] == ["seed_buildable"]


def test_a_seeded_guest_is_deployable_once_the_node_can_build_the_seed(
    store, inventory, trust, tmp_path
) -> None:
    inventory.declare_guest(
        "seededvm",
        {"vm_disk": "files/vm.qcow2", "cloud_init": {"hostname": "seededvm"}},
        author="alice",
    )
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)
    by_id = {item.entry.id: item for item in service.playbooks()}

    assert by_id["deploy_vms_standalone"].available is True
    assert by_id["deploy_vms_standalone"].unmet_codes == []


def test_a_collection_without_the_seed_role_refuses_a_seeded_guest(
    store, inventory, trust, tmp_path
) -> None:
    # The other half of the same precondition, and the worse one. A collection
    # older than the cloud-init support reads no `cloud_init` mapping at all:
    # the run ends green, the guest is created with no seed, and it comes up
    # with the address its image was built with.
    inventory.declare_guest(
        "seededvm",
        {"vm_disk": "files/vm.qcow2", "cloud_init": {"hostname": "seededvm"}},
        author="alice",
    )
    service = build(
        store,
        inventory,
        trust,
        fake.FakeRunAdapter(),
        tmp_path,
        collections=write_fake_collection(tmp_path / "older", roles=[]),
    )
    by_id = {item.entry.id: item for item in service.playbooks()}

    assert by_id["deploy_vms_standalone"].available is False
    refusal = by_id["deploy_vms_standalone"].unmet[0]
    assert "cloud_init_seed" in refusal
    assert "report success" in refusal
    # Every other entry is still offered: one guest asking for a seed says
    # nothing about converging a machine.
    assert by_id["seapath_setup_main"].available is True


def test_a_cloud_init_that_is_not_a_mapping_refuses_the_deployment(
    store, inventory, trust, tmp_path
) -> None:
    # The role reads `hostname` and the rest off the mapping, so a value that
    # is not one fails the task rather than being skipped. Refused with the
    # tool and the role both installed, because neither of them is what is
    # wrong here.
    inventory.declare_guest("seededvm", {"cloud_init": True}, author="alice")
    service = build(store, inventory, trust, fake.FakeRunAdapter(), tmp_path)
    by_id = {item.entry.id: item for item in service.playbooks()}

    assert by_id["deploy_vms_standalone"].available is False
    refusal = by_id["deploy_vms_standalone"].unmet[0]
    assert "seededvm" in refusal
    assert "not a mapping" in refusal


def test_the_time_each_task_took_is_kept(store, inventory, trust, tmp_path) -> None:
    # ansible-runner reports a duration on every host result, so answering
    # "which step took the four minutes" costs nothing and needs no callback
    # plugin. The longest host rather than the sum: hosts run in parallel, and
    # the sum would describe a run nobody waited through.
    run_progress = RunProgress()
    for host, seconds in (("node1", 3.0), ("node2", 41.5), ("node3", 2.0)):
        apply_event(
            run_progress,
            {
                "event": "runner_on_ok",
                "event_data": {
                    "host": host,
                    "task": "Install the packages",
                    "duration": seconds,
                    "res": {},
                },
            },
        )

    assert run_progress.durations == {"Install the packages": 41.5}


def test_a_task_is_named_the_way_ansible_names_it() -> None:
    # Twelve tasks called "Detect Debian distribution" say very little without
    # the role they came from, and the role is in the event already.
    summary = summarise(
        {
            "event": "playbook_on_task_start",
            "event_data": {
                "task": "Copy libvirtd.conf",
                "role": "configure_libvirt",
                "play": "Configure libvirt",
            },
        }
    )

    assert summary["task"] == "configure_libvirt : Copy libvirtd.conf"


def test_a_debug_task_shows_what_it_printed() -> None:
    # The only reason a debug task exists. Everything else in the payload stays
    # out of the browser.
    summary = summarise(
        {
            "event": "runner_on_ok",
            "event_data": {
                "host": "node1",
                "task": "Show seapath_distro",
                # Ansible resolves the module name, so this is what actually
                # arrives. Comparing against the short name matched nothing.
                "task_action": "ansible.builtin.debug",
                "res": {
                    "seapath_distro": "Debian",
                    "changed": False,
                    "_ansible_verbose_always": True,
                    "_ansible_no_log": False,
                },
            },
        }
    )

    assert summary["output"] == '{"seapath_distro": "Debian"}'


def test_a_debug_task_marked_no_log_prints_nothing() -> None:
    # Honoured here as Ansible honours it everywhere else: the task shows that
    # it ran and nothing more.
    summary = summarise(
        {
            "event": "runner_on_ok",
            "event_data": {
                "host": "node1",
                "task": "Show the join token",
                "task_action": "ansible.builtin.debug",
                "res": {"msg": "a secret", "_ansible_no_log": True},
            },
        }
    )

    assert summary["output"] is None


def test_a_result_from_any_other_module_carries_no_payload() -> None:
    summary = summarise(
        {
            "event": "runner_on_ok",
            "event_data": {
                "host": "node1",
                "task": "Copy libvirtd.conf",
                "task_action": "ansible.builtin.copy",
                "res": {"content": "a secret nobody should see", "changed": True},
            },
        }
    )

    assert summary["output"] is None


def test_two_branches_of_the_collection_are_told_apart(tmp_path: Path) -> None:
    # The reason this exists. A site pinned to a branch rather than a release
    # installs a collection whose galaxy.yml declares the same version as every
    # other branch, so "2.0.0" answers nothing about which code converged a
    # machine.
    one = write_fake_collection(tmp_path / "one", contents="---\n# a branch\n")
    other = write_fake_collection(tmp_path / "other", contents="---\n# another\n")

    assert catalogue.identity(one) != catalogue.identity(other)
    assert catalogue.identity(one).startswith("2.0.0+")
    # Same content, same answer, so reinstalling the same code reads the same.
    again = write_fake_collection(tmp_path / "again", contents="---\n# a branch\n")
    assert catalogue.identity(again) == catalogue.identity(one)


def test_a_collection_that_is_not_there_has_no_identity(tmp_path: Path) -> None:
    assert catalogue.identity(tmp_path / "nowhere") is None


def test_the_history_reads_only_the_runs_it_returns(store: RunStore) -> None:
    """`limit` bounds the work, rather than only the answer.

    The ids are timestamps and the directory names are the ids, so the newest
    are known before anything is opened. Reading and parsing every run on the
    node to hand back the first fifty is megabytes of JSON on a machine with a
    year of commissioning behind it, and the Real time page asked for it three
    times over. See D47.
    """
    for hour in range(40):
        store.create(
            RunRecord(
                id=f"20260101T{hour:02d}0000.000000",
                playbook="seapath_setup_main.yaml",
                playbook_id="seapath_setup_main",
                launched_by="alice",
            )
        )

    opened = []
    original = store.load
    store.load = lambda run_id: (opened.append(run_id), original(run_id))[1]  # type: ignore[method-assign]

    newest = store.list(limit=5)

    assert [record.id for record in newest] == [
        f"20260101T{hour:02d}0000.000000" for hour in (39, 38, 37, 36, 35)
    ]
    assert len(opened) == 5


def test_a_run_whose_record_cannot_be_read_costs_nobody_a_row(
    store: RunStore,
) -> None:
    """A directory left behind by an interrupted write is skipped, not counted.

    Asking for ten runs and being handed nine because one of them is a
    half-written file is a history with a hole in it.
    """
    for hour in range(6):
        store.create(
            RunRecord(
                id=f"20260101T{hour:02d}0000.000000",
                playbook="seapath_setup_main.yaml",
                playbook_id="seapath_setup_main",
                launched_by="alice",
            )
        )
    (store.directory("20260101T050000.000000") / "run.json").write_text("{oops")

    assert len(store.list(limit=5)) == 5
