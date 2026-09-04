# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Which version of this service the inventory asks for, and which one answers.

The other half of [D23](../../docs/decisions.md). The collection a node runs is
a file it can be handed; this service is a container image, and replacing it is
a change to the machine. So it is a variable in the inventory, `seapath_webui_image`,
applied by the same Ansible run as everything else. Nothing here writes it, and
nothing here restarts anything.

What this module does is the part that belongs to a node: say which image the
inventory names for it, which version is answering, and whether the two agree.
An operator who edits the variable and never applies it has changed nothing,
and a UI that says "up to date" because it read the desired state would be
worse than silent.

The comparison is on the tag, because the tag follows `__version__` and a test
in `tests/test_packaging.py` holds them together. A reference pinned by digest
carries no readable version, so the answer is "cannot tell" rather than a guess.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app import __version__
from app.inventory.model import WEBUI_IMAGE_VARIABLE
from app.inventory.service import InventoryService

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


class UpdateService:
    def __init__(self, inventory: InventoryService, running: str = __version__) -> None:
        self._inventory = inventory
        self._running = running

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
