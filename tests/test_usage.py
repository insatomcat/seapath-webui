# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What each machine and each workload on it consumes, as counters.

The readings are held against expositions recorded from the demo cluster: the
standalone hypervisor, whose guests name their tap devices after themselves,
and a cluster member, which runs the Ceph daemons as containers. What is
asserted is what the browser divides: the counters, their absence where an
exporter published none, and the moment each was read. See D67.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.cluster import metrics, usage
from app.cluster.exporters import Exposition
from app.cluster.fake import FakeMetricsClient
from app.core.settings import Settings
from app.main import create_app
from app.services.usage import UsageService
from tests.conftest import BASE_URL, sign_in
from tests.test_scrape_window import CountingMetricsClient

EXPOSITIONS = Path(__file__).parent / "expositions"
LIBVIRT = "libvirt-exporter.txt"
_READ = "libvirt_domain_block_stats_read_bytes_total"
_WRITTEN = "libvirt_domain_block_stats_write_bytes_total"


def recorded(name: str, host: str = "ccv-admin") -> Exposition:
    text = (EXPOSITIONS / name).read_text()
    return Exposition(host, "10.0.0.1", metrics.parse(text), read_at=1000.0)


def standalone() -> usage.NodeUsage:
    reading = usage.node(recorded("node-exporter-usage.txt"))
    assert reading is not None
    return reading


def member() -> usage.NodeUsage:
    reading = usage.node(recorded("node-exporter-usage-member.txt"))
    assert reading is not None
    return reading


# The machine


def test_the_isolated_cpus_are_counted_apart_from_the_housekeeping_ones() -> None:
    """ccv-admin isolates 4-23 and 28-47, which leaves eight for the machine."""
    reading = standalone()

    assert reading.housekeeping.cpus == 8
    assert reading.isolated.cpus == 40


def test_a_cpu_side_adds_up_every_mode_and_counts_iowait_as_idle() -> None:
    series = metrics.parse(
        'node_cpu_seconds_total{cpu="0",mode="idle"} 100\n'
        'node_cpu_seconds_total{cpu="0",mode="iowait"} 5\n'
        'node_cpu_seconds_total{cpu="0",mode="user"} 30\n'
        'node_cpu_seconds_total{cpu="0",mode="system"} 15\n'
        'node_cpu_seconds_total{cpu="1",mode="idle"} 50\n'
        'node_cpu_seconds_total{cpu="1",mode="user"} 50\n'
        'node_cpu_isolated{cpu="1"} 1\n'
        "node_time_seconds 1234.5\n"
    )
    reading = usage.node(Exposition("h", "a", series, read_at=9.0))

    assert reading is not None
    assert reading.housekeeping == usage.CpuCounters(
        cpus=1, total_seconds=150, idle_seconds=105
    )
    assert reading.isolated == usage.CpuCounters(
        cpus=1, total_seconds=100, idle_seconds=50
    )


def test_the_machine_is_timed_by_its_own_clock() -> None:
    """node_exporter publishes the time of its scrape, and a rate divides by it.

    The time the answer reached this service is later by however long the
    machine took to walk /proc and /sys, which varies from one scrape to the
    next on a busy hypervisor.
    """
    assert standalone().read_at == pytest.approx(1790157005.389664)


def test_a_machine_whose_exporter_publishes_no_clock_is_timed_on_arrival() -> None:
    series = metrics.parse('node_cpu_seconds_total{cpu="0",mode="idle"} 1\n')
    reading = usage.node(Exposition("h", "a", series, read_at=42.0))

    assert reading is not None
    assert reading.read_at == 42.0


def test_an_exposition_without_cpu_time_is_not_a_machine() -> None:
    assert usage.node(recorded("podman-exporter.txt")) is None


def test_the_memory_is_read_with_its_hugepage_pool() -> None:
    series = metrics.parse(
        'node_cpu_seconds_total{cpu="0",mode="idle"} 1\n'
        "node_memory_MemTotal_bytes 1000\n"
        "node_memory_MemAvailable_bytes 400\n"
        "node_memory_HugePages_Total 4\n"
        "node_memory_HugePages_Free 1\n"
        "node_memory_Hugepagesize_bytes 50\n"
    )
    reading = usage.node(Exposition("h", "a", series))

    assert reading is not None
    assert reading.memory.total_bytes == 1000
    assert reading.memory.available_bytes == 400
    assert reading.memory.hugepages_total_bytes == 200
    assert reading.memory.hugepages_free_bytes == 50
    assert reading.memory.swap_total_bytes is None


