"""Build a project's theme map, offline and at $0.

1. **Pages.** Every knowledge-eligible page of every website in the project's scope
   (the client, its competitors in this project, and competitors it has in other
   projects), from the freshest scrape of each: title, H1, breadcrumbs, headings,
   description and the start of the main text, plus page type, depth and dates.
2. **One word space.** Pages are weighted by their terms (single words and word
   pairs; titles and H1 count three times, headings and breadcrumbs twice), with
   words on nearly every page of one website (its brand, its footer) removed
   for that website. The same space holds every website, so a client page and a
   competitor page about the same subject land together. (Embeddings are not used
   here: websites may have been indexed with different models.)
3. **Themes, then topics.** Pages are grouped by similarity (a seeded spherical
   k-means: the densest pages seed groups, pages join the closest group above a
   threshold, groups are refined and merged, groups of one are left unclustered).
   Each theme is then split the same way, more strictly, into topics.
4. **Names.** A theme is named by the terms that set it apart from the rest of
   the corpus; its id comes from those terms, so it stays stable between builds.
5. **Pillars.** On each website, the theme's pillar is the page that best combines
   being central to the theme, shallow in the site, an offering page in the
   catalog, and deep in content. Other pages are spokes.
6. **Ownership.** Client pages against the strongest competitor's: client-owned,
   contested, competitor-led, client gap (client has nothing) or client-only.
7. **Funnel stage** of each page from its type, address, title and calls to action.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.modules.analyst.playground import Playground, SiteData
from src.modules.content_processor.knowledge.catalog_models import Catalog
from src.modules.content_processor.knowledge.models import Chunk, PageStamp
from src.modules.content_processor.knowledge.numeric import np
from src.modules.theming.models import (
    FunnelStage,
    Ownership,
    PageTheme,
    Pillar,
    SiteCoverage,
    Theme,
    ThemeMap,
    ThemeWebsite,
    Topic,
)

__all__ = ["DEFAULTS", "PageDoc", "build_theme_map", "cluster", "funnel_stage", "ownership"]

DEFAULTS: dict[str, float] = {
    "theme_threshold": 0.20,
    "topic_threshold": 0.38,
    "min_theme_pages": 2,
    "min_topic_pages": 2,
    "max_df": 0.85,
    "site_boilerplate_df": 0.85,
    "body_words": 400,
    "vocabulary": 8000,
}
_WORD = re.compile(r"[a-z][a-z0-9'-]{2,}")
_STOP = frozenset(
    [
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "although",
        "among",
        "and",
        "another",
        "any",
        "are",
        "around",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "can",
        "could",
        "did",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "either",
        "else",
        "ever",
        "every",
        "few",
        "for",
        "from",
        "further",
        "get",
        "gets",
        "got",
        "had",
        "has",
        "have",
        "having",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "however",
        "into",
        "its",
        "itself",
        "just",
        "least",
        "less",
        "let",
        "like",
        "made",
        "make",
        "many",
        "may",
        "might",
        "more",
        "most",
        "much",
        "must",
        "need",
        "never",
        "new",
        "next",
        "not",
        "now",
        "off",
        "often",
        "once",
        "one",
        "only",
        "other",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "per",
        "perhaps",
        "please",
        "rather",
        "really",
        "same",
        "see",
        "seen",
        "should",
        "since",
        "some",
        "still",
        "such",
        "take",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "though",
        "through",
        "thus",
        "too",
        "under",
        "until",
        "upon",
        "use",
        "used",
        "using",
        "very",
        "via",
        "was",
        "way",
        "we",
        "well",
        "were",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "yet",
        "you",
        "your",
        "yours",
        "read",
        "learn",
        "click",
        "here",
        "page",
        "home",
        "menu",
        "skip",
        "content",
        "main",
        "back",
        "top",
        "find",
        "view",
        "link",
        "links",
        "site",
        "website",
        "online",
        "today",
        "posted",
        "updated",
        "share",
        "print",
        "email",
        "follow",
        "opens",
        "tab",
    ]
)
_TITLE_WEIGHT = 3
_STRUCTURE_WEIGHT = 2
_REFINE_PASSES = 3
_LABEL_TERMS = 3
_THEME_TERMS = 8
_OWN_RATIO = 1.5


@dataclass
class PageDoc:
    """One page as the theme map reads it."""

    url: str
    website: str
    title: str | None
    h1: str | None
    weighted: Counter[str] = field(default_factory=Counter)
    words: int = 0
    depth: int = 0
    page_type: str | None = None
    stamp: PageStamp | None = None


# -- reading ------------------------------------------------------------------------------


def _terms(text: str) -> list[str]:
    words = [w.strip("'-") for w in _WORD.findall(text.casefold())]
    words = [w for w in words if len(w) >= 3 and w not in _STOP and not w.isdigit()]
    return words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]


def _chunks_by_url(data: SiteData) -> dict[str, list[Chunk]]:
    path = data.file(".chunks.jsonl")
    out: dict[str, list[Chunk]] = {}
    if path is None:
        return out
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                chunk = Chunk.model_validate_json(line)
                out.setdefault(chunk.url, []).append(chunk)
    return out


def _page_types(play: Playground, data: SiteData) -> dict[str, str | None]:
    store = play.open_store(data)
    try:
        return {p.url: p.page_type for p in store.pages()}
    finally:
        store.close()


def collect(
    play: Playground, settings: dict[str, float]
) -> tuple[list[ThemeWebsite], list[PageDoc], dict[str, Catalog], list[str]]:
    """Every eligible page of every website in scope, and each website's catalog."""
    websites: list[ThemeWebsite] = []
    docs: list[PageDoc] = []
    catalogs: dict[str, Catalog] = {}
    skipped: list[str] = []
    body_words = int(settings["body_words"])
    for website in play.websites():
        data = play.site_data(website)
        if data is None:
            skipped.append(f"{website.name}: not scraped or not indexed yet")
            continue
        stamps = {s.url: s for s in play.stamps(data).values() if s.knowledge_eligible}
        chunks = _chunks_by_url(data)
        types = _page_types(play, data)
        catalog_path = data.file(".catalog.json")
        if catalog_path is not None:
            catalogs[website.key] = Catalog.model_validate_json(
                catalog_path.read_text(encoding="utf-8")
            )
        count = 0
        for url, stamp in sorted(stamps.items()):
            page_chunks = sorted(chunks.get(url, []), key=lambda c: c.order)
            main = [c for c in page_chunks if "main_content" in c.ui_regions] or page_chunks
            body = " ".join(" ".join(c.text for c in main).split()[:body_words])
            weighted: Counter[str] = Counter()
            for text, weight in (
                (f"{stamp.title or ''} {stamp.h1 or ''}", _TITLE_WEIGHT),
                (" ".join([*stamp.breadcrumbs[1:], *stamp.headings]), _STRUCTURE_WEIGHT),
                (f"{stamp.meta_description or ''} {body}", 1),
            ):
                for term in _terms(text):
                    weighted[term] += weight
            if not weighted:
                continue
            docs.append(
                PageDoc(
                    url=url,
                    website=website.key,
                    title=stamp.title,
                    h1=stamp.h1,
                    weighted=weighted,
                    words=stamp.main_word_count or stamp.word_count,
                    depth=stamp.path_depth,
                    page_type=types.get(url),
                    stamp=stamp,
                )
            )
            count += 1
        websites.append(
            ThemeWebsite(
                website=website.key,
                name=website.name,
                role="client" if play.role_in_run(website) == "client" else "competitor",
                project=data.copy.project_name,
                run_id=data.run.run_id,
                pages=count,
            )
        )
    return websites, docs, catalogs, skipped


