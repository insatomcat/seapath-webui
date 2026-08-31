# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Trust relations, and the two things that let one node drive another.

The relation this node has with itself is provisioned at startup, because
without it nothing converges at all, and it is read only from here.

Reaching the *other* machines needs two more, and both are an operator's
explicit act:

- the **site key**, the private half of the key the ISO already installed in
  the `ansible` account of every machine. Uploading it makes this node as
  capable as the control machine it came from, which is the point and the whole
  risk. `app/trust/site_key.py` says the rest.
- their **host keys**, learned with `ssh-keyscan` and written only once an
  operator has compared the fingerprints. Nothing is accepted on this node's
  own authority.

The mutual handshake of cluster-join.md replaces both at M3, and moves no
private key anywhere.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.core.auth import Role, User
from app.core.errors import ApiError
from app.core.security import require_role
from app.trust import keyscan, known_hosts, site_key
from app.trust.service import TrustRelation, TrustService

router = APIRouter(prefix="/trust", tags=["trust"])

viewer = Depends(require_role(Role.VIEWER))
admin = Depends(require_role(Role.ADMIN))


def _service(request: Request) -> TrustService:
    return request.app.state.trust_service


@router.get("/relations")
def relations(request: Request, user: User = viewer) -> list[TrustRelation]:
    return _service(request).relations(request.app.state.node_hostname)


@router.delete("/relations/{comment}", status_code=204, response_class=Response)
def revoke(request: Request, comment: str, user: User = admin) -> Response:
    """Remove one relation, identified by the comment on its key line.

    Revoking the self relation is allowed and is occasionally the right thing,
    for instance before decommissioning a machine. It also means this node can
    no longer converge itself until it is provisioned again, which the run
    preconditions will say in as many words.
    """
    if not _service(request).revoke(comment):
        raise ApiError("unknown_relation", f"There is no relation {comment}.", 404)
    return Response(status_code=204)


class SiteKeyState(BaseModel):
    """What the API says about the site key. The material is never in here."""

    installed: bool
    key_type: str | None = None
    fingerprint: str | None = None


class SiteKeyUpload(BaseModel):
    material: str = Field(description="The private key file, as its text")


class ScanRequest(BaseModel):
    addresses: list[str]


class ScannedKeyOut(BaseModel):
    address: str
    key_type: str
    key: str
    fingerprint: str
    accepted: bool = False


class AcceptRequest(BaseModel):
    keys: list[ScannedKeyOut]


def _settings(request: Request):
    return request.app.state.settings


@router.get("/site-key")
def site_key_state(request: Request, user: User = viewer) -> SiteKeyState:
    described = site_key.describe(_settings(request).ssh_dir)
    if described is None:
        return SiteKeyState(installed=False)
    return SiteKeyState(
        installed=True,
        key_type=described.key_type,
        fingerprint=described.fingerprint,
    )


@router.put("/site-key")
def install_site_key(
    request: Request, payload: SiteKeyUpload, user: User = admin
) -> SiteKeyState:
    """Hold the key a site already uses to reach its own machines.

    Administrator only, and the material never comes back out: the fingerprint
    is what an operator compares against `ssh-keygen -lf` on the machine they
    took it from.
    """
    try:
        described = site_key.install(_settings(request).ssh_dir, payload.material)
    except site_key.InvalidKey as error:
        raise ApiError("invalid_key", str(error), 400) from error
    return SiteKeyState(
        installed=True,
        key_type=described.key_type,
        fingerprint=described.fingerprint,
    )


@router.delete("/site-key", status_code=204, response_class=Response)
def remove_site_key(request: Request, user: User = admin) -> Response:
    """Forget the key. Runs against the other machines stop working at once."""
    if not site_key.remove(_settings(request).ssh_dir):
        raise ApiError("no_site_key", "There is no site key on this node.", 404)
    return Response(status_code=204)


@router.get("/host-keys")
def host_keys(request: Request, user: User = viewer) -> list[ScannedKeyOut]:
    """The peer host keys an operator has accepted on this node.

    Reported in the same shape a scan reports, fingerprints included, so the
    page shows one list whose rows change state rather than two lists that
    replace each other.
    """
    return _accepted(_settings(request).known_hosts_file)


def _accepted(known_hosts_file) -> list[ScannedKeyOut]:
    stored = known_hosts.read_peers(known_hosts_file)
    return [
        ScannedKeyOut(
            address=address,
            key_type=key.split()[0],
            key=key,
            fingerprint=keyscan.fingerprint_of(key),
            accepted=True,
        )
        for address in sorted(stored)
        for key in sorted(stored[address])
    ]


@router.post("/host-keys/scan")
def scan_host_keys(
    request: Request, payload: ScanRequest, user: User = admin
) -> list[ScannedKeyOut]:
    """Read host keys over the network, and write nothing.

    The fingerprints are for a person to compare against
    `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on each machine. This
    endpoint deliberately cannot be the whole of the decision.
    """
    try:
        found = keyscan.scan(payload.addresses)
    except keyscan.ScanFailed as error:
        raise ApiError("scan_failed", str(error), 502) from error
    already = {key.key for key in _accepted(_settings(request).known_hosts_file)}
    return [
        ScannedKeyOut(
            address=key.address,
            key_type=key.key_type,
            key=key.key,
            fingerprint=key.fingerprint,
            accepted=key.key in already,
        )
        for key in found
    ]


@router.post("/host-keys")
def accept_host_keys(
    request: Request, payload: AcceptRequest, user: User = admin
) -> list[ScannedKeyOut]:
    """Record the host keys an operator looked at and accepted.

    A list rather than one key, because a three node cluster is three
    fingerprints an operator checks in one sitting.
    """
    malformed = [key.key for key in payload.keys if not keyscan.is_host_key(key.key)]
    if malformed:
        raise ApiError(
            "invalid_host_key",
            f"{malformed[0]!r} is not an SSH host key. A line here is a key "
            "type and its base64 blob, as ssh-keyscan reports them.",
            400,
        )

    entries: dict[str, list[str]] = {}
    for key in payload.keys:
        entries.setdefault(key.address, []).append(key.key)
    settings = _settings(request)
    known_hosts.accept_peers(settings.known_hosts_file, entries)
    return _accepted(settings.known_hosts_file)


@router.delete("/host-keys/{address}", status_code=204, response_class=Response)
def forget_host_key(request: Request, address: str, user: User = admin) -> Response:
    settings = _settings(request)
    if not known_hosts.forget_peer(settings.known_hosts_file, address):
        raise ApiError("unknown_host", f"No host key is recorded for {address}.", 404)
    return Response(status_code=204)