def test_the_recorded_memory_is_read() -> None:
    reading = member()

    assert reading.memory.total_bytes == 66848161792
    assert reading.memory.available_bytes == 36790472704
    assert reading.memory.hugepages_total_bytes == 0


def test_the_filesystems_leave_out_the_ones_that_hold_no_data() -> None:
    reading = standalone()

    assert [filesystem.mountpoint for filesystem in reading.filesystems] == [
        "/",
        "/boot/efi",
        "/data",
        "/gnu/store",
        "/var/log",
    ]
    root = reading.filesystems[0]
    assert root.fstype == "ext4"
    assert root.device == "/dev/mapper/vg1-root"
    assert 0 < root.available_bytes < root.size_bytes


def test_the_disks_leave_out_the_logical_volumes_counted_on_them() -> None:
    reading = standalone()

    assert [disk.device for disk in reading.disks] == ["sda", "sdb", "sdc"]
    assert all(disk.read_bytes is not None for disk in reading.disks)
    assert all(disk.io_time_seconds is not None for disk in reading.disks)


def test_each_interface_is_told_apart_by_how_its_address_was_given() -> None:
    kinds = {link.device: link.kind for link in member().interfaces}

    assert kinds["eno12399"] == "physical"
    assert kinds["enp26s0f1"] == "physical"
    assert kinds["team0"] == "logical"
    assert kinds["br0"] == "logical"
    assert kinds["eno12419.800"] == "logical"
    assert kinds["processbus"] == "logical"
    assert kinds["vnet0"] == "workload"
    assert kinds["veth0"] == "workload"
    assert kinds["podman0"] == "workload"
    assert kinds["lo"] == "loopback"


def test_an_exporter_that_says_nothing_of_addresses_leaves_the_name_to_go_by() -> None:
    series = metrics.parse(
        'node_cpu_seconds_total{cpu="0",mode="idle"} 1\n'
        'node_network_receive_bytes_total{device="eth0"} 10\n'
        'node_network_receive_bytes_total{device="veth9"} 10\n'
        'node_network_receive_bytes_total{device="lo"} 10\n'
    )
    reading = usage.node(Exposition("h", "a", series))

    assert reading is not None
    kinds = {link.device: link.kind for link in reading.interfaces}
    assert kinds == {"eth0": "physical", "veth9": "workload", "lo": "loopback"}


def test_a_link_that_reports_a_negative_speed_has_none() -> None:
    links = {link.device: link for link in member().interfaces}

    assert links["eno12399"].speed_bytes == 125000000
    assert links["eno12429"].speed_bytes is None


# The guests


def test_a_running_guest_carries_its_counters() -> None:
    guests = {guest.name: guest for guest in usage.guests(recorded(LIBVIRT))}

    podev = guests["PODEV"]
    assert podev.running is True
    assert podev.vcpus == 3
    assert podev.cpu_seconds == pytest.approx(75487.37)
    assert podev.memory_bytes == 12340260864
    assert podev.disk_read_bytes is not None
    assert podev.network_receive_bytes is not None
    assert podev.interfaces == ["PODEV"]


def test_a_guest_that_is_shut_off_uses_nothing() -> None:
    guests = {guest.name: guest for guest in usage.guests(recorded(LIBVIRT))}

    shut = guests["VMUADMIN"]
    assert shut.running is False
    assert shut.cpu_seconds is None
    assert shut.memory_bytes is None
    assert shut.disk_read_bytes is None


def test_a_guest_with_several_interfaces_is_their_sum() -> None:
    exposition = recorded("libvirt-exporter-member.txt", "ccv1")
    received = [
        sample.value
        for sample in (exposition.series or {})[
            "libvirt_domain_interface_stats_receive_bytes_total"
        ]
        if sample.labels["domain"] == "ABB15"
    ]
    guests = {guest.name: guest for guest in usage.guests(exposition)}

    assert len(received) == 4
    assert guests["ABB15"].network_receive_bytes == pytest.approx(sum(received))
    assert guests["ABB15"].interfaces == ["vnet1", "vnet2", "vnet3", "vnet4"]


def _domain(block: str, memory: str = "") -> Exposition:
    text = (
        'libvirt_domain_info_state{domain="g",state_desc="running"} 1\n'
        'libvirt_domain_info_cpu_time_seconds_total{domain="g"} 12\n'
        'libvirt_domain_info_memory_usage_bytes{domain="g"} 2048\n' + memory + block
    )
    return Exposition("h", "a", metrics.parse(text))


