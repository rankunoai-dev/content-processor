"""Write a run's SEO and GEO inventory as JSON lines and as an Excel workbook."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.content_processor.knowledge.inventory_models import PageInventory, SeenIn

__all__ = ["inventory_tables", "seen_label", "write_inventory"]

_SEEN_LABELS: dict[tuple[SeenIn, ...], str] = {
    ("source",): "HTML only (not shown by the browser)",
    ("source", "rendered"): "HTML and browser",
    ("source", "interaction"): "HTML; shown after a click",
    ("source", "rendered", "interaction"): "HTML and browser",
    ("rendered",): "Browser only (needs scripts)",
    ("rendered", "interaction"): "Browser only (needs scripts)",
    ("interaction",): "After a click only",
}
_SEVERITY_ORDER = {"error": 0, "warning": 1, "notice": 2}
_HOW_TO_READ = [
    (
        "What this is",
        "Everything on each page that matters for search engines and AI answer engines: "
        "links, images, videos and embeds, the page's title and tags, its structured data, "
        "and the problems found. Read by code from the pages this run captured; nothing "
        "was fetched again.",
    ),
    (
        "Found in",
        "Where an item exists. 'HTML and browser': in the page the server sends, readable "
        "by every crawler. 'Browser only (needs scripts)': added by scripts, so crawlers "
        "that do not run scripts (many AI crawlers) never see it. 'After a click only': "
        "appears only when a tab, accordion, menu or 'show more' is opened.",
    ),
    (
        "Region and Heading",
        "The part of the page an item sits in (header, main content, card, footer...) and "
        "the nearest heading above it.",
    ),
    (
        "Alt status",
        'present: the image is described; empty: alt="" (fine for decoration only); '
        "missing: no alt attribute; file_name: the alt text is a file name or code.",
    ),
    (
        "Structured data",
        "Each schema.org item on the page. 'Missing required' lists fields Google needs "
        "before it can show a rich result; 'Missing recommended' lists fields that help.",
    ),
    (
        "Issues",
        "error: likely to hurt how the page is indexed or shown; warning: worth fixing; "
        "notice: worth knowing. 'Issue summary' counts the pages affected by each.",
    ),
]


def seen_label(seen: Sequence[SeenIn]) -> str:
    """Plain wording for the views an item was found in."""
    return _SEEN_LABELS.get(tuple(seen), ", ".join(seen))


def _join(values: Sequence[str]) -> str:
    return "\n".join(values)


def _page_row(p: PageInventory) -> dict[str, Cell]:
    head, c = p.head, p.counts
    types = Counter(t for s in p.structured_data for t in s.types)
    return {
        "Page": p.url,
        "Status": p.status,
        "Title": head.title,
        "Title length": head.title_length or None,
        "Description length": head.description_length or None,
        "H1": _join(head.h1),
        "Canonical": head.canonical_status if p.status == "inventoried" else None,
        "Indexable": head.indexable if p.status == "inventoried" else None,
        "Language": head.lang,
        "Links": c.get("links"),
        "Internal links": c.get("links_internal"),
        "External links": c.get("links_external"),
        "Links needing scripts": c.get("links_need_scripts"),
        "Links behind clicks": c.get("links_behind_clicks"),
        "Images": c.get("images"),
        "Images without alt": c.get("images_alt_missing"),
        "Videos and embeds": c.get("media"),
        "Structured data": ", ".join(f"{t} ({n})" if n > 1 else t for t, n in types.items()),
        "Errors": c.get("issues_error"),
        "Warnings": c.get("issues_warning"),
        "Notices": c.get("issues_notice"),
        "Views read": ", ".join(p.views),
    }


def inventory_tables(pages: Sequence[PageInventory]) -> list[Table]:
    """The workbook's sheets."""
    done = [p for p in pages if p.status == "inventoried"]
    page_rows = [_page_row(p) for p in pages]
    links: list[dict[str, Cell]] = [
        {
            "Page": p.url,
            "Link": link.url,
            "Anchor text": link.anchor_text or link.image_alt or link.aria_label or link.title,
            "Kind": link.kind,
            "Rel": " ".join(link.rel),
            "Nofollow": link.nofollow,
            "New tab": link.new_tab,
            "Region": link.ui_region,
            "Heading": link.heading,
            "Found in": seen_label(link.seen_in),
            "Hidden": link.hidden,
            "Times on page": link.occurrences,
        }
        for p in done
        for link in p.links
    ]
    images: list[dict[str, Cell]] = [
        {
            "Page": p.url,
            "Image": i.url,
            "Alt text": i.alt,
            "Alt status": i.alt_status,
            "Caption": i.caption,
            "Width": i.width,
            "Height": i.height,
            "Loading": i.loading,
            "Links to": i.link_url,
            "Region": i.ui_region,
            "Heading": i.heading,
            "Found in": seen_label(i.seen_in),
            "Hidden": i.hidden,
        }
        for p in done
        for i in p.images
    ]
    media: list[dict[str, Cell]] = [
        {
            "Page": p.url,
            "Kind": m.kind,
            "Provider": m.provider,
            "Address": m.url,
            "Title": m.title,
            "Captions": m.captions if m.kind in ("video", "audio") else None,
            "Region": m.ui_region,
            "Heading": m.heading,
            "Found in": seen_label(m.seen_in),
        }
        for p in done
        for m in p.media
    ]
    structured: list[dict[str, Cell]] = [
        {
            "Page": p.url,
            "Syntax": s.syntax,
            "Types": ", ".join(s.types),
            "Name": s.name,
            "Properties": ", ".join(s.properties),
            "Missing required": _join(s.missing_required),
            "Missing recommended": _join(s.missing_recommended),
        }
        for p in done
        for s in p.structured_data
    ]
    issues: list[dict[str, Cell]] = sorted(
        (
            {
                "Page": p.url,
                "Severity": i.severity,
                "Issue": i.code,
                "What it means": i.message,
                "Items": i.count,
                "Examples": _join(i.examples),
            }
            for p in done
            for i in p.issues
        ),
        key=lambda r: (_SEVERITY_ORDER[str(r["Severity"])], str(r["Issue"]), str(r["Page"])),
    )
    affected: Counter[str] = Counter()
    items: Counter[str] = Counter()
    meaning: dict[str, tuple[str, str]] = {}
    for p in done:
        for i in p.issues:
            affected[i.code] += 1
            items[i.code] += i.count
            meaning[i.code] = (i.severity, i.message)
    summary: list[dict[str, Cell]] = sorted(
        (
            {
                "Issue": code,
                "Severity": meaning[code][0],
                "What it means": meaning[code][1],
                "Pages affected": n,
                "Share of pages": round(n / len(done), 3) if done else None,
                "Items": items[code],
            }
            for code, n in affected.items()
        ),
        key=lambda r: (_SEVERITY_ORDER[str(r["Severity"])], -int(r["Pages affected"] or 0)),
    )
    wide = {"Page": 60.0, "Link": 60.0, "Image": 60.0, "Address": 60.0, "What it means": 70.0}
    return [
        Table("Issue summary", list(summary[0]) if summary else ["Issue"], summary, widths=wide),
        Table("Pages", list(page_rows[0]) if page_rows else ["Page"], page_rows, widths=wide),
        Table(
            "Issues",
            list(issues[0]) if issues else ["Page"],
            issues,
            widths=wide,
            wrap_columns=frozenset({"Examples"}),
        ),
        Table("Links", list(links[0]) if links else ["Page"], links, widths=wide),
        Table("Images", list(images[0]) if images else ["Page"], images, widths=wide),
        Table("Videos and embeds", list(media[0]) if media else ["Page"], media, widths=wide),
        Table(
            "Structured data",
            list(structured[0]) if structured else ["Page"],
            structured,
            widths=wide,
            wrap_columns=frozenset({"Missing required", "Missing recommended"}),
        ),
        Table(
            "How to read",
            ["Topic", "Meaning"],
            [{"Topic": t, "Meaning": m} for t, m in _HOW_TO_READ],
            widths={"Topic": 24.0, "Meaning": 110.0},
            wrap_columns=frozenset({"Meaning"}),
        ),
    ]


def write_inventory(pages: Sequence[PageInventory], out_dir: Path, run_name: str) -> dict[str, str]:
    """Write `<run>.inventory.jsonl` and `<run>_seo_inventory.xlsx`; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / f"{run_name}.inventory.jsonl"
    xlsx = out_dir / f"{run_name}_seo_inventory.xlsx"
    with jsonl.open("w", encoding="utf-8") as handle:
        for page in pages:
            handle.write(page.model_dump_json() + "\n")
    write_xlsx(xlsx, inventory_tables(pages))
    return {"inventory": str(jsonl), "workbook": str(xlsx)}
