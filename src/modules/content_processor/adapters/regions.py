"""Classify where an HTML element sits on the page (`UiRegion`).

Signals: the tag, the ARIA role and label, and id/class vocabulary common to
CMS themes and front-end frameworks (Bootstrap, Tailwind UI, WordPress, AEM,
Shopify). The most specific region wins over the region inherited from an
ancestor, so a link inside a hamburger menu inside the header is
`hamburger_menu`, while a paragraph inside `<main>` stays `main_content`
unless it is inside a card, snippet, carousel or similar component.
"""

from __future__ import annotations

import re

from src.modules.content_processor.enums import UiRegion

__all__ = ["classify_region"]

# Ordered: the first matching rule wins for one element.
_TOKEN_RULES: tuple[tuple[UiRegion, re.Pattern[str]], ...] = (
    (
        UiRegion.COOKIE_BANNER,
        re.compile(r"cookie|consent|gdpr|onetrust|osano|cookiebot|cc-window|ccpa", re.I),
    ),
    (
        UiRegion.CHAT_WIDGET,
        re.compile(
            r"intercom|drift|hubspot-messages|crisp|tawk|zendesk|livechat|olark|chat-?widget|chatbot",
            re.I,
        ),
    ),
    (
        UiRegion.AD_SLOT,
        re.compile(r"adsbygoogle|ad-slot|ad_slot|dfp-|google_ads|advert|sponsored", re.I),
    ),
    (
        UiRegion.HAMBURGER_MENU,
        re.compile(
            r"hamburger|burger|offcanvas|off-canvas|mobile-?(menu|nav|navigation)|nav-?drawer|"
            r"drawer-?(menu|nav)|navbar-collapse|side-?nav-?drawer|menu-?toggle|nav-?toggle|"
            r"navbar-toggler|slide-?out-?menu|push-?menu",
            re.I,
        ),
    ),
    (
        UiRegion.MEGA_MENU,
        re.compile(r"mega-?menu|megamenu|dropdown-?menu|sub-?menu|flyout|nav-?dropdown", re.I),
    ),
    (UiRegion.BREADCRUMB, re.compile(r"breadcrumb", re.I)),
    (
        UiRegion.PALETTE,
        re.compile(
            r"palette|swatch|colou?r-?(picker|option|chip|selector)|command-?palette|cmdk|"
            r"theme-?(picker|switcher)",
            re.I,
        ),
    ),
    (
        UiRegion.SNIPPET,
        re.compile(
            r"snippet|excerpt|teaser|blurb|summary-?(card|text)|search-?result|result-?item|"
            r"preview|abstract|lede|standfirst|featured-?snippet",
            re.I,
        ),
    ),
    (UiRegion.TOOLTIP, re.compile(r"tooltip|tippy|hint-?bubble", re.I)),
    (UiRegion.POPOVER, re.compile(r"popover|popup|pop-up|flyout-?panel", re.I)),
    (UiRegion.MODAL, re.compile(r"modal|lightbox|dialog|overlay-?(content|panel)", re.I)),
    (UiRegion.ACCORDION_PANEL, re.compile(r"accordion|collaps(e|ible)|faq-?(item|answer)", re.I)),
    (UiRegion.TAB_PANEL, re.compile(r"tab-?pane|tab-?panel|tabs?-?content", re.I)),
    (UiRegion.CAROUSEL, re.compile(r"carousel|slider|swiper|slick|glide|splide|slideshow", re.I)),
    (UiRegion.HERO, re.compile(r"\bhero\b|hero-|jumbotron|masthead|banner-?(content|text)", re.I)),
    (UiRegion.SEARCH, re.compile(r"search-?(box|bar|form|field)|site-?search", re.I)),
    (UiRegion.SIDEBAR, re.compile(r"sidebar|side-?bar|aside|widget-?area|rail", re.I)),
    (UiRegion.CARD, re.compile(r"\bcard\b|card-|tile|product-?(item|card|tile)", re.I)),
    (UiRegion.FOOTER, re.compile(r"\bfooter\b|site-?footer|footer-", re.I)),
    (UiRegion.HEADER, re.compile(r"\bheader\b|site-?header|top-?bar|navbar\b", re.I)),
)
_ROLE_RULES: dict[str, UiRegion] = {
    "dialog": UiRegion.MODAL,
    "alertdialog": UiRegion.MODAL,
    "tooltip": UiRegion.TOOLTIP,
    "tabpanel": UiRegion.TAB_PANEL,
    "search": UiRegion.SEARCH,
    "complementary": UiRegion.SIDEBAR,
    "contentinfo": UiRegion.FOOTER,
    "banner": UiRegion.HEADER,
    "navigation": UiRegion.NAV_MENU,
    "menu": UiRegion.NAV_MENU,
    "menubar": UiRegion.NAV_MENU,
    "main": UiRegion.MAIN_CONTENT,
    "table": UiRegion.TABLE,
    "grid": UiRegion.TABLE,
    "form": UiRegion.FORM,
}
_TAG_RULES: dict[str, UiRegion] = {
    "main": UiRegion.MAIN_CONTENT,
    "article": UiRegion.MAIN_CONTENT,
    "nav": UiRegion.NAV_MENU,
    "header": UiRegion.HEADER,
    "footer": UiRegion.FOOTER,
    "aside": UiRegion.SIDEBAR,
    "dialog": UiRegion.MODAL,
    "details": UiRegion.ACCORDION_PANEL,
    "table": UiRegion.TABLE,
    "form": UiRegion.FORM,
}
# Regions that a generic container (header, main, nav...) must not override.
_SPECIFIC = frozenset(
    {
        UiRegion.HAMBURGER_MENU,
        UiRegion.MEGA_MENU,
        UiRegion.BREADCRUMB,
        UiRegion.PALETTE,
        UiRegion.SNIPPET,
        UiRegion.TOOLTIP,
        UiRegion.POPOVER,
        UiRegion.MODAL,
        UiRegion.ACCORDION_PANEL,
        UiRegion.TAB_PANEL,
        UiRegion.CAROUSEL,
        UiRegion.CARD,
        UiRegion.COOKIE_BANNER,
        UiRegion.CHAT_WIDGET,
        UiRegion.AD_SLOT,
        UiRegion.SEARCH,
        UiRegion.HERO,
    }
)
# Generic containers that never override a region inherited from an ancestor.
_GENERIC = frozenset(
    {UiRegion.MAIN_CONTENT, UiRegion.HEADER, UiRegion.FOOTER, UiRegion.NAV_MENU, UiRegion.SIDEBAR}
)


