# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The real read only adapter, against a recorded machine.

These tests are the ones that would catch a parser breaking on a kernel that
formats something differently. They run on a laptop because the filesystem root
is a parameter and every command goes through the injected runner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.hosts.local import (
    LocalHostReader,
    parse_cpu_list,
    read_admin_address,
    read_hostname,
)
from app.hosts.models import NodeMode
from app.hosts.reader import CommandResult
from tests.fakes import FakeCommandRunner
from tests.hostfixture import build_host_tree, write_proc_stat


@pytest.fixture
def host(tmp_path: Path) -> Path:
    return build_host_tree(tmp_path / "host")


@pytest.fixture
def runner() -> FakeCommandRunner:
    return FakeCommandRunner(
        {
            "ip -j addr show": CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "ifname": "eno1",
                            "addr_info": [
                                {
                                    "family": "inet",
                                    "local": "192.168.200.121",
                                    "prefixlen": 24,
                                }
                            ],
                        },
                        {"ifname": "lo", "addr_info": []},
                    ]
                ),
                "",
            ),
        }
    )


@pytest.fixture
def reader(host: Path, runner: FakeCommandRunner) -> LocalHostReader:
    return LocalHostReader(root=host, runner=runner)


def test_the_hostname_comes_from_the_host_not_the_container(
    reader: LocalHostReader,
) -> None:
    identity = reader.node_identity()

    assert identity.hostname == "node1"
    assert identity.warnings == []


def test_the_administration_account_is_the_one_holding_uid_1000(
    reader: LocalHostReader,
) -> None:
    # The same question `configure_seapath_distro` asks with
    # `getent passwd 1000`, and the answer `admin_user` is seeded with.
    assert reader.node_identity().admin_account == "admin"