# -- the word space ----------------------------------------------------------------------------


def _boilerplate(docs: Sequence[PageDoc], share: float) -> dict[str, set[str]]:
    """Per website, terms on more than `share` of its pages (brand, footer, template)."""
    out: dict[str, set[str]] = {}
    by_site: dict[str, list[PageDoc]] = {}
    for d in docs:
        by_site.setdefault(d.website, []).append(d)
    for site, members in by_site.items():
        if len(members) < 10:
            out[site] = set()
            continue
        df = Counter(t for d in members for t in set(d.weighted))
        out[site] = {t for t, n in df.items() if n / len(members) > share}
    return out


def vectorize(docs: Sequence[PageDoc], settings: dict[str, float]) -> tuple[Any, list[str]]:
    """Unit-length TF-IDF rows (float32) and the vocabulary."""
    boiler = _boilerplate(docs, settings["site_boilerplate_df"])

    def kept(term: str, site_words: set[str]) -> bool:
        # a boilerplate word also spoils the word pairs it is in ("hancock announces")
        return term not in site_words and not any(w in site_words for w in term.split())

    filtered = [
        Counter({t: n for t, n in d.weighted.items() if kept(t, boiler.get(d.website, set()))})
        for d in docs
    ]
    df = Counter(t for f in filtered for t in f)
    n = len(docs)
    max_df = settings["max_df"]
    keep = [t for t, c in df.items() if c >= 2 and c / max(n, 1) <= max_df]
    keep.sort(key=lambda t: (-df[t], t))
    vocab = sorted(keep[: int(settings["vocabulary"])])
    index = {t: i for i, t in enumerate(vocab)}
    matrix = np.zeros((n, len(vocab)), dtype=np.float32)
    for row, f in enumerate(filtered):
        for term, count in f.items():
            col = index.get(term)
            if col is not None:
                matrix[row, col] = (1.0 + math.log(count)) * math.log((1 + n) / (1 + df[term]))
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms), vocab