def _own_region(tag: str, attrs: dict[str, str | None]) -> UiRegion | None:
    role = (attrs.get("role") or "").lower().strip()
    label = (attrs.get("aria-label") or "").lower()
    if "breadcrumb" in label:
        return UiRegion.BREADCRUMB
    if attrs.get("popover") is not None:
        return UiRegion.POPOVER
    tokens = " ".join(
        str(attrs.get(k) or "") for k in ("id", "class", "data-component", "data-testid")
    )
    if tokens.strip():
        for region, pattern in _TOKEN_RULES:
            if pattern.search(tokens):
                return region
    if "menu" in label and ("main" in label or "mobile" in label):
        return UiRegion.HAMBURGER_MENU if "mobile" in label else UiRegion.NAV_MENU
    if role in _ROLE_RULES:
        return _ROLE_RULES[role]
    return _TAG_RULES.get(tag)


def classify_region(tag: str, attrs: dict[str, str | None], parent: UiRegion) -> UiRegion:
    """Region of an element given its parent's region (most specific wins)."""
    own = _own_region(tag, attrs)
    if own is None:
        return parent
    if parent in _SPECIFIC and own in _GENERIC:
        return parent
    if (
        parent in _SPECIFIC
        and own in _SPECIFIC
        and parent
        in (
            UiRegion.HAMBURGER_MENU,
            UiRegion.MEGA_MENU,
            UiRegion.COOKIE_BANNER,
            UiRegion.CHAT_WIDGET,
        )
    ):
        return parent
    return own
