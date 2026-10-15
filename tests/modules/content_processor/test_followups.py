"""Depth-1 follow-up planning: assets, linked downloads, expected types (A-1, K15)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.modules.content_processor.adapters.html_source import (
    DiscoveredAsset,
    DiscoveredLink,
    extract_html_source,
)
from src.modules.content_processor.contracts import Authorization, RunScope
from src.modules.content_processor.enums import AuthorizationKind, PriorityClass, ScopeMode
from src.modules.content_processor.scope import followups as fu
from src.modules.content_processor.scope.followups import (
    AnchorHints,
    anchor_hints,
    expected_mime_for_name,
    mime_allowed,
    plan_followups,
)

BASE = "https://example.com/docs/"


def _scope(**overrides) -> RunScope:
    data = {
        "tenant_id": "t1",
        "input_urls": [BASE],
        "hosts_in_scope": ["example.com"],
        "authorization": Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 15, tzinfo=UTC),
            hosts=["example.com"],
        ),
    }
    data.update(overrides)
    return RunScope(**data)


def _plan(scope: RunScope | None = None, *, links=(), assets=(), hints=None, depth: int = 0):
    return plan_followups(
        scope=scope or _scope(),
        parent_url=BASE,
        parent_depth=depth,
        links=list(links),
        assets=list(assets),
        hints=hints or {},
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("https://x.test/a/report.PDF", "application/pdf"),
        ("https://x.test/a/logo.png?v=2", "image/png"),
        (
            "policy-wording.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("https://x.test/a/", None),
        ("https://x.test/a/download", None),
        ("archive.unknownext", None),
    ],
)
def test_expected_mime_for_name(name, expected):
    assert expected_mime_for_name(name) == expected


def test_mime_allowed_matches_families_and_wildcards():
    families = ["text/html", "application/pdf", "image/*"]
    assert mime_allowed("image/png", families)
    assert mime_allowed("application/pdf; charset=binary", families)
    assert mime_allowed("image/*", families)
    assert not mime_allowed("video/*", families)
    assert not mime_allowed("application/zip", families)


def test_anchor_hints_keep_type_and_download_only():
    body = (
        b'<a href="a" type="Application/PDF; x=1">A</a><a href="/b" download>B</a><a href="c">C</a>'
    )
    hints = anchor_hints(body, BASE)
    assert hints == {
        "https://example.com/docs/a": AnchorHints(type_attr="application/pdf"),
        "https://example.com/b": AnchorHints(download=True),
    }


def test_links_need_a_download_signal_and_carry_the_promised_type():
    links = [
        DiscoveredLink(url="https://example.com/annual", text="Annual report"),
        DiscoveredLink(url="https://example.com/rates", text="Rate sheet (PDF, 2 MB)"),
        DiscoveredLink(url="https://example.com/terms.pdf", text="Terms"),
        DiscoveredLink(url="https://example.com/wording", text="Wording"),
        DiscoveredLink(url="https://example.com/about", text="About us"),
        DiscoveredLink(url="https://example.com/page.html", text="Another page"),
    ]
    hints = {
        "https://example.com/annual": AnchorHints(type_attr="application/pdf"),
        "https://example.com/wording": AnchorHints(download=True),
    }
    plan = _plan(links=links, hints=hints)
    got = {f.url: (f.expected_mime, f.expected_mime_source) for f in plan.followups}
    assert got == {
        "https://example.com/annual": ("application/pdf", "link_type"),
        "https://example.com/rates": ("application/pdf", "link_text"),
        "https://example.com/terms.pdf": ("application/pdf", "url_extension"),
        "https://example.com/wording": (None, None),
    }
    assert plan.links_not_downloads == 2
    assert all(f.edge_kind == "links_to" for f in plan.followups)
    assert all(f.priority is PriorityClass.DOCUMENT for f in plan.followups)


def test_assets_are_typed_by_tag_and_filtered_by_policy():
    assets = [
        DiscoveredAsset(url="https://example.com/i/logo.png", kind="image"),
        DiscoveredAsset(url="https://example.com/i/chart", kind="image"),
        DiscoveredAsset(url="https://example.com/i/sprite.php", kind="image"),
        DiscoveredAsset(url="https://example.com/frame", kind="frame"),
        DiscoveredAsset(url="https://example.com/embed.pdf", kind="object"),
        DiscoveredAsset(url="https://example.com/x/unknown", kind="object"),
        DiscoveredAsset(url="https://example.com/v/intro", kind="media"),
        DiscoveredAsset(url="https://example.com/s.css", kind="stylesheet"),
        DiscoveredAsset(url="https://example.com/a.js", kind="script"),
        DiscoveredAsset(url="https://cdn.other.net/b.png", kind="image"),
        DiscoveredAsset(url="https://example.com/i/hidden.png", kind="image", hidden=True),
    ]
    plan = _plan(assets=assets)
    got = {f.url: (f.edge_kind, f.expected_mime, f.priority) for f in plan.followups}
    assert got == {
        "https://example.com/i/logo.png": ("embeds", "image/png", PriorityClass.EMBEDDED_ASSET),
        "https://example.com/i/chart": ("embeds", "image/*", PriorityClass.EMBEDDED_ASSET),
        "https://example.com/i/sprite.php": ("embeds", "image/*", PriorityClass.EMBEDDED_ASSET),
        "https://example.com/frame": ("frames", "text/html", PriorityClass.EMBEDDED_ASSET),
        "https://example.com/embed.pdf": ("embeds", "application/pdf", PriorityClass.DOCUMENT),
        "https://example.com/x/unknown": ("embeds", None, PriorityClass.EMBEDDED_ASSET),
    }
    assert {(s.url, s.reason) for s in plan.skipped} == {
        ("https://example.com/v/intro", "mime_not_allowed:video/*"),
        ("https://cdn.other.net/b.png", "out_of_scope_host"),
        ("https://example.com/i/hidden.png", "hidden"),
    }
    summary = plan.summary()
    assert summary["skipped_by_reason"] == {
        "mime_not_allowed:video/*": 1,
        "out_of_scope_host": 1,
        "hidden": 1,
    }
    assert len(summary["followed"]) == 6


def test_traps_self_links_duplicates_and_invalid_urls():
    links = [
        DiscoveredLink(url="https://example.com/t.pdf", text="x", hidden=True),
        DiscoveredLink(url="https://example.com/logout.pdf", text="x", action_like=True),
        DiscoveredLink(url="https://example.com/r.pdf?utm_source=a", text="x"),
        DiscoveredLink(url="https://example.com/r.pdf", text="x"),
        DiscoveredLink(url="ftp://example.com/r.pdf", text="x"),
    ]
    assets = [DiscoveredAsset(url=BASE + "#top", kind="frame")]
    plan = _plan(links=links, assets=assets)
    assert [f.schedule_key for f in plan.followups] == ["https://example.com/r.pdf"]
    assert {s.reason for s in plan.skipped} == {
        "hidden",
        "action_like",
        "invalid:unsupported_scheme:ftp",
    }


def test_mode_and_depth_limit_following():
    links = [DiscoveredLink(url="https://example.com/r.pdf", text="r")]
    assert _plan(_scope(mode=ScopeMode.SUPPLIED_URLS), links=links).followups == []
    assert _plan(links=links, depth=1).followups == []
    assert _plan(_scope(linked_download_depth=0), links=links).followups == []
    assert len(_plan(_scope(linked_download_depth=2), links=links, depth=1).followups) == 1


def test_per_page_cap_is_reported(monkeypatch):
    monkeypatch.setattr(fu, "MAX_FOLLOWUPS_PER_PAGE", 2)
    links = [DiscoveredLink(url=f"https://example.com/{i}.pdf", text="d") for i in range(4)]
    plan = _plan(links=links)
    assert len(plan.followups) == 2
    assert [s.reason for s in plan.skipped] == ["per_page_limit", "per_page_limit"]


def test_unparseable_parent_url_still_plans():
    plan = plan_followups(
        scope=_scope(),
        parent_url="not a url",
        parent_depth=0,
        links=[DiscoveredLink(url="https://example.com/r.pdf", text="r")],
        assets=[],
        hints={},
    )
    assert len(plan.followups) == 1


def test_plans_from_a_real_source_extraction():
    body = (
        b"<html><body><main><p>Docs</p>"
        b'<a href="/annual" type="application/pdf">Annual report</a>'
        b'<img src="/logo.png"><iframe src="/embed"></iframe>'
        b"</main></body></html>"
    )
    extraction = extract_html_source(
        body, base_url=BASE, evidence_id="evidence:x", resource_id="r", state_id="s"
    )
    plan = _plan(links=extraction.links, assets=extraction.assets, hints=anchor_hints(body, BASE))
    assert {(f.url, f.expected_mime) for f in plan.followups} == {
        ("https://example.com/annual", "application/pdf"),
        ("https://example.com/logo.png", "image/png"),
        ("https://example.com/embed", "text/html"),
    }
