"""Analytics (GA4): import, insights, page report, gaps, monitoring, API, CLI and pull."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.modules.analytics.importer import Ga4ImportError, aggregate, ai_source, read_ga4_csv
from src.modules.analytics.insights import build_report
from src.modules.analytics.models import Ga4Row, Ga4Snapshot
from src.modules.analytics.service import analytics_store, import_ga4_csv, pull_ga4
from src.modules.content_processor import cli
from src.modules.content_processor.knowledge.models import Cta, PageStamp
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.gaps.build import build_findings
from src.modules.monitoring.service import analytics_alerts
from src.modules.research.page_models import ConsolidationPair, PageRow
from src.modules.research.pages import _owner_checks, build_page_report
from src.modules.theming.models import ThemeMap
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

NOW = datetime(2026, 11, 26, tzinfo=UTC)
WRITE = {"X-Requested-With": "content-processor"}
S = "https://example.com"

CSV = (
    "# ----------------------------------------\n"
    "# Landing page\n"
    "# Account: Example\n"
    "# Property: Example GA4\n"
    "# ----------------------------------------\n"
    "#\n"
    "# Start date: 20261001\n"
    "# End date: 20261028\n"
    "Landing page + query string,Session default channel group,Sessions,Engaged sessions,"
    "Engagement rate,Average engagement time per session,Key events,Total revenue\n"
    'Grand total,,1000,600,60%,00:01:00,30,"$1,000.00"\n'
    "/life/term?utm=x,Organic Search,400,100,25%,00:00:30,0,0\n"
    "/life/term,Direct,100,80,80%,1m 30s,0,0\n"
    '/retirement/annuities,Organic Search,300,240,80%,95.5,25,"$900.00"\n'
    "(not set),Direct,50,10,20%,5,0,0\n"
    "\n"
    "# second table\n"
    "Date,Sessions\n"
    "20261001,30\n"
)


def _snapshot(rows: list[Ga4Row], *, start: date, end: date, sid: str) -> Ga4Snapshot:
    return Ga4Snapshot(
        snapshot_id=sid,
        project_id="p1",
        website="example.com",
        source="csv_import",
        imported_at=NOW,
        start_date=start,
        end_date=end,
        rows=rows,
        has_channels=True,
        has_sources=True,
    )


def test_the_export_is_read_page_by_page() -> None:
    snap = read_ga4_csv(
        CSV.encode(), project_id="p1", website="www.example.com", now=NOW, snapshot_id="ga4-x"
    )
    assert (snap.start_date, snap.end_date) == (date(2026, 10, 1), date(2026, 10, 28))
    rows = {r.url: r for r in snap.rows}
    assert set(rows) == {f"{S}/life/term", f"{S}/retirement/annuities"}, "totals and (not set) out"
    term = rows[f"{S}/life/term"]
    assert (term.sessions, term.organic_sessions, term.engaged_sessions) == (500, 400, 180)
    assert term.engagement_rate == 0.36 and term.avg_engagement_s == 42.0
    assert term.channels == {"Organic Search": 400, "Direct": 100}
    annuities = rows[f"{S}/retirement/annuities"]
    assert annuities.key_events == 25 and annuities.revenue == 900.0
    assert snap.has_channels and not snap.has_sources
    with pytest.raises(Ga4ImportError):
        read_ga4_csv(b"a,b\n1,2\n", project_id="p1", website="x", now=NOW, snapshot_id="ga4-y")


def test_ai_assistants_are_counted_without_double_counting_sessions() -> None:
    assert ai_source("chatgpt.com / referral") == "chatgpt.com"
    assert ai_source("www.perplexity.ai") == "perplexity.ai" and ai_source("google") is None
    rows = aggregate(
        [
            {"page": "/a", "channel": "Organic Search", "sessions": 100, "engaged": 50},
            {"page": "/a", "source": "chatgpt.com", "sessions": 0, "ai_sessions": 7},
            {"page": "/b", "source": "gemini.google.com", "sessions": 9, "engaged": 9},
        ],
        "example.com",
    )
    by = {r.url: r for r in rows}
    assert by[f"{S}/a"].sessions == 100 and by[f"{S}/a"].ai_sessions == 7
    assert by[f"{S}/b"].ai_sources == {"gemini.google.com": 9}


def _rows(**over: dict[str, Any]) -> list[Ga4Row]:
    base = {
        "/life/term": {
            "sessions": 600,
            "organic_sessions": 500,
            "engaged_sessions": 120,
            "key_events": 0,
        },
        "/retirement/annuities": {
            "sessions": 400,
            "organic_sessions": 300,
            "engaged_sessions": 320,
            "key_events": 40,
        },
        "/contact": {
            "sessions": 200,
            "organic_sessions": 20,
            "engaged_sessions": 150,
            "key_events": 0,
        },
        "/blog/ai": {
            "sessions": 60,
            "organic_sessions": 10,
            "engaged_sessions": 40,
            "key_events": 1,
            "ai_sessions": 12,
            "ai_sources": {"chatgpt.com": 12},
        },
    }
    out = []
    for path, values in base.items():
        v = {**values, **over.get(path, {})}
        sessions = int(v["sessions"])
        v["engagement_rate"] = round(int(v["engaged_sessions"]) / sessions, 4)
        out.append(Ga4Row(url=f"{S}{path}", **v))
    return out


def _themes() -> ThemeMap:
    return ThemeMap.model_validate(
        {
            "map_id": "m1",
            "project_id": "p1",
            "built_at": NOW,
            "method": "t",
            "websites": [],
            "themes": [
                {
                    "theme_id": "th-term",
                    "label": "Term life",
                    "terms": ["term"],
                    "pages": 1,
                    "coverage": [],
                    "ownership": "client_owned",
                    "ownership_reason": "",
                },
                {
                    "theme_id": "th-ann",
                    "label": "Annuities",
                    "terms": ["annuities"],
                    "pages": 1,
                    "coverage": [],
                    "ownership": "client_owned",
                    "ownership_reason": "",
                },
            ],
            "pages": [
                {
                    "url": f"{S}/life/term",
                    "website": "example.com",
                    "theme_id": "th-term",
                    "funnel_stage": "learn",
                },
                {
                    "url": f"{S}/retirement/annuities",
                    "website": "example.com",
                    "theme_id": "th-ann",
                    "funnel_stage": "decide",
                },
            ],
        }
    )


def test_the_report_rolls_up_and_finds_leaks_decays_ctas_ai_and_edits() -> None:
    from src.modules.monitoring.models import PageChange

    before = _snapshot(
        _rows(
            **{"/retirement/annuities": {"key_events": 80}, "/life/term": {"organic_sessions": 900}}
        ),
        start=date(2026, 9, 3),
        end=date(2026, 9, 30),
        sid="ga4-20261001-000000-000",
    )
    now = _snapshot(
        _rows(), start=date(2026, 10, 1), end=date(2026, 10, 28), sid="ga4-20261029-000000-000"
    )
    stamp = PageStamp.model_construct(
        ctas=[Cta(text="Contact an advisor", url=f"{S}/contact#form", strength="strong")],
        components={"form": 1},
    )
    report = build_report(
        now,
        previous=before,
        theme_map=_themes(),
        stamps={f"{S}/contact": stamp},
        changed=[PageChange(url=f"{S}/life/term", head_changes={"title": {}}, drift=0.2)],
        now=NOW,
    )
    assert report.totals["sessions"] == 1260 and report.totals["ai_sessions"] == 12
    themes = {t.label: t for t in report.themes}
    assert themes["Annuities"].key_events == 40 and themes["Annuities"].conversion_rate == 0.1
    assert {g.key for g in report.funnel} == {"learn", "decide"}
    assert [p.url for p in report.leaky] == [f"{S}/life/term"], "500 from search, 20% engaged"
    assert {(d.url, d.metric) for d in report.decays} == {
        (f"{S}/life/term", "organic_sessions"),
        (f"{S}/retirement/annuities", "key_events"),
    }
    assert [c.url for c in report.cta_gaps] == [f"{S}/contact"]
    assert report.ai_referrals[0].sources == {"chatgpt.com": 12}
    assert report.post_edit[0].change.startswith("title changed")


def test_gaps_get_three_kinds_and_converting_themes_rank_higher() -> None:
    report = build_report(
        _snapshot(
            _rows(), start=date(2026, 10, 1), end=date(2026, 10, 28), sid="ga4-20261029-000000-000"
        ),
        previous=_snapshot(
            _rows(**{"/retirement/annuities": {"key_events": 80}}),
            start=date(2026, 9, 3),
            end=date(2026, 9, 30),
            sid="ga4-20261001-000000-000",
        ),
        theme_map=_themes(),
        stamps={f"{S}/contact": PageStamp.model_construct(ctas=[], components={"form": 2})},
        now=NOW,
    )
    plain = build_findings(_themes(), {"example.com": "client"})
    with_ga4 = build_findings(_themes(), {"example.com": "client"}, analytics=report)
    kinds = {f.kind for f in with_ga4}
    assert {"leaky_page", "traffic_decay", "cta_no_conversions"} <= kinds
    ann = [f for f in with_ga4 if f.theme_id == "th-ann" and f.kind not in kinds - {f.kind}]
    before = {f.finding_id: f.priority for f in plain}
    boosted = [f for f in with_ga4 if f.finding_id in before and f.priority > before[f.finding_id]]
    assert all(f.theme_id == "th-ann" and "GA4" in f.detail for f in boosted)
    assert ann is not None


def test_the_page_report_carries_ga4_and_keeps_converting_pages() -> None:
    ga4 = _snapshot(
        _rows(), start=date(2026, 10, 1), end=date(2026, 10, 28), sid="ga4-20261029-000000-000"
    )
    from src.modules.research.models import KeywordRow, ResearchSnapshot, WebsiteResearch
    from tests.modules.research.test_insights import _site

    snap = ResearchSnapshot(
        snapshot_id="research-20261126-000000-001",
        project_id="p1",
        created_at=NOW,
        database="us",
        websites=[
            WebsiteResearch(
                website="www.example.com",
                name="E",
                role="client",
                source="csv_import",
                pulled_at=NOW,
                keywords=[
                    KeywordRow(keyword="x", position=3, volume=10, url="https://www.example.com/a")
                ],
            )
        ],
    )
    report = build_page_report(
        "p1", "www.example.com", snapshot=snap, inventory=_site(), ga4=ga4, now=NOW
    )
    assert report.ga4_snapshot_id == ga4.snapshot_id
    rows = {r.url: r for r in report.pages}
    assert rows[f"{S}/retirement/annuities"].ga_conversions == 40
    assert rows[f"{S}/blog/ai"].ga_ai_sessions == 12
    assert "GA4" not in " ".join(report.notes)
    pair = ConsolidationPair(
        group=1,
        page=f"{S}/a",
        owner=f"{S}/b",
        pattern="differentiate_siblings",
        fix="x",
        weight=10,
        priority="High",
    )
    _owner_checks(
        [pair],
        {
            "example.com/a": PageRow(url=f"{S}/a", ga_sessions=100, ga_conversions=10),
            "example.com/b": PageRow(url=f"{S}/b", ga_sessions=100, ga_conversions=1),
        },
    )
    assert pair.owner_check.startswith("GA4: this page converts 10.0%")


@pytest.fixture
def ga4_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, str]]:
    from src.modules.analytics.standin import make_dataset, serve, write_service_account

    key = write_service_account(tmp_path / "sa.json")
    datasets = [
        make_dataset("example.com", ["/life/term", "/retirement/annuities"], seed="t", period=n)
        for n in (0, 1)
    ]
    server = serve(datasets, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    for name, value in {
        "CP_GA4_ENABLED": "true",
        "GA4_CREDENTIALS_FILE": str(key),
        "CP_GA4_BASE_URL": base,
        "CP_GA4_TOKEN_URL": base + "token",
        "CP_GA4_MIN_INTERVAL_MS": "0",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        yield {"base": base, "end": str(datasets[0].end)}
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.undo()
        get_settings.cache_clear()


def test_api_cli_and_a_pull_through_the_standin(
    themed: World,
    tmp_path: Path,
    ga4_env: dict[str, str],
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(themed.store, ScrapeRunner(themed.store), signer),
        knowledge=themed.access,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    base = f"/api/projects/{themed.project_id}/analytics"
    assert client.get(base).status_code == 404
    assert client.get("/api/analytics/status").json()["enabled"] is True
    token, _ = signer.issue(themed.project_id)
    auth = {**WRITE, "X-Project-Token": token}
    body = {"file_name": "ga4.csv", "content_base64": base64.b64encode(CSV.encode()).decode()}
    assert client.post(f"{base}/import", json=body, headers=WRITE).status_code == 401
    saved = client.post(f"{base}/import", json=body, headers=auth).json()
    assert saved["snapshots"][0]["sessions"] == 800
    bad = {**body, "content_base64": base64.b64encode(b"x,y\n").decode()}
    assert client.post(f"{base}/import", json=bad, headers=auth).status_code == 422
    refused = client.post(f"{base}/pull", json={}, headers=auth)
    assert refused.status_code == 409 and "property" in refused.json()["detail"]
    put = client.post(f"{base}/settings", json={"property_id": "123456789"}, headers=auth)
    assert put.json() == {"property_id": "123456789", "website": "example.com"}
    pulled = client.post(f"{base}/pull", json={}, headers=auth).json()["snapshots"]
    assert len(pulled) == 2 and all("DUMMY" in s["notes"][0] for s in pulled)
    store = analytics_store(themed.store)
    assert store.previous(themed.project_id) is not None
    report = client.get(base).json()
    assert report["previous_snapshot_id"] and report["totals"]["sessions"] > 0
    assert client.get(f"{base}/workbook").content[:2] == b"PK"
    pages = client.get(f"/api/projects/{themed.project_id}/research/pages")
    # no search data: the page report is built from the scrape, with the GA4 figures joined
    assert pages.status_code == 200 and pages.json()["research_snapshot_id"] is None
    assert pages.json()["ga4_snapshot_id"] == store.latest(themed.project_id).snapshot_id

    app_data = ["--app-data", str(themed.store.root)]
    assert cli.main(["analytics", "show", "--project", themed.project_id, *app_data]) == 0
    out = tmp_path / "dummy"
    assert (
        cli.main(
            [
                "analytics-standin",
                "make",
                "--project",
                themed.project_id,
                "--out",
                str(out),
                *app_data,
            ]
        )
        == 0
    )
    assert (out / "ga4-latest.csv").is_file() and (out / "dummy-service-account.json").is_file()
    assert (
        cli.main(
            [
                "analytics",
                "import",
                "--project",
                themed.project_id,
                "--file",
                str(out / "ga4-previous.csv"),
                *app_data,
            ]
        )
        == 0
    )
    assert cli.main(["analytics", "pull", "--project", themed.project_id, *app_data]) == 0
    capsysbinary.readouterr()
    assert analytics_alerts(themed.store, themed.project_id) is not None


def test_pulls_are_refused_until_switched_on(themed: World) -> None:
    from src.modules.analytics.ga4 import Ga4Error

    get_settings.cache_clear()
    with pytest.raises(Ga4Error, match="switched off"):
        pull_ga4(themed.store, themed.project_id)


def test_decays_become_monitoring_alerts(themed: World) -> None:
    store = analytics_store(themed.store)
    pid = themed.project_id
    old = _snapshot(
        _rows(**{"/life/term": {"organic_sessions": 900}}),
        start=date(2026, 9, 3),
        end=date(2026, 9, 30),
        sid="ga4-20261001-000000-000",
    ).model_copy(update={"project_id": pid})
    new = _snapshot(
        _rows(), start=date(2026, 10, 1), end=date(2026, 10, 28), sid="ga4-20261029-000000-000"
    ).model_copy(update={"project_id": pid})
    store.save(old)
    store.save(new)
    alerts = analytics_alerts(themed.store, pid)
    assert [a.kind for a in alerts] == ["client_traffic_decay"]
    assert alerts[0].severity == "medium" and "44%" in alerts[0].title
    assert json.loads(store.settings(pid).model_dump_json())["property_id"] is None
    assert import_ga4_csv is not None
