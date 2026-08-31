# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Asking a machine for its host key, and showing it to a person.

`ssh-keyscan` learns a host key over the network, which means the answer could
come from whoever is between the two machines. That is why nothing here writes
anything: it reports what it saw, with the fingerprint in the form
`ssh-keygen -l` prints, and an operator who compares it against the console of
the machine in question is what turns it into trust.

Reading it off the filesystem, as the local relation does, is strictly better.
It is unavailable for a machine this node has never talked to.
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256

logger = logging.getLogger(__name__)

# Modern algorithms only, matching what is recorded for the local machine.
_TYPES = "ed25519,ecdsa"
_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ScannedKey:
    address: str
    key_type: str
    key: str
    """`<type> <blob>`, the form a known_hosts line carries."""
    fingerprint: str


class ScanFailed(Exception):
    """No key could be read from that address."""


def scan(addresses: list[str]) -> list[ScannedKey]:
    binary = shutil.which("ssh-keyscan")
    if binary is None:
        raise ScanFailed(
            "ssh-keyscan is missing from this image, so host keys cannot be "
            "read over the network."
        )

    found: list[ScannedKey] = []
    for address in addresses:
        completed = subprocess.run(
            [binary, "-T", str(_TIMEOUT_SECONDS), "-t", _TYPES, address],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS * 4,
        )
        for line in completed.stdout.splitlines():
            fields = line.split()
            if line.startswith("#") or len(fields) < 3:
                continue
            found.append(
                ScannedKey(
                    address=address,
                    key_type=fields[1],
                    key=f"{fields[1]} {fields[2]}",
                    fingerprint=_fingerprint(fields[2]),
                )
            )
    if not found:
        raise ScanFailed(
            "No host key answered at "
            + ", ".join(addresses)
            + ". The machines have to be up and reachable on port 22 from here."
        )
    return found


def _fingerprint(blob: str) -> str:
    digest = base64.b64encode(sha256(base64.b64decode(blob)).digest()).decode("ascii")
    return "SHA256:" + digest.rstrip("=")
