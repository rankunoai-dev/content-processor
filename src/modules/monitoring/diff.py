"""What changed on a website between two of its scrapes, page by page.

Pages are compared by their stamps (title, description, H1, canonical, numbers) and
their text, line by line, with case, spacing and punctuation ignored, so a change in
how the engine cuts a page into sections is not a change of the page. A field
captured in only one of the two scrapes is not counted as a rewrite. A page's
**drift** is the share of its distinct lines that were added or removed.
**Prices** are amounts (currency, or a percentage) found in the same wording in
both scrapes with a different value, for example "$25 per month" in "Plans start
at $25 per month" becoming "$19 per month"; an offer in the page's structured data
whose price changed counts too.

**Search-engine facts** (from the stamps, so every scrape has them): structured-data
types gained and lost, a readable block that broke, the robots tag starting or
stopping noindex, a canonical that pointed at the page itself and now points
elsewhere, and strong calls to action gained or lost. They are compared only when
both scrapes read the page's stored HTML. A call to action gained (or lost) on 3+
pages at once is a template change and is left out. Additions are not reported when
the older scrape has none of that fact on any page (an engine that did not record it
yet, not a website change). **Images without alt text** come from the run's
inventory (`<run>.inventory.jsonl`), which is made on demand: they are compared only
when both scrapes have one.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from src.modules.content_processor.knowledge.catalog import url_key
from src.modules.content_processor.knowledge.models import Chunk, PageStamp
from src.modules.monitoring.models import PageChange, PriceChange, TemplateChange

__all__ = [
    "FURNITURE_PAGES",
    "TEMPLATE_PAGES",
    "RunDiff",
    "RunFiles",
    "diff_runs",
    "furniture",
    "load_run_files",
    "price_changes",
]

_AMOUNT = re.compile(
    r"[$€£]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:per|a|/)\s?(?:month|year|yr|mo|day|week|share|unit|user))?"
    r"|\b\d+(?:\.\d+)?\s?%"
)
_WORD = re.compile(r"[a-z]+")
_WORD_OR_NUMBER = re.compile(r"[^\W_]+")
_CONTEXT_WORDS = 4
_MAX_TEXT = 800
_MAX_SAMPLES = 5
_HEAD_FIELDS = ("title", "meta_description", "h1", "canonical")
TEMPLATE_PAGES = 3
# site furniture (menus, footers, disclaimers) left out of the meaning comparison: a line
# on this many pages, or this share of them (the rule of `analysis.semantic`)
FURNITURE_PAGES = 5
FURNITURE_SHARE = 0.02
_MAX_TEMPLATE = 40


@dataclass
class RunFiles:
    """One scrape of a website: its page stamps and sections, by address."""

    stamps: dict[str, PageStamp] = field(default_factory=dict)
    chunks: dict[str, list[Chunk]] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)  # every address of the scrape, compared or not
    # page -> (images, images without alt text); None when the run has no inventory
    images: dict[str, tuple[int, int]] | None = None


_NO_ALT = frozenset({"missing", "file_name"})


def load_image_counts(path: Path) -> dict[str, tuple[int, int]]:
    """Images and images without alt text per page, from an inventory file (hidden left out)."""
    out: dict[str, tuple[int, int]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                page = json.loads(line)
            except ValueError:
                continue
            if page.get("status") != "inventoried":
                continue
            images = [i for i in page.get("images") or [] if not i.get("hidden")]
            missing = sum(1 for i in images if i.get("alt_status") in _NO_ALT)
            out[str(page.get("url"))] = (len(images), missing)
    return out


def load_run_files(folder: Path, run_id: str) -> RunFiles:
    """Stamps and chunks of a run folder (missing files give an empty scrape)."""
    files = RunFiles()
    stamps = folder / f"{run_id}.stamps.jsonl"
    if stamps.exists():
        with stamps.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    stamp = PageStamp.model_validate_json(line)
                    files.seen.add(stamp.url)
                    # tracking pixels, files and error or blocked pages are not pages to watch
                    if stamp.knowledge_eligible and not (stamp.is_tracking or stamp.is_asset):
                        files.stamps[stamp.url] = stamp
    chunks = folder / f"{run_id}.chunks.jsonl"
    if chunks.exists():
        with chunks.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    chunk = Chunk.model_validate_json(line)
                    files.chunks.setdefault(chunk.url, []).append(chunk)
    inventory = folder / f"{run_id}.inventory.jsonl"
    if inventory.exists():
        files.images = load_image_counts(inventory)
    return files


def _context(text: str, start: int, end: int) -> str:
    before = _WORD.findall(text[:start].casefold())[-_CONTEXT_WORDS:]
    after = _WORD.findall(text[end:].casefold())[:2]
    return " ".join([*before, "_", *after])


def _amounts(texts: Iterable[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for text in texts:
        for match in _AMOUNT.finditer(text):
            key = _context(text, match.start(), match.end())
            found.setdefault(key, set()).add(" ".join(match.group(0).split()))
    return found


def price_changes(before: Iterable[str], after: Iterable[str]) -> list[PriceChange]:
    """Amounts whose wording is the same in both texts but whose value changed."""
    old, new = _amounts(before), _amounts(after)
    changes: list[PriceChange] = []
    for key in sorted(set(old) & set(new)):
        gone, came = sorted(old[key] - new[key]), sorted(new[key] - old[key])
        for was, now in zip(gone, came, strict=False):
            changes.append(PriceChange(before=was, after=now, context=key.replace("_", "…")))
    return changes


_PERIOD = re.compile(
    r"(?:per|a|/)\s?(?:month|year|yr|mo|user|seat|license|licence)\b|monthly|annually|/mo\b",
    re.IGNORECASE,
)
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9+&'-]*")
_STOP_TERMS = frozenset(
    {
        "about",
        "with",
        "your",
        "from",
        "that",
        "this",
        "what",
        "when",
        "where",
        "which",
        "their",
        "into",
        "more",
        "than",
        "have",
        "will",
        "they",
        "them",
        "these",
        "those",
        "using",
        "used",
        "make",
        "made",
        "each",
        "other",
        "also",
        "only",
        "over",
        "under",
    }
)
_BLOCK_KINDS = {"table": "tables", "faq": "FAQ answers", "form_steps": "forms"}
_MAX_HEADINGS = 15


def _terms(texts: Iterable[str]) -> dict[str, str]:
    """Distinct meaningful words (case-folded -> as written): acronyms and words of 4+."""
    out: dict[str, str] = {}
    for text in texts:
        for word in _TERM.findall(text):
            low = word.casefold()
            if (len(word) >= 4 or (word.isupper() and len(word) >= 2)) and low not in _STOP_TERMS:
                out.setdefault(low, word)
    return out


def _blocks(chunks: list[Chunk]) -> Counter[str]:
    """Tables, pricing blocks, FAQ answers and forms of a page."""
    counts: Counter[str] = Counter()
    for chunk in chunks:
        name = _BLOCK_KINDS.get(chunk.kind)
        if name:
            counts[name] += 1
        if _AMOUNT.search(chunk.text) and _PERIOD.search(chunk.text):
            counts["pricing blocks"] += 1
    return counts


def _outline(a: PageStamp, b: PageStamp) -> tuple[list[str], list[str], list[str], list[str]]:
    """H2/H3 headings added and removed; words new to and gone from the headings and H1."""
    before = {_norm(h): h for h in a.headings if _norm(h)}
    after = {_norm(h): h for h in b.headings if _norm(h)}
    added = [h for k, h in after.items() if k not in before]
    removed = [h for k, h in before.items() if k not in after]
    old_terms = _terms([*a.headings, a.h1 or ""])
    new_terms = _terms([*b.headings, b.h1 or ""])
    gained = [w for k, w in new_terms.items() if k not in old_terms]
    lost = [w for k, w in old_terms.items() if k not in new_terms]
    return added[:_MAX_HEADINGS], removed[:_MAX_HEADINGS], gained[:20], lost[:20]


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1].rstrip() + "…"


def _lines(chunks: list[Chunk]) -> list[str]:
    """The page's text lines in order (a chunk holds one block per line)."""
    return [
        line.strip()
        for c in sorted(chunks, key=lambda c: c.order)
        for line in c.text.splitlines()
        if line.strip()
    ]


