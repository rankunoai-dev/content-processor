"""Semantic similarity: same meaning in other words, with the template traps closed.

Reference case (the John Hancock hand analysis, 303 pages): two podcast pages on
ageing scored 0.82 by meaning with 14% of words in common, while 243 recurring fund
notices that score 0.9+ against one another were set aside as a series, not duplicates.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from src.modules.content_processor.analysis.semantic import (
    SemanticConfig,
    VectorCache,
    analyse_semantic,
    tier_of,
    title_skeleton,
)
from src.modules.content_processor.analysis.semantic_report import (
    read_tables,
    semantic_tables,
    write_tables,
)
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.knowledge.models import PageStamp
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.project_reports import (
    build_project_reports,
    project_report_files,
)
from src.modules.content_processor.projects.store import ProjectStore, SiteSpec
from src.modules.content_processor.webapi.projects_api import _report_out
from src.modules.reports.catalog import build_report
from tests.modules.content_processor.l5_factory import block, record

HOST = "https://www.acme-insure.com"
FOOTER = (
    "Acme Insure is a trading name of Acme Life Limited, registered in England, authorised "
    "and regulated by the Financial Conduct Authority."
)
NEWSLETTER = "Sign up for our monthly newsletter to receive tips on protecting your family."

# A tiny thesaurus: words in one row mean the same thing. The fake embedder maps every
# word to its concept, so a rewrite with synonyms keeps its meaning and loses its words.
CONCEPTS = [
    ("car", "automobile", "vehicle"),
    ("buy", "purchase", "acquire"),
    ("cheap", "inexpensive", "affordable"),
    ("insurance", "cover", "protection"),
    ("family", "household", "loved ones"),
    ("quickly", "fast", "promptly"),
    ("claim", "request", "application"),
    ("retire", "retirement", "pension"),
    ("save", "saving", "savings"),
    ("minutes", "moments"),
    ("price", "premium"),
    ("shown", "displayed"),
    ("personal", "private"),
    ("details", "information"),
    ("team", "staff"),
    ("outside", "external"),
    ("firm", "company"),
    ("follow", "track"),
    ("step", "stage"),
    ("phone", "mobile"),
    ("plans", "schemes"),
    ("grow", "expand"),
    ("job", "role"),
    ("work", "employment"),
    ("handled", "processed"),
    ("online", "digitally"),
]
_CONCEPT = {w: row[0] for row in CONCEPTS for w in row}


class FakeEmbedder:
    """Bag of concepts: deterministic, 64 numbers, unit length (stands in for the CPU model)."""

    model = "cpu:fake-concepts"

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        out = []
        for text in texts:
            vector = np.zeros(64, dtype=np.float32)
            for word in text.lower().replace(",", " ").replace(".", " ").split():
                concept = _CONCEPT.get(word, word)
                seed = int(hashlib.sha256(concept.encode()).hexdigest()[:8], 16)
                vector += np.random.default_rng(seed).standard_normal(64).astype(np.float32)
            norm = float(np.linalg.norm(vector))
            out.append((vector / norm if norm else vector).tolist())
        return out

    def close(self) -> None:
        """Nothing to release."""


def page(
    url: str, *paragraphs: str, badge=None, footer: bool = True, warnings: list[str] | None = None
) -> UrlRecord:
    rid = "res-" + url.rsplit("/", 1)[-1]
    blocks = [block(rid, i, text) for i, text in enumerate(paragraphs)]
    if footer:
        blocks.append(block(rid, len(blocks), FOOTER, role="main"))
        blocks.append(block(rid, len(blocks) + 1, NEWSLETTER, role="main"))
    kwargs = {"badge": badge} if badge is not None else {}
    return record(url, blocks=blocks, warnings=warnings, **kwargs)


def stamp(
    url: str,
    title: str,
    *,
    published: str | None = None,
    h1: str | None = None,
    robots: str | None = None,
    canonical: str | None = None,
) -> PageStamp:
    path = url.removeprefix(HOST).strip("/").split("/")
    return PageStamp(
        run_id="run-l5",
        url=url,
        page_key=hashlib.sha1(url.encode()).hexdigest()[:12],  # noqa: S324 - a test key
        site="acme",
        host="www.acme-insure.com",
        stamped_at=datetime(2026, 11, 1, tzinfo=UTC),
        knowledge_eligible=True,
        path_l1=path[0] if path and path[0] else None,
        title=title,
        h1=h1 or title,
        robots_meta=robots,
        canonical=canonical,
        published_at=datetime.fromisoformat(published).replace(tzinfo=UTC) if published else None,
        badge="VERIFIED_WITHIN_SCOPE",
    )


ORIGINAL = [
    "You can buy cheap car insurance for your family online in minutes, with the price "
    "shown before you give us any personal details at all.",
    "Every claim is handled quickly by our own team, never an outside firm, and you can "
    "follow each step of the claim from your phone.",
    "We help you save for retirement with plans that grow with your household, from the "
    "first job to the last day at work.",
]
REWRITE = [
    "You can purchase inexpensive automobile cover for your household digitally in moments, "
    "with the premium displayed before you give us any private information at all.",
    "Every request is processed promptly by our own staff, never an external company, and "
    "you can track each stage of the request from your mobile.",
    "We help you saving for your pension with schemes that expand with your loved ones, from "
    "the first role to the last day at employment.",
]
GUIDE = [
    "A guide to protection for your loved ones: what cover costs, how the price is set and "
    "how to choose between the policies on offer this year.",
    "Why a family needs protection, the questions to ask before you buy, and the mistakes "
    "most households make the first time.",
]
BLOG_BLOCK = (
    "Dr. Ada Lovelace presents an exciting new frontier of science, how we can slow the "
    "biological processes of ageing and add healthy years to every life, in this episode."
)


def notice(month: str, day: str, amount: str) -> list[str]:
    return [
        f"Acme Growth Fund declared its monthly distribution of ${amount} per share on {month} "
        f"{day}, 2025, payable to shareholders of record at the close of business that day.",
        "The distribution is paid under the fund's managed distribution plan and may include a "
        "return of capital; the amounts are estimates and must not be used for tax returns.",
        f"The ex-dividend date is {month} {day}, 2025 and the record date is the same day; the "
        f"payment date follows at the end of {month} and the next declaration is due in a month.",
    ]


def world() -> tuple[list[UrlRecord], dict[str, str], dict[str, PageStamp]]:
    """Pages that exercise every rule once."""
    records = [
        page(f"{HOST}/insurance/car", *ORIGINAL),
        page(f"{HOST}/insurance/car-rewrite", *REWRITE),
        page(f"{HOST}/insurance/car-copy", *ORIGINAL),  # the same text at another address
        page(f"{HOST}/guides/protection", *GUIDE),
        page(
            f"{HOST}/guides/protection-2",
            GUIDE[0],
            "Why a household needs protection, and how much cover to keep once the children "
            "have left home, the mortgage is paid and the savings have grown.",
        ),
        page(f"{HOST}/blog/ep-1", BLOG_BLOCK, "Ada talks about sleep and long life with Bob."),
        page(f"{HOST}/blog/ep-2", BLOG_BLOCK, "Carol explains why walking every day matters."),
        page(f"{HOST}/blog/ep-3", BLOG_BLOCK, "Dan on diet, fasting and the heart."),
        page(f"{HOST}/forms/a", "Download the form and sign it."),
        page(f"{HOST}/forms/b", "Download the form and sign it."),
        page(f"{HOST}/forms/c", "Lorem ipsum dolor sit amet, placeholder text to be replaced."),
        page(f"{HOST}/news/blocked", "Access denied.", badge="BLOCKED"),
    ]
    months = [("January", "15", "0.0810"), ("February", "14", "0.0812"), ("March", "17", "0.0815")]
    months += [("April", "15", "0.0818"), ("May", "15", "0.0820")]
    for month, day, amount in months:
        records.append(
            page(f"{HOST}/news/{month.lower()}-distribution", *notice(month, day, amount))
        )
    websites = {r.requested_url: "Acme" for r in records}
    stamps = {
        f"{HOST}/insurance/car": stamp(f"{HOST}/insurance/car", "Cheap car insurance | Acme"),
        f"{HOST}/insurance/car-rewrite": stamp(
            f"{HOST}/insurance/car-rewrite", "Affordable automobile cover | Acme"
        ),
        f"{HOST}/insurance/car-copy": stamp(
            f"{HOST}/insurance/car-copy", "Cheap car insurance | Acme"
        ),
        f"{HOST}/guides/protection": stamp(
            f"{HOST}/guides/protection", "Protection guide | Acme", robots="noindex, follow"
        ),
        f"{HOST}/guides/protection-2": stamp(
            f"{HOST}/guides/protection-2",
            "Protection guide for households and what it",
            h1="Protection guide for households and what it costs at every age",
        ),
        f"{HOST}/forms/c": stamp(f"{HOST}/forms/c", "Form C | Acme"),
    }
    for month, day, _amount in months:
        url = f"{HOST}/news/{month.lower()}-distribution"
        stamps[url] = stamp(
            url,
            f"Acme Growth Fund declares monthly distribution for {month} {day}, 2025 | Acme",
            published=f"2025-{months.index((month, day, _amount)) + 1:02d}-{day}",
        )
    return records, websites, stamps


@pytest.fixture
def result(tmp_path: Path):
    records, websites, stamps = world()
    cache = VectorCache(tmp_path / "vectors.sqlite")
    try:
        return analyse_semantic(records, websites, stamps, FakeEmbedder(), cache, SemanticConfig())
    finally:
        cache.close()


def pair(result, frag_a: str, frag_b: str):
    for p in result.duplicates:
        if {frag_a, frag_b} <= {p.a.rsplit("/", 1)[-1], p.b.rsplit("/", 1)[-1]}:
            return p
    return None


def test_title_skeleton_replaces_dates_numbers_and_amounts():
    a = title_skeleton("Acme Growth Fund declares monthly distribution for January 15, 2025")
    b = title_skeleton("Acme Growth Fund declares monthly distribution for March 17, 2025")
    assert a == b
    assert "<month>" in a and "<n>" in a and "<year>" in a
    assert title_skeleton("Q3 2024 results: $1.2 billion, up 4%") == (
        "<quarter> <year> results <money> billion up <percent>"
    )
    assert title_skeleton("Podcast: Ada on ageing") != title_skeleton("Podcast: Bob on sleep")


def test_tiers():
    config = SemanticConfig()
    assert tier_of(0.99, 0.98, config) == "Identical"
    assert tier_of(0.96, 0.5, config) == "Copy"
    assert tier_of(0.9, None, config) == "High"
    assert tier_of(0.75, None, config) == "Moderate"
    assert tier_of(0.69, None, config) is None


def test_furniture_is_removed_and_blocked_pages_are_skipped(result):
    texts = [text for text, _count in result.furniture]
    assert FOOTER in texts and NEWSLETTER in texts, "text on every page is furniture"
    urls = {p.url for p in result.pages}
    assert f"{HOST}/news/blocked" not in urls
    car = next(p for p in result.pages if p.url.endswith("/insurance/car"))
    assert car.furniture_words > 0 and FOOTER not in car.own
    assert not car.thin


def test_a_rewrite_with_synonyms_is_found_by_meaning_not_words(result):
    found = pair(result, "car", "car-rewrite")
    assert found is not None, [(p.a, p.b, p.tier) for p in result.duplicates]
    assert found.tier in ("Copy", "High")
    assert found.meaning >= 0.85
    assert found.overlap < 0.6, "most words differ"
    assert found.coverage_a is not None and found.coverage_a >= 0.5, "paragraph by paragraph"
    assert "reworded" in found.why or "other words" in found.why


def test_the_same_text_at_another_address_is_identical(result):
    found = pair(result, "car", "car-copy")
    assert found is not None and found.tier == "Identical"
    assert found.same_text == 1.0
    assert (f"{HOST}/insurance/car", f"{HOST}/insurance/car-copy", 1.0) in [
        tuple(x) for x in result.flags.duplicate_addresses
    ]
    forms = pair(result, "a", "b")
    assert forms is not None and forms.tier == "Identical", "thin pages compare on whole text"


def test_recurring_notices_are_a_series_not_duplicates(result):
    assert len(result.series) == 1
    series = result.series[0]
    assert len(series.members) == 5
    assert series.first_published == "2025-01-15" and series.last_published == "2025-05-15"
    assert series.median_meaning is not None and series.median_meaning > 0.85
    assert result.series_pairs == 10, "every pair of the five notices was set aside"
    assert all("distribution" not in p.a and "distribution" not in p.b for p in result.duplicates)
    assert not any(h for h in result.hubs if any("distribution" in m for m in h.members))


def test_same_topic_different_content_is_moderate_and_forms_a_hub(result):
    found = pair(result, "protection", "protection-2")
    assert found is not None and found.tier == "Moderate", found
    assert any(
        {f"{HOST}/guides/protection", f"{HOST}/guides/protection-2"} <= set(h.members)
        for h in result.hubs
    )
    hub = next(h for h in result.hubs if f"{HOST}/guides/protection" in h.members)
    assert hub.label


def test_a_long_paragraph_pasted_on_one_sections_pages_is_a_repeated_block(result):
    blocks = [b for b in result.repeated_blocks if b.text == BLOG_BLOCK]
    assert len(blocks) == 1
    assert sorted(blocks[0].pages) == [
        f"{HOST}/blog/ep-1",
        f"{HOST}/blog/ep-2",
        f"{HOST}/blog/ep-3",
    ]
    assert blocks[0].confined_to == "section /blog"
    assert blocks[0].removed_from_scoring is False, "three pages is below the furniture line"


def test_flags(result):
    assert result.flags.noindex == [f"{HOST}/guides/protection"]
    assert result.flags.placeholders == [f"{HOST}/forms/c"]
    cut = dict(result.flags.cut_off_titles)
    assert f"{HOST}/guides/protection-2" in cut
    assert "stops before the heading" in cut[f"{HOST}/guides/protection-2"]


def test_summary_and_tables(result, tmp_path: Path):
    summary = result.summary()
    assert summary["pages"] == 16 and summary["series"] == 1
    assert summary["pairs"]["Identical"] >= 2
    tables = semantic_tables(result)
    assert [t.name for t in tables] == [
        "Summary",
        "All pages",
        "Duplicates",
        "Topic hubs",
        "Recurring series",
        "Repeated blocks",
        "Flags",
        "Furniture removed",
        "Method",
    ]
    pages = next(t for t in tables if t.name == "All pages")
    january = next(r for r in pages.rows if str(r["URL"]).endswith("january-distribution"))
    assert january["Recurring series"] and january["Published"] == "2025-01-15"
    write_tables(tmp_path / "t.json", tables)
    back = read_tables(tmp_path / "t.json")
    assert [t.name for t in back] == [t.name for t in tables]
    assert back[2].rows == tables[2].rows


def test_vector_cache_roundtrip_and_reuse(tmp_path: Path):
    cache = VectorCache(tmp_path / "v.sqlite")
    cache.put("m", {"h1": [0.6, 0.8, 0.0, 0.0], "h2": [1.0, 0.0, 0.0, 0.0]})
    found = cache.get("m", ["h1", "h2", "h3"])
    assert set(found) == {"h1", "h2"}
    assert found["h1"].tolist() == pytest.approx([0.6, 0.8]), "trailing zeros are trimmed"
    assert cache.count("m") == 2 and cache.count("other") == 0
    cache.close()

    records, websites, stamps = world()
    cache = VectorCache(tmp_path / "w.sqlite")
    first = analyse_semantic(records, websites, stamps, FakeEmbedder(), cache)
    second = analyse_semantic(records, websites, stamps, FakeEmbedder(), cache)
    cache.close()
    assert first.embedded > 0 and first.cached == 0
    assert second.embedded == 0 and second.cached == first.embedded
    assert [(p.a, p.b, p.tier) for p in second.duplicates] == [
        (p.a, p.b, p.tier) for p in first.duplicates
    ]


def test_embedding_failure_is_an_error(tmp_path: Path):
    class Broken:
        model = "cpu:broken"

        def embed(self, texts):
            return None

        def close(self):
            pass

    records, websites, stamps = world()
    cache = VectorCache(tmp_path / "b.sqlite")
    with pytest.raises(RuntimeError, match="failed"):
        analyse_semantic(records, websites, stamps, Broken(), cache)
    cache.close()


def _project(store: ProjectStore) -> str:
    return store.create(
        name="Acme audit",
        owner_name="G",
        client_name="Acme",
        password="correct horse battery",
        client_site=SiteSpec("Acme", HOST),
    ).project_id


def _finish(store: ProjectStore, project_id: str, records, stamps) -> None:
    moment = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    site_id = store.get(project_id).sites[0].site_id
    store.save_run(
        RunState(
            run_id="run-1",
            project_id=project_id,
            site_id=site_id,
            status="finished",
            created_at=moment,
            started_at=moment,
            finished_at=moment,
            started_by="G",
            url_count=len(records),
            batch_size=25,
            batches_total=1,
            batches_done=1,
            pages_done=len(records),
            reports_ready=True,
        )
    )
    folder = store.run_dir(project_id, site_id, "run-1")
    (folder / "run-1.records.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8"
    )
    (folder / "run-1.stamps.jsonl").write_text(
        "".join(s.model_dump_json() + "\n" for s in stamps.values()), encoding="utf-8"
    )


def _sheets(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as book:
        workbook = book.read("xl/workbook.xml").decode()
    return [part.split('"')[0] for part in workbook.split('<sheet name="')[1:]]


def test_the_project_report_includes_the_semantic_workbook(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CP_KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    store = ProjectStore(tmp_path / "app_data")
    project_id = _project(store)
    records, _websites, stamps = world()
    _finish(store, project_id, records, stamps)

    report = build_project_reports(store, project_id, embedder=FakeEmbedder())
    files = project_report_files(store, project_id, report.stamp)
    assert "Acme_audit_semantic_similarity.xlsx" in files
    assert "Acme_audit_semantic_similarity.json" in files
    assert _sheets(files["Acme_audit_semantic_similarity.xlsx"])[:3] == [
        "Summary",
        "All pages",
        "Duplicates",
    ]
    semantic = report.manifest["semantic"]
    assert semantic["model"] == "cpu:fake-concepts"
    assert semantic["pages"] == 16 and semantic["series"] == 1
    assert semantic["duplicate_pairs"] >= 3
    assert (tmp_path / "knowledge" / "semantic" / f"{project_id}.sqlite").is_file(), (
        "paragraph vectors are cached under the knowledge root"
    )
    out = _report_out(report, files)
    assert out.semantic_duplicates == semantic["duplicate_pairs"]
    assert out.semantic_topic_hubs == semantic["topic_hubs"] and out.semantic_note == ""

    download = build_report("semantic_similarity", store, project_id)
    assert download is not None
    assert download.key == "semantic_similarity"
    assert [t.name for t in download.tables][:2] == ["Summary", "All pages"]
    assert "recurring series" in download.summary[0]

    switched_off = build_project_reports(store, project_id, semantic=False)
    assert switched_off.manifest["semantic"] == {
        "skipped": "switched off (CP_SEMANTIC_ENABLED=false)"
    }
    assert not any(
        n.endswith("semantic_similarity.xlsx")
        for n in project_report_files(store, project_id, switched_off.stamp)
    )


def test_without_a_meaning_model_the_bundle_still_builds(tmp_path: Path, monkeypatch):
    """Tests run with the CPU model off and no model server: the words-only stand-in is refused."""
    monkeypatch.setenv("CP_KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    monkeypatch.setenv("CP_MODEL_ENDPOINT", "http://127.0.0.1:9")
    store = ProjectStore(tmp_path / "app_data")
    project_id = _project(store)
    records, _websites, stamps = world()
    _finish(store, project_id, records, stamps)
    report = build_project_reports(store, project_id)
    note = report.manifest["semantic"]["skipped"]
    assert "words-only" in note or "no embedding model" in note
    assert build_report("semantic_similarity", store, project_id) is None
    manifest = json.loads((report.folder / "manifest.json").read_text("utf-8"))
    assert manifest["semantic"]["skipped"] == note
