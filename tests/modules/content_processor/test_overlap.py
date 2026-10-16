"""I2 mode B: shingles, MinHash + LSH candidates, exact verification, groups, R2a/R2b."""

from __future__ import annotations

from itertools import combinations

import pytest

from src.modules.content_processor.analysis.overlap import (
    OverlapConfig,
    choose_bands,
    docs_from_records,
    find_overlaps,
    r2a_table,
    r2b_table,
    shingle_document,
)
from src.modules.content_processor.enums import ContentLayer, UrlBadge
from tests.modules.content_processor.l5_factory import block, record, words

BASE = "https://example.com/"


def _pairs(result) -> dict[tuple[str, str], float]:
    return {(p.a.url, p.b.url): round(p.jaccard, 4) for p in result.pairs}


def test_shingles_hash_word_ngrams_and_keep_spans():
    text = "The Quick, brown fox jumps"
    seq, spans = shingle_document(text, 3)
    assert len(seq) == 3
    assert [text[s:e] for s, e in spans] == [
        "The Quick, brown",
        "Quick, brown fox",
        "brown fox jumps",
    ]
    # Case and punctuation do not change a shingle.
    assert shingle_document("the quick brown", 3)[0][0] == seq[0]
    short, short_spans = shingle_document("two words", 5)
    assert len(short) == 1 and short_spans == [(0, 9)]
    assert shingle_document("  ...  ", 5) == ([], [])


@pytest.mark.parametrize(("floor", "bands", "rows"), [(0.6, 42, 3), (0.2, 128, 1)])
def test_band_choice_meets_the_recall_target(floor, bands, rows):
    assert choose_bands(128, floor) == (bands, rows)
    b, r = choose_bands(128, floor)
    assert 1 - (1 - floor**r) ** b >= 0.99


def test_config_floor_defaults():
    assert OverlapConfig().floor() == pytest.approx(0.6)
    assert OverlapConfig(threshold=0.1).floor() == 0.05
    assert OverlapConfig(metric="containment").floor() == 0.2
    assert OverlapConfig(candidate_floor=0.33).floor() == 0.33
    with pytest.raises(ValueError, match="threshold"):
        find_overlaps([], OverlapConfig(threshold=0))


def test_near_duplicates_are_found_and_unrelated_pages_are_not():
    base = words(1, 400)
    near = base.replace("w1", "zz", 3)  # a few edits
    records = [
        record(BASE + "a", [base]),
        record(BASE + "b", [near]),
        record(BASE + "c", [words(2, 400)]),
    ]
    result = find_overlaps(records)
    assert list(_pairs(result)) == [(BASE + "a", BASE + "b")]
    pair = result.pairs[0]
    assert pair.basis == "lsh_verified" and pair.jaccard >= 0.8
    assert [d.url for d in result.groups[0]] == [BASE + "a", BASE + "b"]
    assert result.summary()["candidates_checked"] >= 1


def test_exact_duplicates_collapse_and_twins_pair_with_every_match():
    text, near = words(5, 300), words(5, 300) + " extra tail words here"
    records = [
        record(BASE + "a1", [text]),
        record(BASE + "a2", [text]),
        record(BASE + "b1", [near]),
        record(BASE + "b2", [near]),
    ]
    result = find_overlaps(records)
    got = _pairs(result)
    assert got[(BASE + "a1", BASE + "a2")] == 1.0
    assert got[(BASE + "b1", BASE + "b2")] == 1.0
    cross = {k for k in got if k[0].startswith(BASE + "a") and k[1].startswith(BASE + "b")}
    assert cross == {(BASE + x, BASE + y) for x in ("a1", "a2") for y in ("b1", "b2")}
    assert {p.basis for p in result.pairs} == {"exact_duplicate", "lsh_verified"}
    assert len(result.groups) == 1 and len(result.groups[0]) == 4


def test_containment_mode_finds_a_page_inside_a_longer_page():
    inner = words(7, 200)
    outer = inner + " " + words(8, 300)
    records = [record(BASE + "long", [outer]), record(BASE + "short", [inner])]
    assert find_overlaps(records).pairs == []  # Jaccard ~0.4 < 80%
    result = find_overlaps(records, OverlapConfig(metric="containment"))
    (pair,) = result.pairs
    assert (pair.a.url, pair.b.url) == (BASE + "long", BASE + "short")
    assert pair.containment_b_in_a == 1.0 and pair.containment_a_in_b < 0.5
    row = r2b_table(result).rows[0]
    assert row["containment_b_in_a_pct"] == 100.0 and row["jaccard_pct"] < 50


