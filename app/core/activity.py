# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""When somebody signed in last asked this service anything.

The one reading that runs on its own, the usage recorder, runs only while
someone is using the service, and this is how it knows. Every request that
carries a live session marks it. A session is no measure of that: it lasts its
whole lifetime from the sign in, whether or not a browser is still open on it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class Activity:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: float | None = None
        self._lock = threading.Lock()

    def mark(self) -> None:
        with self._lock:
            self._last = self._clock()

    def within(self, seconds: float) -> bool:
        """Whether a signed in request arrived in the last `seconds`."""
        with self._lock:
            last = self._last
        return last is not None and self._clock() - last < seconds
