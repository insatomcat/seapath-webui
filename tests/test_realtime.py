# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Real time conformance, one accepting and one refusing case per rule.

The distinction the tests are really holding is the one between the two kinds
of check. A conformance check compares the machine with the inventory and its
answer changes when either side changes. An advice check reports what the
machine came out with and never claims the inventory asked for it, because
nothing in a SEAPATH inventory has an opinion about SMT.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.hosts.fake import FakeHostReader
from app.hosts.models import CpuReading, HugepagePool, RealtimeReading
from app.inventory.model import Inventory, Mode, NodeConfig
from app.inventory.service import InventoryState
from app.services.realtime import Kind, RealtimeService, Status


class _Inventory:
    """Just enough of the inventory service: the state the checks read."""

    def __init__(self, isolcpus: str | None, this_host: str | None = "node1") -> None:
        self._state = InventoryState(
            inventory=(
                Inventory(
                    mode=Mode.STANDALONE,
                    hosts={
                        "node1": NodeConfig(
                            ansible_host="192.168.200.121",
                            network_interface="eno1",
                            isolcpus=isolcpus,
                        )
                    },
                )
                if this_host
                else None
            ),
            commit="abcdef1234",
            this_host=this_host,
        )

    def state(self) -> InventoryState:
        return self._state


class _Runs:
    def list(self, limit: int = 50) -> list:
        return []

    def results(self, run_id: str) -> list:
        return []


class _Reader(FakeHostReader):
    """The fake machine, with one reading overridden per test."""

    def __init__(self, realtime: dict[str, Any] | None = None, **cpu: Any) -> None:
        super().__init__()
        self._realtime = realtime or {}
        self._cpu = cpu

    def realtime(self) -> RealtimeReading:
        return super().realtime().model_copy(update=self._realtime)

    def cpu(self) -> CpuReading:
        return super().cpu().model_copy(update=self._cpu)


def _checks(reader: _Reader, inventory: _Inventory) -> dict[str, Any]:
    report = RealtimeService(reader, inventory, _Runs(), "node1").conformance()
    return {check.id: check for check in report.checks}


# Isolation, the one check the whole page is built around


def test_the_isolated_set_matching_the_inventory_is_a_conformance_pass() -> None:
    checks = _checks(_Reader(), _Inventory("4-7"))

    assert checks["cpu_isolation"].kind is Kind.CONFORMANCE
    assert checks["cpu_isolation"].status is Status.OK
    assert checks["cpu_isolation"].observed == "4-7"
    assert checks["cpu_isolation"].declared == "4-7"


def test_an_isolated_set_that_lags_the_inventory_is_reported_with_the_reason() -> None:
    # The case this check exists for: the inventory was edited and converged,
    # and nobody rebooted. isolcpus is read at boot, so the machine reads
    # exactly like one where the change never happened.
    checks = _checks(_Reader(isolated=[4, 5]), _Inventory("4-7"))

    assert checks["cpu_isolation"].status is Status.WARNING
    assert checks["cpu_isolation"].observed == "4-5"
    assert "reboot" in checks["cpu_isolation"].detail


def test_isolation_is_advice_when_the_inventory_declares_none() -> None:
    # A freshly installed machine has no inventory entry, and the page has to
    # be useful on it: reading the tuning is what an operator does before
    # writing the isolation down.
    checks = _checks(_Reader(), _Inventory(None))

    assert checks["cpu_isolation"].kind is Kind.ADVICE
    assert checks["cpu_isolation"].declared is None


# tuned


def test_the_seapath_profile_is_a_pass_when_the_inventory_asks_for_isolation() -> None:
    checks = _checks(_Reader(), _Inventory("4-7"))

    assert checks["tuned"].kind is Kind.CONFORMANCE
    assert checks["tuned"].status is Status.OK


