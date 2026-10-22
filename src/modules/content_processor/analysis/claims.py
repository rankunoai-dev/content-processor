"""Claim extraction for contradiction candidates (I4, STAGE4_ANALYSIS_AND_INTERFACES.md §2).

A claim is one declarative sentence of a corpus block that carries something
checkable: a quantity, a date, a negation or a condition. Every claim keeps the
exact character span it came from, so its quote is always a slice of the
block's own `normalized_text` (the I4 release gate: no fabricated proofs).

Context is extracted deterministically into the `ClaimContext` contract:
dates with an effective/until cue, geography, audience, conditions and the
page entity (the top heading). This is the minimal subset I4 needs before
O-7 (claim-context extraction on blocks) exists; anything not detected stays
empty and is reported as "context unknown", never guessed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from src.modules.content_processor.adapters.html_source import without_struck_text
from src.modules.content_processor.contracts import ClaimContext, ContentBlock, UrlRecord
from src.modules.content_processor.export.content import DEFAULT_LAYERS, drop_duplicates

__all__ = [
    "CLAIM_ROLES",
    "Claim",
    "Quantity",
    "claims_from_records",
    "codes_of",
    "extract_claims",
    "split_sentences",
    "terms_of",
]

CLAIM_ROLES: tuple[str, ...] = ("main", "unknown", "aside", "legal")
_MIN_WORDS = 4

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9₹$£€])|\n+")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_NEGATION = re.compile(
    r"\b(not|no|never|none|cannot|can't|won't|isn't|aren't|doesn't|don't|didn't|"
    r"wasn't|weren't|non-refundable|nonrefundable|without)\b",
    re.IGNORECASE,
)
_CONDITION = re.compile(
    r"\b(only|unless|except|provided that|subject to|if)\b\s+((?:[\w'-]+\s*){1,5})",
    re.IGNORECASE,
)
_CURRENCY_SYMBOL = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR"}
_CURRENCY_CODE = {"usd": "USD", "gbp": "GBP", "eur": "EUR", "inr": "INR", "rs": "INR"}
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MONEY = re.compile(
    rf"(?P<sym>[$£€₹])\s?(?P<a>{_NUMBER})|(?P<code>USD|GBP|EUR|INR|Rs\.?)\s?(?P<b>{_NUMBER})",
    re.IGNORECASE,
)
_UNITS: dict[str, str] = {
    "%": "percent",
    "percent": "percent",
    "per cent": "percent",
    "day": "day",
    "days": "day",
    "week": "week",
    "weeks": "week",
    "month": "month",
    "months": "month",
    "year": "year",
    "years": "year",
    "hour": "hour",
    "hours": "hour",
    "hr": "hour",
    "hrs": "hour",
    "minute": "minute",
    "minutes": "minute",
    "kg": "kg",
    "g": "g",
    "mg": "mg",
    "lb": "lb",
    "lbs": "lb",
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "km": "km",
    "mb": "mb",
    "gb": "gb",
    "tb": "tb",
    "w": "w",
    "kw": "kw",
    "v": "v",
    "mah": "mah",
    "items": "item",
    "users": "user",
    "seats": "seat",
}
_UNIT_ALTERNATION = "|".join(sorted(map(re.escape, _UNITS), key=len, reverse=True))
_UNIT_RE = re.compile(
    rf"(?P<n>{_NUMBER})\s?(?:-\s?)?(?P<u>{_UNIT_ALTERNATION})(?!\w)",
    re.IGNORECASE,
)
_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for m in names
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE = (
    rf"(?:(?P<d1>\d{{1,2}})\s+)?(?:(?P<mon>{_MONTH_NAMES})\.?\s+)?"
    r"(?:(?P<d2>\d{1,2}),?\s+)?(?P<y>(?:19|20)\d{2})"
)
_FROM_CUE = re.compile(
    r"\b(?:effective(?: from| as of)?|from|since|starting(?: from| on)?|beginning|"
    rf"valid from)\s+{_DATE}",
    re.IGNORECASE,
)
_UNTIL_CUE = re.compile(
    rf"\b(?:until|till|through|ending|ends on|valid until|expires on|up to)\s+{_DATE}",
    re.IGNORECASE,
)
# "As of <date>" states a value at a point in time (K19): two different "as of"
# dates are two snapshots, never a date conflict. "Effective as of" stays a start.
_AS_OF = re.compile(
    r"(?:(?<!effective )\bas of|\b(?:year|period|quarter|month|to[- ]date)\s+through)"
    rf"\s+{_DATE}",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# Short all-capital codes (tickers, plan or product codes): two claims about
# different codes are about different entities (K19). Common acronyms are not codes.
_CODE = re.compile(r"\b[A-Z][A-Z0-9]{1,4}\b")
_NOT_CODE_TEXT = (
    "US USA UK EU UAE NAV USD GBP EUR INR CAD AUD JPY FAQ PDF CEO CFO COO CTO LLC INC LTD "
    "PLC NYSE AMEX CNW PR AM PM ID TV OK FY YTD QTD ET PT CT GMT UTC APR APY ATM VAT GST "
    "HTML URL API TBD NA NB PS"
)
_NOT_CODES = frozenset(_NOT_CODE_TEXT.split())
_GEOGRAPHY: dict[str, str] = {
    "us": "US",
    "u.s.": "US",
    "usa": "US",
    "united states": "US",
    "america": "US",
    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "britain": "GB",
    "england": "GB",
    "india": "IN",
    "indian": "IN",
    "canada": "CA",
    "canadian": "CA",
    "australia": "AU",
    "australian": "AU",
    "eu": "EU",
    "europe": "EU",
    "european": "EU",
    "germany": "DE",
    "france": "FR",
}
_GEO_RE = re.compile(
    r"(?<![\w.])("
    + "|".join(sorted(map(re.escape, _GEOGRAPHY), key=len, reverse=True))
    + r")(?![\w])",
    re.IGNORECASE,
)
_AUDIENCE = re.compile(
    r"\b(new|existing|current|returning|business|enterprise|student|senior|retail|"
    r"premium|free|trial|professional|corporate|individual)\s+"
    r"(customers?|users?|members?|accounts?|clients?|subscribers?|plans?|tier)\b",
    re.IGNORECASE,
)
_STOPWORD_TEXT = """a an and are as at be been being but by can could do does for from had has have
    he her his how i in into is it its may might more most must of on or our ours over
    per she should so such than that the their them then there these they this those to
    up us was we were what when where which while who will with within after before
    about above across against all also any each few other own same some very you your
    yours only unless except if provided subject not no never none cannot without"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
