"""Dates and "latest" in a question, used to reorder search candidates (never to filter).

Websites such as a fund manager's newsroom publish near-identical pages month after
month; their words and meaning barely differ, so a question that names a date or asks
for the latest one gets the wrong month unless the page's publication date counts.

The date in a question is a hint, not a filter: "the 2026 annual meeting" was announced
in 2025, "the three months ended June 30" in July. So candidates are reordered: a third
ranking (pages published closest to the named date, or newest first for "latest") is
fused with the meaning and word rankings, and pages without a date keep their place
through the other two. Explicit filters (a date range chosen by a person or a tool
argument) are separate: they are applied before the search (`published=` in the store).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

__all__ = ["DateIntent", "date_intent"]

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4,
    "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11,
    "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAY_DATE = re.compile(
    rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)\d\d)\b", re.I
)
_MONTH_DATE = re.compile(rf"\b({_MONTH})\.?\s+((?:19|20)\d\d)\b", re.I)
_ISO_DATE = re.compile(r"\b((?:19|20)\d\d)-(\d\d)-(\d\d)\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)?\d\d)\b")
_YEAR = re.compile(r"\b((?:19|20)\d\d)\b")
_LATEST = re.compile(
    # not "recent(ly)", "last" or "current": "if I changed my address recently" or "the
    # last 15 days" are not questions about the newest page
    r"\b(latest|most recent|newest|this (?:month|year)|"
    r"más reciente|último|última|dernier|dernière|neueste)\b",
    re.I,
)


@dataclass(frozen=True)
class DateIntent:
    """What a question says about time: a date it names and whether it wants the newest."""

    target: date | None = None
    latest: bool = False

    @property
    def any(self) -> bool:
        """True when the question names a date or asks for the newest."""
        return self.target is not None or self.latest


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def date_intent(question: str) -> DateIntent:
    """The date a question names (the most precise one found) and whether it asks for the latest."""
    latest = bool(_LATEST.search(question))
    if m := _DAY_DATE.search(question):
        found = _safe(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        if found:
            return DateIntent(found, latest)
    if m := _ISO_DATE.search(question):
        found = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if found:
            return DateIntent(found, latest)
    if m := _NUMERIC_DATE.search(question):  # US order, as on the pages measured (8/31/2026)
        year = int(m.group(3))
        found = _safe(year + 2000 if year < 100 else year, int(m.group(1)), int(m.group(2)))
        if found:
            return DateIntent(found, latest)
    if m := _MONTH_DATE.search(question):
        return DateIntent(date(int(m.group(2)), _MONTHS[m.group(1).lower()], 15), latest)
    if m := _YEAR.search(question):
        return DateIntent(date(int(m.group(1)), 7, 1), latest)
    return DateIntent(None, latest)
