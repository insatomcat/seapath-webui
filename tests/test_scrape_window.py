# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""One scrape of an exporter, shared by the panels of one page.

Drawing a page asks several endpoints, and several of them ask the same exporter
on the same machine. A scrape is work for the machine being scraped rather than
for this service: `node_exporter` answers by reading /proc, /sys and every
filesystem it can see, on a hypervisor whose CPUs belong to its guests.

So the answer serves the page, and coming back to a page an operator has just
left costs the cluster nothing. What is asserted here is the bound: the window
never stands between the reread control and the machines, and it is emptied by
the parameter that control sends. See D45, and D37 for the control itself.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.cluster.exporters import CachingMetricsClient, ScrapeCache
from app.cluster.fake import FakeMetricsClient
from app.core.settings import Settings
from app.main import create_app
from tests.conftest import BASE_URL, sign_in


class CountingMetricsClient:
    """The fake cluster's answers, and how many times each URL was asked for."""

    def __init__(self) -> None:
        self._answers = FakeMetricsClient()
        self.asked: list[str] = []

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        self.asked.append(url)
        return self._answers.fetch(url, timeout=timeout)

    def scrapes_of(self, port: int) -> int:
        return len([url for url in self.asked if f":{port}/" in url])


@pytest.fixture
def scrapes() -> CountingMetricsClient:
    return CountingMetricsClient()


@pytest.fixture
def counted(
    settings: Settings,
    scrapes: CountingMetricsClient,
    reader,
    authenticator,
    directory,
    run_adapter,
    console_adapter,
    rbd_client,
    tag_source,
) -> Iterator[TestClient]:
    """A signed in client whose every scrape is counted."""
    application = create_app(
        settings=settings,
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=console_adapter,
        metrics_client=scrapes,
        rbd_client=rbd_client,
        tag_source=tag_source,
    )
    with TestClient(application, base_url=BASE_URL) as test_client:
        yield sign_in(test_client, "admin")


def test_two_panels_of_one_page_cost_the_cluster_one_scrape(
    counted: TestClient, scrapes: CountingMetricsClient
) -> None:
    """The Cluster page reads Pacemaker and Ceph, and each is a fan out.

    They are separate endpoints because they fail separately, which is what
    lets a cluster with no Ceph still show its membership. Neither of them is a
    reason to scrape a machine twice.
    """
    assert counted.get("/api/v1/cluster").status_code == 200
    first = scrapes.scrapes_of(9664)

    assert counted.get("/api/v1/cluster").status_code == 200

    assert first > 0
    assert scrapes.scrapes_of(9664) == first


def test_the_pages_that_read_the_node_exporter_share_one_scrape(
    counted: TestClient, scrapes: CountingMetricsClient
) -> None:
    """The CPU pool and the container units come out of the same exposition.

    They are two pages and two endpoints, and on a real node they are one file
    the machine built by reading its own /proc.
    """
    assert counted.get("/api/v1/realtime/pool").status_code == 200
    first = scrapes.scrapes_of(9100)

    assert counted.get("/api/v1/containers").status_code == 200

    assert first > 0
    assert scrapes.scrapes_of(9100) == first


def test_the_reading_an_operator_asked_for_reaches_the_machines(
    counted: TestClient, scrapes: CountingMetricsClient
) -> None:
    """`fresh=1` is what the reread control and its timer send.

    This is the bound on the whole thing: an operator pressing the control on a
    panel means the machines, and a window that answered them would be this
    service telling them what the cluster looked like a moment ago while they
    were asking what it looks like now.
    """
    assert counted.get("/api/v1/cluster").status_code == 200
    first = scrapes.scrapes_of(9664)

    assert counted.get("/api/v1/cluster?fresh=1").status_code == 200

    assert scrapes.scrapes_of(9664) == first * 2


