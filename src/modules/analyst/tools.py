"""The tools a card run may call: Module 1 (content), its knowledge spaces and catalogs.

Each tool has a typed input (validated before it runs), reads only inside the
run's scope (`Playground`), enters everything it returns in the evidence ledger,
and returns JSON of bounded size. Tools of modules that are not built yet are
declared with `built=False`: a card that needs one cannot run, a card that only
uses one optionally runs without it and says so.

Tool results are data scraped from websites: they are wrapped as such, and the
shared rules tell Claude never to follow instructions found inside them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.core.schemas import StrictModel
from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.analyst.playground import Playground, ScopeError, SiteData
from src.modules.content_processor.export.commands import load_records
from src.modules.content_processor.knowledge.catalog_models import Catalog
from src.modules.content_processor.knowledge.federated import federated_search
from src.modules.content_processor.knowledge.inventory_models import PageInventory
from src.modules.content_processor.knowledge.models import PageStamp
from src.modules.content_processor.knowledge.numeric import np

__all__ = [
    "MAX_RESULT_CHARS",
    "TOOLS",
    "ToolError",
    "ToolSpec",
    "anthropic_tools",
    "run_tool",
]

MAX_RESULT_CHARS = 40_000
_MAX_CHUNK_CHARS = 2_500
_MAX_PAGES = 300
_MAX_PAIRS = 40
_MAX_CHANGED = 60


class ToolError(Exception):
    """A tool could not answer (bad input, outside the scope, data not built)."""


@dataclass(frozen=True)
class ToolSpec:
    """A tool: its module, description, input model and implementation."""

    name: str
    module: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Playground, Any], dict[str, Any]] | None
    built: bool = True

    def schema(self) -> dict[str, Any]:
        """The JSON schema of the input, as Claude sees it."""
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        schema.pop("description", None)
        return schema


# -- inputs --------------------------------------------------------------------------


class NoInput(StrictModel):
    """No input."""


class SearchInput(StrictModel):
    """Search the knowledge spaces."""

    query: str = Field(min_length=1, max_length=300, description="What to look for")
    websites: list[str] | None = Field(
        default=None, max_length=20, description="Domains to search; omit for the whole scope"
    )
    per_site: int | None = Field(
        default=None, ge=1, le=10, description="Best passages per website (side by side)"
    )
    top_k: int = Field(default=8, ge=1, le=25, description="Passages overall (without per_site)")
    role: str | None = Field(
        default=None,
        description="Only passages labelled with this role: claim, feature, pricing, "
        "proof_point, faq, cta, disclaimer",
    )


class UrlInput(StrictModel):
    """One page."""

    url: str = Field(min_length=8, max_length=2000, description="The page's full address")


class WebsiteInput(StrictModel):
    """One website."""

    website: str = Field(min_length=3, max_length=300, description="Domain, e.g. example.com")


class SimilarInput(StrictModel):
    """Near-duplicate page pairs of one website."""

    website: str = Field(min_length=3, max_length=300)
    min_similarity: float = Field(default=0.85, ge=0.5, le=0.99)


class ThemeInput(StrictModel):
    """The project's theme map."""

    rebuild: bool = Field(default=False, description="Build it again from the latest scrapes")


class MonitorInput(StrictModel):
    """The project's latest monitoring check."""

    run_check: bool = Field(
        default=False, description="Run a new check first (compares the latest scrapes)"
    )


class KeywordInput(StrictModel):
    """One keyword."""

    keyword: str = Field(min_length=1, max_length=200)


class GapInput(StrictModel):
    """The project's gap findings."""

    kind: str | None = Field(
        default=None,
        description="Only this kind: theme_gap, theme_behind, pillar_gap, funnel_gap, "
        "keyword_gap (complete deficit: the client is not in the top 100, its data complete), "
        "keyword_unverified (probable gap: the client's data was cut short), keyword_behind "
        "(the client ranks 21-100), striking_distance (11-20), competitor_momentum, "
        "offering_gap, unwatched_competitor",
    )
    limit: int = Field(default=40, ge=1, le=200)
    rebuild: bool = Field(default=False, description="Build the gap analysis again first")


class RankingsInput(StrictModel):
    """A website's rankings in the latest research snapshot."""

    website: str = Field(min_length=3, max_length=300)
    theme_id: str | None = Field(default=None, description="Only keywords of this theme")
    max_position: int = Field(default=100, ge=1, le=100)
    limit: int = Field(default=100, ge=1, le=500)


# -- helpers -------------------------------------------------------------------------------


