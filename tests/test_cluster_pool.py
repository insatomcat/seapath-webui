# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading the CPU pool of every node from its exporter.

The one reading in this service that leaves the local machine, and the only way
the question can be answered at all: occupancy is the affinity of every QEMU
thread in `/proc`, which this container's PID namespace hides and which
AGENTS.md forbids opening. `seapath-alloc` computes it on the host and
publishes it, so this asks rather than duplicates.

What the tests hold is that a cluster half built still renders: a node that
cannot be reached, a node whose exporter is up but carries no seapath-alloc
metrics, and a node that answers, all in one reading.
"""

from __future__ import annotations

from app.cluster import metrics
from app.cluster.fake import FakeMetricsClient
from app.cluster.pool import PoolReader


class _Client:
    """Answers whatever the test hands it, by address."""

    def __init__(self, answers: dict[str, str | None]) -> None:
        self.answers = answers
        self.urls: list[str] = []

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        self.urls.append(url)
        for key, text in self.answers.items():
            if key in url:
                return (text, "") if text is not None else (None, "Connection refused")
        return None, "No route to host"


_CPU = (
    'seapath_alloc_cpu_detail{cpu="3",isolated="1",ht_pair="3",ht_sibling="15",'
    'state="irq_slot",slot="irq12419",member_count="1",'
    'members="eno12419/181-189/irq FIFO/50",label="irq12419",group="slot",'
    'scheduler="",priority="0"} 1'
)


# The parser


def test_a_label_value_holding_commas_and_spaces_is_read_whole() -> None:
    # `members` is a joined list, so the naive split on commas that a label
    # parser invites would cut it into pieces and lose the rest of the labels.
    sample = metrics.parse(_CPU)["seapath_alloc_cpu_detail"][0]

    assert sample.labels["members"] == "eno12419/181-189/irq FIFO/50"
    assert sample.labels["priority"] == "0"
    assert sample.value == 1.0


def test_an_escaped_quote_in_a_label_survives() -> None:
    line = 'metric{label="a \\"quoted\\" name",other="x"} 1'

    sample = metrics.parse(line)["metric"][0]

    assert sample.labels["label"] == 'a "quoted" name'
    assert sample.labels["other"] == "x"


def test_comments_and_a_series_without_labels_are_both_handled() -> None:
    parsed = metrics.parse(
        "# HELP something A help line\n"
        "# TYPE something gauge\n"
        "seapath_alloc_free_logical_cpus 17\n"
    )

    assert parsed["seapath_alloc_free_logical_cpus"][0].value == 17.0
    assert "# HELP" not in parsed


def test_one_malformed_line_does_not_cost_the_rest_of_the_exposition() -> None:
    # This reads a file another program wrote on another machine. A CPU map
    # missing one core is worth more than no CPU map.
    parsed = metrics.parse("broken{unclosed=\nseapath_alloc_free_logical_cpus 5\n")

    assert parsed["seapath_alloc_free_logical_cpus"][0].value == 5.0


# The reader


def test_every_node_is_asked_on_the_exporter_port() -> None:
    client = _Client({"10.0.0.1": _CPU})

    PoolReader(client, port=9100).read([("node1", "10.0.0.1")])

    assert client.urls == ["http://10.0.0.1:9100/metrics"]


def test_a_node_that_cannot_be_reached_reports_why_and_does_not_raise() -> None:
    # The ordinary state of a cluster being built, and the others must still
    # come back.
    nodes = PoolReader(_Client({"10.0.0.1": _CPU})).read(
        [("node1", "10.0.0.1"), ("node2", "10.0.0.2")]
    )

    assert [node.host for node in nodes] == ["node1", "node2"]
    assert nodes[0].reachable is True
    assert nodes[1].reachable is False
    assert "No route to host" in nodes[1].error
    assert nodes[1].cpus == []


def test_an_exporter_with_no_seapath_alloc_metrics_names_the_role() -> None:
    # node_exporter answering without the textfile collector is a different
    # fault from an unreachable node, and it is fixed by a different role.
    nodes = PoolReader(_Client({"10.0.0.1": "node_cpu_seconds_total 1\n"})).read(
        [("node1", "10.0.0.1")]
    )

    assert nodes[0].reachable is True
    assert nodes[0].cpus == []
    assert "deploy_seapath_alloc" in nodes[0].error


def test_a_cpu_carries_its_occupant_its_core_and_its_sibling() -> None:
    nodes = PoolReader(_Client({"10.0.0.1": _CPU})).read([("node1", "10.0.0.1")])

    slot = nodes[0].cpus[0]
    assert slot.cpu == 3
    assert slot.isolated is True
    assert slot.core == 3
    assert slot.sibling == 15
    assert slot.state == "irq_slot"
    assert slot.label == "irq12419"
    assert slot.members == "eno12419/181-189/irq FIFO/50"


def test_a_hard_fallback_is_read_because_it_is_a_conformance_failure() -> None:
    # An actor that asked for isolation and is running on housekeeping cores is
    # a machine that accepted a pinning profile and could not honour it. No
    # reading of /sys can find that.
    exposition = (
        _CPU + "\n"
        'seapath_alloc_active_fallbacks{severity="hard"} 2\n'
        'seapath_alloc_active_fallbacks{severity="soft"} 1\n'
    )

    nodes = PoolReader(_Client({"10.0.0.1": exposition})).read([("node1", "10.0.0.1")])

    assert nodes[0].hard_fallbacks == 2
    assert nodes[0].soft_fallbacks == 1


def test_the_reading_carries_its_age_because_it_is_never_quite_now() -> None:
    # The collector runs on a fifteen second timer. A page implying otherwise
    # would read as live during the minute a node stopped exporting.
    import time

    exposition = (
        _CPU + f"\nseapath_alloc_scrape_timestamp_seconds {time.time() - 30:.3f}\n"
    )

    nodes = PoolReader(_Client({"10.0.0.1": exposition})).read([("node1", "10.0.0.1")])

    assert 25 <= nodes[0].scrape_age_seconds <= 40


def test_no_target_is_no_request(monkeypatch) -> None:
    client = _Client({})

    assert PoolReader(client).read([]) == []
    assert client.urls == []


def test_the_fake_serves_a_cluster_that_is_not_uniform() -> None:
    # A fake where every machine answers the same exercises one branch. These
    # three are a quiet node, a loaded one, and one that is not there.
    nodes = PoolReader(FakeMetricsClient()).read(
        [("elabo1", "elabo1"), ("elabo2", "elabo2"), ("elabo3", "elabo3")]
    )

    states = {node.host: {slot.state for slot in node.cpus} for node in nodes}
    assert "vm" in states["elabo2"]
    assert "vm" not in states["elabo1"]
    assert nodes[2].reachable is False


# Conformance a node can be asked from a distance


def _with_isolation(isolated: str, uname: str = "") -> str:
    lines = []
    for cpu in range(8):
        lines.append(
            f'seapath_alloc_cpu_detail{{cpu="{cpu}",'
            f'isolated="{1 if str(cpu) in isolated.split(",") else 0}",'
            f'ht_pair="{cpu}",ht_sibling="{cpu}",state="free",slot="",'
            'member_count="0",members="",label="",group="",scheduler="",'
            'priority="0"} 1'
        )
    if uname:
        lines.append(
            'node_uname_info{sysname="Linux",release="6.1.0-18-rt-amd64",'
            f'version="{uname}"}} 1'
        )
    return "\n".join(lines) + "\n"


def test_the_isolated_set_of_a_remote_node_is_compared_with_its_inventory() -> None:
    """The finding this whole addition exists for.

    isolcpus is read at boot, so a machine converged and never rebooted reads
    exactly like one where the change never happened. Until the pool was read
    from every exporter, that could only be caught on the node the browser
    happened to be pointed at.
    """
    nodes = PoolReader(_Client({"10.0.0.1": _with_isolation("4,5,6,7")})).read(
        [("node1", "10.0.0.1")]
    )
    node = nodes[0]

    assert node.observed_isolcpus == "4-7"

    node.declared_isolcpus = "4-7"
    assert node.isolation_matches is True

    node.declared_isolcpus = "2-7"
    assert node.isolation_matches is False


def test_the_two_notations_for_one_set_compare_equal() -> None:
    # The inventory is written by hand and the kernel prints ranges, so `4-7`
    # and `4,5,6,7` are the same isolation and must not read as a mismatch.
    nodes = PoolReader(_Client({"10.0.0.1": _with_isolation("4,5,6,7")})).read(
        [("node1", "10.0.0.1")]
    )
    nodes[0].declared_isolcpus = "4,5,6,7"

    assert nodes[0].isolation_matches is True


def test_a_node_declaring_nothing_is_neither_a_pass_nor_a_failure() -> None:
    # None rather than False: a machine with no isolcpus in its inventory has
    # nothing to converge towards, and drawing that as a mismatch would put a
    # warning on every machine nobody has configured yet.
    nodes = PoolReader(_Client({"10.0.0.1": _with_isolation("4,5")})).read(
        [("node1", "10.0.0.1")]
    )

    assert nodes[0].isolation_matches is None


def test_an_unreachable_node_is_never_reported_as_matching() -> None:
    nodes = PoolReader(_Client({})).read([("node1", "10.0.0.1")])
    nodes[0].declared_isolcpus = "4-7"

    assert nodes[0].isolation_matches is None


def test_the_kernel_of_a_remote_node_comes_from_node_exporter() -> None:
    # PREEMPT_RT before PREEMPT, or every RT kernel reads as an ordinary one.
    rt = PoolReader(
        _Client({"10.0.0.1": _with_isolation("4", "#1 SMP PREEMPT_RT Debian")})
    ).read([("node1", "10.0.0.1")])
    ordinary = PoolReader(
        _Client({"10.0.0.1": _with_isolation("4", "#1 SMP PREEMPT_DYNAMIC Debian")})
    ).read([("node1", "10.0.0.1")])

    assert rt[0].preemption == "PREEMPT_RT"
    assert rt[0].kernel == "6.1.0-18-rt-amd64"
    assert ordinary[0].preemption == "PREEMPT_DYNAMIC"


def test_the_comparison_survives_serialisation(monkeypatch) -> None:
    # A plain property is invisible to model_dump, so the page received no
    # answer and drew every node as "nothing declared". The check is on the
    # serialised form because that is what the page reads.
    nodes = PoolReader(_Client({"10.0.0.1": _with_isolation("4,5,6,7")})).read(
        [("node1", "10.0.0.1")]
    )
    nodes[0].declared_isolcpus = "4-7"

    assert nodes[0].model_dump()["isolation_matches"] is True


# The tuning, which rides in the same exposition as the pool


_TUNING = (
    'seapath_rt_tuned_info{profile="seapath-rt-host",'
    'source="/etc/tuned/active_profile",installed="1"} 1\n'
    'seapath_rt_kernel_cmdline_info{cmdline="isolcpus=4-7 nohz_full=4-7"} 1\n'
    'seapath_rt_transparent_hugepages_info{enabled="never",defrag="never"} 1\n'
)


def test_a_node_carries_its_tuning_beside_its_pool() -> None:
    # One request answers both halves. They are written into the same textfile
    # by the same collector on the same timer, and reading it twice would
    # double what a page refresh costs a hypervisor for nothing.
    nodes = PoolReader(
        _Client({"10.0.0.1": _with_isolation("4,5,6,7") + _TUNING})
    ).read([("node1", "10.0.0.1")])

    assert nodes[0].reading.tuned_profile == "seapath-rt-host"
    assert nodes[0].kernel_cmdline == "isolcpus=4-7 nohz_full=4-7"
    assert nodes[0].tuning_error == ""


def test_a_node_publishing_the_pool_and_no_tuning_names_what_adds_it() -> None:
    # A collector that predates the tuning block is a node to upgrade, which is
    # a different act from an unreachable node and from a failed check. The
    # pool it does publish is kept.
    nodes = PoolReader(_Client({"10.0.0.1": _with_isolation("4,5,6,7")})).read(
        [("node1", "10.0.0.1")]
    )

    assert nodes[0].reading is None
    assert nodes[0].cpus, "the pool it does publish is still read"
    assert "deploy_seapath_alloc" in nodes[0].tuning_error


def test_a_node_with_no_seapath_alloc_at_all_still_names_its_kernel() -> None:
    # node_exporter's own series, which is there as soon as the exporter is.
    # A machine where deploy_seapath_alloc has not run yet says which kernel it
    # booted rather than nothing at all.
    nodes = PoolReader(
        _Client(
            {
                "10.0.0.1": 'node_uname_info{sysname="Linux",'
                'release="6.1.0-18-rt-amd64",version="#1 SMP PREEMPT_RT"} 1\n'
            }
        )
    ).read([("node1", "10.0.0.1")])

    assert nodes[0].preemption == "PREEMPT_RT"
    assert "deploy_seapath_alloc" in nodes[0].error
