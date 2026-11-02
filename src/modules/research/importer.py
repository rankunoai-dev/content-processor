"""Read a Semrush "Organic Positions" export (CSV) instead of calling the API ($0).

The export from the Semrush website has a header line with columns such as Keyword,
Position, Previous position, Search Volume, Keyword Difficulty, CPC, URL, Traffic (%)
and Competition. Columns are found by name, case and spacing aside, so exports with
more, fewer or reordered columns work. Rows without a keyword or a position are skipped.
"""

from __future__ import annotations

import csv
import io
import re

from src.modules.research.models import KeywordRow

__all__ = ["ImportError_", "keyword_rows", "read_positions_csv"]

_ALIASES: dict[str, tuple[str, ...]] = {
    "keyword": ("keyword", "phrase"),
    "position": ("position", "pos"),
    "previous_position": ("previous position", "previous pos"),
    "volume": ("search volume", "volume"),
    "difficulty": ("keyword difficulty", "kd", "kd%"),
    "cpc": ("cpc", "cpc (usd)"),
    "url": ("url", "ur"),
    "traffic_share": ("traffic (%)", "traffic %"),
    "competition": ("competition",),
}


class ImportError_(ValueError):  # noqa: N801 - "ImportError" is a builtin
    """The file is not a positions export."""


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().strip("﻿").casefold())


def _number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in {"-", "n/a"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def keyword_rows(rows: list[dict[str, str]], *, strict: bool = False) -> list[KeywordRow]:
    """Keyword rows from parsed rows (an export, or a Semrush API answer).

    Columns are found by name. With `strict`, rows without Keyword and Position
    columns raise `ImportError_`; otherwise they give no keywords.
    """
    if not rows:
        return []
    headers = {_key(h): h for h in rows[0]}
    columns = {
        field: next((headers[a] for a in aliases if a in headers), None)
        for field, aliases in _ALIASES.items()
    }
    if columns["keyword"] is None or columns["position"] is None:
        if strict:
            msg = "not a Semrush positions export: it needs Keyword and Position columns"
            raise ImportError_(msg)
        return []
    out: list[KeywordRow] = []
    seen: set[str] = set()
    for raw in rows:

        def get(field: str, raw: dict[str, str] = raw) -> str:
            column = columns[field]
            return (raw.get(column) or "").strip() if column else ""

        keyword = get("keyword")
        position = _number(get("position"))
        if not keyword or position is None or position < 1 or keyword.casefold() in seen:
            continue
        seen.add(keyword.casefold())
        previous = _number(get("previous_position"))
        out.append(
            KeywordRow(
                keyword=keyword,
                position=int(position),
                previous_position=int(previous) if previous and previous >= 1 else None,
                volume=int(_number(get("volume")) or 0),
                cpc=max(0.0, _number(get("cpc")) or 0.0),
                competition=_number(get("competition")),
                difficulty=_number(get("difficulty")),
                url=get("url") or None,
                traffic_share=max(0.0, _number(get("traffic_share")) or 0.0),
            )
        )
    return out


def read_positions_csv(data: bytes) -> list[KeywordRow]:
    """Keyword rows of an Organic Positions export; raises `ImportError_` when it is not one."""
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        msg = "the file is empty"
        raise ImportError_(msg)
    rows = [{k: v or "" for k, v in row.items() if k} for row in reader]
    if not rows:
        keyword_rows([dict.fromkeys(reader.fieldnames, "")], strict=True)
        return []
    return keyword_rows(rows, strict=True)
