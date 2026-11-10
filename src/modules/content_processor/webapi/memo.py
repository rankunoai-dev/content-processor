"""A value remembered for a few seconds, for routes that are polled by many tabs.

`GET /api/queue` is asked every few seconds by every open page; its per-URL
rate (an average over the last 20 finished scrapes) reads every project's run
files and changes only when a scrape finishes. Remembering it briefly turns N
tabs x 1 read per poll into one read per `ttl_s`, with an answer that is at
most that many seconds old (an estimate anyway).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

__all__ = ["TimedMemo"]

T = TypeVar("T")


class TimedMemo(Generic[T]):
    """Call `compute` at most once per `ttl_s` seconds; thread-safe."""

    def __init__(
        self, compute: Callable[[], T], ttl_s: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Nothing is computed until the first `get()`."""
        self._compute = compute
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._value: T | None = None
        self._at: float | None = None

    def get(self) -> T:
        """The remembered value, recomputed when older than the ttl."""
        with self._lock:
            now = self._clock()
            if self._value is None or self._at is None or now - self._at >= self._ttl:
                self._value = self._compute()
                self._at = now
            return self._value

    def forget(self) -> None:
        """Drop the remembered value (the next `get()` recomputes)."""
        with self._lock:
            self._value = None
            self._at = None