def test_a_machine_with_no_uid_1000_says_so(host: Path, runner) -> None:
    (host / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")

    identity = LocalHostReader(root=host, runner=runner).node_identity()

    assert identity.admin_account is None
    assert any("UID 1000" in warning for warning in identity.warnings)


def test_the_certificate_names_the_node_not_the_container(host: Path) -> None:
    # The common name of the certificate is what an operator compares against
    # the machine they think they are talking to, and socket.gethostname()
    # inside a container answers with a container id.
    assert read_hostname(host) == "node1"


def test_the_hostname_falls_back_to_this_process_when_nothing_is_mounted(
    tmp_path: Path,
) -> None:
    import socket

    assert read_hostname(tmp_path / "empty") == socket.gethostname()


def test_a_missing_hostname_mount_is_called_out(tmp_path: Path) -> None:
    identity = LocalHostReader(root=tmp_path / "empty").node_identity()

    assert any("etc/hostname" in warning for warning in identity.warnings)


def test_a_machine_with_no_corosync_authkey_reads_as_standalone(
    reader: LocalHostReader,
) -> None:
    # The ordinary state of a standalone node, and a reading in its own right.
    assert reader.node_identity().mode is NodeMode.STANDALONE


def test_the_corosync_authkey_makes_it_a_cluster_member(
    reader: LocalHostReader, host: Path
) -> None:
    (host / "etc/corosync/authkey").write_text("never read, only stat'ed")

    assert reader.node_identity().mode is NodeMode.CLUSTER


def test_the_corosync_conf_the_debian_package_ships_says_nothing(
    reader: LocalHostReader, host: Path
) -> None:
    # Found on a standalone node whose badge said "cluster". `dpkg -S
    # /etc/corosync/corosync.conf` answers `corosync`: the package ships a
    # default configuration, so every machine that installs corosync carries
    # that file whether or not a cluster was ever formed.
    (host / "etc/corosync/corosync.conf").write_text("totem {}\n")

    assert reader.node_identity().mode is NodeMode.STANDALONE


def test_membership_is_unknown_when_the_directory_is_not_mounted(
    tmp_path: Path,
) -> None:
    identity = LocalHostReader(root=tmp_path / "empty").node_identity()

    assert identity.mode is NodeMode.UNKNOWN
    assert any("/etc/corosync" in warning for warning in identity.warnings)


def test_the_service_image_comes_from_the_installed_quadlet(
    reader: LocalHostReader,
) -> None:
    # What the machine boots this service on, which is what the seed inventory
    # pins `seapath_webui_image` from. The fixture also carries a commented
    # `Image=`, which a line based parser has to leave alone.
    assert (
        reader.node_identity().service_image
        == "docker.io/insatomcat/seapath-webui:latest"
    )


def test_a_later_image_assignment_wins_the_way_systemd_reads_it(
    reader: LocalHostReader, host: Path
) -> None:
    quadlet = host / "etc/containers/systemd/seapath-webui.container"
    quadlet.write_text(
        quadlet.read_text() + "Image = registry.example.org:5000/seapath/webui:2.1\n"
    )

    assert (
        reader.node_identity().service_image
        == "registry.example.org:5000/seapath/webui:2.1"
    )


def test_a_machine_with_no_unit_file_reports_no_image_and_no_warning(
    tmp_path: Path,
) -> None:
    # A development checkout, or a container that was handed no host /etc. The
    # seed pins nothing, and the System page already says the inventory names
    # no image, so there is nothing here for a warning to add.
    identity = LocalHostReader(root=tmp_path / "empty").node_identity()

    assert identity.service_image is None
    assert not any("image" in warning.lower() for warning in identity.warnings)


def test_the_isolated_set_is_read_from_sysfs(reader: LocalHostReader) -> None:
    cpu = reader.cpu()

    assert cpu.isolated == [4, 5, 6, 7]
    assert cpu.isolated_source == "sysfs"
    assert cpu.housekeeping == [0, 1, 2, 3]
    assert cpu.model.startswith("Intel(R) Xeon(R)")


def test_the_isolated_set_falls_back_to_the_kernel_command_line(
    reader: LocalHostReader, host: Path
) -> None:
    # An older kernel does not export /sys/devices/system/cpu/isolated.
    (host / "sys/devices/system/cpu/isolated").write_text("\n")

    cpu = reader.cpu()

    assert cpu.isolated == [4, 5, 6, 7]
    assert cpu.isolated_source == "cmdline"


def test_a_machine_with_no_isolation_says_so(
    reader: LocalHostReader, host: Path
) -> None:
    (host / "sys/devices/system/cpu/isolated").write_text("\n")
    (host / "proc/cmdline").write_text("BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro\n")

    cpu = reader.cpu()

    assert cpu.isolated == []
    assert any("seapath_setup_main" in warning for warning in cpu.warnings)


def test_cpu_busy_is_a_rate_between_two_polls_not_an_average_since_boot(
    reader: LocalHostReader, host: Path
) -> None:
    first = reader.cpu()
    assert all(entry.busy_percent is None for entry in first.topology)

    # Half of the elapsed jiffies were busy.
    write_proc_stat(host, busy=1500, idle=9500)
    second = reader.cpu()

    assert all(entry.busy_percent == 50.0 for entry in second.topology)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0-3", [0, 1, 2, 3]),
        ("0-3,8", [0, 1, 2, 3, 8]),
        ("4", [4]),
        ("", []),
        (None, []),
        # `isolcpus` accepts flags before the list.
        ("nohz,domain,4-7", [4, 5, 6, 7]),
    ],
)
def test_the_kernel_cpu_list_syntax(raw: str | None, expected: list[int]) -> None:
    assert parse_cpu_list(raw) == expected


def test_addresses_come_from_iproute_and_links_from_sysfs(
    reader: LocalHostReader,
) -> None:
    network = reader.network()
    interfaces = {item.name: item for item in network.interfaces}

    assert interfaces["eno1"].driver == "igb"
    assert interfaces["eno1"].speed_mbps == 1000
    assert interfaces["eno1"].addresses[0].address == "192.168.200.121"
    assert interfaces["eno1"].addresses[0].prefix_length == 24
    # A virtual device reports -1, which is not a speed.
    assert interfaces["ovsbr0"].speed_mbps is None
    assert network.default_route_interface == "eno1"
    assert network.default_gateway == "192.168.200.1"


def test_missing_iproute_degrades_with_the_reason(host: Path) -> None:
    network = LocalHostReader(root=host, runner=FakeCommandRunner()).network()

    assert network.interfaces
    assert all(item.addresses == [] for item in network.interfaces)
    assert any("addresses are unavailable" in w for w in network.warnings)


def test_the_ptp_clocks_come_from_sysfs(reader: LocalHostReader) -> None:
    assert [clock.clock_name for clock in reader.ptp_clocks()] == ["ice-ptp"]


