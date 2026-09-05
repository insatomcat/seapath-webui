# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A guest's RBD image metadata, read and changed as one request.

Asked of Ceph directly, through `app/cluster/rbd.py`, the way the Cluster page
asks `ha_cluster_exporter` for Pacemaker. The state belongs to Ceph and this
reads it over Ceph's own client, so the page opens filled rather than launching
a run and polling it. D31 has the reasoning and the bounds.

What stays from the first design is the part that mattered: a write reads the
image before and after, so "did this change anything" is answered by the image
rather than by what a browser believed the value was a minute ago. The page
offers the outage that applies a change only when there is a change to apply,
and that outage is still an ordinary run of `cluster_vm`.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.cluster.rbd import (
    KEY,
    MAX_VALUE_BYTES,
    RbdClient,
    RbdUnavailable,
    image_of,
)
from app.core.logging import audit_event

logger = logging.getLogger(__name__)


class InvalidMetadata(Exception):
    """The key or the value cannot be written, and the message says why."""


class MetadataChange(BaseModel):
    """One key that differs between the two readings of a write."""

    key: str
    before: str | None = None
    after: str | None = None


class MetadataView(BaseModel):
    """What a guest's image carries, and what the last write moved."""

    guest: str
    image: str
    entries: dict[str, str] = Field(default_factory=dict)
    changes: list[MetadataChange] = Field(default_factory=list)
    """Empty on a read, and on a write that set a key to the value it held."""

    @property
    def changed(self) -> bool:
        return bool(self.changes)


class MetadataService:
    def __init__(self, client: RbdClient) -> None:
        self._client = client

    def read(self, guest: str) -> MetadataView:
        image = image_of(guest)
        return MetadataView(
            guest=guest, image=image, entries=self._client.list_metadata(image)
        )

    def write(
        self, guest: str, key: str, value: str | None, author: str
    ) -> MetadataView:
        """Set one key, or remove it when `value` is `None`.

        The image is read before and after. A set that wrote the value already
        there reports no change, which is the whole point: applying a metadata
        change stops the guest, and offering that for a write that moved
        nothing would be an outage for nothing.
        """
        self._check(key, value)
        image = image_of(guest)
        before = self._client.list_metadata(image)

        if value is None:
            self._client.remove_metadata(image, key)
        else:
            self._client.set_metadata(image, key, value)

        after = self._client.list_metadata(image)
        changes = _differences(before, after)
        audit_event(
            "vm.metadata",
            guest=guest,
            image=image,
            key=key,
            removed=value is None,
            changed=bool(changes),
            user=author,
        )
        return MetadataView(guest=guest, image=image, entries=after, changes=changes)

    @staticmethod
    def _check(key: str, value: str | None) -> None:
        if not KEY.match(key):
            raise InvalidMetadata(
                f"{key!r} is not an RBD metadata key this service writes. "
                "Letters, digits, underscore, dot and dash, up to 128 of them."
            )
        if value is not None and len(value.encode()) > MAX_VALUE_BYTES:
            raise InvalidMetadata(
                f"A metadata value is at most {MAX_VALUE_BYTES} bytes. That "
                "takes a pinning profile and refuses a file."
            )


def _differences(before: dict[str, str], after: dict[str, str]) -> list[MetadataChange]:
    """Every key the write moved, by name."""
    return [
        MetadataChange(key=key, before=before.get(key), after=after.get(key))
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


__all__ = [
    "InvalidMetadata",
    "MetadataChange",
    "MetadataService",
    "MetadataView",
    "RbdUnavailable",
]