def _clip(text: str, limit: int = _MAX_CHUNK_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _data(play: Playground, website_text: str) -> SiteData:
    try:
        return play.require(play.website(website_text))
    except ScopeError as exc:
        raise ToolError(str(exc)) from exc


def _page_data(play: Playground, url: str) -> SiteData:
    try:
        return play.require(play.website_of_url(url))
    except ScopeError as exc:
        raise ToolError(str(exc)) from exc


def _stamp_for(play: Playground, data: SiteData, url: str) -> PageStamp | None:
    stamps = play.stamps(data)
    for candidate in (url, url.rstrip("/"), url.rstrip("/") + "/"):
        if candidate in stamps:
            return stamps[candidate]
    return None


def _page_fact(play: Playground, data: SiteData, stamp: PageStamp) -> dict[str, Any]:
    website = data.website
    ref = play.ledger.fact(
        stamp.url,
        kind="page",
        website=website.key,
        url=stamp.url,
        project=data.copy.project_name,
        title=stamp.title or stamp.h1 or stamp.url,
        text=stamp.meta_description or "",
    )
    return {
        "ref": ref,
        "url": stamp.url,
        "title": stamp.title,
        "meta_description": stamp.meta_description,
        "h1": stamp.h1,
        "canonical": stamp.canonical,
        "lang": stamp.lang,
        "breadcrumbs": stamp.breadcrumbs,
        "headings": stamp.headings[:40],
        "schema_types": stamp.schema_org.types,
        "faq_questions": stamp.schema_org.faq_questions[:20],
        "offers": stamp.schema_org.offers[:20],
        "ctas": [{"text": c.text, "url": c.url, "strength": c.strength} for c in stamp.ctas[:20]],
        "numbers": stamp.numbers[:30],
        "word_count": stamp.word_count,
        "main_word_count": stamp.main_word_count,
        "reading_ease": stamp.reading_ease,
        "components": stamp.components,
        "published_at": stamp.published_at.isoformat() if stamp.published_at else None,
        "modified_at": stamp.modified_at.isoformat() if stamp.modified_at else None,
        "page_format": stamp.page_format,
        "knowledge_eligible": stamp.knowledge_eligible,
        "exclusion_reason": stamp.exclusion_reason,
        "duplicate_of": stamp.duplicate_of,
        "near_duplicates": stamp.near_duplicates[:10],
        "verdict": stamp.badge,
    }


def _fit(payload: dict[str, Any]) -> str:
    """JSON of the payload, halving its longest list until it fits the size limit."""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    notes: list[str] = []
    while len(text) > MAX_RESULT_CHARS:
        lists = [(k, v) for k, v in payload.get("data", {}).items() if isinstance(v, list) and v]
        if not lists:
            return text[:MAX_RESULT_CHARS]
        key, longest = max(lists, key=lambda kv: len(json.dumps(kv[1], default=str)))
        keep = max(1, len(longest) // 2)
        notes.append(f"{key}: showing {keep} of {len(longest)}; narrow the request for the rest")
        payload["data"][key] = longest[:keep]
        payload["notes"] = [*payload.get("notes", []), notes[-1]]
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return text


# -- handlers ------------------------------------------------------------------------------


def _list_websites(play: Playground, _args: NoInput) -> dict[str, Any]:
    rows = []
    for website in play.websites():
        data = play.site_data(website)
        counts: dict[str, int] = {}
        model = None
        if data is not None:
            store = play.open_store(data)
            try:
                counts = store.counts()
                model = store.meta().get("model")
            finally:
                store.close()
        ref = play.ledger.fact(website.key, kind="website", website=website.key, title=website.name)
        rows.append(
            {
                "ref": ref,
                "website": website.key,
                "name": website.name,
                "role_in_this_run": play.role_in_run(website),
                "note": website.note,
                "tags": website.tags,
                "domains": sorted(website.domains),
                "competes_with": sorted(website.competitors),
                "same_client_as": sorted(w.key for w in play.graph.sisters_of(website)),
                "projects": sorted({c.project_name for c in website.copies}),
                "searchable": data is not None,
                "last_scrape": data.run.finished_at.isoformat()
                if data and data.run.finished_at
                else None,
                "pages": counts.get("page_vectors", 0),
                "passages": counts.get("chunk_vectors", 0),
                "embedding_model": model,
                "search_by_meaning": bool(model) and not str(model).startswith("hashing-"),
            }
        )
    return {"websites": rows}


def _search(play: Playground, args: SearchInput) -> dict[str, Any]:
    if args.websites:
        try:
            websites = [play.website(w) for w in args.websites]
        except ScopeError as exc:
            raise ToolError(str(exc)) from exc
    else:
        websites = play.websites()
    spaces = []
    skipped = []
    for website in websites:
        data = play.site_data(website)
        if data is None:
            skipped.append({"website": website.key, "reason": "not scraped or not indexed yet"})
        else:
            spaces.append(data.copy.space())
    wanted = args.top_k * 3 if args.role else args.top_k
    per_site = args.per_site * 3 if (args.per_site and args.role) else args.per_site
    result = federated_search(
        spaces,
        args.query,
        open_store=play.access.open_store,
        embedder_for=play.access.embedder_for,
        top_k=wanted,
        per_site=per_site,
    )
    by_namespace = {d.copy.namespace: w for w in websites if (d := play.site_data(w)) is not None}
    passages = []
    taken: dict[str, int] = {}
    for found in result.hits:
        chunk = found.hit.chunk
        if args.role and chunk.chunk_role != args.role:
            continue
        owner = by_namespace.get(found.space.namespace)
        key = owner.key if owner else found.space.site_name
        if args.per_site and taken.get(key, 0) >= args.per_site:
            continue
        taken[key] = taken.get(key, 0) + 1
        ref = play.ledger.chunk(
            chunk.chunk_id,
            website=key,
            url=chunk.url,
            project=found.space.project_name,
            headings=chunk.heading_path,
            text=chunk.verbatim_text,
            block_ids=chunk.block_ids,
            evidence_ids=chunk.evidence_ids,
        )
        passages.append(
            {
                "ref": ref,
                "website": key,
                "role_of_website": play.role_in_run(owner) if owner else found.space.role,
                "url": chunk.url,
                "headings": chunk.heading_path,
                "kind": chunk.kind,
                "chunk_role": chunk.chunk_role,
                "offering": chunk.offering_id,
                "text": _clip(chunk.verbatim_text),
                "found_by": [
                    name
                    for name, rank in (
                        ("meaning", found.hit.vector_rank),
                        ("keywords", found.hit.keyword_rank),
                    )
                    if rank
                ],
            }
        )
        if not args.per_site and len(passages) >= args.top_k:
            break
    skipped += [{"website": s.site_name, "reason": why} for s, why in result.skipped]
    searched = sorted(w.key for w in websites if play.site_data(w) is not None)
    search_ref = play.ledger.fact(
        f"{args.query}\x1f{','.join(searched)}\x1f{args.role}",
        kind="search",
        website=",".join(searched),
        title=f'search "{args.query}"' + (f" (role {args.role})" if args.role else ""),
        text=f"{len(passages)} passages found in {', '.join(searched) or 'no website'}",
    )
    out: dict[str, Any] = {
        "search_ref": search_ref,
        "websites_searched": searched,
        "passages": passages,
        "skipped": skipped,
    }
    notes = [
        f"{s.site_name}: {why}; passages that say the same thing in other words may be "
        "missing (start the local model server for search by meaning)"
        for s, why in result.words_only
    ] + [
        f"{s.site_name}: {why}; paraphrases are found, but less reliably than with bge-m3"
        for s, why in result.lighter
    ]
    if notes:
        out["notes"] = notes
    return out


def _get_page(play: Playground, args: UrlInput) -> dict[str, Any]:
    data = _page_data(play, args.url)
    store = play.open_store(data)
    try:
        rows = []
        for candidate in (args.url, args.url.rstrip("/"), args.url.rstrip("/") + "/"):
            rows = store.page_chunks(candidate)
            if rows:
                break
    finally:
        store.close()
    stamp = _stamp_for(play, data, args.url)
    if not rows and stamp is None:
        msg = f"{args.url} was not captured in {data.website.name}'s latest scrape"
        raise ToolError(msg)
    sections = []
    for row in rows:
        ref = play.ledger.chunk(
            row.chunk_id,
            website=data.website.key,
            url=row.url,
            project=data.copy.project_name,
            headings=row.heading_path,
            text=row.verbatim_text,
            block_ids=row.block_ids,
            evidence_ids=row.evidence_ids,
        )
        sections.append(
            {
                "ref": ref,
                "order": row.order,
                "kind": row.kind,
                "headings": row.heading_path,
                "chunk_role": row.chunk_role,
                "offering": row.offering_id,
                "text": _clip(row.verbatim_text, 4_000),
            }
        )
    out: dict[str, Any] = {"website": data.website.key, "sections": sections}
    if stamp is not None:
        out["facts"] = _page_fact(play, data, stamp)
    return out


def _page_facts(play: Playground, args: UrlInput) -> dict[str, Any]:
    data = _page_data(play, args.url)
    stamp = _stamp_for(play, data, args.url)
    if stamp is None:
        msg = f"{args.url} has no page facts in {data.website.name}'s latest scrape"
        raise ToolError(msg)
    return {"website": data.website.key, "facts": _page_fact(play, data, stamp)}


def _list_pages(play: Playground, args: WebsiteInput) -> dict[str, Any]:
    data = _data(play, args.website)
    store = play.open_store(data)
    try:
        page_rows = {p.url: p for p in store.pages()}
    finally:
        store.close()
    stamps = play.stamps(data)
    urls = sorted({s.url for s in stamps.values()} | set(page_rows))
    pages = []
    for url in urls[:_MAX_PAGES]:
        stamp = stamps.get(url)
        row = page_rows.get(url)
        ref = play.ledger.fact(
            url,
            kind="page",
            website=data.website.key,
            url=url,
            project=data.copy.project_name,
            title=(stamp.title if stamp else None) or (row.title if row else None) or url,
        )
        pages.append(
            {
                "ref": ref,
                "url": url,
                "title": stamp.title if stamp else (row.title if row else None),
                "h1": stamp.h1 if stamp else None,
                "page_type": row.page_type if row else None,
                "offering": row.offering_id if row else None,
                "path": "/" + "/".join(stamp.path_segments) if stamp else None,
                "word_count": stamp.main_word_count if stamp else None,
                "published_at": stamp.published_at.isoformat()
                if stamp and stamp.published_at
                else None,
                "modified_at": stamp.modified_at.isoformat()
                if stamp and stamp.modified_at
                else None,
                "in_knowledge_base": row is not None,
                "excluded_because": stamp.exclusion_reason if stamp else None,
            }
        )
    notes = [f"{len(urls) - _MAX_PAGES} more pages not listed"] if len(urls) > _MAX_PAGES else []
    return {"website": data.website.key, "total_pages": len(urls), "pages": pages, "notes": notes}


def _catalog(play: Playground, args: WebsiteInput) -> dict[str, Any]:
    data = _data(play, args.website)
    path = data.file(".catalog.json")
    if path is None:
        msg = f"{data.website.name} has no offering catalog in its latest scrape"
        raise ToolError(msg)
    catalog = Catalog.model_validate_json(path.read_text(encoding="utf-8"))
    offerings = []
    for o in catalog.offerings:
        ref = play.ledger.fact(
            f"{data.website.key}\x1f{o.offering_id}",
            kind="offering",
            website=data.website.key,
            url=o.href,
            project=data.copy.project_name,
            title=o.name,
            text="; ".join(e.detail for e in o.evidence if e.detail)[:500],
        )
        offerings.append(
            {
                "ref": ref,
                "offering_id": o.offering_id,
                "name": o.name,
                "level": o.level,
                "parent_id": o.parent_id,
                "family": o.family,
                "type": o.offering_type,
                "href": o.href,
                "aliases": o.aliases,
                "signals": o.signals,
                "pages": [p.url for p in o.pages][:20],
                "page_count": len(o.pages),
                "features": [a.value for a in o.features][:10],
                "audiences": [a.value for a in o.audiences][:10],
                "pricing": [a.value for a in o.pricing][:10],
                "ctas": [a.value for a in o.ctas][:10],
            }
        )
    sections = [
        {"label": e.label, "role": e.role, "pages": len(e.pages)}
        for e in catalog.other_entries
        if e.pages
    ]
    return {
        "website": data.website.key,
        "site_name": catalog.site_name,
        "offerings": offerings,
        "content_sections": sections[:50],
        "unmapped_pages": len(catalog.unmapped_pages),
        "stats": catalog.stats,
    }


def _inventory(play: Playground, data: SiteData) -> list[PageInventory]:
    path = data.file(".inventory.jsonl")
    if path is None:
        records_path = data.file(".records.jsonl")
        if records_path is None:
            msg = f"{data.website.name}'s latest scrape has no records to inventory"
            raise ToolError(msg)
        from src.modules.content_processor.knowledge.commands import store_loader
        from src.modules.content_processor.knowledge.inventory import inventory_records
        from src.modules.content_processor.knowledge.inventory_report import write_inventory

        root = play.evidence_root
        if root is None:
            from src.core.config import get_settings

            root = get_settings().cp_evidence_root
        result = inventory_records(
            load_records(records_path), store_loader(LocalEvidenceStore(root, play.tenant))
        )
        write_inventory(result.pages, data.folder, data.run_name)
        path = data.file(".inventory.jsonl")
        if path is None:  # pragma: no cover - write_inventory always writes it
            raise ToolError("the inventory could not be written")
    with path.open(encoding="utf-8") as handle:
        return [PageInventory.model_validate_json(line) for line in handle if line.strip()]


def _seo_issues(play: Playground, args: WebsiteInput) -> dict[str, Any]:
    data = _data(play, args.website)
    pages = _inventory(play, data)
    by_code: dict[str, dict[str, Any]] = {}
    for page in pages:
        for issue in page.issues:
            entry = by_code.setdefault(
                issue.code,
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "pages": 0,
                    "items": 0,
                    "example_urls": [],
                },
            )
            entry["pages"] += 1
            entry["items"] += issue.count
            if len(entry["example_urls"]) < 5:
                entry["example_urls"].append(page.url)
    order = {"error": 0, "warning": 1, "notice": 2}
    issues = []
    for entry in sorted(
        by_code.values(), key=lambda e: (order.get(str(e["severity"]), 9), -e["pages"])
    ):
        entry["ref"] = play.ledger.fact(
            f"{data.website.key}\x1f{entry['code']}",
            kind="issue",
            website=data.website.key,
            title=f"{entry['code']}: {entry['message']}",
            text=f"{entry['pages']} pages, {entry['items']} items",
        )
        issues.append(entry)
    captured = sum(1 for p in pages if p.status == "inventoried")
    return {
        "website": data.website.key,
        "pages_inventoried": len(pages),
        "pages_captured": captured,
        "issues": issues,
    }


def _similar_pages(play: Playground, args: SimilarInput) -> dict[str, Any]:
    data = _data(play, args.website)
    store = play.open_store(data)
    try:
        pages = store.pages()
        model = store.meta().get("model", "")
    finally:
        store.close()
    if len(pages) < 2:
        return {"website": data.website.key, "pairs": [], "notes": ["fewer than two pages"]}
    matrix = np.asarray([p.embedding for p in pages], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    unit = matrix / np.where(norms == 0, 1, norms)
    sims = unit @ unit.T
    stamps = play.stamps(data)
    pairs = []
    rows, cols = np.where(np.triu(sims, k=1) >= args.min_similarity)
    for i, j in sorted(zip(rows.tolist(), cols.tolist(), strict=True), key=lambda ij: -sims[ij]):
        a, b = pages[i], pages[j]
        sa, sb = stamps.get(a.url), stamps.get(b.url)
        same_title = bool(sa and sb and sa.title and sa.title == sb.title)
        ref = play.ledger.fact(
            f"{a.url}\x1f{b.url}",
            kind="pair",
            website=data.website.key,
            url=a.url,
            title=f"{a.url} ~ {b.url}",
            text=f"similarity {float(sims[i, j]):.3f}",
        )
        pairs.append(
            {
                "ref": ref,
                "url_a": a.url,
                "url_b": b.url,
                "similarity": round(float(sims[i, j]), 3),
                "title_a": sa.title if sa else a.title,
                "title_b": sb.title if sb else b.title,
                "same_title": same_title,
                "page_type_a": a.page_type,
                "page_type_b": b.page_type,
                "offering_a": a.offering_id,
                "offering_b": b.offering_id,
                "declared_duplicate": bool(
                    sa and (sa.duplicate_of == b.url or b.url in sa.near_duplicates)
                ),
            }
        )
        if len(pairs) >= _MAX_PAIRS:
            break
    notes = []
    if str(model).startswith("hashing-"):
        notes.append(
            "similarity is word overlap (offline embedder), not meaning: confirm each pair "
            "with get_page before calling it cannibalization"
        )
    return {"website": data.website.key, "pages": len(pages), "pairs": pairs, "notes": notes}


def _changes(play: Playground, args: WebsiteInput) -> dict[str, Any]:
    from src.modules.monitoring.diff import diff_runs, load_run_files

    data = _data(play, args.website)
    runs = play.ready_runs(data)
    if len(runs) < 2:
        msg = (
            f"{data.website.name} has {len(runs)} finished scrape(s) in this project copy; "
            "changes need two (scrape it again to compare)"
        )
        raise ToolError(msg)
    old_run, new_run = runs[-2], runs[-1]
    folder = play.store.run_dir
    old = load_run_files(
        folder(data.copy.project_id, data.copy.site_id, old_run.run_id), old_run.run_id
    )
    new = load_run_files(
        folder(data.copy.project_id, data.copy.site_id, new_run.run_id), new_run.run_id
    )
    result = diff_runs(old, new)
    added, removed, changed = result.added, result.removed, result.changed
    unchanged = result.unchanged
    key = data.website.key
    changed_out = []
    for c in changed[:_MAX_CHANGED]:
        ref = play.ledger.fact(
            f"{c.url}\x1f{old_run.run_id}\x1f{new_run.run_id}",
            kind="change",
            website=key,
            url=c.url,
            project=data.copy.project_name,
            title=f"changed: {c.title or c.url}",
            text=" | ".join(_clip(t, 300) for t in c.added_text[:3]),
        )
        changed_out.append(
            {
                "ref": ref,
                "url": c.url,
                "head_changes": c.head_changes,
                "new_numbers": c.new_numbers,
                "prices": [p.model_dump() for p in c.prices],
                "drift": c.drift,
                "added_text": c.added_text,
                "removed_text": c.removed_text,
                "sections_added": c.sections_added,
                "sections_removed": c.sections_removed,
            }
        )
    return {
        "website": key,
        "baseline": {"run": old_run.run_id, "finished": str(old_run.finished_at)},
        "latest": {"run": new_run.run_id, "finished": str(new_run.finished_at)},
        "new_pages": [
            {
                "ref": play.ledger.fact(
                    url, kind="new-page", website=key, url=url, title=new.stamps[url].title or url
                ),
                "url": url,
                "title": new.stamps[url].title,
                "h1": new.stamps[url].h1,
            }
            for url in added
        ],
        "removed_pages": removed,
        "changed_pages": changed_out,
        "unchanged_pages": unchanged,
        "template_changes": [t.model_dump() for t in result.template],
    }


def _monitoring(play: Playground, args: MonitorInput) -> dict[str, Any]:
    from src.modules.monitoring.service import monitor_store, run_project_check

    project_id = play.project_ids[0]
    check = None if args.run_check else monitor_store(play.store).latest(project_id)
    if check is None:
        check, _files = run_project_check(play.store, play.access, project_id)
    alerts = []
    for a in check.alerts:
        ref = play.ledger.fact(
            f"{check.check_id}\x1f{a.alert_id}",
            kind="alert",
            website=a.website,
            url=a.urls[0] if a.urls else None,
            project=project_id,
            title=a.title,
            text=a.detail,
        )
        alerts.append({"ref": ref, **a.model_dump(exclude={"alert_id"})})
    return {
        "check_id": check.check_id,
        "created_at": check.created_at.isoformat(),
        "stats": check.stats,
        "alerts": alerts,
        "websites": [
            {
                "website": d.website,
                "role": d.role,
                "compared": [d.baseline_run, d.latest_run],
                "note": d.note,
                "new_pages": len(d.new_pages),
                "removed_pages": len(d.removed_pages),
                "changed_pages": len(d.changed_pages),
                "drift": d.drift,
            }
            for d in check.websites
        ],
        "theme_drift": [
            t.model_dump()
            for t in check.theme_drift
            if t.new_pages or t.removed_pages or t.ownership_before != t.ownership_after
        ],
    }


def _theme_map(play: Playground, args: ThemeInput) -> dict[str, Any]:
    from src.modules.theming.service import build_project_themes, theme_store

    project_id = play.project_ids[0]
    theme_map = None if args.rebuild else theme_store(play.store).latest(project_id)
    if theme_map is None:
        theme_map, _files = build_project_themes(play.store, play.access, project_id)
    themes = []
    for t in theme_map.themes:
        pillar = next((c.pillar.url for c in t.coverage if c.pillar is not None), None)
        ref = play.ledger.fact(
            f"{theme_map.map_id}\x1f{t.theme_id}",
            kind="theme",
            website=",".join(c.website for c in t.coverage if c.pages),
            url=pillar,
            project=project_id,
            title=f"theme: {t.label}",
            text=f"{t.ownership}: {t.ownership_reason}",
        )
        themes.append(
            {
                "ref": ref,
                "theme_id": t.theme_id,
                "name": t.label,
                "keywords": t.terms[:8],
                "pages": t.pages,
                "ownership": t.ownership,
                "why": t.ownership_reason,
                "per_website": {
                    c.website: {
                        "pages": c.pages,
                        "words": c.words,
                        "pillar": c.pillar.url if c.pillar else None,
                    }
                    for c in t.coverage
                },
                "topics": [{"label": tp.label, "pages": len(tp.pages)} for tp in t.topics],
                "funnel": t.funnel,
                "offerings": t.offerings,
            }
        )
    return {
        "map_id": theme_map.map_id,
        "built_at": theme_map.built_at.isoformat(),
        "websites": [
            {"website": w.website, "role": w.role, "pages": w.pages} for w in theme_map.websites
        ],
        "themes": themes,
        "unclustered_pages": theme_map.stats.get("unclustered", 0),
        "skipped": theme_map.skipped,
    }


def _snapshot(play: Playground) -> Any:
    from src.modules.research.service import research_store

    snapshot = research_store(play.store).latest(play.project_ids[0])
    if snapshot is None:
        msg = (
            "no research for this project yet: import a Semrush Organic Positions export or "
            "pull from Semrush in the Research tab"
        )
        raise ToolError(msg)
    return snapshot


def _rankings(play: Playground, args: RankingsInput) -> dict[str, Any]:
    from src.modules.research.build import estimated_visits

    snapshot = _snapshot(play)
    try:
        website = play.website(args.website)
    except ScopeError as exc:
        raise ToolError(str(exc)) from exc
    site = next((w for w in snapshot.websites if w.website == website.key), None)
    if site is None:
        msg = f"{website.name} is not in the latest research snapshot ({snapshot.snapshot_id})"
        raise ToolError(msg)
    rows = [
        r
        for r in site.keywords
        if r.position <= args.max_position
        and (args.theme_id is None or r.theme_id == args.theme_id)
    ]
    rows.sort(key=lambda r: (-r.volume, r.position))
    out = []
    for r in rows[: args.limit]:
        ref = play.ledger.fact(
            f"{snapshot.snapshot_id}\x1f{site.website}\x1f{r.keyword}",
            kind="ranking",
            website=site.website,
            url=r.url,
            title=f"{r.keyword}: #{r.position} ({r.volume}/month)",
            text=f"source {site.source}, snapshot {snapshot.snapshot_id}",
        )
        out.append(
            {
                "ref": ref,
                **r.model_dump(),
                "estimated_visits": estimated_visits(r.volume, r.position),
            }
        )
    return {
        "website": site.website,
        "source": site.source,
        "snapshot": snapshot.snapshot_id,
        "pulled_at": site.pulled_at.isoformat(),
        "total_keywords": len(site.keywords),
        "keywords": out,
    }


def _serp(play: Playground, args: KeywordInput) -> dict[str, Any]:
    snapshot = _snapshot(play)
    wanted = args.keyword.strip().casefold()
    metric = next((k for k in snapshot.keywords if k.keyword.casefold() == wanted), None)
    ref = play.ledger.fact(
        f"{snapshot.snapshot_id}\x1fserp\x1f{wanted}",
        kind="serp",
        website=",".join(w.website for w in snapshot.websites),
        title=f'search results for "{args.keyword}"',
        text=f"snapshot {snapshot.snapshot_id}",
    )
    project_ranks = [
        {"website": w.website, "role": w.role, "position": r.position, "url": r.url}
        for w in snapshot.websites
        for r in w.keywords
        if r.keyword.casefold() == wanted
    ]
    return {
        "ref": ref,
        "keyword": args.keyword,
        "pulled_results": [s.model_dump() for s in metric.serp] if metric else [],
        "project_websites": sorted(project_ranks, key=lambda x: x["position"]),
        "notes": []
        if metric
        else ["this keyword's full results were not pulled; only the project's websites are shown"],
    }


def _keyword_metrics(play: Playground, args: KeywordInput) -> dict[str, Any]:
    snapshot = _snapshot(play)
    wanted = args.keyword.strip().casefold()
    metric = next((k for k in snapshot.keywords if k.keyword.casefold() == wanted), None)
    rows = [r for w in snapshot.websites for r in w.keywords]
    exact = next((r for r in rows if r.keyword.casefold() == wanted), None)
    related: dict[str, int] = {}
    for r in rows:
        if wanted in r.keyword.casefold() and r.keyword.casefold() != wanted:
            related[r.keyword] = max(related.get(r.keyword, 0), r.volume)
    if metric is None and exact is None and not related:
        msg = f'"{args.keyword}" is not in the latest research snapshot ({snapshot.snapshot_id})'
        raise ToolError(msg)
    ref = play.ledger.fact(
        f"{snapshot.snapshot_id}\x1fmetrics\x1f{wanted}",
        kind="keyword",
        website=",".join(w.website for w in snapshot.websites),
        title=f'"{args.keyword}" demand',
        text=f"snapshot {snapshot.snapshot_id}",
    )
    return {
        "ref": ref,
        "keyword": args.keyword,
        "volume": metric.volume if metric else (exact.volume if exact else None),
        "cpc": metric.cpc if metric else (exact.cpc if exact else None),
        "competition": metric.competition if metric else (exact.competition if exact else None),
        "related": [
            {"keyword": k, "volume": v}
            for k, v in sorted(related.items(), key=lambda kv: -kv[1])[:30]
        ],
    }


def _demand(play: Playground, _args: NoInput) -> dict[str, Any]:
    snapshot = _snapshot(play)
    themes = []
    for d in snapshot.demand:
        ref = play.ledger.fact(
            f"{snapshot.snapshot_id}\x1fdemand\x1f{d.theme_id}",
            kind="demand",
            website=",".join(sorted(d.ranked)),
            title=f"demand: {d.label}",
            text=f"{d.keywords} keywords, {d.volume} searches/month",
        )
        themes.append({"ref": ref, **d.model_dump()})
    return {
        "snapshot": snapshot.snapshot_id,
        "database": snapshot.database,
        "websites": [
            {"website": w.website, "role": w.role, "source": w.source} for w in snapshot.websites
        ],
        "themes": themes,
    }


def _competitors(play: Playground, _args: NoInput) -> dict[str, Any]:
    from src.modules.research.competitors import SOURCE_LABELS
    from src.modules.research.service import competitor_book

    project_id = play.project_ids[0]
    book = competitor_book(play.store, project_id)
    rows = []
    for s in book.suggestions:
        if s.status == "dismissed":
            continue
        figures = [
            f"relevance {s.relevance:.0%}" if s.relevance is not None else "",
            f"{s.common_keywords:,} keywords in common" if s.common_keywords else "",
            f"in the top 10 for {s.search_results} pulled keyword(s)" if s.search_results else "",
        ]
        ref = play.ledger.fact(
            f"competitors\x1f{project_id}\x1f{s.domain}",
            kind="competitor",
            website=s.domain,
            url=f"https://{s.domain}/",
            project=project_id,
            title=f"suggested competitor: {s.domain}",
            text=", ".join(f for f in figures if f) or "seen in the search data",
        )
        rows.append(
            {
                "ref": ref,
                "domain": s.domain,
                "in_project": s.status == "added",
                "found_by": [SOURCE_LABELS[x] for x in s.sources],
                "relevance": s.relevance,
                "common_keywords": s.common_keywords,
                "organic_keywords": s.organic_keywords,
                "organic_traffic": s.organic_traffic,
                "top10_for_pulled_keywords": s.search_results,
                "example_keywords": s.example_keywords,
                "last_seen": s.last_seen.date().isoformat(),
            }
        )
    return {
        "client_domain": book.client_domain,
        "note": "Websites the search data shows competing with the client. in_project false "
        "means it is not scraped yet: say so, and do not claim facts about its pages.",
        "competitors": rows,
    }


def _gaps(play: Playground, args: GapInput) -> dict[str, Any]:
    from src.modules.gaps.service import build_project_gaps, gap_store

    project_id = play.project_ids[0]
    report = None if args.rebuild else gap_store(play.store).latest(project_id)
    if report is None:
        report, _files = build_project_gaps(play.store, play.access, project_id)
    findings = []
    for f in report.findings:
        if args.kind and f.kind != args.kind:
            continue
        if f.status == "dismissed":
            continue
        ref = play.ledger.fact(
            f"{report.report_id}\x1f{f.finding_id}",
            kind="gap",
            website=",".join(f.competitors),
            url=next((e.url for e in f.evidence if e.url), None),
            project=project_id,
            title=f.title,
            text=f.detail,
        )
        findings.append({"ref": ref, **f.model_dump(exclude={"note"})})
    return {
        "report_id": report.report_id,
        "created_at": report.created_at.isoformat(),
        "missing_inputs": report.missing_inputs,
        "findings": findings[: args.limit],
        "resolved_since_last_time": report.resolved,
    }


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "list_websites",
            "content",
            "The websites this run may read (client and competitors), with whether each is "
            "searchable, page and passage counts, and the embedding model.",
            NoInput,
            _list_websites,
        ),
        ToolSpec(
            "search_knowledge",
            "content",
            "Hybrid search (meaning plus exact words) over the websites' captured text. Returns "
            "verbatim passages with a `ref` to cite. Use per_site for side-by-side comparisons "
            "and role to keep only claims, pricing, proof points, FAQs, CTAs or disclaimers.",
            SearchInput,
            _search,
        ),
        ToolSpec(
            "get_page",
            "content",
            "Every captured section of one page in reading order (each with a `ref`), plus the "
            "page's facts: title, description, headings, structured data, CTAs, dates.",
            UrlInput,
            _get_page,
        ),
        ToolSpec(
            "get_page_facts",
            "content",
            "One page's facts only (no text): title, meta description, H1, canonical, "
            "headings, schema types, FAQ questions, offers, CTAs, numbers, dates, word count.",
            UrlInput,
            _page_facts,
        ),
        ToolSpec(
            "list_pages",
            "content",
            "Every captured page of one website: title, H1, page type, offering, path, word "
            "count and dates (up to 300 pages).",
            WebsiteInput,
            _list_pages,
        ),
        ToolSpec(
            "get_offering_catalog",
            "theming",
            "The website's offering tree (families, offerings, sub-offerings) from its menus, "
            "addresses and structured data, with the pages mapped to each and card attributes.",
            WebsiteInput,
            _catalog,
        ),
        ToolSpec(
            "get_seo_issues",
            "content",
            "Technical SEO/GEO issues of a website's pages, grouped by issue with severity, page "
            "counts and example addresses (titles, descriptions, canonicals, headings, "
            "structured data, images, links). The first call may take a minute.",
            WebsiteInput,
            _seo_issues,
        ),
        ToolSpec(
            "find_similar_pages",
            "content",
            "Pairs of the website's pages whose content is nearly the same (cannibalization "
            "and duplicate candidates), with titles, page types and offerings.",
            SimilarInput,
            _similar_pages,
        ),
        ToolSpec(
            "get_changes",
            "monitoring",
            "What changed on a website between its last two scrapes: new and removed pages, "
            "title/H1/description rewrites, added and removed text, new numbers (prices, rates).",
            WebsiteInput,
            _changes,
        ),
        ToolSpec(
            "get_theme_map",
            "theming",
            "The project's theme map: themes (topic clusters) across the client and its "
            "competitors, each with its pages per website, pillar page per website, topics, "
            "funnel stages and ownership (client_owned, contested, competitor_led, client_gap, "
            "client_only). Built offline from the scrapes; reuses the latest build.",
            ThemeInput,
            _theme_map,
        ),
        ToolSpec(
            "get_monitoring_alerts",
            "monitoring",
            "The project's latest monitoring check: alerts ranked by severity (competitor pushes "
            "into client themes, new competitor themes, ownership shifts, price changes, removed "
            "pages, rewrites, pillar drift), what changed on each website, and theme drift. Runs "
            "a check when none exists.",
            MonitorInput,
            _monitoring,
        ),
        ToolSpec(
            "get_gap_findings",
            "gap",
            "The project's gap findings, highest priority first: themes the client misses or "
            "trails in, missing pillar pages and funnel stages, competitor keywords by the "
            "client's position (complete deficit, probable gap, far behind, page two), "
            "competitor momentum, offerings and competitors not in the project; each with "
            "evidence, "
            "a recommended action and its status (dismissed findings are left out).",
            GapInput,
            _gaps,
        ),
        ToolSpec(
            "get_keyword_rankings",
            "research",
            "Keywords a website ranks for in the latest research snapshot (Semrush pull or "
            "export): position, previous position, volume, CPC, ranking URL, theme and "
            "estimated visits. Filter by theme or position.",
            RankingsInput,
            _rankings,
        ),
        ToolSpec(
            "get_serp_overview",
            "research",
            "Who ranks for a keyword: from the snapshot's search results when the keyword was "
            "pulled, else every website of the project that ranks for it, with position and URL.",
            KeywordInput,
            _serp,
        ),
        ToolSpec(
            "get_keyword_metrics",
            "research",
            "Volume, CPC and competition of a keyword, and keywords of the snapshot that contain "
            "it (related demand), from the latest research snapshot.",
            KeywordInput,
            _keyword_metrics,
        ),
        ToolSpec(
            "get_search_demand",
            "research",
            "Search demand per theme of the project: keywords, monthly volume, and each "
            "website's keywords in the top 10 and estimated visits.",
            NoInput,
            _demand,
        ),
        ToolSpec(
            "get_competitor_suggestions",
            "research",
            "Competitors the search data reveals (Semrush organic competitors, competitors "
            "exports and the top 10 of pulled keywords): relevance, keywords in common, traffic, "
            "example keywords, and whether each is already a website of the project.",
            NoInput,
            _competitors,
        ),
    )
}