def test_a_missing_profile_is_a_finding_only_where_isolation_was_declared() -> None:
    absent = {"tuned_profile": None, "tuned_profile_installed": None}

    declared = _checks(_Reader(absent), _Inventory("4-7"))["tuned"]
    undeclared = _checks(_Reader(absent), _Inventory(None))["tuned"]

    assert declared.status is Status.WARNING
    # `configure_hypervisor` gates the whole tuned block on isolcpus, so a
    # machine that declares none is expected to carry no profile. Calling that
    # a failure would put a red badge on every machine nobody has configured
    # yet.
    assert undeclared.status is Status.INFO


def test_a_profile_selected_but_not_installed_is_a_machine_tuned_by_nothing() -> None:
    checks = _checks(
        _Reader({"tuned_profile": "site-rt", "tuned_profile_installed": False}),
        _Inventory("4-7"),
    )

    assert checks["tuned"].status is Status.WARNING
    assert "no profile of that name is installed" in checks["tuned"].detail


# The readings that are advice, and that must never claim to be conformance


@pytest.mark.parametrize(
    "check_id",
    ["preemption", "kernel_cmdline", "sched_rt", "hugepages", "smt", "acpi"],
)
def test_a_check_with_no_inventory_variable_behind_it_stays_advice(
    check_id: str,
) -> None:
    # A SEAPATH inventory has no opinion about SMT or about transparent
    # hugepages, and a check that presented one as a conformance failure would
    # be this service voting on a site's own decision.
    checks = _checks(_Reader(), _Inventory("4-7"))

    assert checks[check_id].kind is Kind.ADVICE
    assert checks[check_id].declared is None


def test_a_preempt_rt_kernel_passes_and_an_ordinary_one_does_not() -> None:
    ok = _checks(_Reader(), _Inventory("4-7"))["preemption"]
    not_ok = _checks(_Reader({"preemption": "PREEMPT"}), _Inventory("4-7"))[
        "preemption"
    ]

    assert ok.status is Status.OK
    assert not_ok.status is Status.WARNING
    # Nothing in the inventory picks the kernel, so the sentence has to point
    # at the image rather than at a variable to edit.
    assert "installed image" in not_ok.detail


def test_real_time_throttling_disabled_passes_and_a_budget_does_not() -> None:
    ok = _checks(_Reader(), _Inventory("4-7"))["sched_rt"]
    throttled = _checks(_Reader({"sched_rt_runtime_us": 950000}), _Inventory("4-7"))[
        "sched_rt"
    ]

    assert ok.status is Status.OK
    assert throttled.status is Status.WARNING


def test_transparent_hugepages_pass_only_when_they_are_off() -> None:
    off = _checks(_Reader({"transparent_hugepages": "never"}), _Inventory("4-7"))
    on = _checks(_Reader({"transparent_hugepages": "madvise"}), _Inventory("4-7"))

    assert off["transparent_hugepages"].status is Status.OK
    assert on["transparent_hugepages"].status is Status.WARNING


def test_smt_on_is_a_finding_only_while_something_is_isolated() -> None:
    isolating = _checks(_Reader(), _Inventory("4-7"))["smt"]
    idle = _checks(_Reader(isolated=[], topology=[]), _Inventory(None))["smt"]

    assert isolating.status is Status.WARNING
    assert "sibling" in isolating.detail
    assert "costs nothing today" in idle.detail


def test_a_numa_node_with_no_hugepages_is_named() -> None:
    # A guest pinned to that node draws from its pool. The machine total looks
    # correct and the guest fails to start.
    checks = _checks(
        _Reader(
            {
                "hugepages": [
                    HugepagePool(size_kb=1048576, total=8, free=8),
                    HugepagePool(size_kb=1048576, total=8, free=8, node=0),
                    HugepagePool(size_kb=1048576, total=0, free=0, node=1),
                ]
            }
        ),
        _Inventory("4-7"),
    )

    assert checks["hugepages"].status is Status.WARNING
    assert "node 1" in checks["hugepages"].detail


def test_an_unreadable_value_is_reported_as_unknown() -> None:
    # On a substation hypervisor "unknown" and "correct" must never look alike,
    # so a failed reading takes a status of its own.
    checks = _checks(_Reader({"preemption": None}), _Inventory("4-7"))

    assert checks["preemption"].status is Status.UNKNOWN


