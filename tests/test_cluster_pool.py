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
