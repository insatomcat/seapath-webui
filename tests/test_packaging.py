# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The quadlet and the image, checked for what a first deployment got wrong.

Nothing here can be caught by running the service: it is about what the
container is handed before Python starts. Each test corresponds to something
that had to be done by hand, or did not work, on a real machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
# Unfolded, because systemd continues a line with a trailing backslash and the
# directories are created by one command spread over three lines.
_QUADLET = (_ROOT / "seapath-webui.container").read_text().replace("\\\n", " ")
_DOCKERFILE = (_ROOT / "Dockerfile").read_text()

_VOLUMES = [
    line.split("=", 1)[1]
    for line in _QUADLET.splitlines()
    if line.startswith("Volume=")
]
_SOURCES = {volume.split(":", 1)[0] for volume in _VOLUMES}
_PRE_START = " ".join(
    line for line in _QUADLET.splitlines() if line.startswith("ExecStartPre=")
)


@pytest.mark.parametrize(
    "directory",
    [
        # The service's own state, needed by hand on the first real deployment.
        "/etc/seapath/webui",
        "/etc/seapath/inventory",
        "/var/lib/seapath-webui",
        # Absent from a machine that has never converged, and a missing bind
        # mount source is a container that does not start.
        "/etc/corosync",
        "/etc/ceph",
    ],
)
def test_a_mount_source_that_may_be_absent_is_created_by_the_unit(
    directory: str,
) -> None:
    # Dropping the quadlet on a machine and starting the unit has to be enough.
    assert directory in _PRE_START
    assert directory in _SOURCES


@pytest.mark.parametrize(
    "absent",
    [
        # The bus and the private socket, which is what `systemctl` needs. Two
        # deployments were spent making that route work from a container, and
        # the reading it served is one prometheus-node-exporter already
        # publishes on every node.
        "/run/systemd",
        "/run/systemd/system",
        "/run/dbus",
        # The journal, and the machine-id journalctl needs to find it.
        "/var/log",
        "/run/log",
        "/etc/machine-id",
        # Read by nothing once the observation plane moved out.
        "/etc/tuned",
        "/run/tuned",
        "/var/lib/pacemaker",
    ],
)
def test_the_container_is_given_no_route_to_the_live_state(absent: str) -> None:
    # Live state is the exporter's job. Bringing one of these back is a design
    # decision, not a convenience, so it fails here first. See
    # docs/deployment.md.
    assert absent not in _SOURCES
    assert absent not in _PRE_START


def test_the_persistent_journal_is_never_created_by_this_service() -> None:
    # Creating /var/log/journal is precisely what switches journald from a
    # volatile journal to a persistent one. Changing how the machine logs is
    # not a side effect this service may have, and it no longer reads the
    # journal at all.
    assert "/var/log/journal" not in _SOURCES
    assert "/var/log/journal" not in _PRE_START


def test_the_host_accounts_are_read_through_a_directory_not_three_files() -> None:
    # `usermod` and `passwd` rename a new file over the old one, so a bind
    # mount of the file pins the inode the container started with: an operator
    # added to seapath-admin stayed locked out until the service was restarted.
    for pinned in ("/etc/passwd", "/etc/group", "/etc/shadow"):
        assert pinned not in _SOURCES
        assert f"ln -sf /run/host/etc/{Path(pinned).name} {pinned}" in _DOCKERFILE
    assert "/etc:/run/host/etc:ro" in _VOLUMES


def test_the_ansible_account_home_is_never_created_by_the_unit() -> None:
    # The service does not create the account, and inventing a home directory
    # for a user nobody created is a second problem, not a recovery. A machine
    # missing it was not installed from the SEAPATH ISO.
    assert "/home/ansible/.ssh" in _SOURCES
    assert "/home/ansible" not in _PRE_START


def test_the_controller_dependencies_are_all_in_one_file() -> None:
    # A service deployed from a source checkout installs requirements.txt and
    # nothing else. netaddr used to be a separate pip install in two Dockerfile
    # stages, so the image had it and a checkout did not, and
    # seapath_setup_network failed on the controller with no obvious cause.
    requirements = (_ROOT / "requirements.txt").read_text()
    dockerfile = (_ROOT / "Dockerfile").read_text()

    assert "netaddr==" in requirements
    assert "pip install --no-cache-dir netaddr" not in dockerfile

    # jmespath is the same kind of dependency, pulled in by the json_query
    # filter roles/cephadm uses to read the ceph-volume inventory.
    assert "jmespath==" in requirements
    assert "pip install --no-cache-dir jmespath" not in dockerfile


def test_the_listen_socket_is_never_the_wildcard() -> None:
    # A hypervisor also sits on the networks carrying sampled values and the
    # storage traffic, and this UI has no business answering on those. The
    # quadlet ships the setting active so a fresh ISO resolves the
    # administration address instead of falling back to every address.
    setting = "Environment=SEAPATH_WEBUI_BIND_ADDRESS="
    active = [line for line in _QUADLET.splitlines() if line.startswith(setting)]
    assert active == [setting + "auto"]


def test_the_image_reference_is_pinned_to_this_version() -> None:
    # `latest` on a substation hypervisor means the machine cannot say which
    # code is answering on it, and a run identified by "inventory commit,
    # collection version" is only half an answer if the service itself is
    # unnamed. Releasing is then: bump __version__, build, install the quadlet.
    from app import __version__

    references = [
        line.split("=", 1)[1]
        for line in _QUADLET.splitlines()
        if line.startswith("Image=")
    ]
    assert len(references) == 1
    repository, _, tag = references[0].rpartition(":")
    assert repository and tag != "latest"
    assert tag == __version__

    # And the build publishes that tag, without it being written twice.
    buildpush = (_ROOT / "buildpush.sh").read_text()
    assert "app/__init__.py" in buildpush


def test_the_site_collection_rides_in_the_state_volume() -> None:
    # A collection installed on the node has to reach the container, and the
    # cheapest way to reach it is to need no new mount at all. D23 puts it
    # under the state volume for exactly that reason, so the day the installer
    # lands there is nothing to add to the quadlet and nothing new to review.
    from app.core.settings import Settings

    site = Settings().site_collections_dir
    assert str(site) not in _SOURCES
    assert any(
        site.is_relative_to(Path(source)) for source in _SOURCES
    ), f"{site} is under no mount the quadlet gives the container"
