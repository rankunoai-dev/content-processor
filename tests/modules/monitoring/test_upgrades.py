"""Monitoring upgrades: SEO facts per page, ranking and link signals, digest, pinned pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.modules.backlinks.models import (
    BacklinkSnapshot,
    BacklinkStats,
    RefDomain,
    WebsiteBacklinks,
)
from src.modules.content_processor.knowledge.models import Cta, SchemaFacts
from src.modules.content_processor.projects.notify import Notifier
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.monitoring.check import _seo_alerts, _signal_alerts
from src.modules.monitoring.diff import RunFiles, diff_runs, load_run_files
from src.modules.monitoring.digest import DigestBook, compose, due_alerts, send_digest
from src.modules.monitoring.models import (
    Alert,
    MonitorCheck,
    PageChange,
    SearchSignals,
    SiteDiff,
)
from src.modules.monitoring.service import monitor_store, run_project_check
from src.modules.monitoring.signals import describe_change, link_signals, rank_signals
from src.modules.monitoring.store import check_tables
from src.modules.research.models import KeywordRow, ResearchSnapshot, WebsiteResearch
from src.modules.research.service import research_store
from tests.modules.analyst.conftest import CLIENT, RIVAL, World, chunk, stamp
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.monitoring.test_monitoring import WRITE, _next_scrapes
from tests.modules.monitoring.test_monitoring import watched as watched  # noqa: F401

T0 = datetime(2026, 11, 1, tzinfo=UTC)


# -- page facts --------------------------------------------------------------------------


def _files(pages: dict[str, dict[str, Any]], run: str = "r") -> RunFiles:
    files = RunFiles()
    for n, (path, extra) in enumerate(pages.items()):
        lines = extra.pop("lines", [f"Body of {path}."])
        extra.setdefault("metadata_source", "stored_html")
        st = stamp(CLIENT, path, f"Page {path}", run, **extra)
        files.stamps[st.url] = st
        files.chunks[st.url] = [chunk(st, f"{run}{n}", "\n".join(lines), 0, ["Page"])]
    return files


def _by_path(diff: Any) -> dict[str, PageChange]:
    return {c.url.removeprefix(CLIENT): c for c in diff.changed}


def test_seo_facts_are_compared_from_the_stamps() -> None:
    faq = SchemaFacts(types=["FAQPage", "Organization"], offers=["Basic: 25 USD"])
    old = _files(
        {
            "/a": {"schema_org": faq},
            "/b": {"robots_meta": "index, follow", "canonical": CLIENT + "/b"},
            "/c": {"schema_org": SchemaFacts(types=["Product"])},
            "/d": {"ctas": [Cta(text="Read more", url="/x", strength="soft")]},
            "/e": {},
        }
    )
    new = _files(
        {
            "/a": {"schema_org": SchemaFacts(types=["Organization"], offers=["Basic: 19 USD"])},
            "/b": {"robots_meta": "noindex, follow", "canonical": CLIENT + "/other"},
            "/c": {"schema_org": SchemaFacts(types=["Product"], unreadable_blocks=1)},
            "/d": {"ctas": [Cta(text="Book a demo", url="/demo", strength="strong")]},
            "/e": {},
        },
        "s",
    )
    pages = _by_path(diff_runs(old, new))
    assert pages["/a"].schema_removed == ["FAQPage"]
    assert [(p.before, p.after) for p in pages["/a"].prices] == [("25 USD", "19 USD")]
    assert pages["/b"].noindex == "added" and pages["/b"].canonical_moved
    assert "now noindex" in pages["/b"].seo_regressions
    assert pages["/c"].schema_broken and not pages["/c"].schema_removed
    assert pages["/d"].ctas_added == ["Book a demo"] and pages["/d"].strong_ctas_before == 0
    assert "/e" not in pages, "nothing changed on it"


def test_template_ctas_and_unrecorded_schema_are_not_page_changes() -> None:
    # the older scrape records no structured data on any page: additions are not reported
    old = _files({f"/p{i}": {} for i in range(4)})
    shared = [Cta(text="Get a quote", url="/quote", strength="strong")]
    new = _files(
        {
            f"/p{i}": {"ctas": shared, "schema_org": SchemaFacts(types=["Product"])}
            for i in range(4)
        },
        "s",
    )
    result = diff_runs(old, new)
    assert result.changed == []
    assert any("structured data" in n for n in result.notes)
    # a call to action added on every page is reported once, for the website
    [cta] = [t for t in result.template if t.line.startswith("Call to action")]
    assert (cta.line, cta.added_on) == ("Call to action: Get a quote", 4)
    rival = SiteDiff(website="rival.com", name="Rival", role="competitor", template_changes=[cta])
    [alert] = _seo_alerts(rival)
    assert alert.kind == "competitor_intent_shift" and "on 4 page(s)" in alert.detail


def test_pages_not_read_from_stored_html_are_not_compared() -> None:
    old = _files({"/a": {"schema_org": SchemaFacts(types=["FAQPage"])}})
    new = _files({"/a": {"metadata_source": "missing_evidence"}}, "s")
    assert diff_runs(old, new).changed == []


def _inventory(folder: Path, run: str, missing: int) -> None:
    images = [{"alt_status": "missing"}] * missing + [{"alt_status": "present"}]
    page = {"url": CLIENT + "/a", "status": "inventoried", "images": images}
    (folder / f"{run}.inventory.jsonl").write_text(json.dumps(page) + "\n", encoding="utf-8")


def test_images_without_alt_need_an_inventory_in_both_scrapes(tmp_path: Path) -> None:
    old, new = _files({"/a": {}}), _files({"/a": {}}, "s")
    _inventory(tmp_path, "r", 0)
    old.images = load_run_files(tmp_path, "r").images
    assert diff_runs(old, new).notes == [
        "images: only one of the two scrapes has an inventory, so alt text was not compared"
    ]
    _inventory(tmp_path, "s", 4)
    new.images = load_run_files(tmp_path, "s").images
    page = _by_path(diff_runs(old, new))["/a"]
    assert page.images_missing_alt == (0, 4)
    assert "images without alt text 0 → 4" in page.seo_regressions


def _diff(role: str, changes: list[PageChange], **kw: Any) -> SiteDiff:
    return SiteDiff(
        website="example.com" if role == "client" else "rival.com",
        name="Client" if role == "client" else "Rival",
        role=role,
        changed_pages=changes,
        **kw,
    )


def test_seo_alerts() -> None:
    client = _diff(
        "client",
        [
            PageChange(url=CLIENT + "/a", drift=0.1, noindex="added"),
            PageChange(url=CLIENT + "/b", drift=0.1, schema_removed=["FAQPage"]),
        ],
    )
    [alert] = _seo_alerts(client)
    assert alert.kind == "client_seo_regression" and alert.severity == "high"
    assert "now noindex: 1 page(s)" in alert.detail
    soft = _diff("client", [PageChange(url=CLIENT + "/b", drift=0.1, schema_removed=["FAQPage"])])
    assert _seo_alerts(soft)[0].severity == "medium"
    rival = _diff(
        "competitor",
        [
            PageChange(url=RIVAL + "/p", drift=0.1, schema_added=["Product", "WebPage"]),
            PageChange(url=RIVAL + "/q", drift=0.1, ctas_added=["Buy now"], strong_ctas_before=0),
            PageChange(url=RIVAL + "/r", drift=0.1, ctas_added=["Buy now"], strong_ctas_before=2),
        ],
    )
    kinds = {a.kind: a for a in _seo_alerts(rival)}
    assert set(kinds) == {"competitor_schema_added", "competitor_intent_shift"}
    assert kinds["competitor_intent_shift"].urls == [RIVAL + "/q"]


# -- rankings ----------------------------------------------------------------------------


def _row(keyword: str, position: int, volume: int = 500, **kw: Any) -> KeywordRow:
    return KeywordRow(keyword=keyword, position=position, volume=volume, **kw)


def _snap(
    when: datetime, client: list[KeywordRow], rival: list[KeywordRow], **kw: Any
) -> ResearchSnapshot:
    limit = kw.pop("limit", None)
    return ResearchSnapshot(
        snapshot_id=when.strftime("research-%Y%m%d-%H%M%S-000"),
        project_id="p",
        created_at=when,
        database=kw.pop("database", "us"),
        websites=[
            WebsiteResearch(
                website="example.com",
                name="Client",
                role="client",
                source="csv_import",
                pulled_at=when,
                keywords=client,
                keyword_limit=limit,
            ),
            WebsiteResearch(
                website="rival.com",
                name="Rival",
                role="competitor",
                source="csv_import",
                pulled_at=when,
                keywords=rival,
            ),
        ],
        **kw,
    )


def test_ranking_drops_and_competitor_gains() -> None:
    old = _snap(
        T0,
        [
            _row("term life", 3, 2000, url=CLIENT + "/term"),
            _row("whole life", 8, url=CLIENT + "/whole"),
            _row("annuities", 12),
            _row("tiny", 4, 10),
            _row("buy term life", 5, 10, intent="transactional"),
            _row("pet cover", 30),
        ],
        [_row("term life", 6), _row("whole life", 2)],
    )
    new = _snap(
        T0 + timedelta(days=30),
        [
            _row("term life", 14, 2000, url=CLIENT + "/term"),
            _row("whole life", 9, url=CLIENT + "/whole-2"),
            _row("annuities", 18),
            _row("tiny", 40, 10),
            _row("pet cover", 60),
        ],
        [_row("term life", 2, url=RIVAL + "/term"), _row("whole life", 1)],
    )
    page = PageChange(
        url=CLIENT + "/term/",
        drift=0.4,
        head_changes={"h1": {"before": "a", "after": "b"}},
        words_before=1200,
        words_after=800,
    )
    scrape = _diff(
        "client", [page], baseline_at=T0 + timedelta(days=5), latest_at=T0 + timedelta(days=12)
    )
    signals = SearchSignals()
    rank_signals(signals, old, new, [scrape], min_volume=50)
    rows = {(r.role, r.keyword): r for r in signals.rankings}
    assert set(rows) == {
        ("client", "term life"),
        ("client", "annuities"),
        ("client", "buy term life"),
        ("competitor", "term life"),
    }, "tiny is under the volume floor; whole life moved 1 place; pet cover was outside the top 20"
    term = rows[("client", "term life")]
    assert term.page_change == "H1 rewritten, words 1,200 → 800"
    assert rows[("client", "buy term life")].after is None
    assert not rows[("client", "buy term life")].uncertain, "the list is complete"
    gain = rows[("competitor", "term life")]
    assert gain.client_position == 14
    alerts = {a.kind: a for a in _signal_alerts(signals)}
    assert alerts["ranking_drop"].severity == "high"
    assert "not proof of the cause" in alerts["ranking_drop"].detail
    assert alerts["competitor_ranking_gain"].severity == "medium", "the client is not in the top 10"


def test_a_page_change_outside_the_ranking_window_is_not_named() -> None:
    old = _snap(T0, [_row("term life", 3, url=CLIENT + "/term")], [])
    new = _snap(T0 + timedelta(days=30), [_row("term life", 14, url=CLIENT + "/term")], [])
    page = PageChange(url=CLIENT + "/term", drift=0.4, head_changes={"h1": {}})
    early = _diff("client", [page], baseline_at=T0 - timedelta(days=20), latest_at=T0)
    signals = SearchSignals()
    rank_signals(signals, old, new, [early], min_volume=0)
    assert signals.rankings[0].page_change == ""


def test_a_cut_list_marks_missing_keywords_uncertain_and_swaps_are_named() -> None:
    old = _snap(
        T0, [_row("term life", 3, url=CLIENT + "/a"), _row("whole", 2, url=CLIENT + "/w")], []
    )
    new = _snap(
        T0 + timedelta(days=7), [_row("whole", 12, url=CLIENT + "/w2"), _row("x", 1)], [], limit=2
    )
    signals = SearchSignals()
    rank_signals(signals, old, new, [], min_volume=0)
    rows = {r.keyword: r for r in signals.rankings}
    assert rows["term life"].uncertain and rows["term life"].after is None
    assert rows["whole"].page_change == f"Google now ranks {CLIENT}/w2 instead"
    assert _signal_alerts(signals)[0].severity == "high", "whole fell from #2 out of the top 10"
    only = SearchSignals(rankings=[rows["term life"]])
    assert _signal_alerts(only)[0].severity == "medium", "an uncertain drop is not high"


def test_nothing_is_compared_across_databases_twice_or_without_a_baseline() -> None:
    a = _snap(T0, [_row("k", 3)], [])
    signals = SearchSignals()
    rank_signals(signals, None, a, [], min_volume=0)
    assert signals.research_note.startswith("first search data") and signals.research_after
    rank_signals(signals := SearchSignals(), a, a, [], min_volume=0)
    assert signals.research_note == "no new search data since the last check"
    uk = _snap(T0 + timedelta(days=1), [_row("k", 30)], [], database="uk")
    rank_signals(signals := SearchSignals(), a, uk, [], min_volume=0)
    assert "different Semrush databases" in signals.research_note and not signals.rankings


def test_dummy_data_is_marked_on_every_alert() -> None:
    old = _snap(T0, [_row("k", 3)], [], notes=["DUMMY DATA from the Semrush stand-in: ..."])
    new = _snap(T0 + timedelta(days=1), [_row("k", 30)], [])
    signals = SearchSignals()
    rank_signals(signals, old, new, [], min_volume=0)
    assert signals.dummy
    assert _signal_alerts(signals)[0].title.startswith("[dummy data] ")


# -- links -------------------------------------------------------------------------------


def _links(when: datetime, client: dict[str, Any], rival: dict[str, Any]) -> BacklinkSnapshot:
    def site(key: str, role: str, data: dict[str, Any]) -> WebsiteBacklinks:
        return WebsiteBacklinks(
            website=key,
            name=key,
            role=role,
            source=data.get("source", "csv_import"),
            pulled_at=when,
            domain_rating=data.get("dr"),
            stats=BacklinkStats(live_refdomains=data.get("rd", 0)),
            refdomains=data.get("domains", []),
        )

    return BacklinkSnapshot(
        snapshot_id=when.strftime("backlinks-%Y%m%d-%H%M%S-000"),
        project_id="p",
        created_at=when,
        websites=[site("example.com", "client", client), site("rival.com", "competitor", rival)],
    )


def test_lost_links_and_authority_shifts() -> None:
    domains = [RefDomain(domain=f"site{i}.com", domain_rating=20.0 + i * 10) for i in range(7)]
    spam = RefDomain(domain="spam.biz", domain_rating=90.0, is_spam=True)
    keep = RefDomain(domain="keep.com", domain_rating=5.0)
    old = _links(T0, {"dr": 40, "rd": 9, "domains": [*domains, spam, keep]}, {"dr": 38, "rd": 50})
    new = _links(
        T0 + timedelta(days=30),
        {"dr": 33, "rd": 1, "domains": [keep]},
        {"dr": 45, "rd": 80, "source": "ahrefs_api"},
    )
    signals = SearchSignals()
    link_signals(signals, old, new)
    client, rival = signals.links
    assert "spam.biz" not in client.lost and len(client.lost) == 7
    assert client.lost_strong[0] == "site6.com (DR 80)"
    assert rival.note.startswith("an export compared with an API pull")
    alerts = {(a.kind, a.website): a for a in _signal_alerts(signals)}
    assert alerts[("backlinks_lost", "example.com")].severity == "high"
    assert alerts[("authority_shift", "example.com")].severity == "high", "DR fell 7 points"
    overtook = alerts[("authority_shift", "rival.com")]
    assert overtook.severity == "high" and "now above the client" in overtook.title


def test_describe_change_reads_in_plain_words() -> None:
    change = PageChange(url="u", drift=0.5, meaning=0.5, headings_removed=["a", "b"])
    assert describe_change(change) == "2 heading(s) removed, topic changed (meaning 0.50)"


# -- a check end to end ------------------------------------------------------------------


def test_checks_read_new_snapshots_once_and_say_what_they_compared(watched: World) -> None:
    research = research_store(watched.store)
    first = _snap(T0, [_row("term life insurance", 2)], [])
    research.save(first.model_copy(update={"project_id": watched.project_id}))
    check = run_project_check(watched.store, watched.access, watched.project_id)[0]
    assert check.signals is not None and check.signals.research_note.startswith("first search")
    _next_scrapes(watched)
    later = _snap(T0 + timedelta(days=20), [_row("term life insurance", 25)], [])
    research.save(later.model_copy(update={"project_id": watched.project_id}))
    second = run_project_check(watched.store, watched.access, watched.project_id)[0]
    assert second.signals is not None
    assert second.signals.research_before == first.snapshot_id
    assert [a.kind for a in second.alerts if a.kind == "ranking_drop"] == ["ranking_drop"]
    assert second.stats["ranking_drops"] == 1
    third = run_project_check(watched.store, watched.access, watched.project_id)[0]
    assert third.signals is not None
    assert third.signals.research_note == "no new search data since the last check"
    assert not any(a.kind == "ranking_drop" for a in third.alerts)
    assert "Rankings" in [t.name for t in check_tables(second)]


def test_a_first_check_compares_the_two_newest_backlink_snapshots(watched: World) -> None:
    from src.modules.backlinks.service import backlinks_store

    store = backlinks_store(watched.store)
    domains = [RefDomain(domain=f"d{i}.com", domain_rating=60.0) for i in range(6)]
    for when, client in (
        (T0, {"dr": 40, "domains": domains}),
        (T0 + timedelta(days=9), {"dr": 40, "domains": domains[:1]}),
    ):
        # (an empty list means "not pulled", never "every domain lost")
        snap = _links(when, client, {"dr": 30})
        store.save(snap.model_copy(update={"project_id": watched.project_id}))
    check = run_project_check(watched.store, watched.access, watched.project_id)[0]
    assert check.signals is not None and check.signals.backlinks_before is not None
    assert [a.kind for a in check.alerts if a.kind == "backlinks_lost"] == ["backlinks_lost"]


# -- digest ------------------------------------------------------------------------------


def _check(alerts: list[Alert], **kw: Any) -> MonitorCheck:
    return MonitorCheck(
        check_id="check-20261101-000000-000",
        project_id="p",
        created_at=T0,
        websites=[],
        alerts=alerts,
        **kw,
    )


def _alert(n: int, severity: str = "high") -> Alert:
    return Alert(
        alert_id=f"al-{n}",
        severity=severity,
        kind="price_change",
        website="rival.com",
        title=f"Prices changed {n}",
        detail="$25 → $19",
        urls=[RIVAL + f"/p{n}"],
    )


def test_digest_sends_new_alerts_once_and_hides_confidential_details(tmp_path: Path) -> None:
    sent: list[EmailMessage] = []
    notifier = Notifier(outbox=tmp_path / "outbox", mailer=sent.append)
    check = _check([_alert(1), _alert(2, "medium"), _alert(3, "low")])
    book = DigestBook(to=["a@example.com", "b@example.com"])
    book, note = send_digest(
        check,
        "high",
        book,
        notifier,
        project_name="Acme\r\nBcc: x@evil.com",
        confidential=False,
        now=T0,
    )
    assert len(sent) == 2 and "emailed a***@example.com" in note
    assert "\n" not in sent[0]["Subject"] and "Prices changed 1" in sent[0].get_content()
    assert "Prices changed 2" not in sent[0].get_content(), "medium is below the level"
    again, note = send_digest(
        check, "high", book, notifier, project_name="A", confidential=False, now=T0
    )
    assert note == "" and len(sent) == 2, "already emailed"
    later = T0 + timedelta(days=31)
    assert due_alerts(check, "high", again.sent, later) == check.alerts[:1]
    _subject, body = compose("A", check, check.alerts[:2], confidential=True, link="L")
    assert "Prices changed" not in body and RIVAL not in body and "price change (1)" in body


def test_a_failed_send_goes_to_the_outbox(tmp_path: Path) -> None:
    def broken(_message: EmailMessage) -> None:
        raise OSError("smtp down")

    notifier = Notifier(outbox=tmp_path / "outbox", mailer=broken)
    _book, note = send_digest(
        _check([_alert(1)]),
        "medium",
        DigestBook(to=["a@example.com"]),
        notifier,
        project_name="A",
        confidential=False,
    )
    assert "could not be sent" in note and list((tmp_path / "outbox").glob("*.eml"))


def test_a_check_emails_its_digest_when_asked(watched: World, tmp_path: Path) -> None:
    store = monitor_store(watched.store)
    run_project_check(watched.store, watched.access, watched.project_id)
    store.set_digest(watched.project_id, DigestBook(to=["team@example.com"]))
    store.set_watch(
        watched.project_id,
        store.watch(watched.project_id).model_copy(update={"digest_level": "high"}),
    )
    _next_scrapes(watched)
    sent: list[EmailMessage] = []
    notifier = Notifier(outbox=tmp_path / "o", mailer=sent.append, base_url="https://app.x")
    check = run_project_check(watched.store, watched.access, watched.project_id, notifier=notifier)[
        0
    ]
    assert any(a.severity == "high" for a in check.alerts)
    assert len(sent) == 1 and "?tab=monitoring" in sent[0].get_content()
    watch = store.watch(watched.project_id)
    assert watch.last_digest_note.startswith("emailed") or "alert(s)" in watch.last_digest_note
    assert watch.digest_to == ["t***@example.com"]
    raw = (store.folder(watched.project_id) / "watch.json").read_text("utf-8")
    assert "team@example.com" not in raw and "digest_to" not in raw


# -- API ---------------------------------------------------------------------------------


def test_api_pins_only_project_pages_and_keeps_digest_addresses_hidden(
    watched: World, tmp_path: Path
) -> None:
    signer = TokenSigner(b"k" * 32)
    runner = ScrapeRunner(watched.store)
    app = create_app(tmp_path, projects=(watched.store, runner, signer), knowledge=watched.access)
    client = TestClient(app, base_url="http://127.0.0.1")
    pid = watched.project_id
    token = {**WRITE, "X-Project-Token": signer.issue(pid)[0]}
    url = f"/api/projects/{pid}/monitoring/watch"
    base = {"auto_check": True, "rescrape_every_days": 0, "push_threshold": 3}
    bad = client.post(
        url, json={**base, "core_urls": [RIVAL + "/x", "https://elsewhere.org/a"]}, headers=token
    )
    assert bad.status_code == 422 and "elsewhere.org" in bad.json()["detail"]
    ok = client.post(
        url,
        json={
            **base,
            "core_urls": [CLIENT + "/term", CLIENT + "/term/", RIVAL + "/pet", " "],
            "digest_level": "medium",
            "digest_to": ["Team@Example.com", "team@example.com"],
            "rank_min_volume": 200,
        },
        headers=token,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["core_urls"] == [CLIENT + "/term", RIVAL + "/pet"]
    assert body["digest_to"] == ["T***@Example.com"] and body["rank_min_volume"] == 200
    assert client.post(url, json={**base, "digest_to": ["nope"]}, headers=token).status_code == 422
    assert client.post(url, json=base, headers=WRITE).status_code == 401


def test_the_watcher_keeps_the_digest_note_of_its_automatic_check(watched: World) -> None:
    from src.modules.monitoring.service import MonitorWatcher

    store = monitor_store(watched.store)
    store.set_digest(watched.project_id, DigestBook(to=["team@example.com"]))
    store.set_watch(
        watched.project_id,
        store.watch(watched.project_id).model_copy(update={"digest_level": "medium"}),
    )
    _next_scrapes(watched)  # no check yet: the watcher's first pass runs one
    done = MonitorWatcher(watched.store, watched.access, None).tick()
    assert done["checked"] == [watched.project_id]
    watch = store.watch(watched.project_id)
    assert watch.last_auto_check_at is not None and watch.last_digest_note, watch
