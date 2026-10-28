"""Change-aware recrawl: page dates, the previous-run baseline, and the change decision."""

from src.modules.content_processor.recrawl.baseline import (
    Baseline,
    conditional_headers,
    extraction_time,
    url_key,
)
from src.modules.content_processor.recrawl.dates import PageDates, extract_page_dates
from src.modules.content_processor.recrawl.decide import (
    ChangeDecision,
    content_fingerprint,
    decide_change,
)
from src.modules.content_processor.recrawl.history import (
    HistoryBaseline,
    HistoryEntry,
    HistoryIndex,
    UrlHistory,
    describe,
    history_path,
)

__all__ = [
    "Baseline",
    "ChangeDecision",
    "HistoryBaseline",
    "HistoryEntry",
    "HistoryIndex",
    "PageDates",
    "UrlHistory",
    "conditional_headers",
    "content_fingerprint",
    "decide_change",
    "describe",
    "extract_page_dates",
    "extraction_time",
    "history_path",
    "url_key",
]
