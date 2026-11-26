"""Keyword intent by rules: informational, navigational, commercial or transactional ($0).

Every keyword of a snapshot is tagged while the snapshot is finished, from the words it
contains (English modifiers; a keyword in another language falls to the length rule):

1. **navigational** when it asks for a place on a website (login, account, contact,
   customer service, careers, phone number...);
2. **transactional** when it asks to act (buy, price, quote, apply, sign up, demo,
   free trial, near me, coupon...);
3. **navigational** when it names one of the project's websites (their domain or name);
4. **commercial** when it compares or chooses (best, top, vs, review, alternatives,
   companies, providers, software, rates, calculator...);
5. **informational** when it asks a question or wants to learn (what, how, why, guide,
   examples, definition...);
6. otherwise by length: a short head term (three words or fewer, such as "term life
   insurance" or "procurement software") is usually a buyer narrowing a choice, so
   **commercial**; a longer one is **informational**.

Demand is also given **weighted by intent** (`WEIGHTS`), so 1,000 "what is" searches do
not count as much as 1,000 "best provider" searches when gaps are ranked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, Literal

__all__ = ["INTENTS", "WEIGHTS", "Intent", "brand_terms", "intent_of", "weighted"]

Intent = Literal["informational", "navigational", "commercial", "transactional"]
INTENTS: Final[tuple[Intent, ...]] = (
    "commercial",
    "transactional",
    "informational",
    "navigational",
)
WEIGHTS: Final[dict[str, float]] = {
    "transactional": 1.2,
    "commercial": 1.0,
    "informational": 0.5,
    "navigational": 0.4,
}

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_PLACE = (
    "login", "log in", "sign in", "signin", "account", "my account", "contact", "contact us",
    "customer service", "customer support", "phone number", "address", "careers", "jobs",
    "headquarters", "portal", "app", "website", "official site",
)  # fmt: skip
_ACT = (
    "buy", "price", "prices", "pricing", "cost", "costs", "quote", "quotes", "coupon",
    "discount", "deal", "deals", "order", "purchase", "subscribe", "sign up", "signup",
    "apply", "application", "download", "free trial", "trial", "demo", "hire", "near me",
    "cheap", "affordable", "for sale", "book", "enroll", "get started",
)  # fmt: skip
_CHOOSE = (
    "best", "top", "vs", "versus", "compare", "comparison", "review", "reviews", "rating",
    "ratings", "alternative", "alternatives", "provider", "providers", "company",
    "companies", "vendor", "vendors", "software", "tool", "tools", "platform", "platforms",
    "agency", "agencies", "service", "services", "solution", "solutions", "rates", "plans",
    "calculator", "options",
)  # fmt: skip
_LEARN = (
    "what", "how", "why", "when", "who", "which", "where", "is", "are", "does", "do", "can",
    "should", "guide", "tutorial", "definition", "meaning", "define", "examples", "example",
    "ideas", "tips", "learn", "history", "explained", "types", "benefits", "pros and cons",
)  # fmt: skip
_SHORT = 3


def _has(words: list[str], text: str, phrases: Iterable[str]) -> bool:
    """A one-word cue must be a whole word; a phrase must appear as words in a row."""
    padded = f" {text} "
    for phrase in phrases:
        if " " in phrase:
            if f" {phrase} " in padded:
                return True
        elif phrase in words:
            return True
    return False


def brand_terms(names: Iterable[str]) -> list[str]:
    """Brand words from websites' domains and names ("johnhancock.com" -> "johnhancock")."""
    out: list[str] = []
    for name in names:
        low = name.casefold().removeprefix("www.")
        stem = low.split(".")[0] if "." in low and " " not in low else low
        for term in {stem, stem.replace("-", " "), " ".join(_WORD.findall(stem))}:
            if len(term) >= 3 and term not in out:
                out.append(term)
    return out


def intent_of(keyword: str, brands: Iterable[str] = ()) -> Intent:
    """The keyword's intent by the rules above (see the module text)."""
    text = " ".join(_WORD.findall(keyword.casefold()))
    words = text.split()
    if not words:
        return "informational"
    if _has(words, text, _PLACE):
        return "navigational"
    if _has(words, text, _ACT):
        return "transactional"
    joined = text.replace(" ", "")
    if any(b in text or b.replace(" ", "") in joined for b in brands):
        return "navigational"
    if _has(words, text, _CHOOSE):
        return "commercial"
    if _has(words, text, _LEARN):
        return "informational"
    return "commercial" if len(words) <= _SHORT else "informational"


def weighted(volume_by_intent: dict[str, int]) -> int:
    """Demand weighted by intent (commercial searches count fully, "what is" half)."""
    return round(sum(WEIGHTS.get(k, 0.5) * v for k, v in volume_by_intent.items()))