# -- grouping -------------------------------------------------------------------------------


def _unit(rows: Any) -> Any:
    norm = float(np.linalg.norm(rows))
    return rows / norm if norm else rows


def _root(parent: list[int], k: int) -> int:
    while parent[k] != k:
        parent[k] = parent[parent[k]]
        k = parent[k]
    return k


def cluster(matrix: Any, threshold: float, min_size: int) -> list[int]:
    """Group rows by cosine similarity; -1 for rows left on their own. Deterministic."""
    n = matrix.shape[0]
    if n == 0:
        return []
    density = matrix @ matrix.sum(axis=0)  # each row's total similarity to all rows
    order = sorted(range(n), key=lambda i: (-float(density[i]), i))
    centroids: list[Any] = []
    sums: list[Any] = []
    labels = [-1] * n
    for i in order:
        if float(np.linalg.norm(matrix[i])) == 0:
            continue
        if centroids:
            scores = np.vstack(centroids) @ matrix[i]
            best = int(np.argmax(scores))
            if float(scores[best]) >= threshold:
                labels[i] = best
                sums[best] = sums[best] + matrix[i]
                centroids[best] = _unit(sums[best])
                continue
        sums.append(matrix[i].copy())
        centroids.append(matrix[i].copy())
        labels[i] = len(centroids) - 1
    if not centroids:
        return [-1] * n
    for _ in range(_REFINE_PASSES):
        stack = np.vstack(centroids)
        scores = matrix @ stack.T
        labels = [
            int(np.argmax(scores[i])) if float(scores[i].max()) >= threshold else -1
            for i in range(n)
        ]
        # merge groups whose centres are as close as a member must be
        parent = list(range(len(centroids)))

        array = np.asarray(labels)
        live = sorted({lab for lab in labels if lab >= 0})
        centre = {lab: _unit(matrix[array == lab].sum(axis=0)) for lab in live}
        for a_pos, a in enumerate(live):
            for b in live[a_pos + 1 :]:
                if float(centre[a] @ centre[b]) >= threshold + (1 - threshold) / 2:
                    parent[_root(parent, b)] = _root(parent, a)
        labels = [_root(parent, lab) if lab >= 0 else -1 for lab in labels]
        groups = sorted({lab for lab in labels if lab >= 0})
        array = np.asarray(labels)
        centroids = [_unit(matrix[array == g].sum(axis=0)) for g in groups]
        if not centroids:
            return [-1] * n
        remap = {g: k for k, g in enumerate(groups)}
        labels = [remap[lab] if lab >= 0 else -1 for lab in labels]
    sizes = Counter(lab for lab in labels if lab >= 0)
    small = {lab for lab, size in sizes.items() if size < min_size}
    kept = sorted(lab for lab in sizes if lab not in small)
    remap = {lab: k for k, lab in enumerate(kept)}
    return [remap.get(lab, -1) for lab in labels]


