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

from app.trust import authorized_keys, site_key
from app.trust.authorized_keys import AuthorizedKey
from app.trust.keys import KeyPair, ensure_key_pair

logger = logging.getLogger(__name__)

_SELF_KEY_NAME = "id_ed25519_self"

# Always in `from=`, because a connection a node makes to itself may leave from
# the loopback rather than from the administration address.
_LOOPBACK = ("127.0.0.1", "::1")


class OfferedKey(BaseModel):
    """One key a run offers, as the line an `authorized_keys` holds."""

    kind: str = Field(
        description=(
            "`node` for this node's own key, `site` for the key the site's "
            "nodes and control machine share"
        )
    )
    line: str
    fingerprint: str


class GuestTrust(BaseModel):
    """What a guest needs installed for a run to reach inside it."""

    account: str
    keys: list[OfferedKey]
    """This node's key, then the site key where one is installed."""

    @property
    def key_lines(self) -> list[str]:
        return [key.line for key in self.keys]

    @property
    def fingerprints(self) -> list[str]:
        return [key.fingerprint for key in self.keys]


# The comment the site key's line carries in a guest. The `.pub` this service
# stores has none, and a line with no comment is a line nobody can identify in
# an `authorized_keys` later.
SITE_KEY_COMMENT = "seapath-site-key"


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

    @staticmethod
    def offered_comment(hostname: str) -> str:
        """The comment on the line this node hands over for an account it cannot write.

        Distinct from `self_comment` on purpose: revocation removes lines by
        their `seapath-webui:` comment from the file this service writes, and a
        line in a guest is outside that file and outside that reach.
        """
        return f"seapath-webui@{hostname}"

    def guest_trust(self, hostname: str) -> GuestTrust:
        """The account and the key lines a guest's seed installs.

        The account is the one every run connects as, so a guest trusted this
        way is reached by the same `ansible_user` a machine is. The lines are
        public by nature, which is what lets them be committed to the
        inventory: what they authorise is the private halves, which never leave
        `/etc/seapath/webui` or the control machine the site key came from.

        This node's own key reaches the guest from this node alone. The site
        key, where one is installed, is the one every node of the site and the
        site's own control machine hold, so it is what lets a measurement be
        launched from another node, or the exported inventory be run from a
        checkout, long after the guest was declared here. Both are installed,
        because a run offers both and either one is enough.
        """
        key = self.self_key()
        keys = [
            OfferedKey(
                kind="node",
                line=f"{key.public_key} {self.offered_comment(hostname)}",
                fingerprint=key.fingerprint,
            )
        ]
        site = site_key.describe(self._ssh_dir)
        site_public = self._ssh_dir / f"{site_key.SITE_KEY_NAME}.pub"
        if site is not None:
            blob = " ".join(site_public.read_text().split()[:2])
            keys.append(
                OfferedKey(
                    kind="site",
                    line=f"{blob} {SITE_KEY_COMMENT}",
                    fingerprint=site.fingerprint,
                )
            )
        return GuestTrust(account=self._ansible_user, keys=keys)

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
