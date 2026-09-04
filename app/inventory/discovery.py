# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Hardware discovery, so the operator starts from a filled form.

Discovery **proposes, it never decides**. Every value here is presented as a
prefilled field the operator confirms, because a NIC that is up is not
necessarily the NIC that carries sampled values, and a disk that is empty today
is not necessarily one the site wants an OSD on tomorrow.

Nothing here is ever committed automatically. The seed inventory is written
once, at first boot, and is a starting point rather than a decision.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app import __version__
from app.hosts.models import NodeMode
from app.hosts.reader import HostReader
from app.inventory.model import (
    WEBUI_IMAGE_VARIABLE,
    Inventory,
    Mode,
    NodeConfig,
)

# A machine freshly installed from the ISO has no isolation yet, so the
# proposal comes from the topology rather than from the kernel command line.
# Reserving the first physical core for housekeeping is the shape every SEAPATH
# reference deployment uses.
_HOUSEKEEPING_CPUS = 4

# The administration account the reference inventories name, used only when
# the machine could not be asked which account it actually has.
_DEFAULT_ADMIN_USER = "admin"

# The tag that moves. The ISO installs it, and the seed resolves it to the
# version answering rather than writing it into the inventory as a version.
_MOVING_TAG = "latest"


class InterfaceCandidate(BaseModel):
    name: str
    mac: str | None = None
    operstate: str | None = None
    speed_mbps: int | None = None
    driver: str | None = None
    carries_default_route: bool = False
    ptp_capable: bool = False
    addresses: list[str] = Field(default_factory=list)


class DiskCandidate(BaseModel):
    path: str
    by_path: str | None = None
    size_bytes: int | None = None
    model: str | None = None
    claimed: bool | None = None
    claim_reason: str | None = None


class Discovery(BaseModel):
    """What this node observes about itself, as proposals."""

    hostname: str
    mode: NodeMode
    proposed: NodeConfig | None = None
    interfaces: list[InterfaceCandidate] = Field(default_factory=list)
    disks: list[DiskCandidate] = Field(default_factory=list)
    cpu_count: int | None = None
    isolated_now: list[int] = Field(default_factory=list)
    service_image: str | None = None
    warnings: list[str] = Field(default_factory=list)


def discover(reader: HostReader) -> Discovery:
    identity = reader.node_identity()
    network = reader.network()
    cpu = reader.cpu()
    disks = reader.disks()

    warnings = [*identity.warnings, *network.warnings, *cpu.warnings, *disks.warnings]

    ptp_devices = {clock.device for clock in reader.ptp_clocks()}
    interfaces = [
        InterfaceCandidate(
            name=item.name,
            mac=item.mac,
            operstate=item.operstate,
            speed_mbps=item.speed_mbps,
            driver=item.driver,
            carries_default_route=item.name == network.default_route_interface,
            # A hardware clock exists on the machine, which says PTP is
            # possible here, not that this interface is the one carrying it.
            ptp_capable=bool(ptp_devices) and item.kind == "physical",
            addresses=[address.address for address in item.addresses],
        )
        for item in network.interfaces
        if item.kind != "loopback"
    ]

    admin = next(
        (item for item in interfaces if item.carries_default_route),
        None,
    )
    address = _first_ipv4(admin.addresses) if admin else None
    prefix = _prefix_length(network, admin.name) if admin else None

    proposed = None
    if admin and address:
        proposed = NodeConfig(
            ansible_host=address,
            network_interface=admin.name,
            subnet=prefix or 24,
            gateway_addr=network.default_gateway,
            dns_servers=[],
            # Never guessed. Which NIC receives sampled values is a cabling
            # fact this machine cannot observe.
            ptp_interface=None,
            ntp_servers=[],
            # The account the installer made. Every package manager
            # distribution needs this variable, and the prerequisites run
            # stops on its first task without it. The name comes from the
            # machine because `configure_seapath_distro` deletes the account
            # holding UID 1000 when `admin_user` names another one.
            admin_user=identity.admin_account or _DEFAULT_ADMIN_USER,
            isolcpus=_propose_isolation(cpu.isolated, cpu.online),
        )
        if not identity.admin_account:
            warnings.append(
                f"The administration account was set to {_DEFAULT_ADMIN_USER!r}, "
                "the name the reference inventories use. Check it before "
                "applying the prerequisites, which removes the account holding "
                "UID 1000 when it is named differently."
            )
    else:
        warnings.append(
            "No interface carries the default route, so the administration "
            "address could not be proposed. Fill it in from the form."
        )

    return Discovery(
        hostname=identity.hostname,
        mode=identity.mode,
        proposed=proposed,
        interfaces=interfaces,
        disks=[
            DiskCandidate(
                path=device.path,
                by_path=device.by_path,
                size_bytes=device.size_bytes,
                model=device.model,
                claimed=device.claimed,
                claim_reason=device.claim_reason,
            )
            for device in disks.devices
        ],
        cpu_count=cpu.online,
        isolated_now=cpu.isolated,
        service_image=identity.service_image,
        warnings=warnings,
    )


