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
import time
from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.hosts.fake import FakeHostReader
from app.inventory.repository import InventoryRepository
from app.inventory.service import InventoryService
from app.runs import catalogue, fake
from app.runs.adapter import RunRequest, prepare
from app.runs.models import RunProgress, RunState
from app.runs.progress import apply_event, summarise
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
) -> RunService:
    return RunService(
        store=store,
        adapter=adapter,
        inventory=inventory,
        trust=trust,
        paths=RunPaths(
            collections_path=write_fake_collection(tmp_path / "collections"),
            private_key_file=tmp_path / "state/ssh/id_ed25519_self",
            known_hosts_file=tmp_path / "state/ssh/known_hosts",
            ssh_config_file=tmp_path / "root/.ssh/config",
        ),
        hostname="seapath-machine",
        collection_version="2.0.0",
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


def test_the_command_never_narrows_the_hosts(tmp_path: Path) -> None:
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

    # Which hosts a playbook plays against is a property of the playbook.
    # cluster_setup_ha on one member of three is not a smaller cluster.
    assert "--limit" not in preparation.command
    assert preparation.command[-1] == "seapath.ansible.cluster_setup_ha"


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
    # forwards only `private_key_file`, so a machine this node drives with the
    # site key is offered the wrong identity, ssh asks for a password, and the
    # run hangs on a prompt nobody can see. Four roles push files that way,
    # `configure_physical_machine` among them.
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


def test_a_failure_keeps_the_reason() -> None:
    summaries = [summarise(event) for event in fake.failed_run()]
    failure = next(s for s in summaries if s and s.get("outcome") == "failed")

    assert failure["message"] == "eno1 does not exist"
    assert failure["host"] == "seapath-machine"


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
    from app.runs.models import RunRecord

    store.create(
        RunRecord(
            id="20260811T090000",
            playbook="seapath.ansible.seapath_setup_main",
            playbook_id="seapath_setup_main",
            state=RunState.RUNNING,
            launched_by="alice",
        ),
        inventory="all: {}\n",
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
    assert "not part of a cluster" in failure.value.message


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
            collections_path=write_fake_collection(
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
