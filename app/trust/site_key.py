# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The key a site already uses to reach its own machines.

Every machine installed from the SEAPATH ISO carries a site public key in the
`ansible` account, baked in at build time, and a conventional control machine
holds the private half. That relationship already exists on every node before
this service is installed, and it is the shortest honest path from one node to
the other two: the operator hands the service the private half, and a run
reaches every machine in the inventory.

What is being said plainly, because it must be: this key is root on every
machine that trusts it, by way of the `NOPASSWD:EXEC:SETENV: /bin/sh` rule the
ISO grants the `ansible` account. Uploading it makes this node as powerful as
the control machine it came from. That is the point of it, and it is also the
whole risk. The alternative, and the destination, is the mutual handshake of
cluster-join.md, which gives each pair of nodes its own key and never moves a
private one anywhere.

So the storage is deliberately dull: one file, `0600`, in the directory the
service already owns, never read back over the API, never logged, and removable
in one click. The service reports the fingerprint so an operator can compare it
with `ssh-keygen -lf` on the machine they took it from, and nothing else.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from cryptography.hazmat.primitives import serialization

logger = logging.getLogger(__name__)

SITE_KEY_NAME = "id_site"


class InvalidKey(Exception):
    """The upload is not a private key this service can use."""


@dataclass(frozen=True)
class SiteKey:
    """What the API is allowed to say about the key. No material, ever."""

    key_type: str
    fingerprint: str
    bits: int | None = None

    @property
    def path_name(self) -> str:
        return SITE_KEY_NAME


def private_key_file(ssh_dir: Path) -> Path:
    return ssh_dir / SITE_KEY_NAME


def describe(ssh_dir: Path) -> SiteKey | None:
    """The installed key, or None. Reads the public half only."""
    public = ssh_dir / f"{SITE_KEY_NAME}.pub"
    try:
        blob = public.read_text().strip()
    except OSError:
        return None
    return _describe_public(blob)


def install(ssh_dir: Path, material: str) -> SiteKey:
    """Store the operator's private key, after proving it is usable.

    An encrypted key is refused rather than stored. Nothing here can type a
    passphrase during a run at three in the morning, and keeping the passphrase
    next to the key it protects would be a decision dressed as a feature.
    """
    try:
        key = serialization.load_ssh_private_key(material.encode(), password=None)
    except (TypeError, ValueError) as error:
        # Which exception carries "this needs a passphrase" has moved between
        # cryptography releases, so the message is what is read, and the
        # fallback is the safe one.
        text = str(error).lower()
        if "password" in text or "passphrase" in text or "encrypted" in text:
            raise InvalidKey(
                "This key is protected by a passphrase. This service cannot "
                "type one during a run, so it refuses to hold a key it could "
                "not use. Decrypt a copy with "
                '`ssh-keygen -p -N "" -f <copy>` and upload that, leaving the '
                "protected original where it is."
            ) from error
        raise InvalidKey(
            "This is not an OpenSSH private key. Upload the private half of "
            "the pair, the file without the .pub suffix."
        ) from error

    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("ascii")
    )

    ssh_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    path = private_key_file(ssh_dir)
    # Written as it arrived. Re-serialising would produce a key ssh reads just
    # as well and would also mean this service decided the format of something
    # it was handed, which is a decision with no upside.
    path.write_text(material if material.endswith("\n") else material + "\n")
    os.chmod(path, 0o600)
    (ssh_dir / f"{SITE_KEY_NAME}.pub").write_text(public + "\n")

    described = _describe_public(public)
    assert described is not None
    # The fingerprint, never the material. This line ends up in the journal.
    logger.info("Site key installed, %s %s", described.key_type, described.fingerprint)
    return described


def remove(ssh_dir: Path) -> bool:
    """Forget the key. Returns whether there was one."""
    path = private_key_file(ssh_dir)
    existed = path.exists()
    path.unlink(missing_ok=True)
    (ssh_dir / f"{SITE_KEY_NAME}.pub").unlink(missing_ok=True)
    if existed:
        logger.info("Site key removed")
    return existed


def _describe_public(blob: str) -> SiteKey | None:
    fields = blob.split()
    if len(fields) < 2:
        return None
    try:
        raw = base64.b64decode(fields[1])
    except ValueError:
        return None
    digest = base64.b64encode(sha256(raw).digest()).decode("ascii").rstrip("=")
    return SiteKey(key_type=fields[0], fingerprint=f"SHA256:{digest}")