def _distinct_terms(matrix: Any, vocab: list[str], members: list[int], top: int) -> list[str]:
    """Terms that set these rows apart from the whole corpus (prefers word pairs)."""
    inside = matrix[members].mean(axis=0)
    overall = matrix.mean(axis=0)
    score = inside - overall
    order = np.argsort(-score)
    picked: list[str] = []
    for idx in order[: top * 6]:
        term = vocab[int(idx)]
        if float(score[int(idx)]) <= 0:
            break
        if any(term in p or p in term for p in picked):
            continue
        picked.append(term)
        if len(picked) >= top:
            break
    return picked


def _stable_id(prefix: str, terms: list[str]) -> str:
    return prefix + hashlib.sha1(" ".join(sorted(terms[:5])).encode()).hexdigest()[:10]  # noqa: S324


def _label(terms: list[str]) -> str:
    """The first few terms that do not repeat a word already used ("monthly distributions")."""
    picked: list[str] = []
    used: set[str] = set()
    for term in terms:
        words = set(term.split())
        if words & used:
            continue
        picked.append(term)
        used |= words
        if len(picked) == _LABEL_TERMS:
            break
    return ", ".join(picked).capitalize() if picked else "Unnamed theme"


# -- funnel, pillars, ownership ------------------------------------------------------------------

_FUNNEL_RULES: list[tuple[FunnelStage, re.Pattern[str]]] = [
    (
        "support",
        re.compile(
            r"help|support|faq|frequently|forms?\b|claims?|log ?in|sign ?in|account|"
            r"customer service|esignature|how to use",
            re.I,
        ),
    ),
    ("onboard", re.compile(r"getting started|get started|onboard|set ?up|welcome|activate", re.I)),
    (
        "compare",
        re.compile(
            r"\bcompare|comparison|\bvs\.?\b|versus|alternatives?|pricing|prices|plans\b|rates",
            re.I,
        ),
    ),
    (
        "decide",
        re.compile(
            r"get a quote|quote|apply|buy|request a demo|demo|contact us|talk to|enrol|enroll", re.I
        ),
    ),
    (
        "learn",
        re.compile(
            r"blog|insights?|news|article|guide|what is|how does|podcast|webinar|"
            r"education|learn|resources?|press|stories",
            re.I,
        ),
    ),
]
_DECIDE_TYPES = {"product", "service", "category", "pricing", "contact"}
_LEARN_TYPES = {"article", "case_study", "event", "document"}
_SUPPORT_TYPES = {"faq", "form"}


def funnel_stage(doc: PageDoc) -> tuple[FunnelStage, str]:
    """The page's funnel stage and the signal that decided it."""
    stamp = doc.stamp
    path = "/" + "/".join(stamp.path_segments) if stamp else doc.url
    title = f"{doc.title or ''} {doc.h1 or ''}"
    for stage, pattern in _FUNNEL_RULES:
        if pattern.search(path.replace("-", " ")):
            return stage, f"address: {path}"
    if doc.page_type in _SUPPORT_TYPES:
        return "support", f"page type: {doc.page_type}"
    if doc.page_type in _DECIDE_TYPES:
        return "decide", f"page type: {doc.page_type}"
    for stage, pattern in _FUNNEL_RULES:
        if pattern.search(title):
            return stage, f"title: {title.strip()[:80]}"
    if stamp is not None and any(c.strength == "strong" for c in stamp.ctas):
        return "decide", "a strong call to action"
    if doc.page_type in _LEARN_TYPES:
        return "learn", f"page type: {doc.page_type}"
    return "unknown", "no signal"