_ABBREVIATIONS = frozenset(
    {
        "rs.",
        "no.",
        "e.g.",
        "i.e.",
        "etc.",
        "vs.",
        "approx.",
        "inc.",
        "ltd.",
        "co.",
        "mr.",
        "ms.",
        "dr.",
        "st.",
        "fig.",
        "p.",
        "pp.",
        "u.s.",
        "u.k.",
    }
)


@dataclass(frozen=True)
class Quantity:
    """A number with its unit, as written in the claim."""

    value: float
    unit: str
    text: str


@dataclass(frozen=True)
class Claim:
    """One claim-bearing sentence with everything needed to compare and prove it."""

    claim_id: str
    claim_key: str
    """Stable across recrawls: url, block_key and the sentence text."""
    run_id: str
    url: str
    badge: str
    block_id: str
    block_key: str
    heading_path: tuple[str, ...]
    layer: str
    role: str
    block_status: str
    block_confidence: float
    method: str
    text: str
    """Verbatim: `block.normalized_text[start:end]`."""
    start: int
    end: int
    evidence_ids: tuple[str, ...]
    captured_at: datetime
    context: ClaimContext
    quantities: tuple[Quantity, ...]
    negated: bool
    years: frozenset[int]
    terms: frozenset[str] = field(default_factory=frozenset)
    as_of: date | None = None
    """Point in time the claim states a value for ("as of March 31, 2025")."""
    codes: frozenset[str] = field(default_factory=frozenset)
    """Entity codes: the claim's own, else the codes its page repeats."""


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Character spans of sentences, trimmed of surrounding whitespace."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(text):
        before = text[cursor : match.start()].rsplit(None, 1)
        if before and before[-1].lower() in _ABBREVIATIONS and "\n" not in match.group(0):
            continue  # "Rs. 49", "e.g. the", "No. 5": not a sentence end
        spans.append((cursor, match.start()))
        cursor = match.end()
    spans.append((cursor, len(text)))
    result: list[tuple[int, int]] = []
    for start, end in spans:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            result.append((start, end))
    return result


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def terms_of(text: str) -> frozenset[str]:
    """Content words, lower-cased and lightly stemmed; numbers and stop words dropped."""
    return frozenset(
        _stem(w)
        for w in (m.group(0).lower() for m in _WORD.finditer(text))
        if len(w) >= 3 and w not in _STOPWORDS and w not in _MONTHS
    )


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _quantities(text: str) -> tuple[Quantity, ...]:
    found: list[Quantity] = []
    taken: list[tuple[int, int]] = []
    for m in _MONEY.finditer(text):
        if m.group("sym"):
            unit, raw = _CURRENCY_SYMBOL[m.group("sym")], m.group("a")
        else:
            unit = _CURRENCY_CODE[m.group("code").lower().rstrip(".")]
            raw = m.group("b")
        found.append(Quantity(value=_number(raw), unit=unit, text=m.group(0)))
        taken.append(m.span())
    for m in _UNIT_RE.finditer(text):
        if any(s <= m.start() < e for s, e in taken):
            continue
        unit = _UNITS[m.group("u").lower()]
        if unit == "year" and 1900 <= _number(m.group("n")) <= 2099:
            continue  # "2026 years" is not a duration we can trust
        found.append(Quantity(value=_number(m.group("n")), unit=unit, text=m.group(0)))
    return tuple(found)