def test_a_guest_whose_disks_all_read_zero_has_no_disk_reading() -> None:
    """A scrape that met another one on a busy exporter publishes every block
    series of a domain as 0, and as a counter that is the next reading looking
    like the whole life of the guest in one interval."""
    [guest] = usage.guests(
        _domain(
            f'{_READ}{{domain="g",target_device="vda"}} 0\n'
            f'{_WRITTEN}{{domain="g",target_device="vda"}} 0\n'
        )
    )

    assert guest.disk_read_bytes is None
    assert guest.disk_written_bytes is None
    assert guest.cpu_seconds == 12


def test_a_guest_that_has_only_read_keeps_its_disk_reading() -> None:
    [guest] = usage.guests(
        _domain(
            f'{_READ}{{domain="g",target_device="vda"}} 7\n'
            f'{_WRITTEN}{{domain="g",target_device="vda"}} 0\n'
        )
    )

    assert guest.disk_read_bytes == 7
    assert guest.disk_written_bytes == 0


def test_a_guest_without_a_resident_set_is_counted_by_its_allocation() -> None:
    [without] = usage.guests(_domain(""))
    [with_rss] = usage.guests(
        _domain("", 'libvirt_domain_memory_stats_rss_bytes{domain="g"} 1024\n')
    )

    assert without.memory_bytes == 2048
    assert with_rss.memory_bytes == 1024


# The containers


def test_a_container_is_named_by_its_info_series() -> None:
    containers = {
        container.name: container
        for container in usage.containers(recorded("podman-exporter.txt"))
    }

    webui = containers["seapath-webui"]
    assert webui.id == "aea5ad7cdb4f"
    assert webui.image == "docker.io/insatomcat/seapath-webui:0.3.177"
    assert webui.state == "running"
    assert webui.running is True
    assert webui.cpu_seconds == pytest.approx(4.858808)
    assert webui.memory_bytes == 87064576


def test_the_ceph_daemons_are_containers_like_the_others() -> None:
    names = [
        container.name
        for container in usage.containers(
            recorded("podman-exporter-member.txt", "ccv1")
        )
    ]

    assert any(name.endswith("-osd-0") for name in names)
    assert "systemd-podman-exporter" in names


def test_a_container_that_has_exited_uses_nothing() -> None:
    text = (
        'podman_container_info{id="abc",image="i",name="done"} 1\n'
        'podman_container_state{id="abc"} 5\n'
        'podman_container_cpu_seconds_total{id="abc"} 3\n'
        'podman_container_mem_usage_bytes{id="abc"} 0\n'
    )
    [container] = usage.containers(Exposition("h", "a", metrics.parse(text)))

    assert container.state == "exited"
    assert container.running is False
    assert container.cpu_seconds is None
    assert container.memory_bytes is None


# The service


def _inventory(hosts: dict[str, str]) -> SimpleNamespace:
    """What the service reads of the inventory: the machines and their addresses."""
    state = SimpleNamespace(
        inventory=SimpleNamespace(
            hosts={
                name: SimpleNamespace(ansible_host=address)
                for name, address in hosts.items()
            }
        ),
        this_host=next(iter(hosts), None),
        commit="abc123",
    )
    return SimpleNamespace(state=lambda: state)


def _service(hosts: dict[str, str]) -> UsageService:
    return UsageService(
        inventory=_inventory(hosts),  # type: ignore[arg-type]
        client=FakeMetricsClient(),
        node_port=9100,
        libvirt_port=9177,
        podman_port=9882,
    )


def test_every_machine_is_asked_on_the_three_exporters() -> None:
    view = _service({"elabo1": "elabo1", "elabo2": "elabo2"}).usage()

    first, second = view.machines
    assert (first.host, second.host) == ("elabo1", "elabo2")
    assert first.node is not None and second.node is not None
    assert len(first.guests) == 8
    assert first.containers and second.containers
    assert first.node_reach.reachable and first.guests_reach.reachable
    assert first.containers_reach.reachable
    assert view.this_host == "elabo1"
    assert view.inventory_commit == "abc123"