def _norm(line: str) -> str:
    """A line as compared: case, spacing and punctuation do not count as a change."""
    return " ".join(_WORD_OR_NUMBER.findall(line.casefold()))


def furniture(files: RunFiles) -> set[str]:
    """Lines (as compared) on many pages of one scrape: menus, footers, disclaimers."""
    counts: Counter[str] = Counter()
    for url in files.stamps:
        counts.update({_norm(t) for t in _lines(files.chunks.get(url, []))} - {""})
    limit = max(FURNITURE_PAGES, math.ceil(len(files.stamps) * FURNITURE_SHARE))
    return {t for t, n in counts.items() if n >= limit}


def _read_head(stamp: PageStamp) -> bool:
    return stamp.metadata_source == "stored_html"


def _noindex(stamp: PageStamp) -> bool:
    return "noindex" in (stamp.robots_meta or "").casefold()


def _self_canonical(stamp: PageStamp) -> bool:
    return bool(stamp.canonical) and url_key(stamp.canonical) == url_key(stamp.url)


def _strong_ctas(stamp: PageStamp) -> dict[str, str]:
    return {_norm(c.text): c.text for c in stamp.ctas if c.strength == "strong" and _norm(c.text)}


def _offer_prices(a: PageStamp, b: PageStamp) -> list[PriceChange]:
    """Offers in the structured data whose price changed ("name: price currency")."""

    def split(offers: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for offer in offers:
            name, sep, price = offer.rpartition(":")
            if sep and price.strip():
                out.setdefault(_norm(name) or "offer", price.strip())
        return out

    old, new = split(a.schema_org.offers), split(b.schema_org.offers)
    return [
        PriceChange(before=old[k], after=new[k], context=f"structured data offer: {k}")
        for k in sorted(set(old) & set(new))
        if _norm(old[k]) != _norm(new[k])
    ]


@dataclass
class _Recorded:
    """Which facts the older scrape records at all (additions count only then)."""

    schema: bool = False
    ctas: bool = False


def _recorded(files: RunFiles) -> _Recorded:
    return _Recorded(
        schema=any(s.schema_org.types for s in files.stamps.values()),
        ctas=any(s.ctas for s in files.stamps.values()),
    )


def _seo_facts(
    a: PageStamp, b: PageStamp, recorded: _Recorded, cta_template: set[str]
) -> dict[str, object]:
    """Search-engine facts that changed (both scrapes must have read the stored HTML)."""
    if not (_read_head(a) and _read_head(b)):
        return {}
    seo: dict[str, object] = {}
    types_a, types_b = set(a.schema_org.types), set(b.schema_org.types)
    if recorded.schema:
        seo["schema_added"] = sorted(types_b - types_a)
    seo["schema_removed"] = sorted(types_a - types_b)
    seo["schema_broken"] = b.schema_org.unreadable_blocks > a.schema_org.unreadable_blocks
    if _noindex(a) != _noindex(b):
        seo["noindex"] = "added" if _noindex(b) else "removed"
    seo["canonical_moved"] = _self_canonical(a) and bool(b.canonical) and not _self_canonical(b)
    cta_a, cta_b = _strong_ctas(a), _strong_ctas(b)
    if recorded.ctas:
        seo["ctas_added"] = [v for k, v in cta_b.items() if k not in set(cta_a) | cta_template]
    seo["ctas_removed"] = [v for k, v in cta_a.items() if k not in set(cta_b) | cta_template]
    if seo.get("ctas_added"):
        seo["strong_ctas_before"] = len(cta_a)
    return seo


@dataclass
class RunDiff:
    """Two scrapes of a website compared."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[PageChange] = field(default_factory=list)
    unchanged: int = 0
    template: list[TemplateChange] = field(default_factory=list)
    # changed pages: their own lines before and after (template and furniture lines
    # removed), for the meaning comparison
    texts: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _page_lines(
    old: RunFiles, new: RunFiles, url: str
) -> tuple[list[str], list[str], set[str], set[str]]:
    before_lines = _lines(old.chunks.get(url, []))
    after_lines = _lines(new.chunks.get(url, []))
    before_set = {_norm(t) for t in before_lines} - {""}
    after_set = {_norm(t) for t in after_lines} - {""}
    gained = list(dict.fromkeys(t for t in after_lines if _norm(t) and _norm(t) not in before_set))
    lost = list(dict.fromkeys(t for t in before_lines if _norm(t) and _norm(t) not in after_set))
    return gained, lost, before_set, after_set


def diff_runs(old: RunFiles, new: RunFiles, *, template_pages: int = TEMPLATE_PAGES) -> RunDiff:
    """Compare two scrapes of a website, page by page.

    A line added (or removed) on `template_pages` pages or more is a template or
    related-content change (navigation, "Read more", links to other articles): it is
    reported once, for the website, and not as a change of each page.
    """
    # new: not in the older scrape at all; removed: not in the newer scrape at all (a page
    # still there but now a duplicate or an error page is not "removed")
    result = RunDiff(
        added=sorted(set(new.stamps) - (old.seen | set(old.stamps))),
        removed=sorted(set(old.stamps) - (new.seen | set(new.stamps))),
    )
    shared = sorted(set(old.stamps) & set(new.stamps))
    raw = {url: _page_lines(old, new, url) for url in shared}
    gained_on: Counter[str] = Counter()
    lost_on: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for gained, lost, _b, _a in raw.values():
        gained_on.update({_norm(x) for x in gained})
        lost_on.update({_norm(x) for x in lost})
        for t in [*gained, *lost]:
            examples.setdefault(_norm(t), t)
    template = {t for t, n in gained_on.items() if n >= template_pages}
    template |= {t for t, n in lost_on.items() if n >= template_pages}
    # calls to action gained or lost on many pages at once are a template change: reported
    # once for the website (a site-wide "Book a demo" is news), not on every page
    cta_gained: Counter[str] = Counter()
    cta_lost: Counter[str] = Counter()
    cta_text: dict[str, str] = {}
    for url in shared:
        before_cta, after_cta = _strong_ctas(old.stamps[url]), _strong_ctas(new.stamps[url])
        cta_gained.update(set(after_cta) - set(before_cta))
        cta_lost.update(set(before_cta) - set(after_cta))
        cta_text.update(before_cta | after_cta)
    cta_template = {k for k, n in (cta_gained + cta_lost).items() if n >= template_pages}
    recorded = _recorded(old)
    if not recorded.schema and any(new.stamps[u].schema_org.types for u in shared):
        result.notes.append(
            "structured data: the older scrape recorded none on any page, so additions are "
            "not reported this time"
        )
    images = old.images is not None and new.images is not None
    if not images and (old.images is not None or new.images is not None):
        result.notes.append(
            "images: only one of the two scrapes has an inventory, so alt text was not compared"
        )
    shared_text = template | furniture(old) | furniture(new)
    ordered = sorted(template, key=lambda t: (-(gained_on.get(t, 0) + lost_on.get(t, 0)), t))
    result.template = [
        TemplateChange(
            line=_clip(examples[t]), added_on=gained_on.get(t, 0), removed_on=lost_on.get(t, 0)
        )
        for t in ordered[:_MAX_TEMPLATE]
    ]
    result.template += [
        TemplateChange(
            line=f"Call to action: {cta_text[k]}",
            added_on=cta_gained.get(k, 0),
            removed_on=cta_lost.get(k, 0),
        )
        for k in sorted(cta_template, key=lambda k: -(cta_gained[k] + cta_lost[k]))[:10]
    ]
    for url in shared:
        a, b = old.stamps[url], new.stamps[url]
        # a field captured in only one scrape is not a rewrite: both values must exist
        head = {
            name: {"before": getattr(a, name), "after": getattr(b, name)}
            for name in _HEAD_FIELDS
            if getattr(a, name)
            and getattr(b, name)
            and _norm(getattr(a, name)) != _norm(getattr(b, name))
        }
        gained, lost, before_set, after_set = raw[url]
        gained = [t for t in gained if _norm(t) not in template]
        lost = [t for t in lost if _norm(t) not in template]
        numbers = sorted(set(b.numbers) - set(a.numbers))
        h_added, h_removed, terms_new, terms_lost = _outline(a, b)
        seo = _seo_facts(a, b, recorded, cta_template)
        alt: tuple[int, int] | None = None
        if old.images is not None and new.images is not None:
            was, now = old.images.get(url), new.images.get(url)
            if was is not None and now is not None:
                alt = (was[1], now[1])
        alt_spike = alt is not None and alt[1] - alt[0] >= 3
        seo_changed = any(bool(v) for v in seo.values()) or alt_spike
        if not (head or gained or lost or h_added or h_removed or seo_changed):
            result.unchanged += 1
            continue
        result.texts[url] = (
            [t for t in _lines(old.chunks.get(url, [])) if _norm(t) not in shared_text],
            [t for t in _lines(new.chunks.get(url, [])) if _norm(t) not in shared_text],
        )
        blocks_before = _blocks(old.chunks.get(url, []))
        blocks_after = _blocks(new.chunks.get(url, []))
        before_set, after_set = before_set - template, after_set - template
        union = before_set | after_set
        result.changed.append(
            PageChange(
                url=url,
                title=b.title,
                head_changes=head,
                sections_added=len(gained),
                sections_removed=len(lost),
                added_text=[_clip(t) for t in gained[:_MAX_SAMPLES]],
                removed_text=[_clip(t) for t in lost[:_MAX_SAMPLES]],
                drift=round(len(union - (before_set & after_set)) / max(len(union), 1), 3),
                prices=[*price_changes(lost, gained), *(_offer_prices(a, b) if seo else [])],
                new_numbers=numbers[:20],
                words_before=a.main_word_count or a.word_count,
                words_after=b.main_word_count or b.word_count,
                headings_added=h_added,
                headings_removed=h_removed,
                new_terms=terms_new if (h_added or "h1" in head) else [],
                dropped_terms=terms_lost if (h_removed or "h1" in head) else [],
                blocks_added={
                    k: n - blocks_before.get(k, 0)
                    for k, n in blocks_after.items()
                    if n > blocks_before.get(k, 0)
                },
                blocks_removed={
                    k: n - blocks_after.get(k, 0)
                    for k, n in blocks_before.items()
                    if n > blocks_after.get(k, 0)
                },
                images_missing_alt=alt,
                **seo,
            )
        )
    return result