def test_the_isolated_range_is_written_the_way_the_inventory_writes_it() -> None:
    # `4-7` rather than `4, 5, 6, 7`, so the comparison an operator makes by
    # eye between the two columns is a comparison of like with like.
    checks = _checks(_Reader(isolated=[2, 4, 5, 6]), _Inventory("2,4-6"))

    assert checks["cpu_isolation"].observed == "2,4-6"


def test_interrupts_reaching_an_isolated_cpu_are_counted_and_the_worst_named() -> None:
    check = _checks(_Reader(), _Inventory("4-7"))["irq_affinity"]

    assert check.status is Status.WARNING
    assert "ahci0" in check.detail


def test_a_topology_with_no_cpu_still_produces_every_check() -> None:
    # The page has to render on a machine whose /sys could not be read. Half a
    # page leaves the operator guessing which half is missing.
    checks = _checks(
        _Reader({}, isolated=[], topology=[], kernel_cmdline=None), _Inventory(None)
    )

    assert len(checks) == 10


# The same ten checks, on every machine of the cluster
#
# The reversal D27 records. The checks used to be about the node the browser
# happened to be pointed at, because eight of the ten read files only that node
# exposes. `seapath-alloc` publishes those readings now, so the service asks
# each node's exporter and runs one implementation of the checks over all of
# them. What the tests below hold is where each machine's answer came from, and
# that the three ways a node can have nothing to say stay apart.


class _Cluster:
    """Three hypervisors, declaring three different isolations."""

    def __init__(self, this_host: str | None = "node1") -> None:
        self._state = InventoryState(
            inventory=Inventory(
                mode=Mode.CLUSTER,
                hosts={
                    "node1": NodeConfig(
                        ansible_host="10.0.0.1",
                        network_interface="eno1",
                        isolcpus="4-7",
                    ),
                    "node2": NodeConfig(
                        ansible_host="10.0.0.2",
                        network_interface="eno1",
                        isolcpus="2-7",
                    ),
                    "node3": NodeConfig(
                        ansible_host="10.0.0.3",
                        network_interface="eno1",
                        isolcpus="4-7",
                    ),
                },
            ),
            commit="abcdef1234",
            this_host=this_host,
        )

    def state(self) -> InventoryState:
        return self._state


def _exposition(isolated: str, tuning: str = "") -> str:
    lines = [
        f'seapath_alloc_cpu_detail{{cpu="{cpu}",'
        f'isolated="{1 if str(cpu) in isolated.split(",") else 0}",'
        f'ht_pair="{cpu}",ht_sibling="{cpu}",state="free",slot="",'
        'member_count="0",members="",label="",group="",scheduler="",'
        'priority="0"} 1'
        for cpu in range(8)
    ]
    lines.append(
        'node_uname_info{sysname="Linux",release="6.1.0-rt-amd64",'
        'version="#1 SMP PREEMPT_RT Debian"} 1'
    )
    return "\n".join(lines) + "\n" + tuning


_TUNING = (
    'seapath_rt_tuned_info{profile="seapath-rt-host",'
    'source="/etc/tuned/active_profile",installed="1"} 1\n'
    'seapath_rt_kernel_cmdline_info{cmdline="isolcpus=4-7 nohz_full=4-7 '
    'rcu_nocbs=4-7 intel_idle.max_cstate=0"} 1\n'
    "seapath_rt_sched_rt_runtime_us -1\n"
    "seapath_rt_sched_rt_period_us 1000000\n"
    'seapath_rt_hugepages_total{size_kb="1048576",node=""} 16\n'
    'seapath_rt_hugepages_free{size_kb="1048576",node=""} 16\n'
    'seapath_rt_transparent_hugepages_info{enabled="never",defrag="never"} 1\n'
    'seapath_rt_smt_info{control="off"} 1\n'
    "seapath_rt_smt_active 0\n"
    "seapath_rt_acpi_present 1\n"
    "seapath_rt_irqs_total 214\n"
    "seapath_rt_irqs_on_isolated_cpus 0\n"
)


class _Exporters:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        for address, text in self.answers.items():
            if address in url:
                return text, ""
        return None, "No route to host"


