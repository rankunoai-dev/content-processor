"""Page-type classifier and site profile (RFC 0003 step B; design note "site types").

Code only: every page type is scored from independent signals (structured data,
Open Graph type, address words and what the page shows: add-to-cart buttons,
prices, product grids, bylines, forms). The best score wins; the confidence is its
margin over the runner-up, and the signals are kept in plain words so a reviewer
can see why. A page with no strong signal is `other`, never a guess.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from src.modules.content_processor.knowledge.structured_models import (
    PageType,
    PageTypeLabel,
    SiteProfile,
)

__all__ = [
    "ADD_TO_CART",
    "MIN_SCORE",
    "SERVICE_CTA",
    "PageSignals",
    "classify_page",
    "site_profile",
]

MIN_SCORE = 2.0
"""Below this, no type is claimed (`other`)."""

_SCHEMA: dict[str, tuple[PageType, float]] = {
    "Product": ("product", 4.0),
    "ProductGroup": ("product", 4.0),
    "IndividualProduct": ("product", 4.0),
    "Offer": ("product", 1.0),
    "CollectionPage": ("category", 3.0),
    "ItemList": ("category", 1.5),
    "OfferCatalog": ("category", 2.0),
    "Service": ("service", 3.0),
    "FinancialProduct": ("service", 2.5),
    "ProfessionalService": ("service", 2.0),
    "Article": ("article", 3.0),
    "NewsArticle": ("article", 3.5),
    "BlogPosting": ("article", 3.5),
    "Report": ("article", 2.0),
    "FAQPage": ("faq", 3.5),
    "ContactPage": ("contact", 4.0),
    "AboutPage": ("about", 4.0),
    "JobPosting": ("careers", 4.0),
    "LocalBusiness": ("location", 2.0),
    "Store": ("location", 2.0),
    "Place": ("location", 1.5),
    "Event": ("event", 4.0),
    "Course": ("service", 2.0),
}
_URL_WORDS: list[tuple[re.Pattern[str], PageType, float]] = [
    (re.compile(r"/(products?|p|item|dp|sku)/[^/]+"), "product", 2.0),
    (
        re.compile(r"/(shop|store|collections?|category|categories|c|catalog|department)(/|$)"),
        "category",
        2.0,
    ),
    (
        re.compile(r"/(services?|solutions?|what-we-do|capabilities|offerings?|industries)(/|$)"),
        "service",
        2.0,
    ),
    (
        re.compile(
            r"/(blog|news|insights?|articles?|press|stories|resources/articles?|newsroom)(/|$)"
        ),
        "article",
        2.0,
    ),
    (
        re.compile(r"/(case-stud(y|ies)|customer-stories|success-stories|customers)(/|$)"),
        "case_study",
        3.0,
    ),
    (re.compile(r"/(faqs?|frequently-asked-questions|help-center|support/faq)(/|$)"), "faq", 2.0),
    (re.compile(r"/(contact(-us)?|get-in-touch|locations?/contact)(/|$)"), "contact", 3.0),
    (re.compile(r"/(about(-us)?|who-we-are|our-story|company|leadership|team)(/|$)"), "about", 2.5),
    (re.compile(r"/(careers?|jobs?|join-us|vacancies)(/|$)"), "careers", 3.0),
    (
        re.compile(r"/(privacy|terms|legal|cookies?|disclosures?|accessibility|sitemap)(/|$|-)"),
        "legal",
        3.0,
    ),
    (re.compile(r"/(pricing|plans|prices)(/|$)"), "pricing", 3.0),
    (
        re.compile(r"/(locations?|stores?|branches|offices|find-an?-(advisor|office|store))(/|$)"),
        "location",
        2.0,
    ),
    (re.compile(r"/(events?|webinars?|conferences?)(/|$)"), "event", 2.0),
    (re.compile(r"/(forms?|apply|request-a-quote|quote)(/|$)"), "form", 1.5),
]
ADD_TO_CART = re.compile(r"\b(add to (cart|bag|basket)|buy now|add to trolley)\b", re.IGNORECASE)
SERVICE_CTA = re.compile(
    r"\b(request a (quote|demo|consultation|callback)|book a (demo|call|consultation)|"
    r"get a quote|talk to (an? )?(expert|advisor|sales)|contact sales|schedule a)\b",
    re.IGNORECASE,
)
_ARTICLE_WORDS = re.compile(r"\b(min(ute)? read|posted (on|by)|written by|by [A-Z][a-z]+ [A-Z])")


@dataclass
class PageSignals:
    """What the classifier looks at, gathered from a page's stored HTML."""

    url: str
    schema_types: list[str] = field(default_factory=list)
    og_type: str | None = None
    title: str | None = None
    words: int = 0
    prices: int = 0
    cart_buttons: int = 0
    service_ctas: list[str] = field(default_factory=list)
    product_cards: int = 0
    cards: int = 0
    has_article_tag: bool = False
    has_author: bool = False
    has_published_date: bool = False
    forms: int = 0
    form_fields: int = 0
    faq_items: int = 0
    variant_controls: int = 0
    facets: int = 0
    byline_text: bool = False
    media_type: str | None = None


