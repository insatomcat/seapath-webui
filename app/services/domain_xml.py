# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A standalone guest's domain and pinning profile, changed as one run each.

What `virsh edit` does, split in the two halves this service may do: the
reading is one `ssh` and one `virsh dumpxml` over the path a run takes, and the
writing is a run of the module `deploy_vms_standalone` defines domains with.
[D66](../../docs/decisions.md) has the reasoning and the bounds.

The gap it covers is the one D31 covers for a cluster guest. The standalone
role defines a domain once, from `vm_template`, and skips every guest libvirt
already has unless its entry carries `force`, which copies the disk image again
and so loses whatever the guest wrote. An existing guest whose domain has to
change without losing its disk had no path at all.

Its pinning profile is the inventory's, and reaches the machine by a play of
two tasks, the ones the standalone role writes the file with.

Both take effect when the guest starts from shut off, so each run can end with
the guest shut down and started, when the operator asks for it in the same
gesture. One run, whatever was chosen, followed or left in the background.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

import yaml
from pydantic import BaseModel

from app.hosts.remote import RemoteRefused, RemoteRequest, RemoteRunner
from app.inventory.model import Mode
from app.inventory.service import InventoryService
from app.runs.actions import GENERATOR, restart_tasks
from app.runs.catalogue import PlaybookEntry, Precondition, Preview, Reboots
from app.runs.models import RunRecord
from app.runs.service import RunPaths
from app.services.vms import has_vnc_display

logger = logging.getLogger(__name__)

#: What a domain may weigh here. A domain with a dozen disks and interfaces is
#: a few kilobytes; this takes any of them and refuses a file pasted by mistake.
MAX_XML_BYTES = 256 * 1024

#: What opens each domain in a reading of several, on a line of its own. A line
#: `virsh dumpxml` prints starts with `<` or with spaces, never with this.
DOMAIN_MARK = "#domain "

#: How many machines are asked at once for the graphic consoles, and how long
#: each is given to answer the connection: the page waits for the slowest.
_DISPLAY_READERS = 8
_DISPLAY_CONNECT_TIMEOUT = 5

DEFINE_RECORD = "vm_define"
PROFILE_RECORD = "seapath_alloc"


class InvalidDomain(Exception):
    """The XML cannot be defined, and the message says why."""


class NoDomain(Exception):
    """There is no domain to read or define for this guest, or no way to it."""


class DomainXml(BaseModel):
    """A guest's persistent definition, as libvirt holds it on its machine."""

    guest: str
    host: str
    """The machine holding the domain, which is where a definition goes."""
    xml: str


def dumpxml_command(guest: str) -> str:
    """The reading, as the far end's shell receives it.

    `--inactive` because that is what `virsh edit` edits and what a definition
    replaces: the running domain carries what libvirt added when it started it,
    live addresses and generated device names among them. No
    `--security-info`, so a VNC password in the domain stays on the machine.
    Through `sudo /bin/sh -c` for the reason the console gives: it is the whole
    of the rule the ISO grants the account.
    """
    inner = f"exec virsh -c qemu:///system dumpxml --inactive {shlex.quote(guest)}"
    return f"sudo -n /bin/sh -c {shlex.quote(inner)}"


def domains_command(guests: list[str]) -> str:
    """Several guests' domains in one reading, each after a line naming it.

    The running definition rather than `--inactive`, since whether a graphic
    console opens is decided by what runs. A guest libvirt does not hold
    prints its line and nothing under it, and `exit 0` keeps that from failing
    the reading of the others. No `--security-info`, as for `dumpxml_command`.
    """
    reads = [
        f"echo {shlex.quote(DOMAIN_MARK + guest)}; "
        f"virsh -c qemu:///system dumpxml {shlex.quote(guest)} 2>/dev/null"
        for guest in guests
    ]
    inner = "; ".join([*reads, "exit 0"])
    return f"sudo -n /bin/sh -c {shlex.quote(inner)}"