def ownership(client: int, best_competitor: int) -> tuple[Ownership, str]:
    """Who owns a theme, from the client's and the strongest competitor's page counts."""
    if client == 0 and best_competitor == 0:
        return "contested", "no pages"
    if client == 0:
        return "client_gap", f"competitors have {best_competitor} page(s), the client none"
    if best_competitor == 0:
        return "client_only", f"only the client covers it ({client} page(s))"
    if client >= _OWN_RATIO * best_competitor:
        return (
            "client_owned",
            f"client {client} page(s) vs at most {best_competitor} for a competitor",
        )
    if best_competitor >= _OWN_RATIO * client:
        return (
            "competitor_led",
            f"a competitor has {best_competitor} page(s) vs the client's {client}",
        )
    return "contested", f"client {client} page(s), strongest competitor {best_competitor}"


def _offering_urls(catalog: Catalog | None) -> set[str]:
    if catalog is None:
        return set()
    urls = {o.href for o in catalog.offerings if o.href}
    urls |= {p.url for o in catalog.offerings for p in o.pages if p.basis == "same_url"}
    return {u.rstrip("/") for u in urls if u}


def _pillar(
    members: list[int], docs: Sequence[PageDoc], matrix: Any, offering_urls: set[str]
) -> Pillar | None:
    if len(members) < 2:
        return None
    centre = _unit(matrix[members].sum(axis=0))
    best: tuple[float, int, list[str]] | None = None
    for i in members:
        d = docs[i]
        central = float(matrix[i] @ centre)
        shallow = 1.0 / (1 + d.depth)
        is_offering = 1.0 if d.url.rstrip("/") in offering_urls else 0.0
        depth_of_content = min(d.words / 1500, 1.0)
        score = 0.5 * central + 0.2 * shallow + 0.2 * is_offering + 0.1 * depth_of_content
        reasons = [f"centrality {central:.2f}", f"{d.depth} level(s) deep", f"{d.words} words"]
        if is_offering:
            reasons.append("an offering page in the catalog")
        if best is None or score > best[0] + 1e-9:
            best = (score, i, reasons)
    if best is None:  # pragma: no cover - members has at least two pages
        return None
    score, i, reasons = best
    return Pillar(url=docs[i].url, title=docs[i].title, score=round(score, 3), reasons=reasons)


def _matching_offerings(terms: list[str], catalogs: dict[str, Catalog]) -> list[str]:
    words = {w for t in terms for w in t.split()}
    found: list[str] = []
    for catalog in catalogs.values():
        for o in catalog.offerings:
            name_words = {w for w in _terms(o.name) if " " not in w}
            if name_words and name_words <= words and o.name not in found:
                found.append(o.name)
    return found


# -- the map ------------------------------------------------------------------------------