def seed_inventory(discovery: Discovery) -> Inventory | None:
    """The minimal inventory a node writes about itself at first boot.

    Returns None when the machine could not describe itself, which is better
    than a file full of placeholders that look like decisions.
    """
    if discovery.proposed is None:
        return None
    node = discovery.proposed.model_copy(deep=True)
    image = _pinned_image(discovery.service_image)
    if image:
        node.extra[WEBUI_IMAGE_VARIABLE] = image
    return Inventory(mode=Mode.STANDALONE, hosts={discovery.hostname: node})


def _pinned_image(reference: str | None) -> str | None:
    """The image reference to seed, from the one the machine boots on.

    The repository comes from the machine, because a site builds and hosts its
    own. The tag is this service's version whenever the machine boots on a
    moving one, which is what the ISO installs: `latest` seeded as `latest`
    would name no version, and the point of writing this variable is that the
    inventory says which code a machine is meant to run. The version answering
    here is the one that image resolved to, and it is published as a tag of its
    own, once, so the pin names an image that exists.

    A reference already naming an exact tag or a digest is a decision somebody
    made, and it is seeded as it stands.
    """
    if reference is None:
        return None
    reference = reference.strip()
    if not reference or "@" in reference:
        return reference or None
    repository, separator, tag = reference.rpartition(":")
    # A colon that comes before the last slash is a registry port, so what it
    # separates is not a tag and the reference carries none.
    if not separator or "/" in tag:
        return f"{reference}:{__version__}"
    if tag == _MOVING_TAG:
        return f"{repository}:{__version__}"
    return reference


def _propose_isolation(isolated_now: list[int], online: int | None) -> str | None:
    """The isolated set, observed if the machine already has one, else proposed.

    A machine installed from the ISO has not had any isolation applied, so the
    proposal comes from the topology. It is a proposal: the operator confirms
    it behind the expert section, and the UI never makes an RT relevant change
    look routine.
    """
    if isolated_now:
        return _as_cpu_list(isolated_now)
    if not online or online <= _HOUSEKEEPING_CPUS:
        return None
    return f"{_HOUSEKEEPING_CPUS}-{online - 1}"


def _as_cpu_list(cpus: list[int]) -> str:
    """Render a CPU set in the kernel's own range syntax."""
    ranges: list[str] = []
    start = previous = cpus[0]
    for cpu in cpus[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        ranges.append(_range(start, previous))
        start = previous = cpu
    ranges.append(_range(start, previous))
    return ",".join(ranges)


def _range(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def _first_ipv4(addresses: list[str]) -> str | None:
    for address in addresses:
        if ":" not in address:
            return address
    return None


def _prefix_length(network, interface_name: str) -> int | None:
    for item in network.interfaces:
        if item.name != interface_name:
            continue
        for address in item.addresses:
            if address.family == "inet" and address.prefix_length:
                return address.prefix_length
    return None
