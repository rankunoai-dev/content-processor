"""Contradictions grouped by topic: one row per topic stated in more than one way.

The R4 pair report lists pairs of sentences. This report answers the analyst's
question instead: *which topics does the website state differently, where, and
how?* (docs/app/INVESTIGATION_DUPLICATES_CONTRADICTIONS_SEMANTIC.md §4.2).

1. **Facts.** Every sentence of every corpus block (boilerplate left out) is read
   for checkable values: times ("7-10 business days", "within 60 days"), money,
   percentages, ages, other quantities, phone numbers and email addresses. Each
   fact keeps its verbatim sentence, URL, section heading, block id and evidence
   ids. Several values of one kind in one sentence make one composite value, so a
   sentence never contradicts itself.
2. **Topics.** Facts of the same kind are linked when their content words (the
   sentence plus the question or heading it sits under) overlap enough, weighted
   so that rare words count more than words on every page. Linked facts form a
   topic; within a topic each distinct value is one version.
3. **Sorting the topics** into three sheets, nothing hidden:
   - *Contradictions*: two or more versions of the same thing.
   - *Differs by condition*: the versions split cleanly by geography, audience,
     product code or website (brand), so they may be intended.
   - *Time-bound figures*: facts from news and press pages, or tied to a date or
     period ("as of", "declared", different years): figures of a period, not
     contradictions.
4. **Severity** by rule: High for customer commitments (payout and processing
   times, fees, prices, eligibility, coverage, limits, legal terms), Low for
   marketing figures, Medium for everything else, including contact details.

Rules only, no model, $0. Every snippet is a slice of the block's own text.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from src.modules.content_processor.adapters.html_source import without_struck_text
from src.modules.content_processor.analysis.claims import (
    CLAIM_ROLES,
    codes_of,
    quantities_in,
    split_sentences,
    terms_of,
)
from src.modules.content_processor.contracts import ContentBlock, UrlRecord
from src.modules.content_processor.export.content import DEFAULT_LAYERS, drop_duplicates
from src.modules.content_processor.export.tabular import Cell, Table

__all__ = [
    "TOPIC_COLUMNS",
    "Fact",
    "FactKind",
    "Topic",
    "TopicConfig",
    "TopicResult",
    "facts_from_records",
    "facts_in_sentence",
    "find_topics",
    "is_time_bound_url",
    "severity_of",
    "topic_tables",
]

FactKind = Literal["time", "money", "percent", "age", "quantity", "phone", "email"]
Sheet = Literal["contradiction", "condition", "time_bound"]
Severity = Literal["High", "Medium", "Low"]

KIND_LABEL: dict[str, str] = {
    "time": "Time or deadline",
    "money": "Amount of money",
    "percent": "Percentage",
    "age": "Age",
    "quantity": "Quantity",
    "phone": "Phone number",
    "email": "Email address",
}
_SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

# -- reading facts ---------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fourteen": 14,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty-five": 45,
    "sixty": 60,
    "ninety": 90,
}
_NUM = r"\d+(?:\.\d+)?|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_TIME = re.compile(
    rf"(?<![\w$£€₹.])(?P<a>{_NUM})(?:\s*(?:-|–|—|to|or)\s*(?P<b>{_NUM}))?"
    r"(?:\s+|-)(?P<kind>business|working|calendar)?\s*-?\s*"
    r"(?P<u>minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)(?![\w])",
    re.IGNORECASE,
)
_TIME_UNIT = {
    "min": "minute",
    "mins": "minute",
    "minute": "minute",
    "minutes": "minute",
    "hour": "hour",
    "hours": "hour",
    "hr": "hour",
    "hrs": "hour",
    "day": "day",
    "days": "day",
    "week": "week",
    "weeks": "week",
    "month": "month",
    "months": "month",
    "year": "year",
    "years": "year",
}
_TIME_UNITS = frozenset(_TIME_UNIT.values())
_TIME_SCALE = {"minute": 0, "hour": 1, "day": 2, "week": 3, "month": 4, "year": 5}
_PLACEHOLDER = re.compile(r"lorem ipsum|dolor sit amet", re.IGNORECASE)
_AGE = re.compile(
    r"\bage[sd]?\s+(?P<a>\d{1,3})(?:\s*(?:-|–|to|through|and)\s*(?P<b>\d{1,3}))?\b"
    r"|\b(?P<c>\d{1,3})\s*(?:years? old|years of age|or older|and older|or over|and over)\b",
    re.IGNORECASE,
)
_PHONE = re.compile(
    r"(?<![\w.])(?:\+?1[\s.-]?)?\(?(?P<area>[2-9]\d{2})\)?[\s.-]?(?P<x>\d{3})[\s.-](?P<n>\d{4})(?![\w])"
    r"|(?<![\w.])\+(?P<intl>\d{1,3}[\s-]?\d[\d\s-]{6,14}\d)(?![\w])"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_TIME_BOUND_CUE = re.compile(
    r"\b(?:declared|distributions?|record date|ex-dividend|ex-date|payable on|as of|"
    r"year[- ]to[- ]date|ytd|for the (?:month|quarter|year|period|fiscal year) (?:ended|ending)|"
    r"(?:first|second|third|fourth) quarter|q[1-4]\b|fiscal (?:year )?\d{4}|announced)\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_TIME_BOUND_PATH = re.compile(
    r"/(?:news|newsroom|press|press-releases?|pressroom|press-room|media-center|media-room|"
    r"mediacenter|announcements?|news-releases?|investor-news)(?:/|$|\.)",
    re.IGNORECASE,
)
_DATED_PATH = re.compile(r"/(?:19|20)\d{2}/(?:[01]?\d/)?")
_GEO = re.compile(
    r"\b(?:in|for|residents of)\s+(?P<g>New York|California|Texas|Florida|Canada|"
    r"the US|the U\.S\.|the UK|India|Puerto Rico|[A-Z][a-z]+ state)\b"
)
_AUDIENCE = re.compile(
    r"\b(new|existing|current|returning|business|enterprise|student|senior|retail|group|"
    r"individual|corporate|employer|employee)\s+"
    r"(customers?|users?|members?|accounts?|clients?|policyholders?|policy owners?|plans?|"
    r"contracts?|policies)\b",
    re.IGNORECASE,
)
# Words that say *how* a value is stated, not what it is about.
_NOT_TOPIC_TEXT = [
    "day",
    "business",
    "working",
    "calendar",
    "week",
    "month",
    "year",
    "hour",
    "minute",
    "percent",
    "dollar",
    "typically",
    "usually",
    "generally",
    "normally",
    "approximately",
    "about",
    "around",
    "least",
    "most",
    "within",
    "please",
    "call",
    "contact",
    "email",
    "phone",
    "number",
    "visit",
    "click",
    "learn",
    "page",
    "here",
    "like",
    "just",
]
_NOT_TOPIC = frozenset(terms_of(" ".join(_NOT_TOPIC_TEXT))) | frozenset(_NOT_TOPIC_TEXT)

_MIN_CONFIDENCE = 0.8
"""Blocks read with less confidence (OCR of images, uncertain text) give no facts."""
_HIGH = re.compile(
    r"\b(?:pay(?:s|ing|ment|ments|out|outs)?|paid|claims?|process(?:ed|es|ing)?|turnaround|"
    r"refunds?|fees?|charges?|costs?|prices?|pricing|premiums?|rates?|apr|apy|interest|"
    r"penalt(?:y|ies)|surrender|eligib\w*|qualif\w*|coverage|covered|covers?|benefits?|"
    r"limits?|maximum|minimum|deductibles?|waiting period|grace period|free[- ]look|"
    r"cancel\w*|guarantee\w*|withdraw\w*|loans?|tax\w*|legal|complian\w*|warrant\w*|"
    r"deadlines?|due|death|disburse\w*|reimburse\w*|returns? policy|expir\w*)\b",
    re.IGNORECASE,
)
_LOW = re.compile(
    r"\b(?:customers|clients|members|employees|people|families|founded|since|awards?|"
    r"award-winning|rated|rating|ranked|trusted|countries|offices|locations|served|serving|"
    r"satisf\w*|reviews?|stars?|million|billion|largest|leading|history|years of experience)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Value:
    """One value as read: its comparison key and how it reads."""

    key: str
    text: str


@dataclass(frozen=True)
class Fact:
    """All values of one kind in one sentence, with everything needed to prove them."""

    fact_id: str
    kind: str
    value_key: str
    """Comparison key: equal keys are the same version ("within 60 days" = "60 days")."""
    value_text: str
    url: str
    website: str
    page_name: str
    section: str
    question: str
    quote: str
    """Verbatim: `block.normalized_text[start:end]`."""
    block_id: str
    evidence_ids: tuple[str, ...]
    captured_at: datetime
    terms: frozenset[str]
    """Content words of the sentence and of its question or heading."""
    time_bound: str
    """Why the fact belongs to a period ('' when it does not)."""
    years: frozenset[int] = field(default_factory=frozenset)
    geography: frozenset[str] = field(default_factory=frozenset)
    audience: frozenset[str] = field(default_factory=frozenset)
    codes: frozenset[str] = field(default_factory=frozenset)
    own_terms: frozenset[str] = field(default_factory=frozenset)
    """Content words of the sentence alone."""
    scales: frozenset[int] = field(default_factory=frozenset)
    """Times only: the units' sizes (minute 0 … year 5), so minutes never join weeks."""


