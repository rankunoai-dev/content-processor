"""The Semrush stand-in: dummy data in Semrush's formats, read back by the module's own code."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.modules.content_processor.projects.store import ProjectStore
from src.modules.research.competitors import read_competitors_csv
from src.modules.research.importer import keyword_rows, read_positions_csv
from src.modules.research.semrush import (
    Pacer,
    SemrushClient,
    SemrushError,
    UnitBudget,
    UnitLog,
    parse_rows,
)
from src.modules.research.service import ResearchSettings, address_refusal, pull_project
from src.modules.research.standin import (
    DUMMY_NOTE,
    Dataset,
    ThemeSeed,
    make_dataset,
    serve,
)

SITES = [("johnhancock.com", "client"), ("metlife.com", "competitor")]
SEEDS = [
    ThemeSeed(
        "Monthly distributions",
        ["monthly distributions", "funds", "declare"],
        ["https://www.johnhancock.com/news/distributions-1", "https://www.johnhancock.com/news/d2"],
    ),
    ThemeSeed("No client pages", ["nothing"], []),
]


def _dataset(period: int = 0) -> Dataset:
    return make_dataset("p1", SITES, SEEDS, brand="john hancock", period=period)


def test_only_semrush_or_this_computer_may_receive_the_key() -> None:
    assert address_refusal("https://api.semrush.com/") is None
    assert address_refusal("http://127.0.0.1:8790/") is None
    assert address_refusal("http://localhost:8790/") is None
    for bad in (
        "http://api.semrush.com/",  # not encrypted
        "https://api.semrush.com.example.org/",
        "https://example.org/",
        "ftp://127.0.0.1/",
    ):
        assert "refused" in (address_refusal(bad) or ""), bad


def test_the_dataset_is_deterministic_and_uses_the_themes() -> None:
    one, two = _dataset(), _dataset()
    assert [k.keyword for k in one.keywords] == [k.keyword for k in two.keywords]
    assert [k.positions for k in one.keywords] == [k.positions for k in two.keywords]
    brand = [k for k in one.keywords if k.kind == "brand"]
    assert {k.keyword for k in brand} == {
        "john hancock monthly distributions",
        "john hancock funds",
        "john hancock declare",
    }, "three terms of the theme with pages; none from the theme without"
    pages = set(SEEDS[0].pages)
    assert all(k.urls["johnhancock.com"] in pages for k in brand)
    market = [k for k in one.keywords if k.kind == "market" and "johnhancock.com" in k.urls]
    assert market and all(k.urls["johnhancock.com"] in pages for k in market), "real pages only"
    for k in one.keywords:
        assert 1 <= len(k.serp) <= 10 and all(1 <= p <= 100 for p in k.positions.values())


def test_the_exports_read_back_with_the_module_importers() -> None:
    data = _dataset()
    rows = read_positions_csv(data.positions_csv("johnhancock.com"))
    assert len(rows) == len(data.ranked("johnhancock.com"))
    first = rows[0]
    assert first.keyword and first.position >= 1 and first.volume and first.url.startswith("https")
    suggestions = read_competitors_csv(data.competitors_csv("johnhancock.com"), datetime.now(UTC))
    assert suggestions is not None and suggestions[0].common_keywords > 0
    assert "metlife.com" in {s.domain for s in suggestions}


def test_the_api_answers_read_back_like_semrush() -> None:
    data = _dataset()
    text = data.answer(
        {
            "type": "domain_organic",
            "key": "k",
            "domain": "www.johnhancock.com",
            "display_limit": "3",
            "export_columns": "Ph,Po,Pp,Nq,Cp,Ur,Tr,Co",
        }
    )
    assert text.splitlines()[0] == (
        "Keyword;Position;Previous Position;Search Volume;CPC;Url;Traffic (%);Competition"
    )
    assert len(keyword_rows(parse_rows(text))) == 3
    overview = parse_rows(
        data.answer(
            {"type": "domain_ranks", "key": "k", "domain": "metlife.com", "export_columns": "Or,Ot"}
        )
    )
    assert int(overview[0]["Organic Keywords"]) > len(data.ranked("metlife.com"))
    phrase = data.keywords[0].keyword
    serp = parse_rows(data.answer({"type": "phrase_organic", "key": "k", "phrase": phrase}))
    assert serp and {"Domain", "Url"} <= set(serp[0])
    assert data.answer({"type": "domain_ranks", "key": "", "domain": "x"}).startswith("ERROR 120")
    assert parse_rows(data.answer({"type": "domain_ranks", "key": "k", "domain": "nope.com"})) == []
    with pytest.raises(SemrushError):
        parse_rows(data.answer({"type": "backlinks", "key": "k"}))


def test_a_later_month_moves_positions_for_movers() -> None:
    before, after = _dataset(), _dataset(period=1)
    moved = [
        (k.keyword, d)
        for k, old in zip(after.keywords, before.keywords, strict=True)
        for d, p in k.positions.items()
        if old.positions[d] != p
    ]
    assert moved
    keyword, domain = moved[0]
    old = next(k for k in before.keywords if k.keyword == keyword)
    new = next(k for k in after.keywords if k.keyword == keyword)
    assert new.previous[domain] == old.positions[domain]


def test_saved_and_loaded_the_same(tmp_path: Path) -> None:
    data = _dataset()
    data.save(tmp_path / "dataset.json")
    again = Dataset.load(tmp_path / "dataset.json")
    assert again.answer({"type": "domain_organic", "key": "k", "domain": "metlife.com"}) == (
        data.answer({"type": "domain_organic", "key": "k", "domain": "metlife.com"})
    )
    assert again.note == DUMMY_NOTE


def test_the_server_answers_the_real_client_and_asks_it_to_slow_down(tmp_path: Path) -> None:
    server = serve(_dataset(), 0, slow_down_every=2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        log = UnitLog(tmp_path)
        budget = UnitBudget(log, enabled=True, monthly_units=10_000, pull_units=5_000)
        client = SemrushClient(
            "dummy", budget, log, "p1", base_url=f"http://127.0.0.1:{port}/", pacer=Pacer(0.0)
        )
        overview = client.domain_overview("johnhancock.com", "us")
        rows = client.domain_organic("johnhancock.com", "us", 5)  # the 2nd call: HTTP 429
        assert overview and len(rows) == 5 and client.retries == 1
        assert budget.spent == 10 + 5 * 10, "a refused call costs nothing"
        client.close()
    finally:
        server.shutdown()
        server.server_close()


def test_a_pull_to_another_address_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    settings = ResearchSettings(
        enabled=True, api_key="real-key", monthly_units=1000, base_url="https://example.org/"
    )
    with pytest.raises(SemrushError, match="refused"):
        pull_project(ProjectStore(tmp_path), None, "p1", settings)  # type: ignore[arg-type]


def test_the_make_command_writes_the_dataset_and_the_exports(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    from src.modules.content_processor.cli import main
    from src.modules.content_processor.projects.store import SiteSpec

    store = ProjectStore(tmp_path / "data")
    project = store.create(
        name="Stand-in",
        owner_name="Owner",
        client_name="Example Life",
        password="a long enough password",
        client_site=SiteSpec(name="Example Life", website="https://www.example-life.com"),
        competitors=[SiteSpec(name="Rival", website="https://rival.example")],
    )
    out = tmp_path / "dummy"
    code = main(
        [
            "research-standin",
            "make",
            "--project",
            project.project_id,
            "--out",
            str(out),
            "--app-data",
            str(tmp_path / "data"),
        ]
    )
    assert code == 0
    assert b"DUMMY DATA" in capsysbinary.readouterr().out
    data = Dataset.load(out / "dataset.json")
    assert {w.domain: w.role for w in data.websites} == {
        "example-life.com": "client",
        "rival.example": "competitor",
    }
    assert (out / "example-life.com-organic-positions.csv").is_file()
    assert (out / "example-life.com-organic-competitors.csv").is_file()
    assert (out / "rival.example-organic-positions.csv").is_file()


def test_search_console_export_and_second_pages_read_back() -> None:
    from src.modules.research.gsc import read_gsc_export

    data = _dataset()
    split = [k for k in data.keywords if k.also.get("johnhancock.com")]
    assert split, "some keywords rank with two client pages"
    rows = {r.keyword: r for r in read_positions_csv(data.positions_csv("johnhancock.com"))}
    assert rows[split[0].keyword].other_urls, "the second page comes back as an other URL"
    snap = read_gsc_export(
        data.search_console_zip("johnhancock.com"),
        project_id="p1",
        website="johnhancock.com",
        now=datetime.now(UTC),
    )
    assert snap.period == "Last 3 months" and snap.queries and snap.pages and snap.pairs
    assert {k.keyword for k in split} <= {p.query for p in snap.pairs}
    assert len({p.query for p in snap.pairs}) == len(snap.queries), "every query with its pages"