def _date_from(match: re.Match[str], *, end_of_period: bool) -> date:
    year = int(match.group("y"))
    month_raw = (match.group("mon") or "").lower()
    day_raw = match.group("d1") or match.group("d2")
    if not month_raw:
        return date(year, 12, 31) if end_of_period else date(year, 1, 1)
    month = _MONTHS[month_raw]
    if day_raw and 1 <= int(day_raw) <= 28:
        return date(year, month, int(day_raw))
    if end_of_period:
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        return date.fromordinal(nxt.toordinal() - 1)
    return date(year, month, 1)


def _context(text: str, block: ContentBlock) -> ClaimContext:
    lower = text.lower()
    effective_from = _FROM_CUE.search(text)
    effective_to = _UNTIL_CUE.search(text)
    geographies = sorted({_GEOGRAPHY[m.group(1).lower()] for m in _GEO_RE.finditer(text)})
    audiences = sorted({m.group(0).lower() for m in _AUDIENCE.finditer(lower)})
    conditions = [
        f"{m.group(1).lower()} {m.group(2).strip().lower()}" for m in _CONDITION.finditer(text)
    ]
    return ClaimContext(
        entity=block.heading_path[0] if block.heading_path else None,
        product_or_variant=block.heading_path[-1] if len(block.heading_path) > 1 else None,
        effective_from=_date_from(effective_from, end_of_period=False) if effective_from else None,
        effective_to=_date_from(effective_to, end_of_period=True) if effective_to else None,
        audience=", ".join(audiences) or None,
        geography=", ".join(geographies) or None,
        conditions=conditions,
    )


