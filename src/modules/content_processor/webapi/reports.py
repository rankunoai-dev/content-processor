"""Duplicates (R2), contradictions (R4) and the review queue for one run.

Each view reads the run's saved report file when the `report` command wrote
one (it may carry reviewer decisions or an NLI judge's scores that cannot be
recomputed here), and otherwise builds the same rows in memory with the
existing report functions (`analysis.overlap`, `analysis.contradictions`,
`export.reports`). Results are cached on the run, keyed by the report files'
signatures, so a rewritten report is picked up.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.modules.content_processor.analysis.contradictions import find_contradictions, r4_table
from src.modules.content_processor.analysis.overlap import find_overlaps, r2a_table, r2b_table
from src.modules.content_processor.export.reports import review_rows
from src.modules.content_processor.webapi.models import (
    Contradiction,
    ContradictionsResponse,
    DuplicateGroup,
    DuplicateMember,
    DuplicatePair,
    DuplicatesResponse,
    ReportSource,
    ReviewItem,
    ReviewResponse,
    Statement,
)
from src.modules.content_processor.webapi.store import RunData

__all__ = ["contradictions", "duplicates", "review"]

Row = Mapping[str, Any]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_FLAG_START = re.compile(r" (?=numeric:|negation_flip|date_conflict:)")
"""Flags are joined with spaces but may contain spaces ("10 percent"); split at flag starts."""
_STALE_TOLERANCE_NS = 10 * 60 * 10**9
"""Reports written within ten minutes of the records belong to the same run."""


def _unguard(value: object) -> str:
    """Undo the CSV writer's formula guard (a leading apostrophe before = + - @)."""
    text = "" if value is None else str(value)
    if text.startswith("'") and text[1:2] and text[1:].startswith(_FORMULA_PREFIXES):
        return text[1:]
    return text


def _float(value: object) -> float:
    try:
        return float(str(value)) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _int(value: object) -> int:
    return int(_float(value))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{k: _unguard(v) for k, v in row.items() if k} for row in csv.DictReader(handle)]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def _report_files(run: RunData, names: Sequence[str]) -> list[Path] | None:
    """The saved report files, if all exist and belong to this run (manifest run id)."""
    folder = run.reports_dir
    paths = [folder / n for n in names]
    if not all(p.is_file() for p in paths):
        return None
    manifest = _manifest(run)
    run_id = run.entries[0].record.run_id if run.entries else None
    if manifest.get("run_id") not in (None, run_id):
        return None
    return paths


def _manifest(run: RunData) -> dict[str, Any]:
    path = run.reports_dir / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _signature(paths: Sequence[Path] | None) -> tuple[tuple[int, int], ...]:
    if not paths:
        return ()
    return tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in paths)


def _stale(run: RunData, paths: Sequence[Path] | None) -> bool:
    """A saved report clearly older than the records file may not match the pages shown."""
    if not paths:
        return False
    return any(p.stat().st_mtime_ns + _STALE_TOLERANCE_NS < run.signature[0] for p in paths)


def _cached(run: RunData, name: str, signature: object) -> Any:
    hit = run.cache.get(name)
    if isinstance(hit, tuple) and hit[0] == signature:
        return hit[1]
    return None


def _source(paths: Sequence[Path] | None) -> ReportSource:
    return "report" if paths else "computed"


# -- duplicates (R2a groups, R2b pairs) ---------------------------------------------


def duplicates(run: RunData) -> DuplicatesResponse:
    """Overlap groups and pairs, from the saved R2 files or computed now."""
    paths = _report_files(run, ("r2a_overlap_groups.csv", "r2b_overlap_pairs.csv"))
    signature = _signature(paths)
    hit = _cached(run, "duplicates", signature)
    if isinstance(hit, DuplicatesResponse):
        return hit
    group_rows: Sequence[Row]
    pair_rows: Sequence[Row]
    if paths:
        group_rows, pair_rows = _read_csv(paths[0]), _read_csv(paths[1])
        settings = _manifest(run).get("overlap")
        settings = settings if isinstance(settings, dict) else {}
        metric = str(settings.get("metric", group_rows[0]["metric"] if group_rows else "jaccard"))
        threshold = _float(
            settings.get("threshold", group_rows[0]["threshold"] if group_rows else 0.8)
        )
    else:
        result = find_overlaps(run.records)
        group_rows, pair_rows = r2a_table(result).rows, r2b_table(result).rows
        metric, threshold = result.config.metric, result.config.threshold
    response = DuplicatesResponse(
        source=_source(paths),
        generated_at=_generated_at(run, paths),
        stale=_stale(run, paths),
        metric=metric,
        threshold_pct=round(threshold * 100, 1),
        groups=_groups(run, group_rows),
        pairs=[_pair(run, r) for r in pair_rows],
    )
    run.cache["duplicates"] = (signature, response)
    return response


def _groups(run: RunData, rows: Sequence[Row]) -> list[DuplicateGroup]:
    by_group: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_group[str(row["group_id"])].append(row)
    groups: list[DuplicateGroup] = []
    for group_id, members in by_group.items():
        # Representative: the page with the most words (the fullest copy), then the shortest URL.
        def words(row: Row) -> int:
            entry = run.by_url.get(str(row["url"]))
            return entry.row.words if entry else 0

        rep = max(members, key=lambda r: (words(r), -len(str(r["url"]))))
        groups.append(
            DuplicateGroup(
                group_id=group_id,
                size=_int(members[0].get("group_size")) or len(members),
                members=[
                    DuplicateMember(
                        url=str(r["url"]),
                        page_key=run.key_for_url(str(r["url"])),
                        title=run.title_for_url(str(r["url"])),
                        badge=str(r.get("badge") or ""),
                        overlap_pct=_float(r.get("overlap_to_group_pct")),
                        shared_blocks=_int(r.get("shared_block_count")),
                        representative=r is rep,
                    )
                    for r in members
                ],
            )
        )
    groups.sort(key=lambda g: (-g.size, g.group_id))
    return groups


