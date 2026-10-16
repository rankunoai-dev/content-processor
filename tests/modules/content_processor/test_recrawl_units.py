"""Page dates, the change decision table, and baseline lookup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.modules.content_processor.contracts import (
    DiscoveryState,
    InventoryCounts,
    StatusVector,
    UrlRecord,
)
from src.modules.content_processor.enums import ChangeStatus, EntityStatus, UrlBadge
from src.modules.content_processor.recrawl import (
    Baseline,
    conditional_headers,
    decide_change,
    extract_page_dates,
)
from src.modules.content_processor.recrawl.dates import parse_date

T0 = datetime(2026, 9, 1, 10, tzinfo=UTC)


def _html(head: str) -> bytes:
    return f"<html><head>{head}</head><body><p>x</p></body></html>".encode()


def test_jsonld_beats_meta_and_http():
    body = _html(
        '<script type="application/ld+json">{"@graph":[{"@type":"WebPage",'
        '"dateModified":"2026-09-01T10:00:00Z","datePublished":"2025-01-15"}]}</script>'
        '<meta property="article:modified_time" content="2026-08-01T00:00:00Z">'
    )
    d = extract_page_dates(
        body=body,
        headers={"last-modified": "Tue, 01 Jul 2025 00:00:00 GMT", "etag": '"a"'},
        sniffed_mime="text/html",
    )
    assert d.modified_at == T0 and d.modified_source == "jsonld:dateModified"
    assert d.published_at == datetime(2025, 1, 15, tzinfo=UTC)
    assert d.http_etag == '"a"'


@pytest.mark.parametrize(
    ("head", "source"),
    [
        ('<meta property="article:modified_time" content="2026-09-01T10:00:00Z">',
         "meta:article_modified_time"),
        ('<meta property="og:updated_time" content="2026-09-01T10:00:00+00:00">',
         "meta:og_updated_time"),
        ('<meta name="dcterms.modified" content="2026-09-01T10:00:00Z">',
         "meta:dcterms_modified"),
        ('<meta itemprop="dateModified" content="2026-09-01T10:00:00Z">',
         "microdata:dateModified"),
    ],
)  # fmt: skip
def test_meta_and_microdata_dates(head: str, source: str):
    d = extract_page_dates(body=_html(head), headers={}, sniffed_mime="text/html")
    assert d.modified_at == T0 and d.modified_source == source


def test_http_last_modified_used_unless_it_is_just_now():
    old = extract_page_dates(
        body=_html(""),
        headers={"last-modified": "Mon, 01 Sep 2026 10:00:00 GMT"},
        sniffed_mime="text/html",
        fetched_at=T0 + timedelta(days=30),
    )
    assert old.modified_at == T0 and old.modified_source == "http:last-modified"
    dynamic = extract_page_dates(
        body=_html(""),
        headers={"last-modified": "Mon, 01 Sep 2026 10:00:00 GMT"},
        sniffed_mime="text/html",
        fetched_at=T0 + timedelta(seconds=5),
    )
    assert dynamic.modified_at is None and dynamic.http_last_modified_is_dynamic


def test_pdf_moddate():
    body = b"%PDF-1.7\n1 0 obj << /ModDate (D:20260901100000+00'00') >> endobj"
    d = extract_page_dates(body=body, headers={}, sniffed_mime="application/pdf")
    assert d.modified_at == T0 and d.modified_source == "pdf:ModDate"


def test_no_signal_means_no_date():
    d = extract_page_dates(body=_html(""), headers={}, sniffed_mime="text/html")
    assert d.modified_at is None and d.published_at is None
    assert parse_date("not a date") is None and parse_date(None) is None


def _record(**kw: object) -> UrlRecord:
    base: dict[str, object] = {
        "run_id": "run-1",
        "requested_url": "https://x.example/fees?utm_source=mail",
        "final_url": "https://x.example/fees",
        "captured_at": T0,
        "tool_versions": {},
        "badge": UrlBadge.PARTIAL,
        "status_vector": StatusVector(
            discovery_closed=False,
            acquisition=EntityStatus.VERIFIED_EXTRACTED,
            fidelity=EntityStatus.VERIFIED_EXTRACTED,
            state_exploration=EntityStatus.VERIFIED_EXTRACTED,
        ),
        "inventory": InventoryCounts(
            inventory_revision=0,
            resources_discovered=1,
            resources_acquired=1,
            resources_blocked=0,
            resources_failed=0,
            regions_discovered=1,
            regions_verified=1,
            regions_needs_review=0,
            regions_blocked=0,
            regions_unvisited=0,
        ),
        "discovery": DiscoveryState(
            closed_within_scope=False, remaining_controls=0, unknown_state_space=True
        ),
        "schedule_key": "https://x.example/fees",
        "resource_status": EntityStatus.VERIFIED_EXTRACTED,
        "payload_sha256": "a" * 64,
        "content_fingerprint": "f" * 64,
        "page_modified_at": T0,
        "http_etag": '"v1"',
        "http_last_modified": "Mon, 01 Sep 2026 10:00:00 GMT",
    }
    base.update(kw)
    return UrlRecord.model_validate(base)


PRIOR = _record()
LATER = T0 + timedelta(days=10)


@pytest.mark.parametrize(
    ("kwargs", "status", "basis", "notes"),
    [
        ({"http_status": 304}, ChangeStatus.UNCHANGED, "http_304_not_modified", []),
        ({"payload_sha256": "a" * 64}, ChangeStatus.UNCHANGED, "payload_identical", []),
        ({"payload_sha256": "b" * 64, "fingerprint": "f" * 64}, ChangeStatus.UNCHANGED,
         "visible_text_identical", ["bytes_differ"]),
        ({"payload_sha256": "b" * 64, "fingerprint": "f" * 64, "modified_at": LATER},
         ChangeStatus.UNCHANGED, "visible_text_identical",
         ["site_modified_date_moved", "bytes_differ"]),
        ({"payload_sha256": "b" * 64, "fingerprint": "g" * 64, "modified_at": LATER},
         ChangeStatus.CHANGED, "site_modified_date_newer", []),
        ({"payload_sha256": "b" * 64, "fingerprint": "g" * 64, "modified_at": T0},
         ChangeStatus.CHANGED, "visible_text_changed", ["site_modified_date_not_updated"]),
        ({"payload_sha256": "b" * 64, "fingerprint": "g" * 64}, ChangeStatus.CHANGED,
         "visible_text_changed", []),
        ({"payload_sha256": "b" * 64}, ChangeStatus.CHANGED, "payload_changed", []),
    ],
)  # fmt: skip
def test_decision_table(
    kwargs: dict[str, object], status: ChangeStatus, basis: str, notes: list[str]
):
    args: dict[str, object] = {
        "http_status": 200,
        "payload_sha256": None,
        "fingerprint": None,
        "modified_at": None,
    }
    args.update(kwargs)
    decision = decide_change(PRIOR, **args)  # type: ignore[arg-type]
    assert (decision.status, decision.basis, decision.notes) == (status, basis, notes)


def test_no_prior_is_new():
    d = decide_change(None, http_status=200, payload_sha256="x", fingerprint=None, modified_at=None)
    assert d.status is ChangeStatus.NEW and not d.reuse


def test_baseline_lookup_and_reusability():
    blocked = _record(
        requested_url="https://x.example/blocked",
        final_url="https://x.example/blocked",
        schedule_key="https://x.example/blocked",
        badge=UrlBadge.BLOCKED,
    )
    baseline = Baseline([PRIOR, blocked])
    assert len(baseline) == 1 and baseline.skipped == 1
    assert baseline.lookup("https://x.example/fees") is PRIOR
    assert baseline.lookup("https://x.example/fees?utm_source=other") is PRIOR
    assert baseline.lookup("https://x.example/blocked") is None
    assert conditional_headers(PRIOR) == {
        "If-None-Match": '"v1"',
        "If-Modified-Since": "Mon, 01 Sep 2026 10:00:00 GMT",
    }
    assert conditional_headers(None) == {}


def test_baseline_from_jsonl(tmp_path):
    path = tmp_path / "prev.records.jsonl"
    path.write_text(PRIOR.model_dump_json() + "\n\n", encoding="utf-8")
    assert Baseline.from_jsonl(path).lookup("https://x.example/fees") is not None
