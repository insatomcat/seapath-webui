# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Trust relations, and the one that exists before any peer does.

A standalone node needs a trust relation **with itself** before it can
configure anything. That follows from the inventory setting
`ansible_connection: ssh` for every host including the local one, which is the
property that makes a machine configured through this service identical to one
configured from a conventional control machine. Without the self relation,
nothing converges at all, not even a single machine.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.trust import authorized_keys
from app.trust.authorized_keys import AuthorizedKey
from app.trust.keys import KeyPair, ensure_key_pair

logger = logging.getLogger(__name__)

_SELF_KEY_NAME = "id_ed25519_self"

# Always in `from=`, because a connection a node makes to itself may leave from
# the loopback rather than from the administration address.
_LOOPBACK = ("127.0.0.1", "::1")


class TrustRelation(BaseModel):
    """One direction of trust, as the trust view shows it."""

    comment: str
    peer: str
    kind: str = Field(description="`self` for the relation a node has with itself")
    fingerprint: str
    from_addresses: list[str]
    installed: bool = Field(
        description="Whether the line is currently in the account's authorized_keys"
    )


class TrustService:
    def __init__(
        self,
        ssh_dir: Path,
        authorized_keys_file: Path,
        ansible_user: str = "ansible",
    ) -> None:
        self._ssh_dir = ssh_dir
        self._authorized_keys_file = authorized_keys_file
        self._ansible_user = ansible_user

    @staticmethod
    def self_comment(hostname: str) -> str:
        return f"{authorized_keys.COMMENT_PREFIX}{hostname}->{hostname}"

    def self_key(self) -> KeyPair:
        return ensure_key_pair(self._ssh_dir, _SELF_KEY_NAME)

    def ensure_self_trust(
        self, hostname: str, addresses: list[str]
    ) -> tuple[TrustRelation, bool]:
        """Provision the relation this node has with itself.

        Called at every start, not only at first boot, and idempotent. Running
        it again is what repairs the relation after the administration address
        changes, which `seapath_setup_network.yaml` can do: the `from=` clause
        names addresses, so an address change silently invalidates the
        restriction until the line is rewritten.
        """
        key_pair = self.self_key()
        from_addresses = _restriction_addresses(addresses)
        comment = self.self_comment(hostname)
        changed = authorized_keys.install(
            self._authorized_keys_file,
            AuthorizedKey(
                comment=comment,
                public_key=key_pair.public_key,
                from_addresses=from_addresses,
                # The console connects over this relation, and a terminal is
                # what `restrict` forbids first. A peer relation carries runs
                # and stays without one.
                allow_pty=True,
            ),
        )
        if changed:
            logger.info(
                "Provisioned the self trust for %s in the %s account, from=%s",
                hostname,
                self._ansible_user,
                ",".join(from_addresses),
            )
        return (
            TrustRelation(
                comment=comment,
                peer=hostname,
                kind="self",
                fingerprint=key_pair.fingerprint,
                from_addresses=list(from_addresses),
                installed=True,
            ),
            changed,
        )

    def relations(self, hostname: str) -> list[TrustRelation]:
        """What is provisioned right now, read from the file, not remembered.

        Deriving the view from `authorized_keys` rather than from a record this
        service keeps means it cannot claim a relation that is not there. An
        operator debugging a failed run needs to see what `sshd` will actually
        accept.
        """
        present = set(authorized_keys.installed(self._authorized_keys_file))
        comment = self.self_comment(hostname)
        if not (self._ssh_dir / _SELF_KEY_NAME).exists():
            return []
        key_pair = self.self_key()
        return [
            TrustRelation(
                comment=comment,
                peer=hostname,
                kind="self",
                fingerprint=key_pair.fingerprint,
                from_addresses=[],
                installed=comment in present,
            )
        ]

    def revoke(self, comment: str) -> bool:
        removed = authorized_keys.remove(self._authorized_keys_file, comment)
        if removed:
            logger.info("Revoked the trust relation %s", comment)
        return removed


def _restriction_addresses(addresses: list[str]) -> tuple[str, ...]:
    """The `from=` list, deduplicated and ordered so the line is stable.

    A line that reorders itself between two starts would rewrite a file the
    site may be watching, for no change at all.
    """
    unique = {address for address in addresses if address}
    unique.update(_LOOPBACK)
    return tuple(sorted(unique))
