"""Change-aware recrawl: page dates, the previous-run baseline, and the change decision."""

from src.modules.content_processor.recrawl.baseline import Baseline, conditional_headers
from src.modules.content_processor.recrawl.dates import PageDates, extract_page_dates
from src.modules.content_processor.recrawl.decide import (
    ChangeDecision,
    content_fingerprint,
    decide_change,
)

__all__ = [
    "Baseline",
    "ChangeDecision",
    "PageDates",
    "conditional_headers",
    "content_fingerprint",
    "decide_change",
    "extract_page_dates",
]