def build_theme_map(
    play: Playground, project_id: str, settings: dict[str, float] | None = None
) -> ThemeMap:
    """The project's theme map (see the module docstring for each step)."""
    cfg = {**DEFAULTS, **(settings or {})}
    websites, docs, catalogs, skipped = collect(play, cfg)
    built = datetime.now(UTC)
    map_id = "themes-" + built.strftime("%Y%m%d-%H%M%S-") + f"{built.microsecond // 1000:03d}"
    pages = [
        PageTheme(
            url=d.url,
            website=d.website,
            title=d.title,
            page_type=d.page_type,
            words=d.words,
            funnel_stage=funnel_stage(d)[0],
            funnel_reason=funnel_stage(d)[1],
        )
        for d in docs
    ]
    method = (
        "TF-IDF over titles, headings, breadcrumbs, descriptions and the start of the main "
        "text (one space for all websites; per-website boilerplate removed); seeded spherical "
        "k-means for themes, then for topics inside each theme"
    )
    if len(docs) < 2:
        return ThemeMap(
            map_id=map_id,
            project_id=project_id,
            built_at=built,
            method=method,
            settings=cfg,
            websites=websites,
            themes=[],
            pages=pages,
            skipped=skipped,
            stats={"pages": len(docs), "themes": 0},
        )
    matrix, vocab = vectorize(docs, cfg)
    labels = cluster(matrix, cfg["theme_threshold"], int(cfg["min_theme_pages"]))
    roles = {w.website: w.role for w in websites}
    themes: list[Theme] = []
    for lab in sorted(set(labels) - {-1}):
        members = [i for i, x in enumerate(labels) if x == lab]
        terms = _distinct_terms(matrix, vocab, members, _THEME_TERMS)
        theme_id = _stable_id("th-", terms)
        centre = _unit(matrix[members].sum(axis=0))
        # topics inside the theme
        sub = matrix[members]
        sub_labels = cluster(sub, cfg["topic_threshold"], int(cfg["min_topic_pages"]))
        topics: list[Topic] = []
        topic_of: dict[int, str] = {}
        for t in sorted(set(sub_labels) - {-1}):
            t_members = [members[k] for k, x in enumerate(sub_labels) if x == t]
            t_terms = _distinct_terms(matrix, vocab, t_members, 5)
            topic_id = _stable_id("tp-", [theme_id, *t_terms])
            for i in t_members:
                topic_of[i] = topic_id
            topics.append(
                Topic(
                    topic_id=topic_id,
                    label=_label(t_terms),
                    terms=t_terms,
                    pages=[docs[i].url for i in t_members],
                    websites=dict(Counter(docs[i].website for i in t_members)),
                )
            )
        if len(topics) == 1 and len(topics[0].pages) == len(members):
            # the theme did not split: its one topic would only repeat it
            topics, topic_of = [], {}
        coverage: list[SiteCoverage] = []
        pillars: set[str] = set()
        for site in websites:
            site_members = [i for i in members if docs[i].website == site.website]
            catalog = catalogs.get(site.website)
            pillar = _pillar(site_members, docs, matrix, _offering_urls(catalog))
            if pillar is not None:
                pillars.add(pillar.url)
            coverage.append(
                SiteCoverage(
                    website=site.website,
                    pages=len(site_members),
                    words=sum(docs[i].words for i in site_members),
                    share=round(len(site_members) / len(members), 3),
                    pillar=pillar,
                )
            )
        client = sum(c.pages for c in coverage if roles.get(c.website) == "client")
        best = max((c.pages for c in coverage if roles.get(c.website) == "competitor"), default=0)
        owner, reason = ownership(client, best)
        for i in members:
            page = pages[i]
            page.theme_id = theme_id
            page.topic_id = topic_of.get(i)
            page.similarity = round(float(matrix[i] @ centre), 3)
            page.role = "pillar" if page.url in pillars else "spoke"
        themes.append(
            Theme(
                theme_id=theme_id,
                label=_label(terms),
                terms=terms,
                pages=len(members),
                coverage=coverage,
                ownership=owner,
                ownership_reason=reason,
                topics=topics,
                funnel=dict(Counter(pages[i].funnel_stage for i in members)),
                offerings=_matching_offerings(terms, catalogs),
            )
        )
    themes.sort(key=lambda t: (-t.pages, t.label))
    stats = {
        "pages": len(docs),
        "themes": len(themes),
        "topics": sum(len(t.topics) for t in themes),
        "unclustered": sum(1 for p in pages if p.theme_id is None),
        **{f"ownership_{k}": v for k, v in Counter(t.ownership for t in themes).items()},
    }
    return ThemeMap(
        map_id=map_id,
        project_id=project_id,
        built_at=built,
        method=method,
        settings=cfg,
        websites=websites,
        themes=themes,
        pages=pages,
        skipped=skipped,
        stats=stats,
    )
