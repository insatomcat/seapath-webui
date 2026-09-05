# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The guests, read from the inventory and from Pacemaker.

Read only, and for now that is the whole of it. A guest is defined by an entry
in the `VMs` group, which is committed through `/inventory` like every other
part of the desired state, and deployed by a run of `deploy_vms_cluster` or
`deploy_vms_standalone` through `/runs`. Neither of those gets a second door
here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.inventory.service import ImportRefused, RefusedWrite
from app.services.vms import GuestsView, InvalidGuest, VmService

router = APIRouter(
    prefix="/vms",
    tags=["vms"],
    dependencies=[Depends(require_role(Role.VIEWER))],
)

admin = Depends(require_role(Role.ADMIN))


def _service(request: Request) -> VmService:
    return request.app.state.vm_service


class GuestDeclaration(BaseModel):
    """A VM to add, in the terms the deployment roles read."""

    name: str = Field(
        description="The libvirt domain name, which is also the inventory key"
    )
    vm_disk: str | None = Field(
        default=None, description="The disk image a creation starts from"
    )
    vm_template: str | None = Field(
        default=None, description="A Jinja2 libvirt XML, rendered per guest"
    )
    xml_path: str | None = Field(
        default=None, description="A libvirt XML taken as it is"
    )
    force: bool = False
    enable: bool = True


class DeclarationResponse(BaseModel):
    name: str
    commit: str
    message: str
    playbook: str = Field(
        description="The catalogue entry that deploys the group in this mode"
    )


@router.get("", response_model=GuestsView)
def guests(request: Request) -> GuestsView:
    """Every guest the inventory declares, with its files and its resource.

    `files` answers whether a deployment would find the disk image and the XML
    each guest names, which is the question worth asking before the run rather
    than during it. `resource` is Pacemaker's line for the guest, absent on a
    standalone machine and whenever nothing publishes one, with `runtime_note`
    saying which of the two it is.
    """
    return _service(request).guests()


@router.post("", status_code=201)
def declare(
    request: Request,
    payload: GuestDeclaration,
    if_match: str | None = Header(default=None, alias="If-Match"),
    user: User = admin,
) -> DeclarationResponse:
    """Add one guest to the inventory, and answer with the run to launch next.

    Adding a VM is three acts that the page presents as one: the disk image
    goes to the artefacts, the libvirt XML is committed with the inventory, and
    this declares the guest that names them. Deploying it is the run named in
    the answer, launched through `/runs` like every other. Nothing here reaches
    a machine. See [D30](decisions.md#d30).
    """
    service = _service(request)
    definition: dict[str, Any] = {
        "vm_disk": payload.vm_disk,
        "vm_template": payload.vm_template,
        "xml_path": payload.xml_path,
    }
    # Both switches default in the roles to what an ordinary deployment wants,
    # so only a guest that departs from that carries one. An entry spelling out
    # `force: false` and `enable: true` says nothing and reads as if it did.
    if payload.force:
        definition["force"] = True
    if not payload.enable:
        definition["enable"] = False

    try:
        commit = service.declare(payload.name, definition, user.username, if_match)
    except InvalidGuest as error:
        raise ApiError("invalid_guest", str(error), 400) from error
    except RefusedWrite as error:
        raise ApiError(
            "refused_write",
            str(error),
            409,
            {"divergences": [d.model_dump() for d in error.divergences]},
        ) from error
    except ImportRefused as error:
        raise ApiError(
            "invalid_inventory",
            str(error),
            422,
            {"findings": [f.model_dump() for f in error.validation.findings]},
        ) from error

    return DeclarationResponse(
        name=payload.name,
        commit=commit.hash,
        message=commit.message,
        playbook=service.deploy_playbook(),
    )