def test_a_guest_tap_named_after_the_guest_is_its_traffic() -> None:
    """ccv-admin's domains name their tap device after themselves, which a
    name alone reads as a port of the machine. libvirt says it is theirs."""
    [machine] = _service({"elabo1": "elabo1"}).usage().machines

    assert machine.node is not None
    kinds = {link.device: link.kind for link in machine.node.interfaces}
    assert kinds["ABBICT"] == "workload"
    assert kinds["EITDEB13"] == "workload"
    assert kinds["eno8303"] == "physical"


def test_a_machine_that_does_not_answer_is_a_line_beside_the_others() -> None:
    view = _service({"elabo1": "elabo1", "elabo3": "192.168.200.127"}).usage()

    answered, silent = view.machines
    assert answered.node is not None
    assert silent.node is None
    assert silent.node_reach.reachable is False
    assert silent.node_reach.error == "No route to host"
    assert silent.guests == [] and silent.containers == []


def test_a_machine_outside_the_hypervisors_has_no_guest_to_report() -> None:
    """elabo2 runs no libvirt-exporter in the fakes, which is a cluster member
    outside the `hypervisors` group: its containers are still read."""
    [machine] = _service({"elabo2": "elabo2"}).usage().machines

    assert machine.guests == []
    assert machine.guests_reach.reachable is False
    assert machine.containers


def test_each_workload_exporter_is_timed_when_it_answered() -> None:
    [machine] = _service({"elabo1": "elabo1"}).usage().machines

    assert machine.guests_read_at > 0
    assert machine.containers_read_at > 0


def test_an_inventory_that_is_not_there_is_said() -> None:
    inventory = SimpleNamespace(
        state=lambda: SimpleNamespace(inventory=None, commit=None, this_host=None)
    )
    service = UsageService(
        inventory=inventory,  # type: ignore[arg-type]
        client=FakeMetricsClient(),
        node_port=9100,
        libvirt_port=9177,
        podman_port=9882,
    )

    view = service.usage()

    assert view.machines == []
    assert "no inventory" in view.note


# The API


def test_a_viewer_may_read_the_usage(signed_in_viewer: TestClient) -> None:
    response = signed_in_viewer.get("/api/v1/usage")

    assert response.status_code == 200
    [machine] = response.json()["machines"]
    assert machine["host"] == "seapath-machine"
    assert machine["node"]["housekeeping"]["cpus"] == 8


def test_the_usage_may_not_be_read_signed_out(client: TestClient) -> None:
    assert client.get("/api/v1/usage").status_code == 401


@pytest.fixture
def counted(
    settings: Settings,
    reader,
    authenticator,
    directory,
    run_adapter,
    console_adapter,
    rbd_client,
    tag_source,
) -> Iterator[tuple[TestClient, CountingMetricsClient]]:
    scrapes = CountingMetricsClient()
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
        yield sign_in(test_client, "admin"), scrapes


def test_every_reading_of_the_usage_reaches_the_machines(
    counted: tuple[TestClient, CountingMetricsClient],
) -> None:
    """Two readings inside the scrape window are still two scrapes.

    A rate divides the difference between two answers by the time between
    them. An answer the window kept is an earlier reading given again, and
    divided by the time since, it is a machine that did nothing.
    """
    signed_in, scrapes = counted

    assert signed_in.get("/api/v1/usage").status_code == 200
    assert signed_in.get("/api/v1/usage").status_code == 200

    assert scrapes.scrapes_of(9100) == 2
    assert scrapes.scrapes_of(9177) == 2
    assert scrapes.scrapes_of(9882) == 2


def test_the_usage_reading_leaves_the_window_of_the_other_pages_alone(
    counted: tuple[TestClient, CountingMetricsClient],
) -> None:
    """It goes around the window rather than emptying it: the pages that
    share one scrape keep sharing it while this one reads every five seconds."""
    signed_in, scrapes = counted

    assert signed_in.get("/api/v1/realtime/pool").status_code == 200
    assert signed_in.get("/api/v1/usage").status_code == 200
    before = scrapes.scrapes_of(9100)
    assert signed_in.get("/api/v1/containers").status_code == 200

    assert scrapes.scrapes_of(9100) == before


def test_a_machine_behind_its_metrics_proxy_is_asked_for_podman_on_its_path(
    signed_in: TestClient,
) -> None:
    """`deploy_metrics_proxy` serves podman's exporter as `podman_exporter`,
    and a machine behind the proxy answers on nothing else."""
    usage_client = signed_in.app.state.usage_service._client  # type: ignore[attr-defined]

    assert usage_client._routes[9882] == "podman_exporter"