def split_domains(output: str) -> dict[str, str]:
    """What `domains_command` printed, by guest, empty for a guest with none."""
    found: dict[str, list[str]] = {}
    lines: list[str] | None = None
    for line in output.splitlines():
        if line.startswith(DOMAIN_MARK):
            lines = found.setdefault(line[len(DOMAIN_MARK) :], [])
        elif lines is not None:
            lines.append(line)
    return {guest: "\n".join(lines).strip() for guest, lines in found.items()}


def checked(guest: str, xml: str, current: str) -> str:
    """The definition to send, or the reason it would not be the same domain.

    `virsh edit` refuses the same things, later and less clearly: a definition
    naming another domain defines a second guest beside this one, and a
    different UUID under the same name is refused by libvirt itself.
    """
    if len(xml.encode()) > MAX_XML_BYTES:
        raise InvalidDomain(
            f"The domain is larger than {MAX_XML_BYTES // 1024} KB, which no "
            "libvirt domain is."
        )
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise InvalidDomain(f"The XML does not parse: {error}.") from error
    if root.tag != "domain":
        raise InvalidDomain(
            f"The document is a <{root.tag}>, and a libvirt domain is a <domain>."
        )
    name = (root.findtext("name") or "").strip()
    if name != guest:
        raise InvalidDomain(
            f"The domain is named {name or 'nothing'}, and this is {guest}. "
            "Defining it would create another guest beside this one rather "
            "than change it: a guest is renamed by declaring it again."
        )
    uuid = (root.findtext("uuid") or "").strip()
    held = (ElementTree.fromstring(current).findtext("uuid") or "").strip()
    if held and uuid != held:
        raise InvalidDomain(
            f"The domain's <uuid> is {uuid or 'missing'}, and libvirt holds "
            f"{guest} under {held}. Keep the one it holds."
        )
    return xml


def _entry(
    record: str, title: str, host: str, disruption: str, restart: bool
) -> PlaybookEntry:
    return PlaybookEntry(
        id=record,
        playbook=f"{GENERATOR}.{record}",
        title=title,
        targets=[host],
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=disruption + (_RESTART if restart else _NEXT_START),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        reviewed=True,
    )


_RESTART = (
    " Then the guest is shut down through ACPI and started again, which is "
    "what applies it: whatever it serves stops in between, and a guest that "
    "ignores ACPI for five minutes fails the run and keeps running."
)
_NEXT_START = (
    " The guest keeps running as it is, and takes the change the next time it "
    "starts from shut off. A reboot from inside it is not one."
)


def _play(guest: str, host: str, title: str, tasks: list[dict], restart: bool) -> str:
    """The generated play, dumped rather than templated, so nothing a value
    carries becomes YAML of its own. Written into the run's staged tree, so
    what was sent is part of the run's record."""
    document = [
        {
            "name": title,
            "hosts": host,
            "gather_facts": False,
            "become": True,
            "tasks": tasks + (restart_tasks(guest) if restart else []),
        }
    ]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def _title(what: str, guest: str, host: str, restart: bool) -> str:
    return f"{what} {guest} on {host}" + (", then restart it" if restart else "")


def define_entry(guest: str, host: str, restart: bool = False) -> PlaybookEntry:
    return _entry(
        DEFINE_RECORD,
        _title("Define", guest, host, restart),
        host,
        f"Replaces the persistent definition of {guest} on {host} with the XML "
        "edited here. The inventory is not changed, so a guest created again "
        "from its entry gets the domain its template renders.",
        restart,
    )


def define_play(guest: str, host: str, xml: str, restart: bool = False) -> str:
    """The module and the command `deploy_vms_standalone` defines with."""
    return _play(
        guest,
        host,
        _title("Define", guest, host, restart),
        [
            {
                "name": f"Define {guest} from the edited XML",
                "community.libvirt.virt": {"command": "define", "xml": xml},
            }
        ],
        restart,
    )


