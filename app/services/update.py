# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Which version of this service the inventory asks for, and which one answers.

The other half of [D23](../../docs/decisions.md). The collection a node runs is
a file it can be handed; this service is a container image, and replacing it is
a change to the machine. So it is a variable in the inventory, `seapath_webui_image`,
applied by the same Ansible run as everything else. This module writes that
variable and nothing else: no file on a host, no unit, no restart.

What this module does is the part that belongs to a node: say which image the
inventory names for it, which version is answering, and whether the two agree.
An operator who edits the variable and never applies it has changed nothing,
and a UI that says "up to date" because it read the desired state would be
worse than silent.

The comparison is on the tag, because the tag follows `__version__` and a test
in `tests/test_packaging.py` holds them together. A reference pinned by digest
carries no readable version, so the answer is "cannot tell" rather than a guess.

Two more things belong to a node, and they are here for the same reason. Which
versions exist is a question the registry the reference already names can
answer, so an operator does not have to go and read a tag list in a browser.
And writing the one they chose is a commit in the inventory, like every other
decision about a machine: this module builds the candidate and hands it to the
inventory service, which validates, splices and commits it. Nothing here
restarts anything, and applying the pin is a run of the catalogue entry below,
confirmed the way every other convergence is.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app import __version__
from app.inventory.model import WEBUI_IMAGE_VARIABLE
from app.inventory.service import InventoryService
from app.services.registry import (
    TAG,
    RegistryUnreachable,
    TagSource,
    is_version,
    newest,
    repository_of,
    version_key,
)

# The inventory variable, and the catalogue entry that applies it. The variable
# is the schema's, so the seed that writes it at first boot and the reading
# here cannot drift apart.
IMAGE_VARIABLE = WEBUI_IMAGE_VARIABLE
PLAYBOOK = "seapath_setup_deploy_seapath_webui"


class ServiceUpdate(BaseModel):
    """What `GET /api/v1/node/update` answers."""

    running: str = Field(description="The version answering this request")
    image: str | None = Field(
        default=None, description="The reference the inventory names for this node"
    )
    wanted: str | None = Field(
        default=None, description="Its tag, where the reference carries a readable one"
    )
    pending: bool = Field(
        default=False, description="Whether an apply would replace this service"
    )
    variable: str = IMAGE_VARIABLE
    playbook: str = PLAYBOOK
    # Why there is nothing to say, when there is nothing to say. A node absent
    # from its own inventory and a node whose image is pinned by digest are
    # different situations, and both look like "no update" from outside.
    reason: str | None = None


class AvailableRelease(BaseModel):
    """What `GET /api/v1/node/update/latest` answers.

    A reading, in the strong sense: asking a registry which tags it holds
    changes nothing here and nothing on a machine.
    """

    repository: str | None = Field(
        default=None, description="The repository that was asked, without its tag"
    )
    latest: str | None = Field(
        default=None, description="The highest version the registry holds"
    )
    pinned: str | None = Field(
        default=None, description="The version the inventory names today"
    )
    newer: bool = Field(
        default=False,
        description="Whether the registry holds a version above the pinned one",
    )
    machines: list[str] = Field(
        default_factory=list,
        description="The machines a pin would rewrite the image of",
    )
    reason: str | None = None
    """Why there is no answer, when there is none. Never a stack trace."""


class Pinned(BaseModel):
    """What `POST /api/v1/node/update` answers: a commit, and what to do next."""

    commit: str | None
    version: str
    image: str
    machines: list[str]
    playbook: str = PLAYBOOK
    """Applying it is a run like any other, confirmed like any other."""


