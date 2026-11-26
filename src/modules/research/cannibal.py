"""Keyword cannibalisation plan: which page should rank for each search, and how to fix it.

The rules of the keyword-cannibalisation playbook (v1.0), on data the app already holds
instead of hand-made exports: the theme map is the topic map (its client pillar page is
each topic's owner), the scrape inventory gives body links and anchors, knowledge chunks
give body paragraphs, Search Console query-page pairs (or the Semrush rows) give the
searches. Pure code, $0.

1. **Cleaning.** Branded searches, searches beyond position 20, and likely automated
   searches (0 clicks, 200+ impressions, 4+ pages of the site shown) are set aside.
2. **Funnel.** Buying wording or a model number is BOFU; comparison or a short commercial
   head term is MOFU; questions and everything else TOFU (from `intent.py`).
3. **Topic and owner.** A search's topic is the theme whose terms share most of its words
   (not the page it lands on); the owner is the client's pillar page of that theme. A
   model number goes to the page whose address carries it.
4. **Status.** Aligned on the owner; also aligned when a TOFU or MOFU search lands on an
   article of the same theme, or on a page of the theme more specific than the owner;
   otherwise misaligned (no page yet when the topic has no client page).
5. **Page alignment** = aligned share of the page's top-20 weight: strong 95%+, within
   tolerance 90-95%, needs work under 90% (scored from 50 impressions or visits up).
6. **Competing pairs** (misaligned searches on needs-work pages, per page and owner) get
   one of eight patterns: remove from index (utility pages), merge and redirect (articles
   sharing half their paragraphs, at least 4), localise (a location page under its
   parent), point up to parent (a child ranking for the hub's term), hand off to child (a
   hub ranking for a child's term), separate by intent (an article ranking for BOFU
   searches), answer then link (a business page ranking for questions), differentiate
   siblings (anything else).
7. **Links** (page text only): repoint a link whose anchor uses the owner's search words
   but points to the competing page; fix generic anchors ("Read more") and image links
   without alt text to the pages of a pair, unless the same page already has a
   descriptive link there; add up to 3 links per page from the competing page to its owner
   when the page's text mentions the owner's topic.
8. **Template text**: a paragraph on 5+ pages is template text, fixed once, and never
   counts as shared content or as a topic mention.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Final, Literal

from src.modules.content_processor.knowledge.inventory_models import PageInventory
from src.modules.research.build import estimated_visits
from src.modules.research.gsc import GscSnapshot
from src.modules.research.intent import intent_of
from src.modules.research.links import CHROME, page_key
from src.modules.research.models import WebsiteResearch
from src.modules.research.page_models import (
    ConsolidationPair,
    ConsolidationPlan,
    Funnel,
    LinkChange,
    PageAlignment,
    Pattern,
    SearchLine,
    SearchStatus,
    TechnicalChange,
)
from src.modules.theming.models import ThemeMap

__all__ = ["FIXES", "GENERIC_ANCHORS", "build_plan", "funnel_of", "page_class"]

STRONG: Final = 0.95
TOLERANCE: Final = 0.10
MIN_WEIGHT: Final = 50.0
MAX_POSITION: Final = 20
BOT_IMPRESSIONS: Final = 200
BOT_PAGES: Final = 4
TEMPLATE_PAGES: Final = 5
MERGE_SHARE: Final = 0.5
MERGE_MIN: Final = 4
MAX_NEW_LINKS: Final = 3
_MATCH_SHARE: Final = 0.5
_MIN_PARAGRAPH_WORDS: Final = 8

FIXES: Final[dict[str, str]] = {
    "hand_off_to_child": "Shorten the hub's copy on this topic to one line and a link to the "
    "child page that owns it.",
    "point_up_to_parent": "Keep the child page's own modifier in its title and headings and "
    "link up to the parent page for the general term.",
    "separate_by_intent": "Keep the article's learning content, drop its sales wording and "
    "calls to buy, and link to the business page for the buying searches.",
    "answer_then_link": "Give a short answer on the business page, then link to the article "
    "that explains it fully.",
    "differentiate_siblings": "Lead each page with what is different about it (model, "
    "audience, size, use) so each owns its own searches.",
    "localise": "Rewrite the intro and add one local section with real local facts; compare "
    "it with its parent page only.",
    "merge_and_redirect": "Move the unique text to the page that stays, 301 redirect this "
    "page to it, repoint internal links and take it out of the sitemap.",
    "remove_from_index": "Set noindex, follow and take the page out of the XML sitemap.",
}
GENERIC_ANCHORS: Final = frozenset(
    {
        "learn more", "read more", "more", "click here", "here", "see more", "find out more",
        "discover more", "view more", "continue reading", "details", "more info", "view",
        "explore", "read", "go", "this", "link",
    }
)  # fmt: skip
_BUSINESS: Final = frozenset(
    {"home", "product", "category", "service", "pricing", "location", "landing"}
)
_ARTICLE: Final = frozenset({"article", "case_study", "faq", "document", "event"})
_UTILITY: Final = frozenset({"legal", "careers", "contact", "about", "form"})
_UTILITY_URL: Final = re.compile(
    r"/(author|authors|tag|tags|sitemap|search|page/\d+|login|account|cart|checkout)(/|$|\?)"
)
_ARTICLE_URL: Final = re.compile(
    r"/(blog|news|insights?|articles?|resources|podcasts?|guides?|stories|press)(/|$)"
)
_LOCATION_URL: Final = re.compile(r"/(locations?|areas?|cities|near-me|service-areas?)(/|$)")
_MODEL: Final = re.compile(r"\b([a-z]{1,3}\d{3,5}[a-z]?)\b")
_QUESTION: Final = frozenset(
    {"what", "how", "why", "when", "who", "which", "where", "is", "are", "does", "do", "can"}
)
_WORD: Final = re.compile(r"[a-z0-9]+")
_STOP: Final = frozenset(
    {"the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "with", "is", "are",
     "what", "how", "why", "best", "vs", "my", "your", "near", "me", "do", "does", "can"}
)  # fmt: skip


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if w not in _STOP and len(w) > 1}


def funnel_of(query: str, brands: Iterable[str] = ()) -> Funnel:
    """BOFU for buying wording or a model number, MOFU for choosing, TOFU otherwise."""
    text = query.casefold()
    if _MODEL.search(text):
        return "bofu"
    first = text.split()[0] if text.split() else ""
    if first in _QUESTION:  # the playbook reads a question first: it is learning
        return "tofu"
    intent = intent_of(query, brands)
    if intent == "transactional":
        return "bofu"
    if intent == "commercial":
        return "mofu"
    return "tofu"


def page_class(url: str, page_type: str | None) -> Literal["business", "article", "utility"]:
    """Business, article or utility page, from its page type or else its address."""
    path = "/" + page_key(url).split("/", 1)[-1]
    if _UTILITY_URL.search(path) or page_type in _UTILITY:
        return "utility"
    if page_type in _ARTICLE:
        return "article"
    if page_type in _BUSINESS:
        return "business"
    return "article" if _ARTICLE_URL.search(path) else "business"


def _path(url: str) -> str:
    return "/" + page_key(url).split("/", 1)[-1].strip("/")


def _is_child(child: str, parent: str) -> bool:
    c, p = _path(child), _path(parent)
    return p != c and (p == "/" or c.startswith(p.rstrip("/") + "/"))


def _norm(text: str) -> str:
    return " ".join(_WORD.findall(text.casefold()))


class _Topics:
    """Topic (theme by words) and its owner (the client's pillar page)."""

    def __init__(self, theme_map: ThemeMap | None, website: str, urls: Sequence[str]) -> None:
        self.words: dict[str, set[str]] = {}
        self.labels: dict[str, str] = {}
        self.owner: dict[str, str] = {}
        self.page_theme: dict[str, str] = {}
        self.page_types: dict[str, str | None] = {}
        self.pillars: set[str] = set()
        self.urls = list(urls)
        if theme_map is None:
            return
        for t in theme_map.themes:
            self.words[t.theme_id] = _words(" ".join(t.terms))
            self.labels[t.theme_id] = t.label
            for cov in t.coverage:
                if cov.website.removeprefix("www.") == website and cov.pillar is not None:
                    self.owner[t.theme_id] = cov.pillar.url
                    self.pillars.add(page_key(cov.pillar.url))
        for p in theme_map.pages:
            key = page_key(p.url)
            self.page_types[key] = p.page_type
            if p.theme_id:
                self.page_theme[key] = p.theme_id

    def topic(self, query: str) -> str | None:
        words = _words(query)
        if not words:
            return None
        best: tuple[float, str] | None = None
        for theme_id, tw in self.words.items():
            share = len(words & tw) / len(words)
            if share >= _MATCH_SHARE and (best is None or share > best[0]):
                best = (share, theme_id)
        return best[1] if best else None

    def model_owner(self, query: str, texts: Mapping[str, str]) -> str | None:
        """The page whose address carries a model number of the query (its text breaks ties)."""
        found = _MODEL.search(query.casefold())
        if not found:
            return None
        model = found.group(1)
        pages = [u for u in self.urls if model in u.casefold()]
        if not pages:
            return None
        named = [u for u in pages if model in texts.get(page_key(u), "")]
        return sorted(named or pages, key=len)[0]


def _searches(
    site: WebsiteResearch | None, gsc: GscSnapshot | None, brands: Sequence[str]
) -> tuple[list[tuple[str, str, float, int | None, float | None]], dict[str, float], str]:
    """(query, page, weight, clicks, position) and what was set aside, by source."""
    aside: dict[str, float] = defaultdict(float)
    out: list[tuple[str, str, float, int | None, float | None]] = []

    def branded(q: str) -> bool:
        text = q.casefold()
        joined = text.replace(" ", "")
        return any(b in text or b.replace(" ", "") in joined for b in brands)

    pair_queries = {p.query for p in gsc.pairs if p.query} if gsc is not None else set()
    full_pairs = (
        gsc is not None
        and bool(pair_queries)
        and (not gsc.queries or len(pair_queries) >= 0.5 * len(gsc.queries))
    )
    if gsc is not None and full_pairs:
        per_query: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        for p in gsc.pairs:
            if p.query and p.page:
                per_query[p.query].append((p.page, p.clicks, p.impressions))
        automated = {
            q
            for q, rows in per_query.items()
            if sum(c for _p, c, _i in rows) == 0
            and sum(i for _p, _c, i in rows) >= BOT_IMPRESSIONS
            and len({page_key(p) for p, _c, _i in rows}) >= BOT_PAGES
        }
        for p in gsc.pairs:
            if not (p.query and p.page):
                continue
            if branded(p.query):
                aside["branded"] += p.impressions
            elif p.query in automated:
                aside["likely automated (0 clicks, 200+ impressions, 4+ pages)"] += p.impressions
            elif p.position > MAX_POSITION:
                aside["beyond position 20"] += p.impressions
            else:
                out.append((p.query, p.page, float(p.impressions), p.clicks, p.position))
        return out, dict(aside), "search_console"
    if site is not None and site.keywords:
        for r in site.keywords:
            visits = estimated_visits(r.volume, r.position)
            if not r.url:
                continue
            if branded(r.keyword):
                aside["branded"] += visits
            elif r.position > MAX_POSITION:
                aside["beyond position 20"] += r.volume * 0.001
            else:
                out.append((r.keyword, r.url, max(visits, 0.1), None, float(r.position)))
                for other in r.other_urls:  # a second page shows for it too, lower
                    out.append((r.keyword, other, max(r.volume * 0.005, 0.1), None, None))
        return out, dict(aside), "search_data"
    return out, dict(aside), "none"


def _template_paragraphs(paragraphs: Mapping[str, Sequence[str]]) -> set[str]:
    counts = Counter(p for paras in paragraphs.values() for p in set(paras))
    return {p for p, n in counts.items() if n >= TEMPLATE_PAGES}


def build_plan(
    website: str,
    *,
    site: WebsiteResearch | None,
    gsc: GscSnapshot | None,
    theme_map: ThemeMap | None,
    inventory: Sequence[PageInventory],
    paragraphs: Mapping[str, Sequence[str]],
    brands: Sequence[str],
    top_keyword: Mapping[str, str] | None = None,
) -> ConsolidationPlan:
    """The plan for one website; `paragraphs` are body paragraphs by page address."""
    raw, aside, source = _searches(site, gsc, brands)
    plan = ConsolidationPlan(source=source, tolerance=TOLERANCE, set_aside=aside)
    by_key = {
        page_key(u): [_norm(p) for p in paras if len(p.split()) >= _MIN_PARAGRAPH_WORDS]
        for u, paras in paragraphs.items()
    }
    template = _template_paragraphs(by_key)
    plan.template_paragraphs = len(template)
    body = {k: [p for p in paras if p not in template] for k, paras in by_key.items()}
    texts = {k: " ".join(paras) for k, paras in body.items()}
    urls = sorted({p.url for p in inventory if p.status == "inventoried"} | {r[1] for r in raw})
    topics = _Topics(theme_map, website, urls)

    def cls(url: str) -> Literal["business", "article", "utility"]:
        return page_class(url, topics.page_types.get(page_key(url)))

    lines: list[SearchLine] = []
    for query, page, weight, clicks, position in raw:
        funnel = funnel_of(query, brands)
        model_page = topics.model_owner(query, texts)
        topic = topics.topic(query)
        owner = model_page or (topics.owner.get(topic) if topic else None)
        status: SearchStatus
        why = ""
        if model_page is None and topic is None:
            status, why = "unplaced", "no theme matches its words"
        elif owner is None:
            status, why = "no_page", "the client has no page for this theme yet"
        elif page_key(owner) == page_key(page):
            status = "aligned"
        elif (
            model_page is None
            and funnel in ("tofu", "mofu")
            and cls(page) == "article"
            and topics.page_theme.get(page_key(page)) == topic
        ):
            status, why = (
                "also_aligned",
                "a learning or comparison search on an article of its theme",
            )
        elif (
            model_page is None
            and topics.page_theme.get(page_key(page)) == topic
            and _is_child(page, owner)
        ):
            status, why = "also_aligned", "the page is more specific than the theme's page"
        else:
            status = "misaligned"
            why = (
                "a model number search belongs on the model's page"
                if model_page
                else f"belongs on the theme's page ({owner})"
            )
        lines.append(
            SearchLine(
                query=query,
                page=page,
                weight=round(weight, 2),
                clicks=clicks,
                position=position,
                funnel=funnel,
                topic=topics.labels.get(topic) if topic else None,
                owner=owner,
                status=status,
                why=why,
            )
        )
    plan.searches = sorted(lines, key=lambda s: -s.weight)

    per_page: dict[str, list[SearchLine]] = defaultdict(list)
    for line in lines:
        if line.status != "unplaced":
            per_page[page_key(line.page)].append(line)
    alignments: dict[str, PageAlignment] = {}
    for key, page_lines in per_page.items():
        scored = [s for s in page_lines if s.status != "no_page"]
        weight = sum(s.weight for s in scored)
        aligned = sum(s.weight for s in scored if s.status in ("aligned", "also_aligned"))
        share = aligned / weight if weight else 1.0
        misaligned: dict[str, float] = defaultdict(float)
        for s in scored:
            if s.status == "misaligned" and s.owner:
                misaligned[s.owner] += s.weight
        level: Literal["strong", "within_tolerance", "needs_work", "too_little_data"]
        if weight < MIN_WEIGHT:
            level = "too_little_data"
        elif share >= STRONG:
            level = "strong"
        elif share >= 1 - TOLERANCE:
            level = "within_tolerance"
        else:
            level = "needs_work"
        url = page_lines[0].page
        alignments[key] = PageAlignment(
            url=url,
            page_class=cls(url),
            weight=round(weight, 2),
            aligned=round(aligned, 2),
            share=round(min(1.0, share), 4),
            status=level,
            misaligned_to={k: round(v, 2) for k, v in misaligned.items()},
        )
    plan.alignment = sorted(alignments.values(), key=lambda a: (a.share, -a.weight))
    plan.scored_pages = sum(1 for a in plan.alignment if a.status != "too_little_data")

    groups: dict[tuple[str, str], list[SearchLine]] = defaultdict(list)
    for key, a in alignments.items():
        if a.status != "needs_work":
            continue
        for s in per_page[key]:
            if s.status == "misaligned" and s.owner:
                groups[(key, page_key(s.owner))].append(s)
    ordered = sorted(groups.items(), key=lambda kv: -sum(s.weight for s in kv[1]))
    for n, ((page, owner_key), searches) in enumerate(ordered, start=1):
        page_url = searches[0].page
        owner_url = searches[0].owner or owner_key
        weight = sum(s.weight for s in searches)
        page_weight = alignments[page].weight or 1.0
        shared = _shared(body.get(page, []), body.get(owner_key, []))
        pattern = _pattern(page_url, owner_url, searches, cls, topics, shared)
        plan.pairs.append(
            ConsolidationPair(
                group=n,
                page=page_url,
                owner=owner_url,
                topic=searches[0].topic,
                pattern=pattern,
                fix=FIXES[pattern],
                weight=round(weight, 2),
                priority="High"
                if weight / page_weight >= 0.25 or weight >= 500
                else "Medium"
                if weight / page_weight >= 0.10
                else "Low",
                searches=sorted(searches, key=lambda s: -s.weight),
                shared_paragraphs=shared,
            )
        )
        if pattern == "merge_and_redirect":
            plan.technical += [
                TechnicalChange(
                    kind="redirect_301", group=n, url=page_url, target=owner_url,
                    reason=f"merged into the page that stays ({shared} paragraphs shared)",
                ),
                TechnicalChange(
                    kind="remove_from_sitemap", group=n, url=page_url,
                    reason="redirected after the merge",
                ),
            ]  # fmt: skip
        elif pattern == "remove_from_index":
            plan.technical += [
                TechnicalChange(
                    kind="noindex_follow", group=n, url=page_url,
                    reason="a utility page taking searches that belong to a content page",
                ),
                TechnicalChange(
                    kind="remove_from_sitemap", group=n, url=page_url,
                    reason="noindex pages stay out of the sitemap",
                ),
            ]  # fmt: skip
    plan.links = _link_changes(plan.pairs, inventory, texts, topics, top_keyword or {})
    return plan


def _shared(a: Sequence[str], b: Sequence[str]) -> int:
    """Paragraphs of one page found on the other (template text already removed)."""
    if not a or not b:
        return 0
    common = len(set(a) & set(b))
    smaller = min(len(set(a)), len(set(b)))
    return common if common >= MERGE_MIN and common >= MERGE_SHARE * smaller else 0


def _pattern(
    page: str,
    owner: str,
    searches: Sequence[SearchLine],
    cls: object,
    topics: _Topics,
    shared: int,
) -> Pattern:
    kind = cls(page)  # type: ignore[operator]
    owner_kind = cls(owner)  # type: ignore[operator]
    weight = sum(s.weight for s in searches) or 1.0
    bofu = sum(s.weight for s in searches if s.funnel == "bofu") / weight
    tofu = sum(s.weight for s in searches if s.funnel == "tofu") / weight
    if kind == "utility":
        return "remove_from_index"
    if shared and kind == "article" and owner_kind == "article":
        return "merge_and_redirect"
    if _LOCATION_URL.search(_path(page)) and (_is_child(page, owner) or shared):
        return "localise"
    if _is_child(page, owner):
        return "point_up_to_parent"
    models = all("model number" in s.why for s in searches)
    if _is_child(owner, page) or (models and kind == "business"):
        return "hand_off_to_child"
    if kind == "article" and owner_kind == "business" and bofu >= 0.5:
        return "separate_by_intent"
    if kind == "business" and owner_kind == "article" and tofu >= 0.5:
        return "answer_then_link"
    return "differentiate_siblings"


def _link_changes(
    pairs: Sequence[ConsolidationPair],
    inventory: Sequence[PageInventory],
    texts: Mapping[str, str],
    topics: _Topics,
    top_keyword: Mapping[str, str],
) -> list[LinkChange]:
    out: list[LinkChange] = []
    links: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)  # source -> links
    for p in inventory:
        if p.status != "inventoried":
            continue
        for link in p.links:
            if link.kind == "internal" and link.ui_region not in CHROME and not link.nofollow:
                links[page_key(p.url)].append((link.url, link.anchor_text.strip(), link.image_alt))
    urls = {page_key(p.url): p.url for p in inventory}
    involved: dict[str, int] = {}
    for pair in pairs:
        involved.setdefault(page_key(pair.owner), pair.group)
        involved.setdefault(page_key(pair.page), pair.group)

    # repoint: anchor carries the owner's search words but points to the competing page
    for pair in pairs:
        target, owner = page_key(pair.page), page_key(pair.owner)
        wanted = [w for s in pair.searches[:5] if len(w := _words(s.query)) >= 2]
        for source, rows in links.items():
            for url, anchor, _alt in rows:
                if page_key(url) != target or source == owner:
                    continue
                words = _words(anchor)
                if words and any(w <= words for w in wanted):
                    out.append(
                        LinkChange(
                            kind="repoint", group=pair.group, source=urls.get(source, source),
                            destination=pair.page, new_destination=pair.owner, anchor=anchor,
                            suggested_anchor=anchor,
                            reason="its words are the owner page's search; point it there",
                        )
                    )  # fmt: skip

    # fix anchor: generic text or an image without alt text, to a page of a pair
    for source, rows in links.items():
        descriptive = {page_key(u) for u, a, _alt in rows if a and _norm(a) not in GENERIC_ANCHORS}
        for url, anchor, alt in rows:
            dest = page_key(url)
            if dest not in involved or dest in descriptive:
                continue
            generic = bool(anchor) and _norm(anchor) in GENERIC_ANCHORS
            bare_image = not anchor and alt is not None and not alt.strip()
            if generic or bare_image:
                out.append(
                    LinkChange(
                        kind="fix_anchor", group=involved[dest], source=urls.get(source, source),
                        destination=url, anchor=anchor or "(image without alt text)",
                        suggested_anchor=top_keyword.get(dest, ""),
                        reason="generic link text says nothing about the page it points to",
                    )
                )  # fmt: skip

    # add link: from the competing page to its owner, when its text is about that topic
    added: Counter[str] = Counter()
    for pair in pairs:
        source, owner = page_key(pair.page), page_key(pair.owner)
        if added[source] >= MAX_NEW_LINKS:
            continue
        if any(page_key(u) == owner for u, _a, _alt in links.get(source, [])):
            continue
        text = set(_WORD.findall(texts.get(source, "")))
        top = pair.searches[0].query if pair.searches else ""
        mentions = [s.query for s in pair.searches if (w := _words(s.query)) and w <= text]
        if not mentions:
            continue  # the page never mentions the owner's topic: a link would be forced
        added[source] += 1
        out.append(
            LinkChange(
                kind="add_link", group=pair.group, source=pair.page, destination=pair.owner,
                suggested_anchor=mentions[0] or top,
                reason=f"the page mentions “{mentions[0]}”; link it to the page that owns it",
            )
        )  # fmt: skip
    return out
