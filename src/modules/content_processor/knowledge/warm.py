"""Warm the search cache, so the first search of a big website is not the slow one.

The SQLite store keeps each website's chunk vectors in memory between searches
(`CP_VECTOR_CACHE_MB`, least recently used dropped first). The first search after a
restart reads them from disk: about 2.2 s for GEP's 35,000 passages, against 0.24 s once
they are in memory. With `CP_VECTOR_WARM` on (the default), the web app reads them in the
background instead:

* when it starts: the freshest copy of every website that has a knowledge space, most
  recently built first, until the cache is full (it never pushes out what it just loaded);
* when a scrape finishes building a website's knowledge space (the index step runs in its
  own process, so the app's cache is stale until then).

It only reads, takes one store at a time on a background thread, and never stops the
app: a store that fails is logged and skipped. Postgres stores are not warmed (pgvector
searches its own index on the database server).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.core.logger import get_logger
from src.modules.content_processor.knowledge.federated import Space
from src.modules.content_processor.knowledge.vector_store import VectorStore, cached_bytes

if TYPE_CHECKING:
    from src.modules.content_processor.projects.store import ProjectStore

__all__ = ["WarmReport", "freshest_spaces", "warm_in_background", "warm_spaces"]

_log = get_logger("knowledge.warm")


@dataclass
class WarmReport:
    """What a warming pass loaded."""

    warmed: list[str] = field(default_factory=list)
    rows: int = 0
    skipped: list[str] = field(default_factory=list)
    seconds: float = 0.0
    full: bool = False


def freshest_spaces(store: ProjectStore, namespace: Callable[[str, str], str]) -> list[Space]:
    """The copy searches read for each website (its freshest), most recently built first."""
    from src.modules.content_processor.projects.websites import WebsiteGraph

    graph = WebsiteGraph.build(store, namespace)
    copies = [c for w in graph.websites() if (c := w.freshest()) is not None]
    copies.sort(key=lambda c: c.knowledge_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return [c.space() for c in copies]


def warm_spaces(
    spaces: Iterable[Space],
    open_store: Callable[[Space], VectorStore],
    *,
    limit_bytes: int,
) -> WarmReport:
    """Load each space's vectors into the cache, in the given order, while there is room."""
    report = WarmReport()
    started = time.perf_counter()
    if limit_bytes <= 0:
        return report
    for space in spaces:
        if cached_bytes() >= limit_bytes:
            report.full = True
            break
        try:
            store = open_store(space)
        except Exception:  # noqa: BLE001 - one store must not stop the others
            _log.warning("warm_open_failed", extra={"space": space.namespace})
            report.skipped.append(space.namespace)
            continue
        try:
            warm = getattr(store, "warm", None)
            if warm is None:
                report.skipped.append(space.namespace)
                continue
            rows = int(warm())
            report.rows += rows
            report.warmed.append(space.namespace)
        except Exception:  # noqa: BLE001
            _log.warning("warm_failed", extra={"space": space.namespace})
            report.skipped.append(space.namespace)
        finally:
            store.close()
    report.seconds = round(time.perf_counter() - started, 3)
    _log.info(
        "vector_cache_warmed",
        extra={
            "spaces": len(report.warmed),
            "rows": report.rows,
            "seconds": report.seconds,
            "full": report.full,
            "skipped": len(report.skipped),
        },
    )
    return report


def warm_in_background(
    spaces: Callable[[], list[Space]],
    open_store: Callable[[Space], VectorStore],
    *,
    limit_bytes: int,
    name: str = "vector-warm",
) -> threading.Thread:
    """Run `warm_spaces` on a daemon thread; `spaces` is read on that thread."""

    def work() -> None:
        try:
            warm_spaces(spaces(), open_store, limit_bytes=limit_bytes)
        except Exception:  # noqa: BLE001 - warming is an optimisation, never a failure
            _log.exception("vector_warm_stopped")

    thread = threading.Thread(target=work, name=name, daemon=True)
    thread.start()
    return thread