def _add(
    scores: dict[PageType, float], why: list[str], kind: PageType, points: float, reason: str
) -> None:
    scores[kind] = scores.get(kind, 0.0) + points
    why.append(f"{kind} +{points:g}: {reason}")


def classify_page(s: PageSignals) -> PageTypeLabel:
    """Score every page type from the signals and return the best one with its reasons."""
    scores: dict[PageType, float] = {}
    why: list[str] = []
    mime = (s.media_type or "").lower()
    if mime and "html" not in mime:
        return PageTypeLabel(page_type="document", confidence=1.0, signals=[f"file type {mime}"])
    path = urlsplit(s.url).path.lower() or "/"
    if path in ("/", "/index.html", "/home", "/en", "/en/", "/us/en", "/us/en/"):
        _add(scores, why, "home", 4.0, "the site's home address")
    for kind in dict.fromkeys(s.schema_types):
        if kind in _SCHEMA:
            page_type, points = _SCHEMA[kind]
            _add(scores, why, page_type, points, f"structured data {kind}")
    og = (s.og_type or "").lower()
    if og in ("product", "og:product", "product.item"):
        _add(scores, why, "product", 2.0, f"og:type {og}")
    elif og == "article":
        _add(scores, why, "article", 1.0, "og:type article")
    for pattern, page_type, points in _URL_WORDS:
        if pattern.search(path):
            _add(scores, why, page_type, points, f"address {path}")
    if s.cart_buttons:
        _add(scores, why, "product", 3.0, "an add-to-cart button")
    if s.variant_controls:
        _add(scores, why, "product", 1.5, "size or colour choices")
    if s.product_cards >= 4:
        _add(scores, why, "category", 3.0, f"a grid of {s.product_cards} priced items")
    if s.facets:
        _add(scores, why, "category", 2.0, f"{s.facets} filter groups")
    if s.prices == 1 or (s.prices and s.prices <= 3 and not s.product_cards):
        _add(scores, why, "product", 1.0, "a price on the page")
    if s.service_ctas:
        _add(scores, why, "service", 1.5, f"call to action '{s.service_ctas[0]}'")
    if s.has_article_tag and s.words >= 250:
        _add(scores, why, "article", 1.0, "an <article> with running text")
    if s.has_author or s.byline_text:
        _add(scores, why, "article", 1.5, "an author byline")
    if s.has_published_date:
        _add(scores, why, "article", 1.0, "a publication date")
    if s.faq_items >= 3:
        _add(scores, why, "faq", 1.5 if s.faq_items < 6 else 2.5, f"{s.faq_items} questions")
    if s.forms and s.form_fields >= 3 and s.words < 400:
        _add(scores, why, "form", 2.0, f"a form with {s.form_fields} fields and little text")
    if not scores:
        return PageTypeLabel(page_type="other", confidence=0.0, signals=["no page-type signal"])
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, top = ranked[0]
    second, runner = ranked[1] if len(ranked) > 1 else (None, 0.0)
    if top < MIN_SCORE:
        return PageTypeLabel(
            page_type="other",
            confidence=0.0,
            signals=[*why, f"best score {top:g} is below {MIN_SCORE:g}"],
            runner_up=best,
            scores={k: round(v, 2) for k, v in ranked},
        )
    confidence = round(min(1.0, (top - runner) / top + 0.25 * min(top, 8.0) / 8.0), 2)
    return PageTypeLabel(
        page_type=best,
        confidence=min(confidence, 1.0),
        signals=why,
        runner_up=second,
        scores={k: round(v, 2) for k, v in ranked},
    )


def site_profile(labels: Iterable[PageTypeLabel], *, cart_pages: int = 0) -> SiteProfile:
    """What kind of website the pages describe (e-commerce, B2B services, publisher, mixed)."""
    counts: Counter[str] = Counter(label.page_type for label in labels)
    typed = sum(n for kind, n in counts.items() if kind not in ("other", "document", "legal"))
    if not typed:
        return SiteProfile(profile="unknown", page_types=dict(counts), signals=["no typed pages"])
    shop = counts["product"] + counts["category"]
    services = counts["service"] + counts["case_study"] + counts["pricing"]
    articles = counts["article"]
    signals = [f"{n} {kind} pages" for kind, n in counts.most_common() if n]
    if cart_pages:
        signals.append(f"{cart_pages} pages with an add-to-cart button")
    shares = {
        "ecommerce": shop / typed,
        "b2b_services": services / typed,
        "publisher": articles / typed,
    }
    if cart_pages and shop / typed >= 0.2:
        profile = "ecommerce"
    else:
        ranked: Sequence[tuple[str, float]] = sorted(shares.items(), key=lambda kv: -kv[1])
        name, share = ranked[0]
        profile = name if share >= 0.4 and share - ranked[1][1] >= 0.15 else "mixed"
    return SiteProfile(profile=profile, page_types=dict(counts), signals=signals)
