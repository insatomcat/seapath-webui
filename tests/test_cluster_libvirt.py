# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Reading a domain's disks from `libvirt-exporter`.

The recorded exposition in `tests/expositions` covers the ordinary reading.
These hold what a busy exporter publishes instead, which no recording keeps
because it depends on the moment of the scrape.
"""

from __future__ import annotations

from app.cluster import libvirt, metrics
from app.cluster.exporters import Exposition


def read(text: str) -> dict[str, libvirt.LibvirtDomain]:
    reading = libvirt.read(
        Exposition(host="ccv-admin", address="ccv-admin", series=metrics.parse(text))
    )
    return {domain.name: domain for domain in reading.domains}


def exposition(vda: str, vdb: str) -> str:
    info = 'libvirt_domain_block_stats_info{{domain="{d}",target_device="{t}"}} 1\n'
    capacity = (
        'libvirt_domain_block_stats_capacity_bytes{{domain="{d}",'
        'target_device="{t}"}} {v}\n'
    )
    state = (
        'libvirt_domain_info_state{domain="debian14",'
        'state_desc="the domain is running"} 1\n'
    )
    return (
        "libvirt_up 1\n"
        + state
        + info.format(d="debian14", t="vda")
        + info.format(d="debian14", t="vdb")
        + capacity.format(d="debian14", t="vda", v=vda)
        + capacity.format(d="debian14", t="vdb", v=vdb)
    )


def test_every_disk_is_read_with_its_capacity() -> None:
    domains = read(exposition("3.221225472e+09", "376832"))

    assert [
        (disk.device, disk.capacity_bytes) for disk in domains["debian14"].disks
    ] == [
        ("vda", 3221225472),
        ("vdb", 376832),
    ]
    assert domains["debian14"].disks_unread is False


def test_a_disk_published_as_empty_is_a_reading_that_failed() -> None:
    # What the exporter publishes for a domain when two scrapes meet: 0 on
    # every block series. Shown, it was a guest of 0 B.
    domains = read(exposition("0", "0"))

    assert domains["debian14"].disks == []
    assert domains["debian14"].disks_unread is True


def test_one_failed_disk_costs_the_domain_all_of_them() -> None:
    # The sum of the disks that did answer is a size the guest does not have.
    domains = read(exposition("0", "376832"))

    assert domains["debian14"].disks == []
    assert domains["debian14"].disks_unread is True