def profile_entry(guest: str, host: str, restart: bool = False) -> PlaybookEntry:
    return _entry(
        PROFILE_RECORD,
        _title("Write the pinning profile of", guest, host, restart),
        host,
        f"Writes the pinning profile the inventory gives {guest} to "
        f"/etc/seapath/alloc.d/{guest}.yaml on {host}, or removes that file "
        "when the entry names none. Nothing else on the machine is touched.",
        restart,
    )


def profile_play(guest: str, host: str, restart: bool = False) -> str:
    """The file `deploy_vms_standalone` writes, and nothing else it does.

    The two tasks are the role's own, on one guest: the content is read from
    the inventory at run time, `hostvars` of the committed entry, so what lands
    on the machine is what the role would write from the same file and a
    convergence afterwards changes nothing. The whole role, or the alloc one
    that writes the same tasks, also creates guests, starts them, or
    redeploys seapath-alloc, and none of that is what an operator changing
    one profile asked for. D66 records why this play exists.
    """
    variable = "hostvars[seapath_webui_guest].seapath_alloc"
    path = "/etc/seapath/alloc.d/{{ seapath_webui_guest }}.yaml"
    document = yaml.safe_load(
        _play(
            guest,
            host,
            _title("Write the pinning profile of", guest, host, restart),
            [
                {
                    "name": f"Write the pinning profile of {guest}",
                    "ansible.builtin.copy": {
                        "content": "{{ " + variable + " }}",
                        "dest": path,
                        "owner": "root",
                        "group": "root",
                        "mode": "0644",
                    },
                    "when": f"{variable} is defined",
                },
                {
                    "name": f"Remove the pinning profile of {guest}",
                    "ansible.builtin.file": {"path": path, "state": "absent"},
                    "when": f"{variable} is not defined",
                },
            ],
            restart,
        )
    )
    # The name travels as a play variable, so no template is built from it.
    document[0]["vars"] = {"seapath_webui_guest": guest}
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


