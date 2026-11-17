"""Reviewers approve or reject the engine's verdict on each page of a project scrape.

Each page of a scrape carries the engine's verdict (its badge: verified, partial, needs
review, failed, blocked, excluded) and the gaps behind it. A reviewer either
**approves** it, or **rejects** it with a reason and a next step:

- `scrape_again`: the next scrape of the website fetches the page in full again, never
  reusing the earlier capture (even when the site says it did not change);
- `obtain_manually`: someone gets the content by hand (listed as a task);
- `exclude`: the page is left out of the project reports.

Decisions go to the scrape's append-only decision log (`<run>.decisions.jsonl`, kind
`verdict`), with who and when: the history is the audit trail, the latest line counts,
and the scrape's own reports read it like every other decision.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.export.decisions import (
    Decision,
    append_decision,
    decisions_path,
    read_decisions,
    verdict_decisions,
)
from src.modules.content_processor.knowledge.models import PageStamp
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.store import ProjectStore

__all__ = [
    "PageVerdict",
    "VerdictDecisionOut",
    "VerdictList",
    "excluded_urls",
    "page_verdicts",
    "rescrape_urls",
    "save_verdict",
]

_FINISHED: Final = "finished"


class VerdictDecisionOut(StrictModel):
    """One decision about a page's verdict."""

    decision: str
    next_step: str = ""
    note: str = ""
    reviewer: str
    at: datetime


class PageVerdict(StrictModel):
    """One page of the scrape with the engine's verdict and the reviewer's decision."""

    url: str
    title: str | None = None
    badge: str
    gaps: list[str] = Field(default_factory=list)
    is_page: bool = Field(description="False for files, images, tracking pixels")
    note: str = Field(default="", description="Why the page is not used, when it is not")
    decision: VerdictDecisionOut | None = None
    history: list[VerdictDecisionOut] = Field(default_factory=list)


class VerdictList(StrictModel):
    """Every page of a scrape, with counts."""

    run_id: str
    site_id: str
    pages: list[PageVerdict]
    counts: dict[str, int] = Field(default_factory=dict)


def _stamps(folder: Path, run_id: str) -> list[PageStamp]:
    path = folder / f"{run_id}.stamps.jsonl"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [PageStamp.model_validate_json(line) for line in handle if line.strip()]


_DOCUMENTS: Final = frozenset({"html", "pdf", "office"})


def _is_page(stamp: PageStamp) -> bool:
    """A document a person reads; not an image, file, tracking pixel or policy exclusion."""
    return (
        stamp.page_format in _DOCUMENTS
        and not (stamp.is_asset or stamp.is_tracking)
        and stamp.exclusion_reason != "excluded_by_policy"
    )


def _out(d: Decision) -> VerdictDecisionOut:
    return VerdictDecisionOut(
        decision=d.decision, next_step=d.next_step, note=d.note, reviewer=d.reviewer, at=d.at
    )


def page_verdicts(store: ProjectStore, project_id: str, site_id: str, run_id: str) -> VerdictList:
    """The scrape's pages (from its page stamps) with their current decisions and history."""
    folder = store.run_dir(project_id, site_id, run_id)
    log = read_decisions(decisions_path(folder, run_id))
    current = verdict_decisions(log)
    history: dict[str, list[VerdictDecisionOut]] = {}
    for d in log:
        if d.kind == "verdict":
            history.setdefault(d.key, []).append(_out(d))
    pages: list[PageVerdict] = []
    seen: set[str] = set()
    for stamp in _stamps(folder, run_id):
        if stamp.url in seen:  # one row per address (a PDF can have a stamp per part)
            continue
        seen.add(stamp.url)
        found = current.get(stamp.url)
        pages.append(
            PageVerdict(
                url=stamp.url,
                title=stamp.title,
                badge=stamp.badge,
                gaps=list(stamp.gap_types),
                is_page=_is_page(stamp),
                note=stamp.exclusion_reason or "",
                decision=_out(found) if found else None,
                history=list(reversed(history.get(stamp.url, []))),
            )
        )
    counts = Counter(p.badge for p in pages if p.is_page)
    decided = Counter(
        (p.decision.next_step or p.decision.decision) for p in pages if p.decision is not None
    )
    return VerdictList(
        run_id=run_id,
        site_id=site_id,
        pages=pages,
        counts={
            **{f"badge:{k}": v for k, v in counts.items()},
            **{f"decision:{k}": v for k, v in decided.items()},
            "undecided": sum(1 for p in pages if p.is_page and p.decision is None),
        },
    )


def save_verdict(
    store: ProjectStore,
    project_id: str,
    site_id: str,
    run_id: str,
    *,
    url: str,
    decision: str,
    next_step: str,
    note: str,
    reviewer: str,
) -> VerdictDecisionOut:
    """Append one decision; raises `KeyError` for a page not in the scrape, `ValueError`."""
    folder = store.run_dir(project_id, site_id, run_id)
    if url not in {s.url for s in _stamps(folder, run_id)}:
        raise KeyError(url)
    saved = Decision(
        reviewer=reviewer.strip(),
        kind="verdict",
        key=url,
        decision=decision,
        next_step=next_step if decision == "rejected" else "",
        note=note.strip(),
    )
    append_decision(decisions_path(folder, run_id), saved)
    return _out(saved)


def _latest_finished(runs: Iterable[RunState], site_id: str) -> RunState | None:
    done = [r for r in runs if r.site_id == site_id and r.status == _FINISHED]
    return max(done, key=lambda r: r.created_at) if done else None


def rescrape_urls(store: ProjectStore, project_id: str, site_id: str) -> list[str]:
    """Pages a reviewer sent back (`scrape_again`) in the website's latest finished scrape."""
    run = _latest_finished(store.runs(project_id, site_id), site_id)
    if run is None:
        return []
    folder = store.run_dir(project_id, site_id, run.run_id)
    log = read_decisions(decisions_path(folder, run.run_id))
    return sorted(
        url
        for url, d in verdict_decisions(log).items()
        if d.decision == "rejected" and d.next_step == "scrape_again"
    )


def excluded_urls(records: Path, run_id: str) -> set[str]:
    """Pages a reviewer excluded from the reports, next to a scrape's records file."""
    log = read_decisions(decisions_path(records.parent, run_id))
    return {
        url
        for url, d in verdict_decisions(log).items()
        if d.decision == "rejected" and d.next_step == "exclude"
    }