def _num(raw: str) -> float:
    lower = raw.lower()
    return float(_NUMBER_WORDS[lower]) if lower in _NUMBER_WORDS else float(raw)


def _fmt(value: float) -> str:
    return f"{value:g}"


def _times(text: str) -> list[Value]:
    found: list[Value] = []
    for m in _TIME.finditer(text):
        a = _num(m.group("a"))
        b = _num(m.group("b")) if m.group("b") else None
        unit = _TIME_UNIT[m.group("u").lower()]
        if unit == "year" and (1900 <= a <= 2099 or (b is not None and 1900 <= b <= 2099)):
            continue
        kind = (m.group("kind") or "").lower()
        kind = "business" if kind == "working" else kind
        span = f"{_fmt(a)}–{_fmt(b)}" if b is not None and b != a else _fmt(a)
        plural = "" if (b is None and a == 1) else "s"
        words = " ".join(w for w in (kind, unit + plural) if w)
        qualifier = kind or ("calendar" if unit == "day" else "")
        key = " ".join(w for w in (span, qualifier, unit) if w)
        found.append(Value(key=key, text=f"{span} {words}"))
    return found


def _ages(text: str) -> list[Value]:
    found: list[Value] = []
    for m in _AGE.finditer(text):
        if m.group("c"):
            found.append(Value(key=f"{m.group('c')}+", text=f"{m.group('c')} or older"))
            continue
        a, b = m.group("a"), m.group("b")
        if b:
            found.append(Value(key=f"{a}-{b}", text=f"ages {a}–{b}"))
        else:
            found.append(Value(key=a, text=f"age {a}"))
    return found


