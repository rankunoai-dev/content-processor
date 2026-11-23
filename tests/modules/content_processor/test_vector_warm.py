"""Warming the search cache: the first search after a restart reads from memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.modules.content_processor.knowledge import vector_store
from src.modules.content_processor.knowledge.federated import Space
from src.modules.content_processor.knowledge.indexing import embed_hash
from src.modules.content_processor.knowledge.vector_store import (
    ChunkRow,
    SqliteVectorStore,
    VectorStore,
    cached_bytes,
    clear_vector_cache,
)
from src.modules.content_processor.knowledge.warm import (
    freshest_spaces,
    warm_in_background,
    warm_spaces,
)
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture


def chunk(cid: str, text: str) -> ChunkRow:
    return ChunkRow(
        chunk_id=cid,
        run_id="r1",
        url=f"https://x.example/{cid}",
        page_key=cid,
        order=0,
        kind="text",
        heading_path=["Page"],
        context_header="Page",
        verbatim_text=text,
        embed_text=text,
        embed_hash=embed_hash(text + cid),
        block_ids=["b1"],
        evidence_ids=["e1"],
        embedding=[1.0, float(len(cid)), 0.5, 0.0],
    )


def make_store(tmp_path: Path, name: str, rows: list[ChunkRow]) -> SqliteVectorStore:
    store = SqliteVectorStore(tmp_path / name / "vectors.sqlite")
    store.set_meta({"model": "m", "dims": "4"})
    store.upsert_chunks(rows)
    return store


def _space(name: str) -> Space:
    return Space(name, "p", "P", name, name.upper(), "client")


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    clear_vector_cache()


def _stores(tmp_path: Path, sizes: dict[str, int]) -> None:
    for name, n in sizes.items():
        make_store(
            tmp_path, name, [chunk(f"{name}{i}", f"Fund distribution {i}") for i in range(n)]
        ).close()


def _open(tmp_path: Path):  # type: ignore[no-untyped-def]
    def open_store(space: Space) -> VectorStore:
        return SqliteVectorStore(tmp_path / space.namespace / "vectors.sqlite")

    return open_store


def test_warming_loads_each_store_so_the_first_search_reads_no_vectors(tmp_path: Path) -> None:
    _stores(tmp_path, {"a": 5, "b": 3})
    report = warm_spaces([_space("a"), _space("b")], _open(tmp_path), limit_bytes=10**9)
    assert report.warmed == ["a", "b"] and report.rows == 8 and not report.full
    assert cached_bytes() > 0

    store = SqliteVectorStore(tmp_path / "a" / "vectors.sqlite")
    warmed = vector_store._MATRICES[store._key][1]
    assert store._load_matrix() is warmed, "the first search is served from memory"
    vector = store.vectors(["a0"])["a0"]
    assert store.vector_search(vector, 3, None)
    store.close()


def test_warming_stops_when_the_cache_is_full_and_never_evicts_what_it_loaded(
    tmp_path: Path,
) -> None:
    _stores(tmp_path, {"a": 4, "b": 4, "c": 4})
    warm_spaces([_space("a")], _open(tmp_path), limit_bytes=10**9)
    one = cached_bytes()
    clear_vector_cache()
    report = warm_spaces([_space("a"), _space("b"), _space("c")], _open(tmp_path), limit_bytes=one)
    assert report.warmed == ["a"] and report.full


def test_warming_with_no_cache_does_nothing(tmp_path: Path) -> None:
    _stores(tmp_path, {"a": 2})
    report = warm_spaces([_space("a")], _open(tmp_path), limit_bytes=0)
    assert report.warmed == [] and cached_bytes() == 0


def test_a_broken_store_is_skipped_and_the_rest_are_warmed(tmp_path: Path) -> None:
    _stores(tmp_path, {"b": 2})

    def open_store(space: Space) -> VectorStore:
        if space.namespace == "a":
            raise OSError("disk gone")
        return SqliteVectorStore(tmp_path / space.namespace / "vectors.sqlite")

    report = warm_spaces([_space("a"), _space("b")], open_store, limit_bytes=10**9)
    assert report.skipped == ["a"] and report.warmed == ["b"]


def test_warming_runs_on_a_background_thread(tmp_path: Path) -> None:
    _stores(tmp_path, {"a": 3})
    thread = warm_in_background(lambda: [_space("a")], _open(tmp_path), limit_bytes=10**9)
    thread.join(timeout=30)
    assert not thread.is_alive() and thread.daemon and cached_bytes() > 0


def test_freshest_spaces_lists_each_built_website_once_newest_first(world: World) -> None:
    from src.modules.content_processor.knowledge.federated import space_namespace

    spaces = freshest_spaces(world.store, lambda p, s: space_namespace("rankuno-dev", p, s))
    assert {s.site_id for s in spaces} == set(world.site_ids.values())
    assert len({s.namespace for s in spaces}) == len(spaces)