class PinRefused(Exception):
    """The version cannot be written, with the reason as a sentence."""

    def __init__(self, message: str, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class UpdateService:
    def __init__(
        self,
        inventory: InventoryService,
        running: str = __version__,
        tags: TagSource | None = None,
    ) -> None:
        self._inventory = inventory
        self._running = running
        self._tags = tags

    def state(self) -> ServiceUpdate:
        image = self._wanted_image()
        if image is None:
            return ServiceUpdate(
                running=self._running,
                reason=(
                    f"The inventory names no {IMAGE_VARIABLE} for this machine, "
                    "so an apply leaves this service as it is."
                ),
            )
        tag = _tag(image)
        if tag is None:
            return ServiceUpdate(
                running=self._running,
                image=image,
                reason=(
                    "This image is named by digest, which carries no version to "
                    "compare against."
                ),
            )
        return ServiceUpdate(
            running=self._running,
            image=image,
            wanted=tag,
            pending=tag != self._running,
        )

    def available(self) -> AvailableRelease:
        """The highest version the registry holds for the image this node names.

        The repository comes from the inventory rather than from a constant: a
        site builds and hosts its own image, and the question "is there a newer
        one" is only meaningful about the repository the machines actually
        pull from.
        """
        machines = self._pinnable()
        image = self._wanted_image()
        repository = repository_of(image) if image else None
        if repository is None:
            return AvailableRelease(
                machines=list(machines),
                reason=(
                    "This machine's image is named by digest or not named at all, "
                    f"so there is no repository to ask. Set {IMAGE_VARIABLE} to a "
                    "reference carrying a tag."
                ),
            )
        if self._tags is None:
            return AvailableRelease(
                repository=repository,
                machines=list(machines),
                reason="This service was built without a way to reach a registry.",
            )
        try:
            found = self._tags.tags(repository)
        except RegistryUnreachable as error:
            return AvailableRelease(
                repository=repository,
                machines=list(machines),
                reason=str(error),
            )
        latest = newest(found)
        if latest is None:
            return AvailableRelease(
                repository=repository,
                machines=list(machines),
                reason=(
                    f"{repository} holds no tag naming a version, so there is "
                    "nothing to compare."
                ),
            )
        pinned = _tag(image)
        return AvailableRelease(
            repository=repository,
            latest=latest,
            pinned=pinned,
            newer=_above(latest, pinned),
            machines=list(machines),
        )

    def pin(
        self, version: str, author: str, expected_head: str | None = None
    ) -> Pinned:
        """Write `version` as the image tag of every machine that names one.

        Every machine, rather than this one, because a run plays the whole
        inventory and the role templates each machine's quadlet from its own
        variable. Pinning this node alone would converge a cluster onto two
        versions of this service, which is a state nobody asked for and nobody
        would see until a page behaved differently on one machine.

        Only the tag moves. The repository each machine names is its own, and a
        site mirroring the image on its own registry keeps that mirror.
        """
        version = version.strip()
        if not TAG.match(version):
            raise PinRefused(
                f"{version!r} is not a tag an image can carry.",
                "invalid_version",
                400,
            )

        state = self._inventory.state()
        if state.inventory is None:
            raise PinRefused("There is no inventory yet on this node.", "no_inventory")
        targets = self._pinnable()
        if not targets:
            raise PinRefused(
                f"No machine in this inventory names a {IMAGE_VARIABLE} with a "
                "tag, so there is nothing to pin.",
                "no_image_variable",
            )

        candidate = state.inventory.model_copy(deep=True)
        image = ""
        for name, repository in targets.items():
            image = f"{repository}:{version}"
            candidate.hosts[name].extra[IMAGE_VARIABLE] = image

        machines = sorted(targets)
        commit, validation = self._inventory.save(
            candidate,
            author,
            expected_head=expected_head,
            message=f"webui: run {version} on {', '.join(machines)}",
        )
        if commit is None and not validation.valid:
            raise PinRefused(validation.errors()[0].message, "invalid_inventory", 422)
        return Pinned(
            commit=commit.hash if commit else None,
            version=version,
            image=image,
            machines=machines,
        )

    def _pinnable(self) -> dict[str, str]:
        """Every machine that names an image with a readable repository.

        A machine pinned by digest is left alone: that is a decision somebody
        made about an exact image, and a tag written over it would undo it
        silently.
        """
        state = self._inventory.state()
        if state.inventory is None:
            return {}
        found: dict[str, str] = {}
        for name, node in state.inventory.hosts.items():
            value = node.extra.get(IMAGE_VARIABLE)
            if not isinstance(value, str) or not value.strip():
                continue
            repository = repository_of(value)
            if repository is not None:
                found[name] = repository
        return found

    def _wanted_image(self) -> str | None:
        """The reference the inventory names for the machine serving this page.

        Read from this node's effective variables, so a fleet that sets it once
        under `all` and a site that pins one machine both work: the resolver
        applies group variables before host variables, the way Ansible does.
        """
        state = self._inventory.state()
        if state.inventory is None or state.this_host is None:
            return None
        node = state.inventory.hosts.get(state.this_host)
        if node is None:
            return None
        value = node.extra.get(IMAGE_VARIABLE)
        return str(value) if isinstance(value, str) and value.strip() else None


def _tag(reference: str) -> str | None:
    """The tag of an image reference, or None when there is none to read.

    A registry port and a tag are both a colon, so the tag is the one after the
    last slash. A digest reference has no tag at all.
    """
    if "@" in reference:
        return None
    name = reference.rsplit("/", 1)[-1]
    tag = name.rpartition(":")[2] if ":" in name else ""
    return tag or None


def _above(latest: str, pinned: str | None) -> bool:
    """Whether `latest` is worth offering, given what the inventory names.

    A pinned tag that names no version, `latest` or a branch name, is not
    compared: it is a decision, and the version found is offered next to it
    rather than declared newer than it.
    """
    if pinned is None or not is_version(pinned):
        return False
    return version_key(latest) > version_key(pinned)
