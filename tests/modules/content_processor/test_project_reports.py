"""Project reports: contradictions grouped by topic, and duplication across the client's websites.

Reference case (the user's example): the death-claim payout time is "7-10 business
days after the claim is in good order" on the help centre and the life-insurance
FAQ, and "typically within 60 days of filing" on the death-claims page.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.modules.content_processor.analysis.topics import (
    Topic,
    facts_from_records,
    facts_in_sentence,
    find_topics,
    is_time_bound_url,
    recommended_action,
    severity_of,
    topic_rows,
)
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.project_reports import (
    build_project_reports,
    build_status,
    project_report_files,
    project_reports,
    report_sources,
    start_project_reports,
    zip_project_report,
)
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore, SiteSpec
from src.modules.content_processor.webapi.app import WRITE_HEADER, WRITE_HEADER_VALUE, create_app
from tests.modules.content_processor.l5_factory import block, record

PASSWORD = "correct horse battery"
DEATH_Q = "How long does it take to receive a death claim payment?"


def page(url: str, heading: list[str], *texts: str) -> UrlRecord:
    """A page whose first block is its heading and whose other blocks sit under it."""
    rid = "res-" + url.rstrip("/").rsplit("/", 1)[-1]
    blocks = [block(rid, 0, heading[-1], heading=heading)]
    blocks += [block(rid, i, t, heading=heading) for i, t in enumerate(texts, start=1)]
    return record(url, blocks=blocks)


def death_claim_pages(host: str = "https://www.insurer.com") -> list[UrlRecord]:
    return [
        page(
            f"{host}/help-center",
            ["Help center", DEATH_Q],
            "Death claim payments are usually made 7-10 business days after the claim is in "
            "good order.",
        ),
        page(
            f"{host}/life-insurance/faq",
            ["Life insurance FAQ", DEATH_Q],
            "We pay within 7-10 business days after the claim is in good order.",
        ),
        page(
            f"{host}/claims/death-claims", ["Death claims"], "Typically within 60 days of filing."
        ),
    ]


def other_pages(host: str = "https://www.insurer.com") -> list[UrlRecord]:
    """Pages with figures on other topics: none of them may join the death-claim topic."""
    return [
        page(
            f"{host}/annuities/forms",
            ["Online forms", "Is your contract eligible?"],
            "The address of record has been changed within the last 15 days.",
            "Please note this link will expire in 30 days.",
        ),
        page(
            f"{host}/quotes",
            ["Get a quote"],
            "To obtain a quote, please call 888-955-5432 Monday to Friday, 9 a.m. to 5 p.m.",
        ),
        page(
            f"{host}/markets", ["Markets"], "Data delayed by 20 mins.", "The 52 week low was set."
        ),
    ]


# -- reading facts ------------------------------------------------------------------


def test_values_are_read_and_normalised():
    assert facts_in_sentence("We pay within 7-10 business days after approval.")["time"].text == (
        "7–10 business days"
    )
    within = facts_in_sentence("Paid within 60 days of filing.")["time"].key
    plain = facts_in_sentence("Paid 60 days after filing.")["time"].key
    assert within == plain, "'within 60 days' and '60 days' are one version"
    business = facts_in_sentence("Paid in 60 business days of filing.")["time"].key
    assert business != plain, "business days and calendar days are different versions"
    assert facts_in_sentence("A 30-day free look period applies.")["time"].text == "30 days"
    phones = {
        facts_in_sentence(f"Call us at {n} today.")["phone"].key
        for n in ("1-800-387-2747", "(800) 387-2747", "800.387.2747")
    }
    assert phones == {"8003872747"}
    ages = facts_in_sentence("You can apply from age 18 to 85 if you qualify.")
    assert ages["age"].key == "18-85" and "time" not in ages
    money = facts_in_sentence("The fee is $25 or 2% of the amount.")
    assert money["money"].key == "USD 25" and money["percent"].key == "2%"
    both = facts_in_sentence("Pay in 30 days, or 60 days for group plans.")["time"]
    assert both.key == "30 calendar day; 60 calendar day", "one sentence is one (composite) value"
    assert facts_in_sentence("Founded in 1862 and proud of it.") == {}, "a year is not a duration"


def test_news_and_dated_paths_are_time_bound():
    assert is_time_bound_url("https://x.com/about/news/fund-declares-distribution")
    assert is_time_bound_url("https://x.com/press-releases/q3")
    assert is_time_bound_url("https://x.com/blog/2024/05/rates")
    assert not is_time_bound_url("https://x.com/claims/death-claims")


# -- topics --------------------------------------------------------------------------


def test_the_death_claim_example_comes_out_as_one_row():
    result = find_topics(facts_from_records([*death_claim_pages(), *other_pages()]))

    assert len(result.on("contradiction")) == 1, [t.label for t in result.topics]
    topic = result.on("contradiction")[0]
    a, b = topic.versions
    assert topic.label == DEATH_Q
    assert topic.severity == "High"
    assert a.value_text == "7–10 business days" and b.value_text == "60 days"
    assert a.urls == [
        "https://www.insurer.com/help-center",
        "https://www.insurer.com/life-insurance/faq",
    ]
    assert b.urls == ["https://www.insurer.com/claims/death-claims"]
    assert b.quote == "Typically within 60 days of filing."
    assert a.where == [f"Help center › {DEATH_Q}", f"Life insurance FAQ › {DEATH_Q}"]

    row = topic_rows([topic])[0]
    assert list(row)[:10] == [
        "Topic",
        "Version A",
        "Version A URLs",
        "Where A appears",
        "Version B",
        "Where B appears",
        "Version B URLs",
        "Severity",
        "Recommended action",
        "Approved answer (ML to fill)",
    ]
    assert row["Approved answer (ML to fill)"] == ""
    assert row["Recommended action"] == (
        "Agree one approved timeline and state it the same way on all 3 pages, with its "
        "condition (for example when the clock starts)."
    )
    assert row["Evidence ids A"] and row["Block ids B"] == "res-death-claims:b1"


def test_unrelated_figures_are_not_one_topic():
    result = find_topics(facts_from_records(other_pages()))
    assert result.topics == [], "15 days / 30 days, 20 minutes / 52 weeks are different things"


def test_press_release_figures_go_to_the_time_bound_sheet():
    news = [
        page(
            f"https://www.insurer.com/about/news/fund-declares-distribution-{month}",
            ["Fund declares monthly distribution"],
            f"Under the Plan, the Fund makes monthly distributions of {amount} per share.",
        )
        for month, amount in (("may", "$0.1380"), ("june", "$0.1580"), ("july", "$0.1380"))
    ]
    result = find_topics(facts_from_records([*news, *death_claim_pages()]))

    assert [t.label for t in result.on("contradiction")] == [DEATH_Q]
    bound = result.on("time_bound")
    assert len(bound) == 1 and bound[0].time_bound == "news or press page"
    assert recommended_action(bound[0]).startswith("No change needed")

    yearly = [
        page(
            f"https://www.insurer.com/plans/{y}", ["Plan limits"], f"In {y} the plan limit is {v}."
        )
        for y, v in ((2024, "$23,000"), (2025, "$23,500"))
    ]
    by_year = find_topics(facts_from_records(yearly))
    assert [t.time_bound for t in by_year.topics] == ["each version names a different year"]


def test_versions_split_by_condition_or_website_are_set_apart():
    states = [
        page(
            "https://www.insurer.com/claims/new-york",
            ["Claims"],
            "Claims for residents of New York are paid within 30 days of filing.",
        ),
        page(
            "https://www.insurer.com/claims/california",
            ["Claims"],
            "Claims for residents of California are paid within 45 days of filing.",
        ),
    ]
    result = find_topics(facts_from_records(states))
    assert [t.sheet for t in result.topics] == ["condition"]
    assert result.topics[0].condition == "Geography: New York | California"

    brands = (
        death_claim_pages("https://brand-a.com")[:1] + death_claim_pages("https://brand-b.com")[2:]
    )
    websites = {brands[0].requested_url: "Brand A", brands[1].requested_url: "Brand B"}
    split = find_topics(facts_from_records(brands, websites))
    assert [t.condition for t in split.topics] == ["Website: Brand B | Brand A"], (
        "A = 60 days (ties sort by value)"
    )
    assert split.topics[0].websites == ["Brand A", "Brand B"]


def test_severity_rules():
    assert severity_of("time", ["Death claims are paid within 60 days"]) == "High"
    assert severity_of("money", ["The annual fee is $25"]) == "High"
    assert severity_of("age", ["Ages 18 to 85"]) == "High"
    assert severity_of("phone", ["Call our claims team"]) == "Medium", "contact details"
    assert severity_of("quantity", ["Trusted by 10 million customers"]) == "Low"
    assert severity_of("quantity", ["The printer weighs 12 kg"]) == "Medium"


# -- the project bundle -----------------------------------------------------------------


def _project(store: ProjectStore) -> str:
    return store.create(
        name="Insurer audit",
        owner_name="G",
        client_name="Insurer",
        password=PASSWORD,
        client_site=SiteSpec("Insurer", "https://www.insurer.com"),
        client_brands=[SiteSpec("Insurer Direct", "https://direct-insurer.com")],
        competitors=[SiteSpec("Rival", "https://rival.com")],
    ).project_id


def _finish(
    store: ProjectStore, project_id: str, site_id: str, records: list[UrlRecord], run_id: str
) -> None:
    moment = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    store.save_run(
        RunState(
            run_id=run_id,
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
    path = store.run_dir(project_id, site_id, run_id) / f"{run_id}.records.jsonl"
    path.write_text("".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8")


SHARED = (
    "Our advisers help you choose cover that fits your family, explain every option in plain "
    "words, review your policy each year and stay with you when you need to make a claim."
)


def _scraped(store: ProjectStore, project_id: str) -> None:
    main = [
        *death_claim_pages(),
        *other_pages(),
        page("https://www.insurer.com/why-us", ["Why us"], SHARED),
    ]
    brand = [page("https://direct-insurer.com/why-us", ["Why Insurer Direct"], SHARED)]
    rival = [
        page(
            "https://rival.com/claims",
            ["Claims"],
            "Death claims are paid in 10 business days after approval.",
        ),
        page(
            "https://rival.com/faq",
            ["FAQ"],
            "Death claims are paid in 14 business days after approval.",
        ),
    ]
    _finish(store, project_id, "insurer", main, "run-1")
    _finish(store, project_id, "insurer-direct", brand, "run-2")
    _finish(store, project_id, "rival", rival, "run-3")


def _sheets(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as book:
        workbook = book.read("xl/workbook.xml").decode()
    return [part.split('"')[0] for part in workbook.split('<sheet name="')[1:]]


def test_project_bundle_reads_the_client_websites_only(tmp_path: Path):
    store = ProjectStore(tmp_path)
    project_id = _project(store)
    with pytest.raises(ValueError, match="finished scrape"):
        build_project_reports(store, project_id)
    assert [s.reason for s in report_sources(store, store.get(project_id))] == [
        "No finished scrape yet."
    ] * 3

    _scraped(store, project_id)
    report = build_project_reports(store, project_id, built_by="G")
    files = project_report_files(store, project_id, report.stamp)

    assert sorted(files) == [
        "Insurer_audit_contradictions_by_topic.csv",
        "Insurer_audit_contradictions_by_topic.jsonl",
        "Insurer_audit_contradictions_by_topic.xlsx",
        "Insurer_audit_duplication.xlsx",
        "Insurer_audit_project_report.xlsx",
        "manifest.json",
    ], "no competitor workbook unless asked"
    m = report.manifest
    assert m["pages"] == 8, "the main website and the brand, never the competitor"
    assert m["contradictions"]["contradictions"] == 1
    assert m["duplication"]["pairs_across_websites"] == 1, (
        "the same text on the main site and the brand"
    )
    assert _sheets(files["Insurer_audit_contradictions_by_topic.xlsx"]) == [
        "Contradictions",
        "Differs by condition",
        "Time-bound figures",
        "How to read",
        "Websites used",
    ]
    assert _sheets(files["Insurer_audit_duplication.xlsx"])[:3] == [
        "Duplicate pages (80%+)",
        "Duplicate groups (80%+)",
        "How to read",
    ]
    assert _sheets(files["Insurer_audit_project_report.xlsx"]) == [
        "Pages",
        "Matching content",
        "Shared text by line",
        "How to read",
    ], "the same page-by-page report as each website's scrape writes"
    assert m["duplication"]["project_report"] == "built from 2 website(s)"
    rows = [
        json.loads(line)
        for line in files["Insurer_audit_contradictions_by_topic.jsonl"]
        .read_text("utf-8")
        .splitlines()
    ]
    assert [r["Topic"] for r in rows] == [DEATH_Q]
    assert {f["url"] for v in rows[0]["versions"] for f in v["facts"]} == {
        "https://www.insurer.com/help-center",
        "https://www.insurer.com/life-insurance/faq",
        "https://www.insurer.com/claims/death-claims",
    }
    csv_text = files["Insurer_audit_contradictions_by_topic.csv"].read_text("utf-8-sig")
    assert csv_text.startswith("Topic,Version A,Version A URLs,Where A appears,Version B")

    with_rivals = build_project_reports(store, project_id, include_competitors=True)
    assert with_rivals.stamp != report.stamp, "every build is kept"
    rival_book = project_report_files(store, project_id, with_rivals.stamp)[
        "Insurer_audit_competitors.xlsx"
    ]
    assert _sheets(rival_book) == ["Rival – Contradictions", "Rival – Duplicates", "Websites used"]
    assert with_rivals.manifest["competitors"] == {
        "Rival": {"contradictions": 1, "duplicate_pairs": 0}
    }
    assert with_rivals.manifest["contradictions"]["contradictions"] == 1, (
        "a competitor never enters the client's contradictions"
    )
    assert [r.stamp for r in project_reports(store, project_id)] == [
        with_rivals.stamp,
        report.stamp,
    ]
    archive = zip_project_report(store, project_id, report.stamp)
    assert archive is not None
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == sorted(files)
    assert project_report_files(store, project_id, "../../etc") == {}


@pytest.fixture
def client(tmp_path: Path) -> tuple[TestClient, ProjectStore]:
    store = ProjectStore(tmp_path / "app_data")
    runner = ScrapeRunner(store, command=lambda *_a: 0, report=lambda *_a: 0, batch_size=2)
    signer = TokenSigner.from_file(tmp_path / "app_data" / "secret.key")
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(exports, projects=(store, runner, signer))
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_the_app_builds_and_serves_project_reports(client: tuple[TestClient, ProjectStore]):
    c, store = client
    project_id = _project(store)
    write = {WRITE_HEADER: WRITE_HEADER_VALUE}
    token = c.post(
        f"/api/projects/{project_id}/unlock", json={"password": PASSWORD}, headers=write
    ).json()["token"]
    unlocked = {**write, "X-Project-Token": token}

    empty = c.get(f"/api/projects/{project_id}/reports").json()
    assert empty["reports"] == [] and [s["site_name"] for s in empty["sources"]] == [
        "Insurer",
        "Insurer Direct",
        "Rival",
    ]
    assert c.post(f"/api/projects/{project_id}/reports", json={}, headers=write).status_code == 401
    refused = c.post(f"/api/projects/{project_id}/reports", json={}, headers=unlocked)
    assert refused.status_code == 409 and "finished scrape" in refused.json()["detail"]

    _scraped(store, project_id)
    started = c.post(
        f"/api/projects/{project_id}/reports", json={"include_competitors": False}, headers=unlocked
    )
    assert started.status_code == 200, started.text
    assert started.json()["building"] is True, (
        "large websites take minutes: built in the background"
    )
    deadline = time.monotonic() + 60
    built = c.get(f"/api/projects/{project_id}/reports")
    while built.json()["building"] and time.monotonic() < deadline:
        time.sleep(0.2)
        built = c.get(f"/api/projects/{project_id}/reports")
    assert not built.json()["building"] and built.json()["last_error"] == ""
    report = built.json()["reports"][0]
    assert report["contradictions"] == 1 and report["contradictions_high"] == 1
    assert report["duplicate_pairs_across_websites"] == 1
    assert [w["site_name"] for w in report["websites"]] == ["Insurer", "Insurer Direct"]
    assert all(s["run_id"] for s in built.json()["sources"])

    name = "Insurer_audit_contradictions_by_topic.xlsx"
    assert name in [f["name"] for f in report["files"]]
    got = c.get(f"/api/projects/{project_id}/reports/{report['stamp']}/downloads/{name}")
    assert got.status_code == 200 and got.content[:2] == b"PK"
    whole = c.get(f"/api/projects/{project_id}/reports/{report['stamp']}/downloads.zip")
    assert whole.status_code == 200
    assert name in zipfile.ZipFile(io.BytesIO(whole.content)).namelist()
    assert (
        c.get(f"/api/projects/{project_id}/reports/{report['stamp']}/downloads/x.xlsx").status_code
        == 404
    )
    assert (
        c.get(f"/api/projects/{project_id}/reports/20990101-000000/downloads.zip").status_code
        == 404
    )


def test_topic_rows_keep_extra_versions():
    pages = death_claim_pages() + [
        page(
            "https://www.insurer.com/claims/how",
            ["Death claims"],
            "Death claims are typically paid within 30 days of filing.",
        )
    ]
    topic: Topic = find_topics(facts_from_records(pages)).topics[0]
    assert len(topic.versions) == 3
    row = topic_rows([topic])[0]
    assert row["Value A"] == "7–10 business days"
    rest = f"{row['Value B']} {row['Other versions']}"
    assert "30 days" in rest and "60 days" in rest and "claims/" in str(row["Other versions"])


def test_one_client_website_reuses_its_scrape_project_report(tmp_path: Path):
    store = ProjectStore(tmp_path)
    project_id = store.create(
        name="Solo",
        owner_name="G",
        client_name="Insurer",
        password=PASSWORD,
        client_site=SiteSpec("Insurer", "https://www.insurer.com"),
    ).project_id
    _finish(store, project_id, "insurer", death_claim_pages(), "run-9")
    reports = store.run_dir(project_id, "insurer", "run-9") / "reports"
    reports.mkdir()
    (reports / "run-9_project_report.xlsx").write_bytes(b"PK the scrape's own workbook")

    report = build_project_reports(store, project_id)

    copied = project_report_files(store, project_id, report.stamp)["Solo_project_report.xlsx"]
    assert copied.read_bytes() == b"PK the scrape's own workbook"
    assert report.manifest["duplication"]["project_report"] == "copied from the scrape run-9"


def test_builds_run_in_the_background_one_at_a_time(tmp_path: Path):
    store = ProjectStore(tmp_path)
    project_id = _project(store)
    with pytest.raises(ValueError, match="finished scrape"):
        start_project_reports(store, project_id)
    _scraped(store, project_id)

    status = start_project_reports(store, project_id, wait=True)
    assert status.include_competitors is False
    assert build_status(project_id) is None, "a finished build leaves no status behind"
    assert len(project_reports(store, project_id)) == 1

    from src.modules.content_processor.projects import project_reports as module

    module._STATUS[project_id] = module.BuildStatus(
        building=True, started_at=datetime.now(UTC), include_competitors=False
    )
    try:
        with pytest.raises(ValueError, match="already being built"):
            start_project_reports(store, project_id)
    finally:
        module._STATUS.pop(project_id, None)

    def broken(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    original = module.build_project_reports
    module.build_project_reports = broken  # type: ignore[assignment]
    try:
        start_project_reports(store, project_id, wait=True)
    finally:
        module.build_project_reports = original
    failed = build_status(project_id)
    assert failed is not None and not failed.building
    assert failed.error == "The report could not be built: disk full"
    module._STATUS.pop(project_id, None)
