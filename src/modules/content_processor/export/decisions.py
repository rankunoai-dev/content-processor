"""Reviewer decisions saved from the analyst web app, one append-only log per run.

A run `<name>` (records in `<name>.records.jsonl`) keeps its decisions in
`<name>.decisions.jsonl` in the same folder. Every save appends one line, so
the file is also the audit trail: who decided what, when, and every earlier
answer. The current decision for an item is its latest line; a line with an
empty decision and an empty note clears it.

Two kinds of item:

* `contradiction`: keyed by the R4 `pair_id` (stable across recrawls); the
  decision is one of `REVIEWER_DECISIONS` (confirmed, not_a_contradiction,
  context_differs).
* `review`: keyed by the requested URL of a review-queue page; the decision is
  one of `REVIEW_ACTIONS`, with an optional free-text note.

The `report` command and dry runs read the log and pre-fill R4's
`reviewer_decision` and the review queue's `operator_action` /
`operator_note`, so the next report carries every decision forward.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from src.core.schemas import StrictModel
from src.modules.content_processor.analysis.contradictions import REVIEWER_DECISIONS

__all__ = [
    "DECISIONS_SUFFIX",
    "REVIEW_ACTIONS",
    "Decision",
    "DecisionKind",
    "append_decision",
    "contradiction_decisions",
    "current_decisions",
    "decisions_path",
    "merge_carried",
    "read_decisions",
    "review_decisions",
]

DECISIONS_SUFFIX = ".decisions.jsonl"
DecisionKind = Literal["contradiction", "review"]

REVIEW_ACTIONS: frozenset[str] = frozenset(
    {
        "resolved",
        "accept_as_is",
        "retry_later",
        "obtain_manually",
        "approve_higher_tier",
        "exclude",
    }
)
"""What a reviewer decided for a page in the review queue."""

_LOCK = threading.Lock()


class Decision(StrictModel):
    """One saved decision (one line of the log)."""

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reviewer: str = Field(min_length=1, max_length=80)
    kind: DecisionKind
    key: str = Field(min_length=1, max_length=4096)
    decision: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _known_decision(self) -> Decision:
        """Only the decisions the reports understand are accepted (empty clears)."""
        allowed = REVIEWER_DECISIONS if self.kind == "contradiction" else REVIEW_ACTIONS
        if self.decision and self.decision not in allowed:
            msg = f"unknown {self.kind} decision {self.decision!r}"
            raise ValueError(msg)
        if self.kind == "contradiction" and self.note:
            msg = "contradiction decisions have no note"
            raise ValueError(msg)
        return self

    @property
    def cleared(self) -> bool:
        """True for a line that removes the item's decision."""
        return not self.decision and not self.note


def decisions_path(folder: Path, run_name: str) -> Path:
    """`<folder>/<run_name>.decisions.jsonl`."""
    return folder / f"{run_name}{DECISIONS_SUFFIX}"


def append_decision(path: Path, decision: Decision) -> None:
    """Append one decision and flush it to disk (one writer at a time)."""
    line = decision.model_dump_json() + "\n"
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def read_decisions(path: Path) -> list[Decision]:
    """Every decision in the log, oldest first; unreadable lines are skipped."""
    if not path.is_file():
        return []
    out: list[Decision] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                out.append(Decision.model_validate_json(line))
            except ValidationError:
                continue  # a half-written or hand-edited line never breaks the reports
    return out


def current_decisions(log: Iterable[Decision]) -> dict[tuple[str, str], Decision]:
    """The latest decision per (kind, key); cleared items are absent."""
    latest: dict[tuple[str, str], Decision] = {}
    for decision in log:
        latest[(decision.kind, decision.key)] = decision
    return {k: d for k, d in latest.items() if not d.cleared}


def contradiction_decisions(log: Iterable[Decision]) -> dict[str, str]:
    """`pair_id -> decision` for R4 (what `r4_table` carries forward)."""
    return {
        key: d.decision
        for (kind, key), d in current_decisions(log).items()
        if kind == "contradiction" and d.decision
    }


def review_decisions(log: Iterable[Decision]) -> dict[str, tuple[str, str]]:
    """`url -> (action, note)` for the review queue."""
    return {
        key: (d.decision, d.note)
        for (kind, key), d in current_decisions(log).items()
        if kind == "review"
    }


def merge_carried(
    saved: Mapping[str, str] | None, earlier: Mapping[str, str] | None
) -> dict[str, str]:
    """R4 decisions from an earlier report, overridden by decisions saved since."""
    return {**(earlier or {}), **(saved or {})}