def run_tool(play: Playground, name: str, raw_input: Any) -> tuple[str, bool]:
    """Run a tool on raw input; returns (JSON text, is_error). Never raises for tool errors."""
    spec = TOOLS.get(name)
    if spec is None or spec.handler is None or not spec.built:
        return json.dumps({"error": f"tool {name!r} is not available"}), True
    try:
        args = spec.input_model.model_validate(raw_input if raw_input is not None else {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
        )
        return json.dumps({"error": f"invalid input: {problems}"}), True
    try:
        data = spec.handler(play, args)
    except ToolError as exc:
        return json.dumps({"error": str(exc)}), True
    notes = data.pop("notes", []) if isinstance(data.get("notes"), list) else []
    payload: dict[str, Any] = {
        "tool": name,
        "untrusted_website_content": True,
        "data": data,
    }
    if notes:
        payload["notes"] = notes
    return _fit(payload), False


def anthropic_tools(names: Sequence[str]) -> list[dict[str, Any]]:
    """Tool definitions for the Claude API, in a fixed order (keeps the prompt cache stable)."""
    return [
        {
            "name": TOOLS[n].name,
            "description": TOOLS[n].description,
            "input_schema": TOOLS[n].schema(),
            "eager_input_streaming": True,
        }
        for n in sorted(names)
        if n in TOOLS and TOOLS[n].built
    ]
