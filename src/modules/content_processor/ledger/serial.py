"""One lock in front of a ledger so several workers may call it from several threads.

The run engine reads documents in worker threads while pages are still being
fetched and rendered on the event loop (`scheduler/runner.py`). Neither ledger
was written for that: `MemoryLedger` iterates plain dicts and `TaskLedger`
drives one psycopg connection with short transactions. Rather than teach each
call site about threads, `serial_ledger()` wraps the ledger once, where the
workers are built, and every method call then runs under one re-entrant lock.
Calls are short (a lease, an upsert, a count), so the lock is never held across
I/O to a site and the event loop never waits on it for long.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

__all__ = ["SerialLedger", "serial_ledger"]

T = TypeVar("T")


class SerialLedger:
    """Proxy that serialises every method call on the wrapped ledger.

    Attributes that are not callable (`MemoryLedger.resources`, `events`, ...)
    are returned as they are; reading them while workers run is the caller's
    business, as before.
    """

    def __init__(self, inner: Any) -> None:
        """Wrap `inner`; the lock is re-entrant so a ledger method may call another."""
        self._inner = inner
        self._lock = threading.RLock()

    @property
    def inner(self) -> Any:
        """The wrapped ledger."""
        return self._inner

    def __getattr__(self, name: str) -> Any:
        """Return `inner.name`, wrapped to run under the lock when it is callable."""
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        fn: Callable[..., Any] = attr

        @functools.wraps(fn)
        def locked(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return fn(*args, **kwargs)

        return locked


def serial_ledger(ledger: T) -> T:
    """`ledger` behind one lock; typed as the ledger it wraps so call sites do not change."""
    if isinstance(ledger, SerialLedger):
        return ledger
    return cast(T, SerialLedger(ledger))