def test_a_reading_asked_for_empties_the_window_for_every_panel(
    counted: TestClient, scrapes: CountingMetricsClient
) -> None:
    """The control on one panel is the operator asking about the machines.

    The other panels of the page are read from the same machines a moment
    later, by the round the timer walks, and an answer kept from before the
    fresh one would make the page disagree with itself.
    """
    assert counted.get("/api/v1/cluster").status_code == 200
    assert counted.get("/api/v1/storage").status_code == 200
    ceph = scrapes.scrapes_of(9283)

    assert counted.get("/api/v1/cluster?fresh=1").status_code == 200
    assert counted.get("/api/v1/storage").status_code == 200

    assert scrapes.scrapes_of(9283) == ceph * 2


def test_a_window_of_zero_asks_the_machines_every_time(
    settings: Settings,
    scrapes: CountingMetricsClient,
    reader,
    authenticator,
    directory,
    run_adapter,
    console_adapter,
    rbd_client,
    tag_source,
) -> None:
    """A site that wants no window at all sets it to zero.

    It costs a page one scrape per panel, which is the behaviour this service
    had before the window, so the setting is a way back to it rather than a
    tuning knob nobody understands.
    """
    application = create_app(
        settings=settings.model_copy(update={"scrape_window_seconds": 0.0}),
        reader=reader,
        authenticator=authenticator,
        role_directory=directory,
        session_secret=b"test-secret",
        run_adapter=run_adapter,
        console_adapter=console_adapter,
        metrics_client=scrapes,
        rbd_client=rbd_client,
        tag_source=tag_source,
    )
    with TestClient(application, base_url=BASE_URL) as client:
        signed_in = sign_in(client, "admin")
        assert signed_in.get("/api/v1/cluster").status_code == 200
        first = scrapes.scrapes_of(9664)
        assert signed_in.get("/api/v1/cluster").status_code == 200

    assert first > 0
    assert scrapes.scrapes_of(9664) == first * 2


def test_an_exporter_that_did_not_answer_is_kept_like_one_that_did() -> None:
    """The expensive case, and the one the window is most worth having for.

    A machine that is down costs the whole timeout, and a page used to pay it
    once per panel: three panels against a node that is off is six seconds of a
    page rendering nothing.
    """
    asked: list[str] = []

    class Down:
        def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
            asked.append(url)
            return None, "No route to host"

    client = CachingMetricsClient(Down(), ScrapeCache(window_seconds=30.0))
    url = "http://10.0.0.9:9664/metrics"

    assert client.fetch(url) == (None, "No route to host")
    assert client.fetch(url) == (None, "No route to host")
    assert asked == [url]


def test_the_window_closes_on_its_own() -> None:
    """A few seconds, and not a cache: the answer ages out without being asked to."""
    asked: list[str] = []

    class Once:
        def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
            asked.append(url)
            return f"reading {len(asked)}", ""

    cache = ScrapeCache(window_seconds=-1.0)
    client = CachingMetricsClient(Once(), cache)

    assert client.fetch("http://10.0.0.1:9100/metrics")[0] == "reading 1"
    assert client.fetch("http://10.0.0.1:9100/metrics")[0] == "reading 2"


def test_each_exporter_is_remembered_on_its_own() -> None:
    """One exporter's answer must never be handed out for another's.

    Keyed by URL, which carries the machine and the port: one node serves three
    exporters saying three different things, and a cluster of three serves nine.
    """
    cache = ScrapeCache(window_seconds=30.0)
    client = CachingMetricsClient(FakeMetricsClient(), cache)

    pool = client.fetch("http://192.168.200.125:9100/metrics")[0]
    pacemaker = client.fetch("http://192.168.200.125:9664/metrics")[0]

    assert pool is not None
    assert pacemaker is not None
    assert pool != pacemaker
    assert client.fetch("http://192.168.200.125:9100/metrics")[0] == pool
    # And a machine nothing answers for is still that machine's answer.
    assert client.fetch("http://10.0.0.9:9100/metrics") == (None, "No route to host")
