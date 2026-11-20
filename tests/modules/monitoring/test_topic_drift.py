"""Topic drift: a changed page's old and new versions compared by meaning."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.modules.content_processor.analysis.semantic import VectorCache
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.monitoring import check as check_module
from src.modules.monitoring.check import _competitor_moves
from src.modules.monitoring.meaning import page_meaning
from src.modules.monitoring.models import PageChange, SiteDiff
from src.modules.monitoring.service import run_project_check
from src.modules.monitoring.store import check_tables
from tests.modules.analyst.conftest import RIVAL, World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.monitoring.test_monitoring import _client, _rival
from tests.modules.theming.test_theming import PET, _add, _catalog, _page

_TOPICS = (
    {"term", "life", "death", "premiums", "benefit", "years", "level", "coverage", "policy"},
    {"pet", "vet", "dogs", "cats", "wellness", "accidents", "illness", "bills"},
    {"plans", "month", "price", "adults", "start"},
)


class TopicEmbedder:
    """A meaning model in miniature: one dimension per topic, by the words a text uses."""

    model = "topic-test-model"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        self.calls.append(len(texts))
        if self.fail:
            return None
        out = []
        for text in texts:
            words = re.findall(r"[a-z]+", text.lower())
            out.append([sum(w in topic for w in words) + 0.01 for topic in _TOPICS])
        return out

    def close(self) -> None:
        """Nothing to release."""


LIFE = [
    "Term life insurance gives level premiums for a fixed term of years.",
    "The death benefit is paid to your family if you die during the term.",
    "Choose a policy length of ten, twenty or thirty years of coverage.",
]
LIFE_REWORDED = [
    "Level premiums and a fixed term of years: that is term life insurance.",
    "Your family receives the death benefit when you die within the term.",
    "Pick coverage for ten, twenty or thirty years with one policy.",
]
PETS = [
    "Pet insurance covers vet bills for dogs and cats after accidents.",
    "Illness and wellness plans keep your dogs and cats healthy at the vet.",
    "Claims for accidents and illness are paid within days of the vet visit.",
]


def test_meaning_scores_rewording_high_and_a_new_topic_low(tmp_path: Path) -> None:
    cache = VectorCache(tmp_path / "meaning.sqlite")
    embedder = TopicEmbedder()
    scores = page_meaning(
        {
            "/reworded": (LIFE, LIFE_REWORDED),
            "/moved": (LIFE, PETS),
            "/thin": (["Term life insurance."], LIFE),
        },
        embedder,
        cache,
    )
    assert scores["/reworded"] >= 0.95
    assert scores["/moved"] < 0.85
    assert "/thin" not in scores  # too little text on one side to judge
    first = sum(embedder.calls)
    assert page_meaning({"/moved": (LIFE, PETS)}, embedder, cache) == {"/moved": scores["/moved"]}
    assert sum(embedder.calls) == first  # every line came from the cache
    with pytest.raises(RuntimeError, match="failed"):
        fresh = [line + " Updated." for line in PETS]  # not in the cache: the model is asked
        page_meaning({"/x": (fresh, LIFE_REWORDED)}, TopicEmbedder(fail=True), cache)
    assert page_meaning({}, embedder, cache) == {}
    cache.close()


def _change(url: str, meaning: float | None, *, pillar: bool = False) -> PageChange:
    return PageChange(url=url, title=url, drift=1.0, meaning=meaning, is_pillar=pillar)


def test_topic_drift_alert_for_competitor_pages_below_the_threshold() -> None:
    site = SiteDiff(
        website="rival.com",
        name="Rival",
        role="competitor",
        changed_pages=[_change("/a", 0.97), _change("/b", 0.62), _change("/c", None)],
    )
    alerts = [a for a in _competitor_moves(site) if a.kind == "competitor_topic_drift"]
    assert len(alerts) == 1 and alerts[0].severity == "medium" and alerts[0].urls == ["/b"]
    assert "meaning 0.62 old vs new" in alerts[0].detail
    site.changed_pages.append(_change("/pillar", 0.4, pillar=True))
    alerts = [a for a in _competitor_moves(site) if a.kind == "competitor_topic_drift"]
    assert alerts[0].severity == "high" and alerts[0].urls == ["/pillar", "/b"]
    assert not [
        a for a in _competitor_moves(site, topic_drift=0.3) if a.kind == "competitor_topic_drift"
    ]


def _moved_rival(run_id: str) -> list[Any]:
    pages = _rival(run_id)
    pages[0] = _page(RIVAL, "/term", "Pet insurance plans", run_id, PET + "\n" + "\n".join(PETS), 1)
    return pages


def test_check_scores_meaning_with_the_index_model_and_alerts(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(world, "client", "run-c2", _client("run-c2"), 10, _catalog("run-c2"))
    _add(world, "competitor", "run-r2", _rival("run-r2"), 10)
    run_project_check(world.store, world.access, world.project_id)
    _add(world, "competitor", "run-r3", _moved_rival("run-r3"), 20)

    # the test world is indexed with the words-only stand-in: meaning is not scored
    words_only = run_project_check(world.store, world.access, world.project_id)[0]
    rival = next(d for d in words_only.websites if d.website == "rival.com")
    assert [c.meaning for c in rival.changed_pages] == [None]
    assert "words-only" in rival.meaning_note
    assert not [a for a in words_only.alerts if a.kind == "competitor_topic_drift"]

    # with a meaning model behind the index, the moved page is scored and raises an alert
    _add(world, "competitor", "run-r5", _moved_rival("run-r5"), 40)
    monkeypatch.setattr(check_module, "is_semantic", lambda _model: True)
    meaning_access = KnowledgeAccess(
        open_store=world.access.open_store, embedder_for=lambda _m: TopicEmbedder()
    )
    unchanged = run_project_check(world.store, meaning_access, world.project_id)[0]
    rival = next(d for d in unchanged.websites if d.website == "rival.com")
    assert rival.changed_pages == []  # r3 -> r5: the same text, nothing to score
    check_module_store = Path(str(world.store.root)) / "monitoring" / "meaning.sqlite"
    _add(world, "competitor", "run-r6", _rival("run-r6"), 50)  # back to life insurance
    moved = run_project_check(world.store, meaning_access, world.project_id)[0]
    rival = next(d for d in moved.websites if d.website == "rival.com")
    term = next(c for c in rival.changed_pages if c.url == RIVAL + "/term")
    assert term.meaning is not None and term.meaning < 0.85
    assert rival.meaning_model == "topic-test-model" and rival.meaning_note == ""
    drift = [a for a in moved.alerts if a.kind == "competitor_topic_drift"]
    assert drift and drift[0].urls == [RIVAL + "/term"]
    assert moved.stats["topic_drift_pages"] == 1
    assert check_module_store.is_file()
    rows = {r["URL"]: r for r in {t.name: t for t in check_tables(moved)}["Changed pages"].rows}
    assert rows[RIVAL + "/term"]["Meaning (old vs new)"] == term.meaning


def test_site_furniture_is_left_out_of_the_meaning_comparison() -> None:
    from src.modules.monitoring.diff import RunFiles, diff_runs, furniture
    from tests.modules.analyst.conftest import chunk, stamp

    footer = "Insurance products are issued by our company and its subsidiaries in each state."

    def scrape(run: str, first: str) -> RunFiles:
        files = RunFiles()
        for i in range(6):
            st = stamp(RIVAL, f"/p{i}", f"Page {i}", run)
            text = (first if i == 0 else f"Unique page {i} text about plans.") + "\n" + footer
            files.stamps[st.url], files.seen = st, files.seen | {st.url}
            files.chunks[st.url] = [chunk(st, f"{run}-{i}", text, 0, [])]
        return files

    old, new = scrape("a", LIFE[0]), scrape("b", PETS[0])
    assert footer.casefold().rstrip(".") in furniture(new)
    result = diff_runs(old, new)
    assert [c.url for c in result.changed] == [RIVAL + "/p0"]
    assert result.texts[RIVAL + "/p0"] == ([LIFE[0]], [PETS[0]])  # the footer is not compared