def _cluster(answers: dict[str, str], this_host: str | None = "node1"):
    from app.cluster.pool import PoolReader

    service = RealtimeService(
        _Reader(),
        _Cluster(this_host),
        _Runs(),
        "node1",
        pool=PoolReader(client=_Exporters(answers)),
    )
    pool = service.pool()
    return {node.host: node for node in pool.nodes}, pool


def test_every_node_is_checked_against_its_own_inventory_entry() -> None:
    # node2 declares 2-7 and booted 4-7, which is the machine converged and
    # never rebooted. Nothing on the local machine says so, and until its
    # tuning was published nothing here could.
    nodes, _ = _cluster(
        {
            "10.0.0.1": _exposition("4,5,6,7", _TUNING),
            "10.0.0.2": _exposition("4,5,6,7", _TUNING),
        }
    )

    node2 = {check.id: check for check in nodes["node2"].checks}

    assert node2["cpu_isolation"].status is Status.WARNING
    assert node2["cpu_isolation"].declared == "2-7"
    assert node2["cpu_isolation"].kind is Kind.CONFORMANCE


def test_a_remote_node_answers_all_ten_checks_from_its_exporter() -> None:
    # The eight that used to need a file only the local node exposes. The tuned
    # profile is the one an operator reaches for first, and it is the one an
    # exporter had nothing to say about before D27.
    nodes, _ = _cluster({"10.0.0.2": _exposition("4,5,6,7", _TUNING)})

    node2 = {check.id: check for check in nodes["node2"].checks}

    assert len(node2) == 10
    assert node2["tuned"].observed == "seapath-rt-host"
    assert node2["transparent_hugepages"].status is Status.OK
    assert node2["smt"].observed == "off"


def test_the_local_node_is_read_from_its_own_files_rather_than_its_exporter() -> None:
    # It needs no exporter, it is never stale, and it answers on a machine
    # where nothing has been deployed yet. The fake reader isolates 4-7 and
    # leaves transparent hugepages on, and that is what the local column says
    # whatever the exporter publishes.
    nodes, _ = _cluster({"10.0.0.1": _exposition("0,1", _TUNING)})

    node1 = {check.id: check for check in nodes["node1"].checks}

    assert node1["cpu_isolation"].observed == "4-7"
    assert node1["transparent_hugepages"].status is Status.WARNING


def test_a_node_that_published_no_tuning_gets_no_checks_and_says_why() -> None:
    # Ten grey rows would read as ten failures and bury the one act that fixes
    # it, which is upgrading the collector on that machine.
    nodes, _ = _cluster({"10.0.0.2": _exposition("4,5,6,7")})

    assert nodes["node2"].checks == []
    assert "deploy_seapath_alloc" in nodes["node2"].tuning_error


def test_an_unreachable_node_keeps_its_column_and_its_reason() -> None:
    nodes, _ = _cluster({"10.0.0.2": _exposition("4,5,6,7", _TUNING)})

    assert nodes["node3"].checks == []
    assert nodes["node3"].reachable is False
    assert nodes["node3"].error


def test_the_local_node_comes_first_and_the_commit_travels_with_the_report() -> None:
    # The machine the operator is standing on, and the desired state every
    # column was compared against. A conformance report with no commit behind
    # it is an anecdote.
    _, pool = _cluster({"10.0.0.2": _exposition("4,5,6,7", _TUNING)})

    assert pool.nodes[0].host == "node1"
    assert pool.inventory_commit == "abcdef1234"


def test_a_machine_with_no_inventory_still_has_a_conformance_column() -> None:
    # A freshly installed machine has no inventory at all, and reading the
    # tuning is exactly what an operator does before writing the isolation
    # down. An empty cluster view would take that away.
    service = RealtimeService(
        _Reader(), _Inventory(None, this_host=None), _Runs(), "n1"
    )
    pool = service.pool()

    assert len(pool.nodes) == 1
    assert pool.nodes[0].host == "n1"
    assert len(pool.nodes[0].checks) == 10
    assert pool.available is False
