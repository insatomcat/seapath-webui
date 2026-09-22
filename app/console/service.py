# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""What the console is allowed to be, decided in one place.

The adapter knows how to open a terminal. This decides whether it may be
opened at all, and where: the console can be turned off, it is capped so a
forgotten tab cannot exhaust an sshd, and it refuses to try when the trust it
would use has not been provisioned, because "Permission denied (publickey)"
inside a terminal is a worse answer than saying so before opening one.

The account is the `ansible` one, with the keys this node already holds. That
is a deliberate consequence rather than an oversight: this service has no other
credential, and inventing one, a per operator key or a password path, would be
a second trust story to secure, replicate and revoke. What it means in practice
is that the console gives what the configuration plane already gives, which is
root through `sudo`, so `console_min_role` exists for a site that wants the
button to require more than reading.

Where a console goes is a name, never an address. The browser asks for an
entry of the inventory, and the address is the `ansible_host` a run would
connect to. A console that accepted an address would be an ssh relay to
anything the administration network routes, carrying the site key; one that
accepts a name reaches exactly the machines and guests a run reaches. See D51.

A guest's serial console is the same connection with one fixed command at the
end of it: `vm-mgr console`, as root, on a machine that can reach the guest's
libvirt. `vm_manager` finds the hypervisor through Pacemaker and reaches it as
`libvirtadmin`, with the root key `add_libvirtadmin_user` provisioned, so this
service reimplements none of it and adds no trust. See D52. A standalone guest
has no Pacemaker to ask, and is attached with `virsh console` on its machine.

A guest's graphic console is that connection again, to the hypervisor that runs
the guest, with a relay at the end of it: the VNC server listens on that
machine's loopback, and the hardening role turns ssh forwarding off, so a short
program run as root asks `virsh` for the display and copies bytes between it
and the ssh. See D62.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from app.console.adapter import (
    ConsoleAdapter,
    ConsoleProcess,
    ConsoleRequest,
    StreamProcess,
)
from app.core.auth import Role
from app.inventory.model import Inventory, Mode
from app.inventory.service import InventoryState
from app.trust import known_hosts

logger = logging.getLogger(__name__)

_MIN_COLUMNS, _MAX_COLUMNS = 20, 500
_MIN_LINES, _MAX_LINES = 5, 200


class ConsoleUnavailable(Exception):
    """The console cannot be opened, with a code the front end can branch on."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class TargetKind(str, Enum):
    THIS_MACHINE = "this_machine"
    MACHINE = "machine"
    GUEST = "guest"


class ConsoleTarget(BaseModel):
    """One place a console can be opened on."""

    name: str
    kind: TargetKind
    address: str
    host_key_known: bool
    """Whether `known_hosts` holds a key for the address.

    The console checks host keys strictly and never learns one, so an address
    nobody accepted a key for is listed and refused until somebody does.
    """


class ConsoleInfo(BaseModel):
    """What `GET /node/console` answers, so the page knows what to offer."""

    enabled: bool
    user: str
    target: str
    required_role: Role
    idle_timeout_seconds: int
    max_sessions: int
    active_sessions: int
    targets: list[ConsoleTarget]


@dataclass(frozen=True)
class OpenedConsole:
    process: ConsoleProcess
    target: ConsoleTarget
    guest: str | None = None
    """The guest whose serial console this is, reached through `target`."""


@dataclass(frozen=True)
class OpenedDisplay:
    stream: StreamProcess
    target: ConsoleTarget
    guest: str


def serial_command(guest: str, cluster: bool = True) -> str:
    """What the far end runs for a guest's serial console, quoted once per shell.

    `sudo /bin/sh -c` because that is the whole of the rule the ISO grants the
    `ansible` account, and `-n` so a machine where it is missing answers with an
    error rather than a password prompt. `exec` so that `virsh` leaving, on
    `Ctrl+]` or when the guest's console goes away, ends the session with no
    shell left behind it on the hypervisor.

    A standalone guest is attached with `virsh` on its own machine, which is
    the call `vm_manager` makes in its libvirt mode. `vm-mgr` picks that mode
    only where the Ceph and Pacemaker bindings fail to import, and the Debian
    ISO installs them on every machine, standalone included: there it asked
    `crm_mon` for a cluster that does not exist, and the console never opened.
    """
    if cluster:
        inner = f"exec vm-mgr console {shlex.quote(guest)}"
    else:
        inner = f"exec virsh -c qemu:///system console {shlex.quote(guest)}"
    return f"sudo -n /bin/sh -c {shlex.quote(inner)}"


# What runs on the hypervisor for a graphic console, the whole of it. It asks
# libvirt for the domain's VNC display, which also says whether the running
# domain has one at all, connects to it, and copies bytes between the display
# and the ssh until either side ends. Written for the `python3` Ansible
# already needs on every machine, with nothing imported beyond its standard
# library. `virsh` prints `vnc://127.0.0.1:0`, the display number, and names
# a server listening on every address `localhost`, which is reached on the
# loopback like the default one. Exit 3 is "no display", with virsh's reason.
RELAY = r"""
import os, re, select, socket, subprocess, sys
shown = subprocess.run(["virsh", "domdisplay", "--type", "vnc", sys.argv[1]],
                       capture_output=True, text=True)