class DomainXmlService:
    def __init__(
        self,
        inventory: InventoryService,
        remote: RemoteRunner,
        keys: RunPaths,
        ansible_user: str,
        locate: Callable[[str], str | None],
        launch: Callable[[PlaybookEntry, str, str, str], RunRecord] | None = None,
    ) -> None:
        self._inventory = inventory
        self._remote = remote
        self._keys = keys
        self._user = ansible_user
        self._locate = locate
        self._launch = launch

    def read(self, guest: str) -> DomainXml:
        host, address = self._machine(guest)
        try:
            xml = self._remote.run(
                RemoteRequest(
                    address=address,
                    user=self._user,
                    command=dumpxml_command(guest),
                    private_key_file=self._keys.private_key_file,
                    known_hosts_file=self._keys.known_hosts_file,
                    extra_key_files=self._keys.extra_key_files(),
                )
            )
        except RemoteRefused as error:
            raise NoDomain(
                f"{host} did not hand over {guest}'s domain: {error}"
            ) from error
        return DomainXml(guest=guest, host=host, xml=xml)

    def displays(self, located: dict[str, str]) -> dict[str, bool | None]:
        """Which standalone guests run with a VNC display, for the console button.

        `located` is where each guest's libvirt exporter reports its domain,
        which is where the console would open. One ssh per machine, asking
        for every guest it holds at once. `None` for the guests of a machine
        that did not answer, and a guest libvirt does not hold is left out.
        See D62.
        """
        inventory = self._inventory.state().inventory
        if inventory is None:
            return {}
        by_host: dict[str, list[str]] = {}
        for guest, host in located.items():
            if (
                guest in inventory.guests
                and inventory.deployment_of(guest) is not Mode.CLUSTER
                and host in inventory.hosts
                and host not in inventory.cluster_members
            ):
                by_host.setdefault(host, []).append(guest)
        if not by_host:
            return {}

        def read(host: str, guests: list[str]) -> dict[str, bool | None]:
            address = (inventory.hosts[host].ansible_host or "").strip()
            if not address or "{{" in address:
                return dict.fromkeys(guests)
            try:
                output = self._remote.run(
                    RemoteRequest(
                        address=address,
                        user=self._user,
                        command=domains_command(guests),
                        private_key_file=self._keys.private_key_file,
                        known_hosts_file=self._keys.known_hosts_file,
                        extra_key_files=self._keys.extra_key_files(),
                        connect_timeout=_DISPLAY_CONNECT_TIMEOUT,
                    )
                )
            except RemoteRefused as error:
                logger.info("Could not read the domains on %s: %s", host, error)
                return dict.fromkeys(guests)
            domains = split_domains(output)
            return {
                guest: has_vnc_display(domains[guest])
                for guest in guests
                if domains.get(guest)
            }

        found: dict[str, bool | None] = {}
        workers = min(_DISPLAY_READERS, len(by_host))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for answer in pool.map(read, by_host.keys(), by_host.values()):
                found.update(answer)
        return found

    def define(
        self, guest: str, xml: str, author: str, restart: bool = False
    ) -> RunRecord | None:
        """Launch the run that defines the domain, or `None` if nothing moved.

        The domain is read again first, rather than trusted from the page: what
        the definition is checked against, and compared with, is what libvirt
        holds now. `restart` ends the same run with the guest shut down and
        started, which is what applies it.
        """
        current = self.read(guest)
        definition = checked(guest, xml, current.xml)
        if _same(definition, current.xml):
            return None
        return self._run(
            define_entry(guest, current.host, restart),
            author,
            define_play(guest, current.host, definition, restart),
            guest,
        )

    def write_profile(
        self, guest: str, author: str, restart: bool = False
    ) -> RunRecord:
        """Launch the run that writes the committed profile to the machine."""
        host, _ = self._machine(guest)
        return self._run(
            profile_entry(guest, host, restart),
            author,
            profile_play(guest, host, restart),
            guest,
        )

    def _run(self, entry: PlaybookEntry, author: str, play: str, guest: str):
        if self._launch is None:
            raise NoDomain("This service has no run path to reach the machine.")
        return self._launch(entry, author, play, guest)

    def machine(self, guest: str) -> str | None:
        """The standalone machine holding the guest, when one can be named."""
        try:
            return self._machine(guest)[0]
        except NoDomain:
            return None

    def _machine(self, guest: str) -> tuple[str, str]:
        """The standalone machine holding the guest's domain, and its address.

        Where its libvirt exporter reports it, and otherwise the one standalone
        machine of the file, the console's rule for the same question. A
        cluster guest has no single machine: its domain follows Pacemaker, and
        its definition lives in the metadata of its image.
        """
        state = self._inventory.state()
        inventory = state.inventory
        if inventory is None or guest not in inventory.guests:
            raise NoDomain(f"No guest called {guest!r} is declared in this inventory.")
        if inventory.deployment_of(guest) is Mode.CLUSTER:
            raise NoDomain(
                f"{guest} is a cluster guest. Its domain is kept in the metadata "
                "of its image, under `xml`, which the Metadata window edits."
            )
        standalone = [
            name
            for name in inventory.hypervisors()
            if name not in inventory.cluster_members
        ]
        located = self._locate(guest)
        if located in standalone:
            host = located
        elif len(standalone) == 1:
            host = standalone[0]
        else:
            raise NoDomain(
                f"No libvirt exporter reports {guest}, and the inventory names "
                "no single machine it could be on."
            )
        address = (
            inventory.hosts[host].ansible_host if host in inventory.hosts else None
        )
        if not address:
            raise NoDomain(f"{host} has no ansible_host, so nothing here reaches it.")
        return host, address


def _same(one: str, other: str) -> bool:
    """Whether two definitions are the same text, give or take the whitespace
    around them, which is all a browser's textarea adds."""
    return one.strip() == other.strip()
