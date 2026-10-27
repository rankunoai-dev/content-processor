"""Offering catalog discovery (RFC 0002, Stage B), deterministic part.

Input is step 1's output: the site's menu trees, page stamps and chunks.

1. Menu entries are classified from their own label and the group they sit in:
   offering groups ("Products & services"), tasks ("File a claim"), tools
   ("calculator"), content ("Insights"), corporate ("Careers"), audiences
   ("Individuals"), and overview links that stand for their parent. Top-level
   entries that are none of these are families; entries under a family or an
   offering group are offerings, and entries under an offering are sub-offerings.
2. Entries are merged into one offering when they link to the same address, or
   when their names match once family words ("insurance") are set aside.
3. Pages are mapped to the offering whose address is the longest prefix of
   theirs (or by breadcrumb or structured-data name), and each mapped page's
   next path segment shared by several pages becomes a sub-offering.
4. Cards are filled from code only: description, audiences, numbers, questions,
   calls to action and tools, each with its evidence.

Model help (synonyms by embedding, card attributes by an instruct model) is in
`assist.py` and only ever adds checked facts or suggestions on top of this.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from src.modules.content_processor.analysis.claims import quantities_in, split_sentences
from src.modules.content_processor.knowledge.catalog_models import (
    CardAttribute,
    Catalog,
    EntryRole,
    Evidence,
    MenuEntry,
    Offering,
    OfferingLevel,
    PageBasis,
    PageLink,
    Signal,
)
from src.modules.content_processor.knowledge.models import (
    Chunk,
    MenuItem,
    PageStamp,
    SiteMenu,
    SiteStamp,
)

__all__ = [
    "build_catalog",
    "classify_label",
    "merge_offerings",
    "name_key",
    "url_key",
]

_MAX_NUMBERS = 40
_MAX_QUESTIONS = 30
_MAX_CTAS = 10
_MIN_FAMILY_PAGES = 2

_GROUP = re.compile(
    r"^(our )?(products?( (&|and) services)?|services( (&|and) products)?|solutions|offerings"
    r"|plans|coverage( options)?|what we offer|products and solutions)$",
    re.IGNORECASE,
)
_LANDING = re.compile(r"(^|\s)(overview|homepage|home page|home)$", re.IGNORECASE)
_TOOL = re.compile(
    r"\b(calculators?|tools?|estimators?|quiz|score|apps?|planners?|simulators?)\b", re.IGNORECASE
)
_TASK = re.compile(
    r"\b(sign in|log ?in|register|make a payment|pay( my)? bill|payments?|change (my )?address"
    r"|manage|file a claim|claims?|find (a|and) |forms?|tax cent(er|re)|request|update"
    r"|beneficiar(y|ies)|help cent(er|re)|service your|customer service|get help|contact a"
    r"|talk to|speak (to|with)|lost or unclaimed|unclaimed|lost policy)\b",
    re.IGNORECASE,
)
_CONTENT = re.compile(
    r"\b(insights?|resources?|faqs?|glossary|blog|articles?|podcasts?|stories|docuseries"
    r"|webinars?|events?|education|learn(ing)?|guides?|research|reports?|index|library|videos?)\b",
    re.IGNORECASE,
)
_CORPORATE = re.compile(
    r"\b(about( us)?|leadership|careers?|jobs|inclusion|diversity|community( investment)?"
    r"|news(room)?|press|media|investor relations|customer stories|sustainability|our story"
    r"|history|contact( us)?|privacy|terms|legal|accessibility|site ?map|cookies?|security"
    r"|fraud|disclosures?|personal information|do not sell|opt.?out|unsubscribe|stock price"
    r"|annual report)\b",
    re.IGNORECASE,
)
_AUDIENCE = re.compile(
    r"^(for )?(individuals?|financial professionals?|advisors?|brokers?|plan sponsors?"
    r"|employers?|institutional( investors)?|institutions|businesses|business|small business"
    r"|members?|partners?|investors?|agents?|producers?|consumers?|personal|corporate)$",
    re.IGNORECASE,
)
_STRUCTURAL_SEGMENT = re.compile(
    r"^(\d+|forms?|faqs?|resources?|articles?|insights?|news|blog|help-cent(er|re)|tools?"
    r"|calculators?|contact.*|about.*|overview|learn.*|tax-cent(er|re)|search|sitemap"
    r"|en|us|en-us|index(\.html?)?)$",
    re.IGNORECASE,
)
_FAMILY_WORDS = frozenset(
    {"insurance", "plan", "plans", "solution", "solutions", "service", "services", "product"}
)
_STOP = frozenset({"the", "our", "a", "an", "your", "for", "of"})
_TOKEN = re.compile(r"[a-z0-9]+")


# -- names and addresses -----------------------------------------------------------


def _tokens(name: str) -> list[str]:
    text = name.lower().replace("&", " and ")
    out: list[str] = []
    for tok in _TOKEN.findall(text):
        if tok in _STOP or tok == "and":
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.append(tok)
    return out


def name_key(name: str) -> str:
    """Comparable form of an offering name: lower case, no punctuation, singular words."""
    return " ".join(_tokens(name))


def url_key(href: str | None) -> str | None:
    """Comparable form of an address: host and path, no query, fragment or trailing slash."""
    if not href:
        return None
    parts = urlsplit(href)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/").lower()
    return f"{host}{path}" if host else None


def _path(href: str | None) -> list[str]:
    if not href:
        return []
    return [s for s in urlsplit(href).path.lower().split("/") if s]


def _host(href: str | None) -> str:
    return (urlsplit(href or "").hostname or "").lower().removeprefix("www.")


def _humanize(segment: str) -> str:
    words = re.sub(r"[-_]+", " ", segment).strip()
    return words[:1].upper() + words[1:]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "x"


def classify_label(label: str) -> EntryRole:
    """Role of a menu entry from its label alone (`unknown` when nothing matches)."""
    text = label.strip()
    if _GROUP.match(text):
        return "group"
    if _LANDING.search(text):
        return "landing"
    if _TOOL.search(text):
        return "tool"
    if _TASK.search(text):
        return "task"
    if _CONTENT.search(text):
        return "content"
    if _CORPORATE.search(text):
        return "corporate"
    if _AUDIENCE.match(text):
        return "audience"
    return "unknown"


def _same_name(a: str, b: str, family: str | None) -> bool:
    """`Term life` and `Term life insurance` under Life insurance are one offering."""
    ka, kb = set(_tokens(a)), set(_tokens(b))
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    small, big = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if not small <= big:
        return False
    allowed = _FAMILY_WORDS | set(_tokens(family or ""))
    return (big - small) <= allowed


# -- drafts --------------------------------------------------------------------------


@dataclass(eq=False)
class _Draft:
    name: str
    level: OfferingLevel
    href: str | None
    parent: _Draft | None
    aliases: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    schema_types: set[str] = field(default_factory=set)
    offering_id: str = ""

    def family(self) -> _Draft:
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def depth(self) -> int:
        return 0 if self.parent is None else 1 + self.parent.depth()


@dataclass
class _Builder:
    drafts: list[_Draft] = field(default_factory=list)
    by_url: dict[str, _Draft] = field(default_factory=dict)
    others: list[MenuEntry] = field(default_factory=list)
    other_owner: list[_Draft | None] = field(default_factory=list)
    audiences: list[tuple[str, str, Evidence]] = field(default_factory=list)

    def find(self, name: str, href: str | None, parent: _Draft | None) -> _Draft | None:
        key = url_key(href)
        if key and key in self.by_url:
            return self.by_url[key]
        family = parent.family().name if parent is not None else None
        for draft in self.drafts:
            same_branch = draft.parent is parent or (
                parent is not None and draft.family() is parent.family()
            )
            if (same_branch or draft.parent is None or parent is None) and _same_name(
                draft.name, name, family
            ):
                if key and url_key(draft.href) and url_key(draft.href) != key:
                    continue  # same words, different pages: keep both
                return draft
        return None

    def add(
        self,
        name: str,
        href: str | None,
        level: OfferingLevel,
        parent: _Draft | None,
        signal: Signal,
        evidence: Evidence,
    ) -> _Draft:
        draft = self.find(name, href, parent)
        if draft is None:
            draft = _Draft(name=name, level=level, href=href, parent=parent)
            self.drafts.append(draft)
        else:
            if name != draft.name and name not in draft.aliases:
                draft.aliases.append(name)
            if draft.href is None and href:
                draft.href = href
            # The more specific placement wins: an entry listed both at the top
            # and under a family belongs to that family.
            if draft.parent is None and parent is not None and not _is_ancestor(draft, parent):
                draft.parent, draft.level = parent, level
        if signal not in draft.signals:
            draft.signals.append(signal)
        if evidence not in draft.evidence:
            draft.evidence.append(evidence)
        key = url_key(draft.href)
        if key:
            self.by_url.setdefault(key, draft)
        return draft


def _is_ancestor(node: _Draft, of: _Draft) -> bool:
    probe: _Draft | None = of
    while probe is not None:
        if probe is node:
            return True
        probe = probe.parent
    return False


def _signal(menu: SiteMenu) -> Signal:
    return {"header": "header_menu", "footer": "footer_menu"}.get(menu.kind, "other_menu")  # type: ignore[return-value]


def _child_level(parent: _Draft | None) -> OfferingLevel:
    if parent is None:
        return "offering"
    return "offering" if parent.level == "family" else "sub_offering"


def _walk_menu(builder: _Builder, menu: SiteMenu) -> None:
    signal = _signal(menu)

    def visit(
        items: list[MenuItem],
        path: list[str],
        owner: _Draft | None,
        inherited: EntryRole | None,
    ) -> None:
        for item in items:
            label = item.label.strip()
            if not label:
                continue
            here = [*path, label]
            evidence = Evidence(kind="menu", ref=menu.menu_id, detail=" › ".join(here))
            role = classify_label(label)
            if role == "unknown" and inherited in ("task", "tool", "content", "corporate"):
                role = inherited
            top = not path
            if role == "landing":
                if owner is not None and owner.href is None and item.href:
                    owner.href = item.href
                    builder.by_url.setdefault(url_key(item.href) or "", owner)
                continue
            if role == "audience" and top:
                if item.href:
                    builder.audiences.append((label, item.href, evidence))
                visit(item.children, here, owner, None)
                continue
            if role == "group":
                visit(item.children, here, owner, None)
                continue
            if role in ("unknown", "audience"):
                if menu.kind == "header" and owner is None:
                    draft = builder.add(label, item.href, "family", None, signal, evidence)
                else:
                    draft = builder.add(
                        label, item.href, _child_level(owner), owner, signal, evidence
                    )
                visit(item.children, here, draft, None)
                continue
            builder.others.append(MenuEntry(label=label, role=role, href=item.href, menu_path=here))
            builder.other_owner.append(owner)
            if (
                owner is not None
                and role in ("tool", "task")
                and not item.children
                and label not in owner.tools
            ):
                owner.tools.append(label)
            visit(item.children, here, owner, role)

    visit(menu.items, [], None, None)


# -- pages ---------------------------------------------------------------------------


@dataclass
class _Mapping:
    draft: _Draft
    basis: PageBasis


def _map_pages(builder: _Builder, stamps: Sequence[PageStamp], site: str) -> dict[str, _Mapping]:
    by_path: list[tuple[list[str], str, _Draft]] = []
    for draft in builder.drafts:
        if draft.href and _host(draft.href) == site and _path(draft.href):
            by_path.append((_path(draft.href), _host(draft.href), draft))
    by_path.sort(key=lambda t: -len(t[0]))
    keys = {name_key(d.name): d for d in builder.drafts}
    for d in builder.drafts:
        for alias in d.aliases:
            keys.setdefault(name_key(alias), d)
    mapped: dict[str, _Mapping] = {}
    for stamp in stamps:
        if not stamp.knowledge_eligible:
            continue
        url = stamp.final_url or stamp.url
        segs = _path(url)
        for path, _, draft in by_path:
            if segs[: len(path)] == path:
                basis: PageBasis = "same_url" if len(segs) == len(path) else "url_family"
                mapped[stamp.url] = _Mapping(draft, basis)
                break
        if stamp.url in mapped:
            continue
        for crumb in reversed(stamp.breadcrumbs):
            hit = keys.get(name_key(crumb))
            if hit is not None:
                mapped[stamp.url] = _Mapping(hit, "breadcrumb")
                break
        if stamp.url in mapped:
            continue
        for name in [*stamp.schema_org.services, *stamp.schema_org.products]:
            hit = keys.get(name_key(name))
            if hit is not None:
                mapped[stamp.url] = _Mapping(hit, "schema_name")
                break
    return mapped


def _schema_offerings(
    builder: _Builder, stamps: Sequence[PageStamp], mapped: dict[str, _Mapping]
) -> None:
    """Services and products a page declares become offerings (under its page's offering)."""
    for stamp in stamps:
        if not stamp.knowledge_eligible:
            continue
        owner = mapped[stamp.url].draft if stamp.url in mapped else None
        for kind, names in (
            ("Service", stamp.schema_org.services),
            ("Product", stamp.schema_org.products),
        ):
            for name in names:
                evidence = Evidence(kind="schema", ref=stamp.url, detail=f"{kind}.name: {name}")
                if owner is not None and _same_name(owner.name, name, owner.family().name):
                    target = owner
                    if evidence not in target.evidence:
                        target.evidence.append(evidence)
                    if "schema" not in target.signals:
                        target.signals.append("schema")
                else:
                    target = builder.add(name, None, _child_level(owner), owner, "schema", evidence)
                    current = mapped.get(stamp.url)
                    if current is None or current.basis == "url_family":
                        # The product the page declares is more specific than its folder.
                        mapped[stamp.url] = _Mapping(target, "schema_name")
                target.schema_types.add(kind)


def _url_sub_offerings(
    builder: _Builder, stamps: Sequence[PageStamp], mapped: dict[str, _Mapping]
) -> None:
    """A path segment under an offering shared by several pages is a sub-offering."""
    groups: dict[tuple[int, str], list[PageStamp]] = defaultdict(list)
    owners: dict[int, _Draft] = {}
    for stamp in stamps:
        m = mapped.get(stamp.url)
        if m is None or m.basis != "url_family" or not m.draft.href:
            continue
        base = _path(m.draft.href)
        segs = _path(stamp.final_url or stamp.url)
        if len(segs) <= len(base):
            continue
        segment = segs[len(base)]
        if _STRUCTURAL_SEGMENT.match(segment) or len(segs) == len(base) + 1:
            continue  # leaf pages of the offering itself, or structural folders
        groups[(id(m.draft), segment)].append(stamp)
        owners[id(m.draft)] = m.draft
    for (owner_id, segment), members in sorted(groups.items(), key=lambda kv: kv[0][1]):
        if len(members) < _MIN_FAMILY_PAGES:
            continue
        owner = owners[owner_id]
        href = owner.href.rstrip("/") + "/" + segment if owner.href else None
        evidence = Evidence(
            kind="url", ref=href or segment, detail=f"{len(members)} pages under this folder"
        )
        sub = builder.add(
            _humanize(segment), href, _child_level(owner), owner, "url_family", evidence
        )
        for stamp in members:
            mapped[stamp.url] = _Mapping(sub, "url_family")


# -- cards ---------------------------------------------------------------------------


def _sentence_with(text: str, needle: str) -> str:
    for start, end in split_sentences(text):
        if needle in text[start:end]:
            return text[start:end][:240]
    return needle


def _fill_card(
    offering: Offering,
    links: list[PageLink],
    stamps_by_url: dict[str, PageStamp],
    chunks_by_page: dict[str, list[Chunk]],
    audiences: list[tuple[str, str, Evidence]],
) -> None:
    own = next((link for link in links if link.basis == "same_url"), None)
    if own is not None:
        stamp = stamps_by_url[own.url]
        text = stamp.meta_description or stamp.page_summary
        if text:
            field_name = "meta_description" if stamp.meta_description else "page_summary"
            offering.description = CardAttribute(
                value=text, evidence=[Evidence(kind="page_meta", ref=own.url, detail=field_name)]
            )
    href_path = _path(offering.href)
    for label, href, evidence in audiences:
        a_path = _path(href)
        if a_path and href_path[: len(a_path)] == a_path:
            offering.audiences.append(CardAttribute(value=label, evidence=[evidence]))
    seen_numbers: set[str] = set()
    seen_questions: set[str] = set()
    seen_ctas: set[str] = set()
    for link in links:
        stamp = stamps_by_url[link.url]
        for question in stamp.schema_org.faq_questions:
            if question not in seen_questions and len(offering.questions) < _MAX_QUESTIONS:
                seen_questions.add(question)
                offering.questions.append(
                    CardAttribute(
                        value=question,
                        evidence=[Evidence(kind="schema", ref=link.url, detail="Question.name")],
                    )
                )
        for offer in stamp.schema_org.offers:
            offering.pricing.append(
                CardAttribute(
                    value=offer, evidence=[Evidence(kind="schema", ref=link.url, detail="Offer")]
                )
            )
        for cta in stamp.ctas:
            if (
                cta.strength != "strong"
                or cta.text.lower() in seen_ctas
                or len(offering.ctas) >= _MAX_CTAS
            ):
                continue
            seen_ctas.add(cta.text.lower())
            offering.ctas.append(
                CardAttribute(
                    value=cta.text,
                    evidence=[Evidence(kind="url", ref=link.url, detail=cta.url)],
                )
            )
        for chunk in chunks_by_page.get(link.url, []):
            first = chunk.text.split("\n", 1)[0].strip()
            if (
                chunk.kind == "faq"
                and first.endswith("?")
                and first not in seen_questions
                and len(offering.questions) < _MAX_QUESTIONS
            ):
                seen_questions.add(first)
                offering.questions.append(
                    CardAttribute(
                        value=first,
                        evidence=[Evidence(kind="chunk", ref=chunk.chunk_id, detail=first)],
                    )
                )
            for q in quantities_in(chunk.text):
                if q.text in seen_numbers or len(offering.numbers) >= _MAX_NUMBERS:
                    continue
                seen_numbers.add(q.text)
                offering.numbers.append(
                    CardAttribute(
                        value=q.text,
                        evidence=[
                            Evidence(
                                kind="chunk",
                                ref=chunk.chunk_id,
                                detail=_sentence_with(chunk.text, q.text),
                            )
                        ],
                    )
                )


def _offering_ids(drafts: list[_Draft]) -> None:
    taken: set[str] = set()
    for draft in sorted(drafts, key=lambda d: (d.depth(), d.name.lower())):
        base = "off-" + _slug(draft.name)
        candidate = base
        if candidate in taken and draft.parent is not None:
            candidate = f"off-{_slug(draft.parent.name)}--{_slug(draft.name)}"
        n = 2
        while candidate in taken:
            candidate = f"{base}-{n}"
            n += 1
        taken.add(candidate)
        draft.offering_id = candidate


def _ordered(drafts: list[_Draft]) -> list[_Draft]:
    children: dict[int, list[_Draft]] = defaultdict(list)
    roots: list[_Draft] = []
    for d in drafts:
        (children[id(d.parent)] if d.parent is not None else roots).append(d)
    out: list[_Draft] = []

    def add(node: _Draft) -> None:
        out.append(node)
        for child in children[id(node)]:
            add(child)

    for root in roots:
        add(root)
    return out


def _map_sections(entries: list[MenuEntry], stamps: Sequence[PageStamp], site: str) -> set[str]:
    """File pages that are under no offering into the non-offering section that covers them.

    By the longest address prefix of a section's link, else by the page's breadcrumb.
    Returns the pages filed.
    """
    with_path = sorted(
        (e for e in entries if e.href and _host(e.href) == site and _path(e.href)),
        key=lambda e: -len(_path(e.href)),
    )
    by_label: dict[str, MenuEntry] = {}
    for entry in entries:
        by_label.setdefault(name_key(entry.label), entry)
    filed: set[str] = set()
    for stamp in stamps:
        segs = _path(stamp.final_url or stamp.url)
        target = next((e for e in with_path if segs[: len(_path(e.href))] == _path(e.href)), None)
        if target is None:
            target = next(
                (
                    by_label[k]
                    for c in reversed(stamp.breadcrumbs)
                    if (k := name_key(c)) in by_label
                ),
                None,
            )
        if target is not None:
            target.pages.append(stamp.url)
            filed.add(stamp.url)
    return filed


def merge_offerings(catalog: Catalog, keep_id: str, drop_id: str) -> None:
    """Fold one offering into another: names, signals, evidence, pages and children move."""
    by_id = {o.offering_id: o for o in catalog.offerings}
    keep, drop = by_id[keep_id], by_id[drop_id]
    for name in [drop.name, *drop.aliases]:
        if name != keep.name and name not in keep.aliases:
            keep.aliases.append(name)
    for signal in drop.signals:
        if signal not in keep.signals:
            keep.signals.append(signal)
    for evidence in drop.evidence:
        if evidence not in keep.evidence:
            keep.evidence.append(evidence)
    known = {p.url for p in keep.pages}
    keep.pages.extend(p for p in drop.pages if p.url not in known)
    for attr in (
        "audiences",
        "features",
        "deliverables",
        "pricing",
        "numbers",
        "questions",
        "ctas",
    ):
        target = getattr(keep, attr)
        target.extend(a for a in getattr(drop, attr) if a not in target)
    keep.tools.extend(t for t in drop.tools if t not in keep.tools)
    keep.description = keep.description or drop.description
    keep.href = keep.href or drop.href
    for other in catalog.offerings:
        if other.parent_id == drop_id:
            other.parent_id = keep_id
    for entry in catalog.other_entries:
        if entry.offering_id == drop_id:
            entry.offering_id = keep_id
    catalog.offerings = [o for o in catalog.offerings if o.offering_id != drop_id]


def build_catalog(
    site: SiteStamp,
    stamps: Sequence[PageStamp],
    chunks: Iterable[Chunk],
    *,
    now: datetime | None = None,
) -> Catalog:
    """The site's offering tree with evidence-backed cards (code only, no model)."""
    builder = _Builder()
    order = {"header": 0, "other": 1, "footer": 2}
    for menu in sorted(site.menus, key=lambda m: order.get(m.kind, 3)):
        if menu.kind != "breadcrumb":
            _walk_menu(builder, menu)
    mapped = _map_pages(builder, stamps, site.site)
    _schema_offerings(builder, stamps, mapped)
    mapped.update(
        {k: v for k, v in _map_pages(builder, stamps, site.site).items() if k not in mapped}
    )
    _url_sub_offerings(builder, stamps, mapped)
    _offering_ids(builder.drafts)

    chunks_by_page: dict[str, list[Chunk]] = defaultdict(list)
    stamp_by_final: dict[str, str] = {}
    for stamp in stamps:
        stamp_by_final[stamp.final_url or stamp.url] = stamp.url
    for chunk in chunks:
        chunks_by_page[stamp_by_final.get(chunk.url, chunk.url)].append(chunk)
    stamps_by_url = {s.url: s for s in stamps}

    links: dict[int, list[PageLink]] = defaultdict(list)
    for url, m in sorted(mapped.items()):
        links[id(m.draft)].append(
            PageLink(
                url=url,
                basis=m.basis,
                chunk_ids=[c.chunk_id for c in chunks_by_page.get(url, [])],
            )
        )

    offerings: list[Offering] = []
    for draft in _ordered(builder.drafts):
        family = draft.family()
        level: OfferingLevel = (
            "family" if draft.parent is None and draft.level == "family" else draft.level
        )
        offering = Offering(
            offering_id=draft.offering_id,
            name=draft.name,
            aliases=draft.aliases,
            level=level,
            parent_id=draft.parent.offering_id if draft.parent is not None else None,
            family=family.name if family is not draft else None,
            href=draft.href,
            signals=draft.signals,
            evidence=draft.evidence,
            tools=draft.tools,
            pages=links.get(id(draft), []),
        )
        _fill_card(offering, offering.pages, stamps_by_url, chunks_by_page, builder.audiences)
        offerings.append(offering)

    by_draft = {id(d): d.offering_id for d in builder.drafts}
    others = [
        entry.model_copy(update={"offering_id": by_draft[id(owner)] if owner is not None else None})
        for entry, owner in zip(builder.others, builder.other_owner, strict=True)
    ]
    eligible = [s for s in stamps if s.knowledge_eligible]
    in_sections = _map_sections(others, [s for s in eligible if s.url not in mapped], site.site)
    unmapped = sorted(s.url for s in eligible if s.url not in mapped and s.url not in in_sections)
    roles = Counter(e.role for e in others)
    levels = Counter(o.level for o in offerings)
    stats = {
        "families": levels["family"],
        "offerings": levels["offering"],
        "sub_offerings": levels["sub_offering"],
        "pages": len(eligible),
        "pages_mapped": len(mapped),
        "pages_in_sections": len(in_sections),
        "pages_unmapped": len(unmapped),
        "chunks_mapped": sum(len(p.chunk_ids) for o in offerings for p in o.pages),
        "single_signal_offerings": sum(1 for o in offerings if len(set(o.signals)) == 1),
        **{f"menu_{role}": n for role, n in sorted(roles.items())},
    }
    return Catalog(
        run_id=site.run_id,
        site=site.site,
        site_name=site.site_name,
        built_at=now or datetime.now(UTC),
        engines={"catalog": "code"},
        offerings=offerings,
        other_entries=others,
        unmapped_pages=unmapped,
        stats=stats,
    )
