"""Analyst API (Lane L8): runs, overview, pages, detail, reports and downloads."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.enums import (
    ContentLayer,
    EntityStatus,
    GapType,
    UiRegion,
    UrlBadge,
)
from src.modules.content_processor.export.bundle import write_run_reports
from src.modules.content_processor.webapi import __main__ as cli
from src.modules.content_processor.webapi.app import create_app
from src.modules.content_processor.webapi.store import (
    UNCONFIRMED_TEXT,
    RunStore,
    page_key_for,
)
from tests.modules.content_processor.l5_factory import block, gap, record, words

BASE = "http://127.0.0.1"


def _write(path: Path, records: list[UrlRecord], extra_lines: list[str] | None = None) -> None:
    lines = [r.model_dump_json() for r in records] + (extra_lines or [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _records() -> list[UrlRecord]:
    shared = words(1, 120)
    pricing_blocks = [
        block("res-pricing", 0, "Pricing", heading=["Pricing"]),
        block("res-pricing", 1, "Basic plan costs $10 per month.", heading=["Pricing"]),
        block("res-pricing", 2, "Footer links", role="footer"),
        block("res-pricing", 3, "Footer links", role="footer", dedupe_of="res-pricing:b2"),
        block("res-pricing", 4, "Hidden note", layer=ContentLayer.HIDDEN),
    ]
    pricing_blocks[1] = pricing_blocks[1].model_copy(
        update={"ui_region": UiRegion.MAIN_CONTENT, "status": EntityStatus.EXTRACTED_UNVERIFIED}
    )
    pricing = record(
        "https://a.example.com/pricing",
        badge=UrlBadge.NEEDS_REVIEW,
        blocks=pricing_blocks,
        warnings=["frames_from_source_dom_only"],
    )
    pricing.inventory.regions_needs_review = 1
    pricing.status_vector.fidelity = EntityStatus.EXTRACTED_UNVERIFIED
    partial = record(
        "https://a.example.com/faq",
        [shared, "The fee is $5 for members."],
        badge=UrlBadge.PARTIAL,
        gaps=[gap("res-faq", GapType.UNVISITED_STATE, "unvisited_tab:never_visible:Plans")],
    )
    partial.gaps[0].retry_eligible = True
    copy = record("https://b.example.org/faq-copy", [shared, "The fee is $7 for members."])
    blocked = record(
        "https://b.example.org/login",
        badge=UrlBadge.BLOCKED,
        gaps=[gap("res-login", GapType.ACCESS_BLOCKED, "login_wall:blocks_main_content")],
    )
    image = record("https://a.example.com/img/hero.jpg", badge=UrlBadge.PARTIAL)
    image.sniffed_mime = "image/jpeg"
    return [pricing, partial, copy, blocked, image]


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    _write(tmp_path / "demo.records.jsonl", _records(), extra_lines=['{"half": '])
    (tmp_path / "demo.json").write_text(
        json.dumps(
            {
                "mode": "dryrun",
                "scheduled": 5,
                "execution": {"stop_reason": "drained", "elapsed_s": 90},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "not-a-run.content.jsonl").write_text("{}\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(exports: Path) -> TestClient:
    return TestClient(create_app(exports), base_url=BASE)


def _key(url: str) -> str:
    return page_key_for(next(r for r in _records() if r.requested_url == url))


def test_runs_lists_record_files_with_counts(client: TestClient) -> None:
    body = client.get("/api/runs").json()
    assert [r["name"] for r in body["runs"]] == ["demo"]
    run = body["runs"][0]
    assert run["page_count"] == 5
    assert run["badges"]["BLOCKED"] == 1
    assert run["badges"]["VERIFIED_WITHIN_SCOPE"] == 1
    assert run["fully_captured_pct"] == 20.0
    assert run["unreadable_lines"] == 1
    assert run["has_reports"] is False
    assert run["hosts"] == ["a.example.com", "b.example.org"]


def test_overview_ranks_issues_and_reads_the_manifest(client: TestClient) -> None:
    body = client.get("/api/runs/demo/overview").json()
    keys = {i["key"]: i for i in body["issues"]}
    assert keys[UNCONFIRMED_TEXT]["pages"] == 1
    assert keys["UNVISITED_STATE"]["retryable_pages"] == 1
    assert body["formats"] == [{"value": "image", "count": 1}]
    assert body["facts"] == {
        "mode": "dryrun",
        "stop_reason": "drained",
        "elapsed_s": 90.0,
        "scheduled": 5,
    }


def test_pages_filter_sort_page_and_search(client: TestClient) -> None:
    body = client.get("/api/runs/demo/pages", params={"size": 2}).json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["items"][0]["badge"] == "BLOCKED"  # most severe first
    second = client.get("/api/runs/demo/pages", params={"size": 2, "page": 3}).json()
    assert len(second["items"]) == 1

    partial = client.get("/api/runs/demo/pages", params={"status": "PARTIAL"}).json()
    assert {i["url"] for i in partial["items"]} == {
        "https://a.example.com/faq",
        "https://a.example.com/img/hero.jpg",
    }
    assert partial["facets"]["status"]["BLOCKED"] == 1  # counts ignore the status filter

    both = client.get("/api/runs/demo/pages?status=PARTIAL,BLOCKED&host=b.example.org").json()
    assert [i["url"] for i in both["items"]] == ["https://b.example.org/login"]

    found = client.get("/api/runs/demo/pages", params={"q": "basic PLAN"}).json()
    assert [i["title"] for i in found["items"]] == ["Pricing"]

    issues = client.get("/api/runs/demo/pages", params={"has_issues": "false"}).json()
    assert all(i["issues"] == 0 for i in issues["items"])
    by_issue = client.get("/api/runs/demo/pages", params={"issue": UNCONFIRMED_TEXT}).json()
    assert by_issue["total"] == 1

    images = client.get("/api/runs/demo/pages", params={"format": "image"}).json()
    assert images["items"][0]["title"] == "Hero"

    words_desc = client.get(
        "/api/runs/demo/pages", params={"sort": "words", "order": "desc"}
    ).json()
    counts = [i["words"] for i in words_desc["items"]]
    assert counts == sorted(counts, reverse=True)
    for sort in ("title", "url", "issues", "modified", "captured", "type"):
        assert client.get("/api/runs/demo/pages", params={"sort": sort}).json()["total"] == 5

    assert client.get("/api/runs/demo/pages", params={"size": 999}).status_code == 422
    assert client.get("/api/runs/demo/pages", params={"sort": "evil"}).status_code == 422


def test_page_detail_has_blocks_gaps_findings_and_facts(client: TestClient) -> None:
    key = _key("https://a.example.com/pricing")
    body = client.get(f"/api/runs/demo/pages/{key}").json()
    assert body["row"]["title"] == "Pricing"
    assert body["row"]["issue_keys"] == [UNCONFIRMED_TEXT]
    assert body["findings"] == [{"key": UNCONFIRMED_TEXT, "unconfirmed": 1, "total": 5}]
    assert body["repeated_blocks"] == 1
    texts = [b["text"] for b in body["blocks"]]
    assert "Hidden note" in texts and texts.count("Footer links") == 1
    unverified = next(b for b in body["blocks"] if b["text"].startswith("Basic"))
    assert unverified["verified"] is False and unverified["region"] == "main_content"
    assert body["warnings"] == ["frames_from_source_dom_only"]

    raw = client.get(f"/api/runs/demo/pages/{key}/raw").json()
    assert raw["requested_url"] == "https://a.example.com/pricing"

    assert client.get("/api/runs/demo/pages/nope").status_code == 404
    assert client.get("/api/runs/demo/pages/nope/raw").status_code == 404


def test_unknown_run_and_bad_names_are_404(client: TestClient) -> None:
    for path in ("/api/runs/missing/overview", "/api/runs/..%2Fdemo/pages", "/api/runs/.x/review"):
        response = client.get(path)
        assert response.status_code == 404
    assert "no run called" in client.get("/api/runs/missing/overview").json()["detail"]
    assert client.get("/api/whatever").status_code == 404


def test_reports_are_computed_when_no_report_folder(client: TestClient) -> None:
    dups = client.get("/api/runs/demo/duplicates").json()
    assert dups["source"] == "computed"
    assert dups["groups"] and dups["groups"][0]["size"] == 2
    members = dups["groups"][0]["members"]
    assert sum(m["representative"] for m in members) == 1
    assert all(m["page_key"] for m in members)
    assert dups["pairs"][0]["shared_passages"]

    review = client.get("/api/runs/demo/review").json()
    assert review["source"] == "computed"
    assert review["items"][0]["badge"] == "BLOCKED"
    assert {i["title"] for i in review["items"]} >= {"Pricing", "Hero"}

    contra = client.get("/api/runs/demo/contradictions").json()
    assert contra["source"] == "computed"
    assert isinstance(contra["items"], list)
    assert client.get("/api/runs/demo/downloads").json() == {"folder": None, "files": []}


def test_reports_are_read_from_the_saved_folder(exports: Path) -> None:
    out = exports / "demo_reports"
    write_run_reports(_records(), out)
    # A saved R4 row whose flags contain spaces, as the engine writes them.
    row = {
        "pair_id": "cp-1",
        "bucket": "context_matched",
        "url_a": "https://a.example.com/faq",
        "quote_a": "The fee is $5 for members.",
        "url_b": "https://b.example.org/faq-copy",
        "quote_b": "The fee is $7 for members.",
        "contradiction_pct": 81.5,
        "judge_status": "disabled(no_local_provider)",
        "deterministic_flags": "numeric:percent:10 percent!=15 percent negation_flip",
        "context_match": "entity=match; codes=differs(A|B)",
        "confidence_band": "high",
        "confidence_reason": "",
        "heading_path_a": "FAQ",
        "badge_a": "PARTIAL",
        "badge_b": "VERIFIED_WITHIN_SCOPE",
        "reviewer_decision": "confirmed",
    }
    (out / "r4_contradictions.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with (out / "r2b_overlap_pairs.csv").open("a", encoding="utf-8") as handle:
        handle.write("run-l5,https://x/a,https://x/b,X,Y,1,2,3,4,'=SUM(1),exact_duplicate,\n")
    client = TestClient(create_app(exports), base_url=BASE)

    dups = client.get("/api/runs/demo/duplicates").json()
    assert dups["source"] == "report" and dups["stale"] is False
    assert dups["generated_at"]
    guarded = dups["pairs"][-1]
    assert guarded["shared_passages"] == ["=SUM(1)"] and guarded["exact"] is True
    assert guarded["key_a"] is None and guarded["title_a"] == "A"

    contra = client.get("/api/runs/demo/contradictions").json()
    item = contra["items"][0]
    assert item["flags"] == ["numeric:percent:10 percent!=15 percent", "negation_flip"]
    assert item["context"] == {"entity": "match", "codes": "differs(A|B)"}
    assert item["a"]["page_key"] == _key("https://a.example.com/faq")
    assert item["reviewer_decision"] == "confirmed"
    assert client.get("/api/runs/demo/review").json()["source"] == "report"
    # cached, and still correct on the second call
    assert client.get("/api/runs/demo/contradictions").json() == contra

    files = client.get("/api/runs/demo/downloads").json()
    assert files["folder"] == "demo_reports"
    names = {f["name"] for f in files["files"]}
    assert "r1_url_content.xlsx" in names
    got = client.get("/api/runs/demo/downloads/review_queue.csv")
    assert got.status_code == 200
    assert "attachment" in got.headers["content-disposition"]
    for bad in ("..%2Fdemo.json", "demo.json", "%2E%2E%2Fdemo.records.jsonl", "nope.csv"):
        assert client.get(f"/api/runs/demo/downloads/{bad}").status_code == 404


def test_report_from_another_run_is_ignored_and_old_report_is_stale(exports: Path) -> None:
    out = exports / "demo_reports"
    write_run_reports(_records(), out, run_id="someone-else")
    client = TestClient(create_app(exports), base_url=BASE)
    assert client.get("/api/runs/demo/review").json()["source"] == "computed"

    write_run_reports(_records(), out)
    old = 1_000_000_000
    for path in out.iterdir():
        os.utime(path, (old, old))
    assert client.get("/api/runs/demo/review").json()["stale"] is True


def test_records_are_reloaded_when_the_file_changes(exports: Path) -> None:
    store = RunStore(exports)
    first = store.get("demo")
    assert store.get("demo") is first
    _write(exports / "demo.records.jsonl", _records()[:2])
    second = store.get("demo")
    assert second is not first
    assert len(second.entries) == 2


def test_duplicate_schedule_keys_get_distinct_page_keys(tmp_path: Path) -> None:
    one = record("https://a.example.com/x", ["a"])
    _write(tmp_path / "dup.records.jsonl", [one, one])
    run = RunStore(tmp_path).get("dup")
    assert len({e.key for e in run.entries}) == 2


def test_missing_or_bad_manifest_gives_no_facts(tmp_path: Path) -> None:
    _write(tmp_path / "a.records.jsonl", [record("https://a.example.com/x", ["a"])])
    store = RunStore(tmp_path)
    assert store.overview(store.get("a")).facts is None
    (tmp_path / "a.json").write_text("not json", encoding="utf-8")
    assert store.overview(store.get("a")).facts is None
    (tmp_path / "a.json").write_text("[1]", encoding="utf-8")
    assert store.overview(store.get("a")).facts is None
    assert RunStore(tmp_path / "missing").names() == []


def test_read_only_local_only_and_security_headers(client: TestClient, exports: Path) -> None:
    assert client.post("/api/runs").status_code == 405
    assert client.delete("/api/runs/demo/overview").status_code == 405
    response = client.get("/api/runs")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    foreign = TestClient(create_app(exports), base_url="http://evil.example.com")
    assert foreign.get("/api/runs").status_code == 400


def test_serves_the_built_app_with_route_fallback(exports: Path, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    client = TestClient(create_app(exports, static_dir=dist), base_url=BASE)
    assert "id=root" in client.get("/").text
    assert "id=root" in client.get("/runs/demo/pages/abc").text
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    (dist / "assets" / "new.js").write_text("console.log(2)", encoding="utf-8")
    assert client.get("/assets/new.js").text == "console.log(2)"
    assert "id=root" in client.get("/../../secret.txt").text

    bare = TestClient(create_app(exports), base_url=BASE)
    assert bare.get("/").status_code == 404


def test_command_line_checks_its_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.main(["--exports", str(tmp_path / "missing")]) == 2
    assert cli.main(["--exports", str(tmp_path), "--static", str(tmp_path)]) == 2
    started: dict[str, object] = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: started.update(kw))
    assert cli.main(["--exports", str(tmp_path), "--port", "8799"]) == 0
    assert started == {"host": "127.0.0.1", "port": 8799, "log_level": "warning"}
    assert cli.parse_args(["--exports", "x"]).port == 8780
