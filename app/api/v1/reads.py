# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""The parameter a reading an operator asked for carries.

Drawing one page asks several endpoints, and several of them ask the same
exporter on the same machine: the Real time page reads the CPU pool and the
conformance, the Cluster page reads Pacemaker and then Ceph, the Containers page
reads the exposition the pool already fetched. `ScrapeCache` lets one scrape
answer all of them, which is what makes moving between the tabs cost the machines
nothing, and D44 says why that is worth having.

It is a window of a few seconds, and it must never stand between an operator and
a machine. `fresh=1` empties it before the reading starts, and the two gestures
that mean "tell me what the cluster is doing now", the control on the panel and
the timer behind it, both send it. See D37 for those two, and D45 for the window.
"""

from __future__ import annotations

from fastapi import Depends, Query, Request

_DESCRIBED = (
    "Scrape the exporters again rather than reusing an answer from the last few "
    "seconds. What the reread control on a panel sends, and what an automation "
    "client should send when the age of the answer is what it is asking about."
)


def fresh_reading(
    request: Request, fresh: bool = Query(default=False, description=_DESCRIBED)
) -> bool:
    """Empty the scrape window when the caller asked for a reading of its own."""
    if fresh:
        request.app.state.scrapes.clear()
    return fresh


#: For the `dependencies` of a reading that fans out to the machines, which puts
#: `fresh` in OpenAPI beside the endpoint's own parameters and empties the window
#: before the handler runs.
reading = Depends(fresh_reading)