def test_strict_groups_require_every_pair_to_pass():
    # Jaccard a~b ~82% and b~c ~70% pass 65%, a~c ~61% does not: one component, two cliques.
    core = words(9, 300)
    a = core + " " + words(10, 60)
    b = core
    c = core + " " + words(11, 60) + " " + words(12, 60)
    records = [record(BASE + n, [t]) for n, t in (("a", a), ("b", b), ("c", c))]
    loose = find_overlaps(records, OverlapConfig(threshold=0.65))
    strict = find_overlaps(records, OverlapConfig(threshold=0.65, strict=True))
    assert len(loose.pairs) == 2
    assert [[d.url for d in g] for g in loose.groups] == [[BASE + "a", BASE + "b", BASE + "c"]]
    assert [[d.url for d in g] for g in strict.groups] == [[BASE + "a", BASE + "b"]]
    assert r2a_table(strict).rows[0]["mode"] == "content_overlap_strict"


def test_lsh_recall_matches_brute_force_on_a_few_hundred_pages():
    # 60 families of near-duplicates (5 variants each) plus 100 unrelated pages.
    records = []
    for fam in range(60):
        base = words(1000 + fam, 250).split()
        for v in range(5):
            variant = list(base)
            for k in range(v * 3):
                variant[(k * 37 + v) % len(variant)] = f"edit{fam}_{v}_{k}"
            records.append(record(f"{BASE}f{fam}-v{v}", [" ".join(variant)]))
    records.extend(record(f"{BASE}solo{i}", [words(5000 + i, 250)]) for i in range(100))
    cfg = OverlapConfig(threshold=0.8)
    result = find_overlaps(records, cfg)
    docs, _ = docs_from_records(records, cfg)
    truth = set()
    for x, y in combinations(docs, 2):
        shared = len(x.shingles & y.shingles)
        if shared / len(x.shingles | y.shingles) >= 0.8:
            truth.add(tuple(sorted((x.url, y.url))))
    found = set(_pairs(result))
    assert truth, "the fixture must contain true pairs"
    assert found <= truth  # nothing unverified is reported
    assert len(found) / len(truth) >= 0.98
    assert result.candidates_checked < len(docs) * (len(docs) - 1) // 2 // 10


def test_r2_rows_quote_verbatim_and_list_shared_blocks():
    shared = "Annual plans renew automatically unless you cancel thirty days before renewal."
    a = record(
        BASE + "a",
        blocks=[
            block("ra", 0, "Pricing", heading=["Pricing"]),
            block("ra", 1, shared),
            block("ra", 2, words(20, 40)),
            block("ra", 3, "Site menu links", role="nav"),
        ],
        badge=UrlBadge.PARTIAL,
    )
    b = record(
        BASE + "b",
        blocks=[block("rb", 0, "Pricing", heading=["Pricing"]), block("rb", 1, shared)],
    )
    result = find_overlaps([a, b], OverlapConfig(metric="containment"))
    pairs = r2b_table(result)
    (row,) = pairs.rows
    assert row["badge_a"] == "PARTIAL" and row["run_id"] == "run-l5"
    corpus_a = {blk.normalized_text for blk in a.blocks}
    for quote in str(row["shared_spans"]).split(" || "):
        # Zero fabricated proofs: every quoted line is verbatim text of a block of URL A.
        assert all(any(line in t for t in corpus_a) for line in quote.split("\n")), quote
    assert "evidence:ra" in str(row["evidence_ids"]) and "evidence:rb" in str(row["evidence_ids"])
    groups = r2a_table(result)
    by_url = {r["url"]: r for r in groups.rows}
    assert by_url[BASE + "b"]["overlap_to_group_pct"] == 100.0
    assert by_url[BASE + "a"]["shared_block_count"] == 2
    assert by_url[BASE + "a"]["group_id"] == "g-0001"
    # Boilerplate is out of the analysis by default, in with the toggle.
    assert "menu" not in result.groups[0][0].text
    with_nav = find_overlaps([a, b], OverlapConfig(metric="containment", include_boilerplate=True))
    assert "menu" in with_nav.groups[0][0].text


def test_records_without_text_are_reported_as_skipped():
    records = [
        record(BASE + "empty", [], badge=UrlBadge.BLOCKED),
        record(BASE + "dup-only", blocks=[block("rd", 0, "x y z", dedupe_of="rd:b9")]),
        record(BASE + "hidden", blocks=[block("rh", 0, "secret", layer=ContentLayer.HIDDEN)]),
    ]
    result = find_overlaps(records)
    assert [u for u, _ in result.skipped] == [BASE + "empty", BASE + "dup-only", BASE + "hidden"]
    assert result.summary()["skipped"][0] == {"url": BASE + "empty", "reason": "no_content"}
    assert r2a_table(result).rows == [] and r2b_table(result).rows == []


def test_pair_cap_is_reported():
    text = words(3, 100)
    records = [record(f"{BASE}{i}", [text]) for i in range(5)]
    result = find_overlaps(records, OverlapConfig(max_pairs=2))
    assert len(result.pairs) == 2 and result.pairs_capped
    assert result.summary()["pairs_capped"] is True


def test_long_quotes_are_cut_with_an_ellipsis():
    text = words(4, 200)
    result = find_overlaps([record(BASE + "a", [text]), record(BASE + "b", [text])])
    quote = str(r2b_table(result).rows[0]["shared_spans"])
    assert quote.endswith("…") and len(quote) == 301
    assert text.startswith(quote[:-1])