def test_a_machine_with_no_ptp_hardware_reads_as_empty(tmp_path: Path) -> None:
    # An observer node has no PTP clock, which is ordinary rather than
    # degraded: the discovery form simply does not offer a ptp_interface.
    assert LocalHostReader(root=tmp_path / "empty").ptp_clocks() == []


def test_disks_carry_the_by_path_name_and_their_claim_state(
    reader: LocalHostReader,
) -> None:
    devices = {device.name: device for device in reader.disks().devices}

    assert set(devices) == {"sda", "sdb", "sdc"}
    assert devices["sda"].claimed is True
    assert devices["sda"].partitions == ["sda1", "sda2"]
    assert devices["sdb"].claimed is False
    assert devices["sdb"].by_path == ("/dev/disk/by-path/pci-0000:03:00.0-scsi-0:2:1:0")
    assert devices["sdb"].size_bytes == 3750748848 * 512
    assert devices["sdc"].claimed is True
    assert devices["sdc"].holders == ["dm-0"]


def test_a_disk_reading_that_worked_carries_no_warning(
    reader: LocalHostReader,
) -> None:
    # The banner is worth reading only as long as it means something went wrong
    # on this machine. How the claim state is derived, and that a whole disk
    # filesystem is invisible from /sys, is a permanent property of the
    # reading: it belongs in the disks card, and that is where it is now.
    assert reader.disks().warnings == []


def test_the_by_path_name_of_a_disk_is_never_a_partition_link(
    reader: LocalHostReader,
) -> None:
    # Choosing sda1's link for sda would put a partition in ceph_osd_disks.
    devices = {device.name: device for device in reader.disks().devices}

    assert devices["sda"].by_path == ("/dev/disk/by-path/pci-0000:03:00.0-scsi-0:2:0:0")


def test_the_reading_shells_out_only_for_the_addresses(
    reader: LocalHostReader, runner: FakeCommandRunner
) -> None:
    # Everything else is a file under /proc, /sys or /etc. This is the property
    # that keeps the container out of the host's systemd, its bus and its
    # journal, so it is asserted rather than left to the reviewer's memory.
    reader.node_identity()
    reader.cpu()
    reader.network()
    reader.ptp_clocks()
    reader.disks()

    assert [argv[0] for argv in runner.calls] == ["ip"]


def test_the_seapath_distribution_is_read_from_os_release(tmp_path: Path) -> None:
    # The same five names `detect_seapath_distro` produces, worked out from the
    # file rather than asked of Ansible: the answer decides whether a button is
    # offered, which happens long before any run.
    cases = {
        'ID=debian\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n': "Debian",
        'ID="centos"\nNAME="CentOS Stream"\n': "CentOS",
        'ID="rhel"\nNAME="Red Hat Enterprise Linux"\n': "CentOS",
        # Oracle Linux carries ID_LIKE="fedora" and a Red Hat compatible
        # release file, so it has to be tested before CentOS rather than after.
        'ID="ol"\nNAME="Oracle Linux Server"\nID_LIKE="fedora"\n': "OracleLinux",
        'ID="sles"\nNAME="SLES"\n': "SLES",
        # A SEAPATH Yocto image names itself in ways no regex catches, and
        # CPE_NAME is the signal the upstream role itself trusts.
        'ID="seapath"\nCPE_NAME="cpe:/o:openembedded:nodistro:0.1"\n': "Yocto",
        # Anything else, and an unreadable file, answer nothing rather than
        # guessing: nothing downstream may block on a guess.
        'ID="ubuntu"\nNAME="Ubuntu"\n': None,
        "": None,
    }

    for content, expected in cases.items():
        root = tmp_path / (expected or "none") / content[:12].replace("/", "_")
        (root / "etc").mkdir(parents=True, exist_ok=True)
        (root / "etc/os-release").write_text(content)
        (root / "etc/hostname").write_text("machine\n")

        identity = LocalHostReader(root=root).node_identity()

        assert identity.seapath_distro == expected, content


def test_the_administration_address_is_the_one_on_the_default_route(
    host: Path, runner: FakeCommandRunner
) -> None:
    # The address the listening socket is bound to at first boot, and the same
    # one the inventory form proposes as ip_addr.
    assert read_admin_address(host, runner) == "192.168.200.121"