def _pair(run: RunData, row: Row) -> DuplicatePair:
    url_a, url_b = str(row["url_a"]), str(row["url_b"])
    spans = [s.strip() for s in str(row.get("shared_spans") or "").split(" || ") if s.strip()]
    return DuplicatePair(
        url_a=url_a,
        url_b=url_b,
        key_a=run.key_for_url(url_a),
        key_b=run.key_for_url(url_b),
        title_a=run.title_for_url(url_a),
        title_b=run.title_for_url(url_b),
        jaccard_pct=_float(row.get("jaccard_pct")),
        a_in_b_pct=_float(row.get("containment_a_in_b_pct")),
        b_in_a_pct=_float(row.get("containment_b_in_a_pct")),
        shared_passages=spans,
        exact=str(row.get("pair_basis")) == "exact_duplicate",
    )


def _generated_at(run: RunData, paths: Sequence[Path] | None) -> str | None:
    if not paths:
        return None
    value = _manifest(run).get("generated_at")
    return value if isinstance(value, str) else None


# -- contradictions (R4) --------------------------------------------------------------


def contradictions(run: RunData) -> ContradictionsResponse:
    """Contradiction candidates, from the saved R4 file or computed now (no judge)."""
    paths = _report_files(run, ("r4_contradictions.jsonl",))
    signature = _signature(paths)
    hit = _cached(run, "contradictions", signature)
    if isinstance(hit, ContradictionsResponse):
        return hit
    rows: Sequence[Row]
    if paths:
        rows = _read_jsonl(paths[0])
        settings = _manifest(run).get("contradictions")
        judge = settings.get("judge_status") if isinstance(settings, dict) else None
        judge_status = str(judge) if judge else (str(rows[0]["judge_status"]) if rows else None)
    else:
        found = find_contradictions(run.records)
        rows = r4_table(found).rows
        judge_status = found.judge_status
    response = ContradictionsResponse(
        source=_source(paths),
        generated_at=_generated_at(run, paths),
        stale=_stale(run, paths),
        judge_status=judge_status,
        items=[_contradiction(run, r) for r in rows],
    )
    run.cache["contradictions"] = (signature, response)
    return response


def _statement(run: RunData, row: Row, side: str) -> Statement:
    url = str(row[f"url_{side}"])
    return Statement(
        url=url,
        page_key=run.key_for_url(url),
        title=run.title_for_url(url),
        quote=str(row.get(f"quote_{side}") or ""),
        heading=str(row.get(f"heading_path_{side}") or ""),
        badge=str(row.get(f"badge_{side}") or ""),
    )


def _context(text: str) -> dict[str, str]:
    context: dict[str, str] = {}
    for part in text.split("; "):
        name, sep, value = part.partition("=")
        if sep and name.strip():
            context[name.strip()] = value.strip()
    return context


def _contradiction(run: RunData, row: Row) -> Contradiction:
    pct = row.get("contradiction_pct")
    return Contradiction(
        pair_id=str(row.get("pair_id") or ""),
        bucket=str(row.get("bucket") or "context_unknown"),
        confidence=str(row.get("confidence_band") or ""),
        confidence_reason=str(row.get("confidence_reason") or ""),
        flags=[
            f for f in _FLAG_START.split(str(row.get("deterministic_flags") or "").strip()) if f
        ],
        context=_context(str(row.get("context_match") or "")),
        contradiction_pct=None if pct in (None, "") else _float(pct),
        a=_statement(run, row, "a"),
        b=_statement(run, row, "b"),
        reviewer_decision=str(row.get("reviewer_decision") or ""),
    )


# -- review queue ------------------------------------------------------------------------


def review(run: RunData) -> ReviewResponse:
    """Pages needing a decision, from the saved review queue or computed now."""
    paths = _report_files(run, ("review_queue.jsonl",))
    signature = _signature(paths)
    hit = _cached(run, "review", signature)
    if isinstance(hit, ReviewResponse):
        return hit
    rows: Sequence[Row] = _read_jsonl(paths[0]) if paths else review_rows(run.records)
    items: list[ReviewItem] = []
    for row in rows:
        url = str(row.get("url") or "")
        entry = run.by_url.get(url)
        items.append(
            ReviewItem(
                url=url,
                page_key=entry.key if entry else None,
                title=entry.row.title if entry else run.title_for_url(url),
                badge=str(row.get("badge") or ""),
                issue_keys=list(entry.row.issue_keys) if entry else [],
                gap_types=[t for t in str(row.get("gap_types") or "").split(";") if t],
                suggested_action=str(row.get("suggested_action") or ""),
                operator_action=str(row.get("operator_action") or ""),
                operator_note=str(row.get("operator_note") or ""),
            )
        )
    response = ReviewResponse(
        source=_source(paths),
        generated_at=_generated_at(run, paths),
        stale=_stale(run, paths),
        items=items,
    )
    run.cache["review"] = (signature, response)
    return response