def _phones(text: str) -> list[Value]:
    found: list[Value] = []
    for m in _PHONE.finditer(text):
        if m.group("intl"):
            digits = re.sub(r"\D", "", m.group("intl"))
            found.append(Value(key="+" + digits, text=m.group(0).strip()))
        else:
            digits = m.group("area") + m.group("x") + m.group("n")
            found.append(Value(key=digits, text=f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"))
    return found


def _emails(text: str) -> list[Value]:
    return [Value(key=m.group(0).lower(), text=m.group(0).lower()) for m in _EMAIL.finditer(text)]


def _quantities(text: str) -> dict[str, list[Value]]:
    out: dict[str, list[Value]] = defaultdict(list)
    for q in quantities_in(text):
        if q.unit in _TIME_UNITS:
            continue  # read by `_times`, with ranges and business days
        if q.unit in ("USD", "GBP", "EUR", "INR"):
            out["money"].append(Value(key=f"{q.unit} {_fmt(q.value)}", text=q.text.strip()))
        elif q.unit == "percent":
            out["percent"].append(Value(key=f"{_fmt(q.value)}%", text=f"{_fmt(q.value)}%"))
        else:
            out["quantity"].append(Value(key=f"{_fmt(q.value)} {q.unit}", text=q.text.strip()))
    return out


def facts_in_sentence(text: str) -> dict[str, Value]:
    """The values of each kind in one sentence, combined per kind (sorted, `; `-joined)."""
    current = without_struck_text(text)
    by_kind: dict[str, list[Value]] = defaultdict(list)
    phones = _phones(current)
    by_kind["phone"] = phones
    without_phones = _PHONE.sub(" ", current)
    by_kind["email"] = _emails(without_phones)
    ages = _ages(without_phones)
    by_kind["age"] = ages
    without_ages = _AGE.sub(" ", without_phones)
    by_kind["time"] = _times(without_ages)
    for kind, values in _quantities(without_ages).items():
        by_kind[kind].extend(values)
    out: dict[str, Value] = {}
    for kind, values in by_kind.items():
        unique = {v.key: v for v in values}
        if not unique:
            continue
        keys = sorted(unique)
        out[kind] = Value(key="; ".join(keys), text="; ".join(unique[k].text for k in keys))
    return out


def _scales(key: str) -> frozenset[int]:
    return frozenset(_TIME_SCALE[w] for w in re.findall(r"[a-z]+", key) if w in _TIME_SCALE)


def is_time_bound_url(url: str) -> bool:
    """News, press and announcement pages, and paths dated like `/2024/05/`."""
    path = urlsplit(url).path
    return bool(_TIME_BOUND_PATH.search(path) or _DATED_PATH.search(path))


def _page_name(record: UrlRecord, blocks: Sequence[ContentBlock]) -> str:
    for block in blocks:
        if block.heading_path:
            return block.heading_path[0]
    path = urlsplit(record.requested_url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    return tail.replace("-", " ").replace("_", " ").strip().capitalize() or record.requested_url


def _fact_id(url: str, block_key: str, sentence: str, kind: str) -> str:
    raw = f"{url}|{block_key}|{sentence}|{kind}"
    return "fa-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def facts_from_records(
    records: Iterable[UrlRecord], websites: Mapping[str, str] | None = None
) -> list[Fact]:
    """Facts from every URL's corpus blocks; `websites` maps URL to website name."""
    facts: list[Fact] = []
    names = websites or {}
    for record in records:
        blocks = drop_duplicates(
            [
                b
                for b in record.blocks
                if b.normalized_text
                and b.layer in DEFAULT_LAYERS
                and b.role in CLAIM_ROLES
                and "ocr" not in b.method.lower()
                and b.confidence >= _MIN_CONFIDENCE
            ]
        )
        page = _page_name(record, blocks)
        page_bound = "news or press page" if is_time_bound_url(record.requested_url) else ""
        for block in blocks:
            text = block.normalized_text
            if block.heading_path and text == block.heading_path[-1]:
                continue  # the heading itself
            if _PLACEHOLDER.search(text) or _PLACEHOLDER.search(" ".join(block.heading_path)):
                continue  # template placeholder text, not content
            section = block.heading_path[-1] if block.heading_path else ""
            question = section if section.rstrip().endswith("?") else ""
            heading_terms = terms_of(section)
            for start, end in split_sentences(text):
                sentence = text[start:end]
                if len(sentence.split()) < 3 or sentence.rstrip().endswith("?"):
                    continue
                values = facts_in_sentence(sentence)
                if not values:
                    continue
                cue = _TIME_BOUND_CUE.search(sentence)
                bound = page_bound or (f"tied to a period ('{cue.group(0)}')" if cue else "")
                own = frozenset(terms_of(sentence) - _NOT_TOPIC)
                terms = own | (heading_terms - _NOT_TOPIC)
                for kind, value in values.items():
                    facts.append(
                        Fact(
                            fact_id=_fact_id(record.requested_url, block.block_key, sentence, kind),
                            kind=kind,
                            value_key=value.key,
                            value_text=value.text,
                            url=record.requested_url,
                            website=names.get(record.requested_url, ""),
                            page_name=page,
                            section=section,
                            question=question,
                            quote=sentence,
                            block_id=block.block_id,
                            evidence_ids=tuple(block.evidence_ids),
                            captured_at=record.captured_at,
                            terms=terms,
                            time_bound=bound,
                            years=frozenset(int(y) for y in _YEAR.findall(sentence)),
                            geography=frozenset(m.group("g") for m in _GEO.finditer(sentence)),
                            audience=frozenset(
                                m.group(0).lower() for m in _AUDIENCE.finditer(sentence)
                            ),
                            codes=codes_of(sentence),
                            own_terms=own,
                            scales=_scales(value.key) if kind == "time" else frozenset(),
                        )
                    )
    return facts


# -- grouping into topics -----------------------------------------------------------


@dataclass(frozen=True)
class TopicConfig:
    """Knobs of the topic grouping."""

    min_similarity: float = 0.5
    """Weighted share of the *shorter* statement's content words that the other one has too
    (rare words weigh more), for two facts to be one topic. Measured against the shorter
    side, so "typically within 60 days of filing" under "Death claims" still joins a long
    FAQ answer about death claim payments."""
    min_shared_terms: int = 2
    max_term_share: float = 0.2
    """Words in more than this share of a kind's facts do not link facts on their own."""
    max_pairs: int = 400_000


@dataclass
class Version:
    """One way a topic is stated, and every place it is stated so."""

    value_key: str
    value_text: str
    facts: list[Fact]

    @property
    def urls(self) -> list[str]:
        """Pages stating this version, in first-seen order."""
        return list(dict.fromkeys(f.url for f in self.facts))

    @property
    def quote(self) -> str:
        """The shortest sentence stating it (the clearest snippet)."""
        return min((f.quote for f in self.facts), key=lambda q: (len(q), q))

    @property
    def where(self) -> list[str]:
        """Page name and section of each place, without repeats."""
        out: list[str] = []
        for f in self.facts:
            place = (
                f.page_name
                if not f.section or f.section == f.page_name
                else f"{f.page_name} › {f.section}"
            )
            out.append(place)
        return list(dict.fromkeys(out))


@dataclass
class Topic:
    """A topic stated in two or more versions."""

    topic_id: str
    kind: str
    label: str
    versions: list[Version]
    sheet: Sheet
    severity: Severity
    condition: str = ""
    time_bound: str = ""

    @property
    def urls(self) -> list[str]:
        """Every page involved."""
        return list(dict.fromkeys(u for v in self.versions for u in v.urls))

    @property
    def websites(self) -> list[str]:
        """Every website involved."""
        return sorted({f.website for v in self.versions for f in v.facts if f.website})


@dataclass
class TopicResult:
    """Topics by sheet, and the counts behind them."""

    topics: list[Topic]
    facts: int
    config: TopicConfig
    pairs_capped: bool = False

    def on(self, sheet: Sheet) -> list[Topic]:
        """The topics of one sheet."""
        return [t for t in self.topics if t.sheet == sheet]

    def summary(self) -> dict[str, object]:
        """Counts for the manifest."""
        return {
            "facts": self.facts,
            "contradictions": len(self.on("contradiction")),
            "contradictions_high": sum(1 for t in self.on("contradiction") if t.severity == "High"),
            "differs_by_condition": len(self.on("condition")),
            "time_bound": len(self.on("time_bound")),
            "min_similarity": self.config.min_similarity,
            "pairs_capped": self.pairs_capped,
        }


_MIN_TERM_CAP = 50
"""A word links facts unless it is in more facts than this *and* than `max_term_share`."""


class _Union:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _components(facts: Sequence[Fact], config: TopicConfig) -> tuple[list[list[Fact]], bool]:
    """Facts of one kind linked by weighted content-word overlap."""
    n = len(facts)
    df: Counter[str] = Counter(t for f in facts for t in f.terms)
    idf = {
        t: 1.0 + math.log((n + 1) / (c + 0.5)) for t, c in df.items()
    }  # smoothed: every word counts
    cap = max(_MIN_TERM_CAP, int(config.max_term_share * n))
    index: dict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(facts):
        for t in f.terms:
            if df[t] <= cap:
                index[t].append(i)
    shared: set[tuple[int, int]] = set()
    capped = False
    for members in index.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                shared.add((members[x], members[y]))
        if len(shared) > config.max_pairs:
            capped = True
            break
    union = _Union(n)
    weight = {i: sum(idf[t] for t in f.terms) for i, f in enumerate(facts)}
    for i, j in shared:
        a, b = facts[i], facts[j]
        if len(a.terms & b.terms) < config.min_shared_terms:
            continue
        if a.quote == b.quote and a.url == b.url:
            continue
        if a.scales and b.scales and min(abs(x - y) for x in a.scales for y in b.scales) > 1:
            continue  # "20 minutes" and "52 weeks" are not the same commitment
        if a.url == b.url and a.section == b.section and len(a.own_terms & b.own_terms) < 2:
            continue  # two figures under one heading of one page: usually two different things
        common = sum(idf[t] for t in a.terms & b.terms)
        smaller = min(weight[i], weight[j])
        if smaller > 0 and common / smaller >= config.min_similarity:
            union.join(i, j)
    groups: dict[int, list[Fact]] = defaultdict(list)
    for i, f in enumerate(facts):
        groups[union.find(i)].append(f)
    return [g for g in groups.values() if len(g) > 1], capped


def _label(facts: Sequence[Fact], kind: str) -> str:
    questions = Counter(f.question for f in facts if f.question)
    if questions:
        return questions.most_common(1)[0][0]
    sections = Counter(f.section for f in facts if f.section and f.section != f.page_name)
    if sections and sections.most_common(1)[0][1] >= 2:
        return f"{sections.most_common(1)[0][0]}: {KIND_LABEL[kind].lower()}"
    terms = Counter(t for f in facts for t in f.terms)
    top = [t for t, c in terms.most_common() if c >= 2][:4] or [t for t, _ in terms.most_common(3)]
    return f"{' '.join(top).capitalize()}: {KIND_LABEL[kind].lower()}"


def severity_of(kind: str, texts: Iterable[str]) -> Severity:
    """High for customer commitments, Medium for contact details and specs, Low for marketing."""
    if kind in ("phone", "email"):
        return "Medium"
    if kind == "age":
        return "High"
    joined = " ".join(texts)
    if _HIGH.search(joined):
        return "High"
    if _LOW.search(joined):
        return "Low"
    return "Medium"


def _split(a: Iterable[frozenset[str]], b: Iterable[frozenset[str]]) -> tuple[str, str] | None:
    left = frozenset().union(*a)
    right = frozenset().union(*b)
    if left and right and left.isdisjoint(right):
        return ", ".join(sorted(left)), ", ".join(sorted(right))
    return None


def _condition(first: Version, second: Version) -> str:
    """How two versions split by condition, or '' when they do not."""
    checks = (
        ("Geography", lambda f: f.geography),
        ("Audience", lambda f: f.audience),
        ("Product or fund code", lambda f: f.codes),
        ("Website", lambda f: frozenset({f.website}) if f.website else frozenset()),
    )
    for name, get in checks:
        split = _split((get(f) for f in first.facts), (get(f) for f in second.facts))
        if split:
            return f"{name}: {split[0]} | {split[1]}"
    return ""


def _years_split(versions: Sequence[Version]) -> bool:
    """Every version names its own year(s): figures of different periods."""
    sets = [frozenset().union(*(f.years for f in v.facts)) for v in versions]
    if any(not s for s in sets):
        return False
    return all(
        sets[i].isdisjoint(sets[j]) for i in range(len(sets)) for j in range(i + 1, len(sets))
    )


def _topic(facts: Sequence[Fact], kind: str) -> Topic | None:
    by_value: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        by_value[f.value_key].append(f)
    if len(by_value) < 2:
        return None
    versions = [
        Version(
            value_key=k, value_text=Counter(f.value_text for f in fs).most_common(1)[0][0], facts=fs
        )
        for k, fs in by_value.items()
    ]
    versions.sort(key=lambda v: (-len(v.urls), -len(v.facts), v.value_key))
    label = _label(facts, kind)
    texts = [label, *(f.quote for f in facts), *(f.section for f in facts)]
    severity = severity_of(kind, texts)
    bound = next((f.time_bound for f in facts if f.time_bound), "")
    sheet: Sheet = "contradiction"
    condition = ""
    if bound and all(f.time_bound for f in facts):
        sheet = "time_bound"
    elif _years_split(versions):
        sheet, bound = "time_bound", "each version names a different year"
    else:
        condition = _condition(versions[0], versions[1])
        if condition:
            sheet = "condition"
    key = "|".join(sorted(f.fact_id for f in facts))
    topic_id = "tp-" + hashlib.sha256(key.encode()).hexdigest()[:12]
    return Topic(
        topic_id=topic_id,
        kind=kind,
        label=label,
        versions=versions,
        sheet=sheet,
        severity=severity,
        condition=condition,
        time_bound=bound if sheet == "time_bound" else "",
    )


def find_topics(facts: Sequence[Fact], config: TopicConfig | None = None) -> TopicResult:
    """Group facts into topics and keep those stated in two or more versions."""
    cfg = config or TopicConfig()
    by_kind: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        by_kind[f.kind].append(f)
    topics: list[Topic] = []
    capped = False
    for kind in sorted(by_kind):
        # Time-bound facts are grouped among themselves, so a press release never
        # joins a product page's topic.
        for bound in (False, True):
            part = [f for f in by_kind[kind] if bool(f.time_bound) == bound]
            groups, was_capped = _components(part, cfg)
            capped = capped or was_capped
            for group in groups:
                topic = _topic(group, kind)
                if topic is not None:
                    topics.append(topic)
    topics.sort(
        key=lambda t: (_SEVERITY_ORDER[t.severity], -len(t.urls), t.label.lower(), t.topic_id)
    )
    return TopicResult(topics=topics, facts=len(facts), config=cfg, pairs_capped=capped)


# -- tables -------------------------------------------------------------------------

TOPIC_COLUMNS: tuple[str, ...] = (
    "Topic",
    "Version A",
    "Version A URLs",
    "Where A appears",
    "Version B",
    "Where B appears",
    "Version B URLs",
    "Severity",
    "Recommended action",
    "Approved answer (ML to fill)",
    "Kind",
    "Value A",
    "Value B",
    "Other versions",
    "Pages involved",
    "Websites",
    "Condition",
    "Why time-bound",
    "Topic ID",
    "Block ids A",
    "Block ids B",
    "Evidence ids A",
    "Evidence ids B",
    "Captured at",
)
_WRAP = frozenset(
    {
        "Topic",
        "Version A",
        "Version A URLs",
        "Where A appears",
        "Version B",
        "Where B appears",
        "Version B URLs",
        "Recommended action",
        "Other versions",
    }
)
_WIDTHS = {
    "Topic": 34,
    "Version A": 48,
    "Version A URLs": 44,
    "Where A appears": 32,
    "Version B": 48,
    "Where B appears": 32,
    "Version B URLs": 44,
    "Severity": 10,
    "Recommended action": 44,
    "Approved answer (ML to fill)": 36,
    "Other versions": 48,
}
_WHAT = {
    "time": "timeline",
    "money": "amount",
    "percent": "percentage",
    "age": "age limit",
    "quantity": "figure",
    "phone": "phone number",
    "email": "email address",
}


def recommended_action(topic: Topic) -> str:
    """A fixed, plain sentence per kind and sheet."""
    n = len(topic.urls)
    what = _WHAT[topic.kind]
    if topic.sheet == "time_bound":
        return (
            "No change needed unless an old figure is still presented as current; date it if not."
        )
    if topic.sheet == "condition":
        return (
            f"Check the difference is intended ({topic.condition}); if it is, show the condition "
            f"next to the {what} on all {n} pages."
        )
    if topic.kind == "time":
        return (
            f"Agree one approved {what} and state it the same way on all {n} pages, with its "
            "condition (for example when the clock starts)."
        )
    if topic.kind in ("phone", "email"):
        return f"Confirm the right {what} for this purpose and use it on all {n} pages."
    if topic.kind in ("money", "percent"):
        return (
            f"Confirm the correct {what} and use it on all {n} pages; if it depends on product, "
            "plan or date, say so next to it."
        )
    return f"Confirm the correct {what} and state it the same way on all {n} pages."


def _lines(items: Iterable[str]) -> str:
    return "\n".join(items)


def topic_rows(topics: Sequence[Topic]) -> list[dict[str, Cell]]:
    """One row per topic in the requested layout, evidence at the end."""
    rows: list[dict[str, Cell]] = []
    for t in topics:
        a, b = t.versions[0], t.versions[1]
        others = [
            f'{v.value_text}: "{v.quote}" ({len(v.urls)} page{"s" if len(v.urls) != 1 else ""}: '
            f"{', '.join(v.urls)})"
            for v in t.versions[2:]
        ]
        captured = max(f.captured_at for v in t.versions for f in v.facts)
        rows.append(
            {
                "Topic": t.label,
                "Version A": a.quote,
                "Version A URLs": _lines(a.urls),
                "Where A appears": "; ".join(a.where),
                "Version B": b.quote,
                "Where B appears": "; ".join(b.where),
                "Version B URLs": _lines(b.urls),
                "Severity": t.severity,
                "Recommended action": recommended_action(t),
                "Approved answer (ML to fill)": "",
                "Kind": KIND_LABEL[t.kind],
                "Value A": a.value_text,
                "Value B": b.value_text,
                "Other versions": _lines(others),
                "Pages involved": len(t.urls),
                "Websites": ", ".join(t.websites),
                "Condition": t.condition,
                "Why time-bound": t.time_bound,
                "Topic ID": t.topic_id,
                "Block ids A": " ".join(dict.fromkeys(f.block_id for f in a.facts)),
                "Block ids B": " ".join(dict.fromkeys(f.block_id for f in b.facts)),
                "Evidence ids A": " ".join(
                    dict.fromkeys(e for f in a.facts for e in f.evidence_ids)
                ),
                "Evidence ids B": " ".join(
                    dict.fromkeys(e for f in b.facts for e in f.evidence_ids)
                ),
                "Captured at": captured.isoformat(),
            }
        )
    return rows


_NOTES: tuple[tuple[str, str], ...] = (
    (
        "Contradictions",
        (
            "Topics stated in two or more different ways across the pages. Fix these first, High "
            "severity at the top."
        ),
    ),
    (
        "Differs by condition",
        (
            "The versions split cleanly by geography, audience, product or fund code, or website "
            "(brand). Often intended: check, and show the condition on the page."
        ),
    ),
    (
        "Time-bound figures",
        (
            "Figures from news and press pages, or tied to a date or period (as of, declared, "
            "different years). Not contradictions; listed so nothing is hidden."
        ),
    ),
    (
        "Topic",
        (
            "The question or heading the statements sit under, else the words they share, with the "
            "kind of value."
        ),
    ),
    (
        "Version A / B",
        (
            "The sentence as written on the page (the shortest one stating that value). "
            "Version A is "
            "the one on the most pages."
        ),
    ),
    ("Where A appears", "Page name › section, for every place stating that version."),
    (
        "Severity",
        (
            "High: customer commitments (payout or processing times, fees, prices, eligibility, "
            "coverage, limits, legal terms). Medium: product details and contact details. Low: "
            "marketing figures."
        ),
    ),
    ("Approved answer (ML to fill)", "Left empty for the team to write the one approved answer."),
    (
        "Value A / B",
        (
            "The values read from the sentences, normalised: '7–10 business days', "
            "'within 60 days' "
            "is read as '60 days'."
        ),
    ),
    ("Other versions", "Versions C, D … with their sentence and pages."),
    (
        "How it is found",
        (
            "Rules only, no AI model: every sentence is read for times, amounts, "
            "percentages, ages, "
            "quantities, phone numbers and emails; statements are grouped into one topic when they "
            "share enough uncommon words (the sentence plus its question or heading). Statements "
            "worded completely differently are not grouped: the Semantic module will catch those."
        ),
    ),
    (
        "Evidence",
        "Block ids and evidence ids point to the captured page text, as in every other report.",
    ),
)


def topic_tables(result: TopicResult, *, name_prefix: str = "") -> list[Table]:
    """Sheets: Contradictions, Differs by condition, Time-bound figures, How to read."""

    def table(name: str, topics: Sequence[Topic]) -> Table:
        return Table(
            name=f"{name_prefix}{name}",
            columns=list(TOPIC_COLUMNS),
            rows=topic_rows(topics),
            wrap_columns=_WRAP,
            widths=_WIDTHS,
        )

    sheets = [
        table("Contradictions", result.on("contradiction")),
        table("Differs by condition", result.on("condition")),
        table("Time-bound figures", result.on("time_bound")),
    ]
    if not name_prefix:
        sheets.append(
            Table(
                name="How to read",
                columns=["Item", "Meaning"],
                rows=[{"Item": k, "Meaning": v} for k, v in _NOTES],
                wrap_columns=frozenset({"Meaning"}),
                widths={"Item": 28, "Meaning": 110},
            )
        )
    return sheets