found = re.fullmatch(r"vnc://(\[[^\]]*\]|[^:/]*):(\d+)/?", shown.stdout.strip())
if found is None:
    sys.stderr.write((shown.stderr.strip() or "the domain has no VNC display") + "\n")
    sys.exit(3)
host = found.group(1).strip("[]")
if host in ("", "localhost", "0.0.0.0", "::"):
    host = "127.0.0.1"
port = 5900 + int(found.group(2))
try:
    display = socket.create_connection((host, port), 10)
except OSError as error:
    sys.stderr.write(f"cannot reach the VNC display at {host}:{port}: {error}\n")
    sys.exit(4)
while True:
    ready = select.select([0, display], [], [])[0]
    if 0 in ready:
        data = os.read(0, 65536)
        if not data:
            break
        display.sendall(data)
    if display in ready:
        data = display.recv(65536)
        if not data:
            break
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]
"""


def graphic_command(guest: str) -> str:
    """What the far end runs for a guest's graphic console, quoted once per shell.

    The same rule as `serial_command`, with the relay in place of `vm-mgr`.
    `-I` keeps the environment and the working directory from deciding what
    `python3` imports, since it runs as root.
    """
    inner = f"exec python3 -I -c {shlex.quote(RELAY)} {shlex.quote(guest)}"
    return f"sudo -n /bin/sh -c {shlex.quote(inner)}"


class ConsoleService:
    def __init__(
        self,
        adapter: ConsoleAdapter,
        *,
        target: str,
        user: str,
        private_key_file: Path,
        known_hosts_file: Path,
        hostname: str = "",
        inventory: Callable[[], InventoryState] | None = None,
        extra_key_files: Callable[[], tuple[Path, ...]] = tuple,
        locate: Callable[[str], str | None] | None = None,
        enabled: bool = True,
        required_role: Role = Role.VIEWER,
        max_sessions: int = 4,
        idle_timeout_seconds: int = 900,
    ) -> None:
        self._adapter = adapter
        self._target = target
        self._user = user
        self._private_key_file = private_key_file
        self._known_hosts_file = known_hosts_file
        self._hostname = hostname
        self._inventory = inventory
        self._extra_key_files = extra_key_files
        self._locate = locate
        self._enabled = enabled
        self._required_role = required_role
        self._max_sessions = max_sessions
        self._idle_timeout = idle_timeout_seconds
        self._active = 0

    @property
    def required_role(self) -> Role:
        return self._required_role

    @property
    def idle_timeout_seconds(self) -> int:
        return self._idle_timeout

    @property
    def user(self) -> str:
        return self._user

    def info(self) -> ConsoleInfo:
        return ConsoleInfo(
            enabled=self._enabled,
            user=self._user,
            target=self._target,
            required_role=self._required_role,
            idle_timeout_seconds=self._idle_timeout,
            max_sessions=self._max_sessions,
            active_sessions=self._active,
            targets=self.targets(),
        )

    def targets(self) -> list[ConsoleTarget]:
        """This machine first, then the machines and the guests a run reaches.

        Read from the inventory at each call, so a guest declared a minute ago
        is offered and a machine taken out of the file is not. An entry with
        no `ansible_host` is left out rather than reached by its name: a run
        would ask the resolver, and a console that did the same would land
        wherever DNS says today, checked against a key recorded for an address.
        """
        state = self._inventory() if self._inventory is not None else None
        inventory = state.inventory if state is not None else None
        this_host = (state.this_host if state is not None else None) or self._hostname

        # The loopback, as before: the startup records its key from the host's
        # own filesystem at every start, so there is nothing to check here.
        targets = [
            ConsoleTarget(
                name=this_host,
                kind=TargetKind.THIS_MACHINE,
                address=self._target,
                host_key_known=True,
            )
        ]
        if inventory is None:
            return targets

        recorded = known_hosts.recorded_names(self._known_hosts_file)
        seen = {this_host}
        declared = [
            (name, TargetKind.MACHINE, node.ansible_host)
            for name, node in inventory.hosts.items()
        ] + [
            (name, TargetKind.GUEST, guest.ansible_host)
            for name, guest in inventory.guests.items()
        ]
        for name, kind, address in declared:
            address = (address or "").strip()
            # A templated address is resolved by Ansible and by nothing here.
            if name in seen or not address or "{{" in address:
                continue
            seen.add(name)
            targets.append(
                ConsoleTarget(
                    name=name,
                    kind=kind,
                    address=address,
                    host_key_known=address in recorded,
                )
            )
        return targets

    def resolve(self, name: str | None) -> ConsoleTarget:
        targets = self.targets()
        if not name:
            return targets[0]
        for target in targets:
            if target.name == name:
                return target
        raise ConsoleUnavailable(
            "unknown_host",
            f"{name} is not a machine or a guest of the inventory with an "
            "ansible_host, so no console can be opened on it.",
            status=404,
        )

    def _guest_inventory(self, guest: str, what: str) -> Inventory:
        state = self._inventory() if self._inventory is not None else None
        inventory = state.inventory if state is not None else None
        if inventory is None or guest not in inventory.guests:
            raise ConsoleUnavailable(
                "unknown_guest",
                f"{guest} is not a guest of the inventory, so no {what} "
                "can be opened on it.",
                status=404,
            )
        return inventory

    def _standalone_candidates(
        self, inventory: Inventory, guest: str, this_machine: str
    ) -> list[str]:
        """The machine a standalone guest is on, or the one it is tried on.

        The one whose libvirt exporter reports its domain; when nothing
        reports it, the one standalone machine if there is one, and this
        machine if it is among several. Picking another would open a console
        on a guest that is not there, which reads like a guest that is broken.
        """
        standalone = [
            name
            for name in inventory.hypervisors()
            if name not in inventory.cluster_members
        ]
        located = self._located(guest)
        if located in standalone:
            return [located]
        if len(standalone) == 1:
            return standalone
        if this_machine in standalone:
            return [this_machine]
        return []

    def _reachable(
        self, targets: list[ConsoleTarget], candidates: list[str]
    ) -> ConsoleTarget | None:
        this_machine = targets[0].name
        by_name = {target.name: target for target in targets}
        for name in sorted(candidates, key=lambda name: name != this_machine):
            target = by_name.get(name)
            if target is not None and target.host_key_known:
                return target
        return None

    def graphic_route(self, guest: str) -> ConsoleTarget:
        """The machine a guest's graphic console is opened on.

        The one running the guest, since its VNC server listens on that
        machine's loopback and nowhere else: where Pacemaker or libvirt
        reports it. A cluster guest nothing reports has no machine to go to,
        and every member but one would only say it has no such domain.
        """
        inventory = self._guest_inventory(guest, "graphic console")
        targets = self.targets()
        if inventory.deployment_of(guest) is Mode.CLUSTER:
            located = self._located(guest)
            candidates = [located] if located else []
        else:
            candidates = self._standalone_candidates(inventory, guest, targets[0].name)
        target = self._reachable(targets, candidates)
        if target is not None:
            return target
        raise ConsoleUnavailable(
            "no_hypervisor",
            (
                f"{guest} runs on {candidates[0]}, and this node cannot reach it: "
                "it has no ansible_host with an accepted host key."
                if candidates
                else f"Nothing reports {guest} running on any machine, so there "
                "is no display to open. Start it first."
            ),
        )

    def serial_route(self, guest: str) -> tuple[ConsoleTarget, str]:
        """The machine a guest's serial console is opened from, and what it runs.

        A cluster guest from a hypervisor of the cluster, this one when it is
        one: `vm-mgr` asks Pacemaker where the guest runs and goes there
        itself, so any of them will do. A standalone guest from the machine
        whose libvirt exporter reports its domain, since `vm-mgr` there only
        knows its own libvirt; when nothing reports it, the one standalone
        machine if there is one, and this machine if it is among several.
        """
        inventory = self._guest_inventory(guest, "serial console")
        targets = self.targets()
        cluster = inventory.deployment_of(guest) is Mode.CLUSTER
        if cluster:
            candidates = inventory.placement_hosts()
        else:
            candidates = self._standalone_candidates(inventory, guest, targets[0].name)
        target = self._reachable(targets, candidates)
        if target is not None:
            return target, serial_command(guest, cluster)
        raise ConsoleUnavailable(
            "no_hypervisor",
            f"No machine this node can reach runs vm-mgr for {guest}: "
            + (
                "the candidates are " + ", ".join(candidates) + ", and none of "
                "them has an ansible_host with an accepted host key."
                if candidates
                else "no libvirt exporter reports its domain, and the inventory "
                "names no single machine it could be on."
            ),
        )

    def _located(self, guest: str) -> str | None:
        if self._locate is None:
            return None
        try:
            return self._locate(guest)
        except Exception as failure:  # noqa: BLE001 - a reading, never fatal
            logger.warning("Could not tell where %s runs: %s", guest, failure)
            return None

    async def open(
        self,
        username: str,
        columns: int,
        lines: int,
        host: str | None = None,
        serial: str | None = None,
    ) -> OpenedConsole:
        self._check_enabled()
        if serial:
            target, command = self.serial_route(serial)
        else:
            target, command = self.resolve(host), ""
        extra = self._admit(target)
        columns, lines = clamp_window(columns, lines)
        request = ConsoleRequest(
            address=target.address,
            user=self._user,
            private_key_file=self._private_key_file,
            known_hosts_file=self._known_hosts_file,
            extra_key_files=extra,
            command=command,
            columns=columns,
            lines=lines,
        )
        process = await self._adapter.open(request)
        self._active += 1
        # The audit line. `git log` records who changed the desired state, and
        # a run records who launched it; a shell records nothing at all, so the
        # only trace this node keeps of one is here and in the journal.
        logger.info(
            "Console opened by %s on %s, %s@%s (%d open)",
            username,
            (
                f"the serial console of {serial} through {target.name}"
                if serial
                else target.name
            ),
            self._user,
            target.address,
            self._active,
        )
        return OpenedConsole(process=process, target=target, guest=serial)

    async def open_graphic(self, username: str, guest: str) -> OpenedDisplay:
        """A byte stream to a guest's VNC display, through the relay.

        Counted with the terminals: what the limit protects is this node and
        the sshd at the other end, and a screen holds one of each.
        """
        self._check_enabled()
        target = self.graphic_route(guest)
        extra = self._admit(target)
        request = ConsoleRequest(
            address=target.address,
            user=self._user,
            private_key_file=self._private_key_file,
            known_hosts_file=self._known_hosts_file,
            extra_key_files=extra,
            command=graphic_command(guest),
            terminal=False,
        )
        stream = await self._adapter.open_stream(request)
        self._active += 1
        logger.info(
            "Console opened by %s on the graphic console of %s on %s, %s@%s "
            "(%d open)",
            username,
            guest,
            target.name,
            self._user,
            target.address,
            self._active,
        )
        return OpenedDisplay(stream=stream, target=target, guest=guest)

    async def close_graphic(self, opened: OpenedDisplay, username: str) -> None:
        code = await opened.stream.close()
        self._active = max(0, self._active - 1)
        logger.info(
            "Console of %s on the graphic console of %s closed, ssh exit %s "
            "(%d open)",
            username,
            opened.guest,
            "unknown" if code is None else code,
            self._active,
        )

    def _check_enabled(self) -> None:
        if not self._enabled:
            raise ConsoleUnavailable(
                "console_disabled",
                "The console is turned off on this node.",
            )

    def _admit(self, target: ConsoleTarget) -> tuple[Path, ...]:
        """Refuse what would fail inside the session, and give the keys to offer."""
        if self._active >= self._max_sessions:
            raise ConsoleUnavailable(
                "console_busy",
                f"There are already {self._active} consoles open on this node, "
                "which is the maximum. Close one and retry.",
            )
        if not self._private_key_file.exists():
            raise ConsoleUnavailable(
                "trust_missing",
                "This node has no key for the ansible account yet, so no "
                "console can be opened. The journal says why the self trust "
                "could not be provisioned.",
            )
        if not target.host_key_known:
            raise ConsoleUnavailable(
                "host_key_unknown",
                f"No host key is recorded for {target.name} at {target.address}. "
                "The console never accepts a key it has not seen. Accept it "
                "under Host keys on the Deployment page; a guest declared to "
                "accept its key on first use records it at the first run "
                "that reaches it.",
            )

        # This machine is reached over its self relation alone, which is the
        # one that carries `pty`. Any other gets the keys a run offers it, in
        # the same order.
        if target.kind is TargetKind.THIS_MACHINE:
            return ()
        return tuple(path for path in self._extra_key_files() if path.exists())

    async def close(self, opened: OpenedConsole, username: str) -> None:
        code = await opened.process.close()
        self._active = max(0, self._active - 1)
        logger.info(
            "Console of %s on %s closed, ssh exit %s (%d open)",
            username,
            (
                f"the serial console of {opened.guest}"
                if opened.guest
                else opened.target.name
            ),
            "unknown" if code is None else code,
            self._active,
        )


def clamp_window(columns: int, lines: int) -> tuple[int, int]:
    """A window size the far end can use.

    The size arrives from a browser and is handed to an ioctl, so it is bounded
    here rather than trusted. Both ends of the range are absurd on purpose:
    what is being refused is a zero, a negative or a number large enough to
    make a shell redraw the world.
    """
    return (
        _clamp(columns, _MIN_COLUMNS, _MAX_COLUMNS),
        _clamp(lines, _MIN_LINES, _MAX_LINES),
    )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))
