# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Asking every machine of the inventory what its exporters publish.

One place reaches the network, and three readings share it: the CPU pool and
the real time tuning on `node_exporter`, the cluster on `ha_cluster_exporter`,
and the storage on the Ceph manager. Each of them is a GET of a text document
and a parse, so what differs between them is what they make of the series, not
how they get them.

Fetched in parallel, because the page waits on the slowest node and a machine
that is down costs the whole timeout: three nodes in series with one
unreachable is six seconds before anything renders.

A node that does not answer is a result rather than an error. A cluster half
built is the ordinary state of a cluster being built, and every panel here has
to render the machines that did answer beside the reason the others did not.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from app.cluster import metrics

logger = logging.getLogger(__name__)


class MetricsClient(Protocol):
    """Fetches one exporter's exposition, or explains why it could not.

    Injected for the same reason the command runner is: the whole test suite
    runs with no cluster, and the set of things this service may reach over the
    network stays a short list in one place.
    """

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]: ...


class UrllibMetricsClient:
    """The stdlib, because this is one GET of a text document.

    An HTTP library would be a dependency in a substation image for a request
    `urllib` already makes. The timeout is short and the failure is a sentence:
    a node that cannot be reached is an ordinary state on a cluster being
    built, and the page says which one rather than failing whole.
    """

    def fetch(self, url: str, timeout: float = 2.0) -> tuple[str | None, str]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                return response.read().decode("utf-8", errors="replace"), ""
        except urllib.error.HTTPError as error:
            return None, f"the exporter answered {error.code}"
        except urllib.error.URLError as error:
            return None, f"{error.reason}"
        except (TimeoutError, OSError) as error:
            return None, str(error)
        except Exception as error:  # pragma: no cover - defensive
            return None, str(error)


class Exposition:
    """What one exporter answered, parsed, or why it answered nothing."""

    __slots__ = ("host", "address", "series", "error")

    def __init__(
        self,
        host: str,
        address: str,
        series: dict[str, list[metrics.Sample]] | None = None,
        error: str = "",
    ) -> None:
        self.host = host
        self.address = address
        self.series = series
        self.error = error

    @property
    def answered(self) -> bool:
        return self.series is not None

    def has(self, name: str) -> bool:
        """Whether this exporter publishes a family at all.

        The question that separates "this node is not reachable" from "this
        node answered and runs none of what was asked about", which are two
        different sentences to put in front of an operator.
        """
        return bool(self.series and name in self.series)


def read_all(
    client: MetricsClient,
    targets: list[tuple[str, str]],
    port: int,
    timeout: float = 2.0,
) -> list[Exposition]:
    """Every target's exposition on one port, in parallel, in the order given."""
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        return list(
            pool.map(
                lambda item: _read(client, item[0], item[1], port, timeout), targets
            )
        )


def _read(
    client: MetricsClient, host: str, address: str, port: int, timeout: float
) -> Exposition:
    url = f"http://{address}:{port}/metrics"
    text, error = client.fetch(url, timeout=timeout)
    if text is None:
        logger.debug("No metrics from %s: %s", url, error)
        return Exposition(host=host, address=address, error=error)
    return Exposition(host=host, address=address, series=metrics.parse(text))


def value(
    series: dict[str, list[metrics.Sample]], name: str, default: float | None = None
) -> float | None:
    """The value of a family with a single sample, when it published one."""
    samples = series.get(name, [])
    return samples[0].value if samples else default


def total(series: dict[str, list[metrics.Sample]], name: str) -> float:
    """Every sample of a family, added up."""
    return sum(sample.value for sample in series.get(name, []))