def _is_claim(
    text: str,
    quantities: Sequence[Quantity],
    negated: bool,
    years: set[int],
    context: ClaimContext,
) -> bool:
    """True when the sentence carries something checkable.

    That is a quantity, date, negation or condition, or a named audience or
    geography (the stand-in for entities until O-7).
    """
    if len(text.split()) < _MIN_WORDS or text.rstrip().endswith("?"):
        return False
    return bool(
        quantities
        or negated
        or years
        or _CONDITION.search(text)
        or context.audience
        or context.geography
    )


def extract_claims(record: UrlRecord, block: ContentBlock) -> list[Claim]:
    """Claims in one block; empty when the block carries nothing checkable."""
    text = block.normalized_text
    if block.heading_path and text == block.heading_path[-1]:
        return []  # the heading element's own text
    claims: list[Claim] = []
    for start, end in split_sentences(text):
        sentence = text[start:end]
        # A struck-out passage ("~~$200~~") is what the page no longer says: it is
        # quoted verbatim but never read as a current amount or negation.
        current = without_struck_text(sentence)
        quantities = _quantities(current)
        negated = bool(_NEGATION.search(current))
        years = {int(y) for y in _YEAR.findall(sentence)}
        context = _context(sentence, block)
        if not _is_claim(sentence, quantities, negated, years, context):
            continue
        key = f"{record.requested_url}|{block.block_key}|{sentence}"
        claims.append(
            Claim(
                claim_id="cl-" + hashlib.sha256(f"{key}|{start}".encode()).hexdigest()[:16],
                claim_key=key,
                run_id=record.run_id,
                url=record.requested_url,
                badge=record.badge.value,
                block_id=block.block_id,
                block_key=block.block_key,
                heading_path=tuple(block.heading_path),
                layer=block.layer.value,
                role=block.role,
                block_status=block.status.value,
                block_confidence=block.confidence,
                method=block.method,
                text=sentence,
                start=start,
                end=end,
                evidence_ids=tuple(block.evidence_ids),
                captured_at=record.captured_at,
                context=context,
                quantities=quantities,
                negated=negated,
                years=frozenset(years),
                terms=terms_of(sentence),
                as_of=_as_of(sentence),
                codes=codes_of(sentence),
            )
        )
    return claims


def _as_of(text: str) -> date | None:
    match = _AS_OF.search(text)
    return _date_from(match, end_of_period=False) if match else None


def _is_code(token: str) -> bool:
    return token not in _NOT_CODES and token.lower() not in _STOPWORDS


def codes_of(text: str) -> frozenset[str]:
    """Short all-capital codes in `text`, minus common acronyms."""
    return frozenset(c for c in _CODE.findall(text) if _is_code(c))


def _page_codes(record: UrlRecord) -> frozenset[str]:
    """Codes a page repeats (at least twice): the page's own entities."""
    counts: dict[str, int] = {}
    for block in record.blocks:
        for code in _CODE.findall(block.normalized_text):
            counts[code] = counts.get(code, 0) + 1
    return frozenset(c for c, n in counts.items() if n >= 2 and _is_code(c))


def _with_page_codes(claim: Claim, page_codes: Mapping[str, frozenset[str]]) -> Claim:
    """A claim without its own code inherits the codes its page repeats."""
    if claim.codes:
        return claim
    inherited = page_codes.get(claim.url, frozenset())
    return replace(claim, codes=inherited) if inherited else claim


def claims_from_records(
    records: Iterable[UrlRecord], *, include_boilerplate: bool = False
) -> list[Claim]:
    """Claims from every URL's corpus blocks (R1 layers; boilerplate roles excluded)."""
    claims: list[Claim] = []
    page_codes: dict[str, frozenset[str]] = {}
    for record in records:
        page_codes[record.requested_url] = _page_codes(record)
        candidates = [
            block
            for block in record.blocks
            if block.normalized_text
            and block.layer in DEFAULT_LAYERS
            and (include_boilerplate or block.role in CLAIM_ROLES)
        ]
        for block in drop_duplicates(candidates):
            claims.extend(extract_claims(record, block))
    return [_with_page_codes(c, page_codes) for c in claims]
