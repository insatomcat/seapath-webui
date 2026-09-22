# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The libvirt domain of a standalone guest, read and defined again.

What `virsh edit` does, split in the two halves this service may do: the
reading is one `ssh` and one `virsh dumpxml` over the path a run takes, and the
writing is a run of the module `deploy_vms_standalone` defines domains with.
[D66](../../docs/decisions.md) has the reasoning and the bounds.

The gap it covers is the one D31 covers for a cluster guest. The standalone
role defines a domain once, from `vm_template`, and skips every guest libvirt
already has unless its entry carries `force`, which copies the disk image again
and so loses whatever the guest wrote. An existing guest whose domain has to
change without losing its disk had no path at all.

Defining a domain changes its persistent configuration and leaves the running
one alone: libvirt applies it at the next start from shut off. So the page
offers the shut down and start that applies it as an act of its own, only once
a definition moved something.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from xml.etree import ElementTree

import yaml
from pydantic import BaseModel

from app.hosts.remote import RemoteRefused, RemoteRequest, RemoteRunner
from app.inventory.model import Mode
from app.inventory.service import InventoryService
from app.runs.actions import GENERATOR
from app.runs.catalogue import PlaybookEntry, Precondition, Preview, Reboots
from app.runs.models import RunRecord
from app.runs.service import RunPaths

logger = logging.getLogger(__name__)

#: What a domain may weigh here. A domain with a dozen disks and interfaces is
#: a few kilobytes; this takes any of them and refuses a file pasted by mistake.
MAX_XML_BYTES = 256 * 1024

RECORD = "vm_define"


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


def define_entry(guest: str, host: str) -> PlaybookEntry:
    return PlaybookEntry(
        id=RECORD,
        playbook=f"{GENERATOR}.{RECORD}",
        title=f"Define {guest} again on {host}",
        targets=[host],
        preview=Preview.NONE,
        reboots=Reboots.NO,
        disruption=(
            f"Replaces the persistent definition of {guest} on {host} with the "
            "XML edited here. The guest keeps running as it was started: "
            "libvirt applies a definition at the next start from shut off, "
            "which is the shut down and start offered once this ends. The "
            "inventory is not changed, so a guest created again from its "
            "entry gets the domain its template renders."
        ),
        requires=[
            Precondition.INVENTORY_VALID,
            Precondition.SELF_TRUST,
            Precondition.PEER_REACHABLE,
        ],
        reviewed=True,
    )


def define_play(guest: str, host: str, xml: str) -> str:
    """One task, the module and the command `deploy_vms_standalone` defines with.

    Dumped rather than templated, so nothing in the XML becomes YAML of its own,
    and written into the run's staged tree, so the definition sent is part of
    the run's record.
    """
    document = [
        {
            "name": f"Define {guest} again on {host}",
            "hosts": host,
            "gather_facts": False,
            "become": True,
            "tasks": [
                {
                    "name": f"Define {guest} from the edited XML",
                    "community.libvirt.virt": {"command": "define", "xml": xml},
                }
            ],
        }
    ]
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

    def define(self, guest: str, xml: str, author: str) -> RunRecord | None:
        """Launch the run that defines the domain, or `None` if nothing moved.

        The domain is read again first, rather than trusted from the page: what
        the definition is checked against, and compared with, is what libvirt
        holds now.
        """
        current = self.read(guest)
        definition = checked(guest, xml, current.xml)
        if _same(definition, current.xml):
            return None
        if self._launch is None:
            raise NoDomain("This service has no run path to define it with.")
        return self._launch(
            define_entry(guest, current.host),
            author,
            define_play(guest, current.host, definition),
            guest,
        )

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
