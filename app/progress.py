"""Rate-limit progress events so a scan cannot redraw the page on every file batch."""

from __future__ import annotations

import threading
import time
from typing import Callable

Emit = Callable[[dict], None]


def throttle(emit: Emit, interval: float = 0.5, now: Callable[[], float] = time.monotonic) -> Emit:
    """Drop `progress` events that arrive within `interval` seconds of the last one sent.

    Every other event type (done, caches, ...) is passed through untouched.
    """
    lock = threading.Lock()
    last: list[float | None] = [None]

    def wrapped(payload: dict) -> None:
        if payload.get("type") == "progress":
            moment = now()
            with lock:
                if last[0] is not None and moment - last[0] < interval:
                    return
                last[0] = moment
        emit(payload)

    return wrapped