def test_no_administration_address_without_a_default_route(
    host: Path, runner: FakeCommandRunner
) -> None:
    (host / "proc/net/route").write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
    )
    assert read_admin_address(host, runner) is None


# Real time. The whole reading comes from files the container already sees: its
# own /proc, the read only /sys, and the host's /etc that PAM brought in. A
# test that needed a new mount here would be a test of a design change.


def test_the_tuned_profile_is_the_configured_one_not_the_running_one(
    reader: LocalHostReader,
) -> None:
    # /etc/tuned/active_profile is what `configure_hypervisor` writes and what
    # survives a reboot, so it is the value an inventory can be held against.
    # The daemon's own /run/tuned/active_profile is live state and D13 keeps it
    # out.
    realtime = reader.realtime()

    assert realtime.tuned_profile == "seapath-rt-host"
    assert realtime.tuned_profile_source == "/etc/tuned/active_profile"
    assert realtime.tuned_profile_installed is True


def test_a_selected_profile_that_is_not_installed_is_reported_as_such(
    host: Path, runner: FakeCommandRunner
) -> None:
    # tuned-adm on the host reports the name either way, so a machine tuned by
    # nothing looks configured. The directory is the only thing that says.
    (host / "etc/tuned/active_profile").write_text("site-rt\n")

    realtime = LocalHostReader(root=host, runner=runner).realtime()

    assert realtime.tuned_profile == "site-rt"
    assert realtime.tuned_profile_installed is False


def test_the_preemption_model_prefers_preempt_rt_over_preempt(
    reader: LocalHostReader,
) -> None:
    # A PREEMPT_RT kernel carries PREEMPT in the same string, so the shorter
    # marker matched first would report every RT kernel as an ordinary one.
    assert reader.realtime().preemption == "PREEMPT_RT"


def test_a_preemptible_kernel_is_not_read_as_a_real_time_one(
    host: Path, runner: FakeCommandRunner
) -> None:
    (host / "proc/version").write_text(
        "Linux version 6.1.0-18-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.1.76-1\n"
    )

    assert LocalHostReader(root=host, runner=runner).realtime().preemption == (
        "PREEMPT_DYNAMIC"
    )


def test_hugepages_are_read_machine_wide_and_per_numa_node(
    reader: LocalHostReader,
) -> None:
    # Both, because a guest pinned to one socket draws from that socket's pool:
    # the right total with nothing on the node the guest sits on fails to start
    # while the total looks correct.
    pools = reader.realtime().hugepages

    assert (1048576, None, 8) in [(p.size_kb, p.node, p.total) for p in pools]
    assert (1048576, 0, 8) in [(p.size_kb, p.node, p.total) for p in pools]
    assert (2048, None, 0) in [(p.size_kb, p.node, p.total) for p in pools]


def test_the_bracketed_sysfs_choice_is_the_value_that_is_read(
    reader: LocalHostReader,
) -> None:
    # `always madvise [never]` is a list with the selection in brackets, and
    # reporting the whole line would make every comparison against it wrong.
    realtime = reader.realtime()

    assert realtime.transparent_hugepages == "never"
    assert realtime.transparent_hugepage_defrag == "never"


def test_an_interrupt_allowed_on_an_isolated_cpu_is_found_and_named(
    reader: LocalHostReader,
) -> None:
    realtime = reader.realtime()

    assert realtime.irq_count == 2
    assert [
        (irq.number, irq.name, irq.cpus) for irq in realtime.irqs_on_isolated_cpus
    ] == [("34", "ahci0", [4])]


def test_no_interrupt_is_reported_when_none_reaches_the_isolated_set(
    host: Path, runner: FakeCommandRunner
) -> None:
    (host / "proc/irq/34/smp_affinity_list").write_text("0-3\n")

    realtime = LocalHostReader(root=host, runner=runner).realtime()

    assert realtime.irq_count == 2
    assert realtime.irqs_on_isolated_cpus == []


def test_a_machine_with_no_tuned_profile_says_so_rather_than_guessing(
    host: Path, runner: FakeCommandRunner
) -> None:
    (host / "etc/tuned/active_profile").unlink()

    realtime = LocalHostReader(root=host, runner=runner).realtime()

    assert realtime.tuned_profile is None
    assert any("configure_hypervisor" in warning for warning in realtime.warnings)
