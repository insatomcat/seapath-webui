# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""A guest's RBD image metadata, read and changed by `rbd`.

`vm_manager` stores what Pacemaker needs to know about a guest as metadata on
its system disk image: `_preferred_host`, `_priority`, `_live_migration`,
`_seapath_alloc` and the rest. It reads them all back in `enable_vm`, and it
writes them at `create` alone. There is no supported way to change one on a
guest that exists, which left a UI two choices: recreate the guest from its
seed image and lose its disk, or reach the metadata directly.

So this reaches it directly, with `rbd image-meta`, over the same SSH path as
everything else and inside an ordinary run. The pool and the `system_` prefix
are `vm_manager`'s own, hardcoded there and hardcoded here so the two can be
compared rather than guessed at.

The bounds are worth stating, because this is the one place where the service
touches state a role would normally own:

- it writes metadata on an RBD image, and nothing else. No file on a host, no
  service restarted, no Pacemaker command;
- every write reads the image before and after in the same run, so "did this
  change anything" is answered by the image rather than by what a browser
  believed a minute ago;
- applying a change is `disable` then `enable` through `cluster_vm`, which is
  the upstream path and an outage, and the page says so before it happens.

See D31.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.runs.actions import RESULTS_FILE


class MetadataChange(BaseModel):
    """One key that differs between the two readings of a write."""

    key: str
    before: str | None = None
    after: str | None = None


class MetadataResult(BaseModel):
    """What a metadata run brought back."""

    entries: dict[str, str] = Field(default_factory=dict)
    """The metadata as it stands, which is the reading taken last."""
    changes: list[MetadataChange] = Field(default_factory=list)
    """What the write actually altered. Empty on a read, and on a write that
    set a key to the value it already held."""

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def read(results_dir: Path) -> MetadataResult | None:
    """The result a metadata run wrote, or nothing if it wrote none.

    Nothing is the ordinary answer for a run still going and for one that died
    before its last task, so it is a value rather than an error.
    """
    path = results_dir / RESULTS_FILE
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None

    before = _mapping(document.get("before"))
    after = _mapping(document.get("after"))
    return MetadataResult(entries=after, changes=_differences(before, after))


def _mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _differences(before: dict[str, str], after: dict[str, str]) -> list[MetadataChange]:
    """Every key the write moved, in the order an operator reads them.

    A set that wrote the value already there produces nothing here, which is
    the whole point: the page offers the outage that applies a change only when
    there is a change to apply.
    """
    changes = [
        MetadataChange(key=key, before=before.get(key), after=after.get(key))
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]
    return changes
