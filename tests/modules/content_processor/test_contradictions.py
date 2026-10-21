"""I4 contradiction candidates and report R4: known-answer site, judge, buckets, proofs."""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from src.modules.content_processor.analysis.claims import Claim
from src.modules.content_processor.analysis.contradictions import (
    R4_COLUMNS,
    ContradictionConfig,
    ContradictionResult,
    find_contradictions,
    load_reviewer_decisions,
    r4_table,
)
from src.modules.content_processor.analysis.nli import NliVerdict
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.enums import ContentLayer, EntityStatus, UrlBadge
from src.modules.content_processor.export.commands import register
from tests.modules.content_processor.l5_factory import block, record, report_path


def site() -> list[UrlRecord]:
    """A small site whose contradictions, context differences and non-issues are known."""
    return [
        record(
            "http://h/pricing",
            [
                "The annual plan costs $120 per year.",
                "The monthly plan costs $12 per month.",
                "Refunds are available within 30 days of purchase.",
                "Shipping is free for orders over $50 in the US.",
            ],
        ),
        record(
            "http://h/plans",
            [
                "The annual plan costs $150 per year.",
                "The monthly plan costs $12 per month.",
                "Refunds are not available after purchase.",
                "Shipping is free for orders over $75 in the UK.",
            ],
        ),
        record(
            "http://h/policy",
            [
                "In 2024 the return window was 14 days.",
                "Effective from 1 March 2026 the new returns policy applies to all orders.",
                "Our support team answers within 2 hours.",
            ],
        ),
        record(
            "http://h/policy-2026",
            [
                "From 2026 the return window is 30 days.",
                "Effective from 1 June 2026 the new returns policy applies to all orders.",
                "The battery lasts 30 hours on a single charge.",
            ],
        ),
        record(
            "http://h/specs",
            [
                "The battery lasts 20 hours on a single charge.",
                "The headphones weigh 250 g with the case.",
            ],
        ),
    ]


EXPECTED = {
    ("http://h/plans", "http://h/pricing", "numeric:USD:$150!=$120", "context_unknown"),
    ("http://h/plans", "http://h/pricing", "negation_flip", "context_unknown"),
    (
        "http://h/policy-2026",
        "http://h/specs",
        "numeric:hour:30 hours!=20 hours",
        "context_unknown",
    ),
    (
        "http://h/policy",
        "http://h/policy-2026",
        "date_conflict:from:2026-03-01!=2026-06-01",
        "context_matched",
    ),
    ("http://h/policy", "http://h/policy-2026", "numeric:day:14 days!=30 days", "context_differs"),
    ("http://h/plans", "http://h/pricing", "numeric:USD:$75!=$50", "context_differs"),
}


class FakeJudge:
    """Scores pairs from a table of (premise, hypothesis) → contradiction probability."""

    def __init__(self, scores: dict[tuple[str, str], float]) -> None:
        self.scores = scores
        self.calls: list[tuple[str, str]] = []

    @property
    def status(self) -> str:
        return "enabled(fake)"

    @property
    def model(self) -> str | None:
        return "fake-nli"

    def judge(self, premise: str, hypothesis: str) -> NliVerdict | None:
        self.calls.append((premise, hypothesis))
        c = self.scores.get((premise, hypothesis))
        if c is None:
            return None
        return NliVerdict(entailment=0.0, neutral=1.0 - c, contradiction=c, model="fake-nli")


def _found(result: ContradictionResult) -> set[tuple[str, str, str, str]]:
    return {(p.a.url, p.b.url, " ".join(p.flags), p.bucket) for p in result.pairs}


def test_known_site_yields_exactly_the_expected_pairs_and_buckets():
    result = find_contradictions(site())
    assert _found(result) == EXPECTED
    summary = result.summary()
    assert summary["judge_status"] == "disabled(no_local_provider)"
    assert summary["judged_pairs"] == 0 and summary["pair_basis"] == ["lexical"]
    assert summary["by_bucket"] == {
        "context_matched": 1,
        "context_unknown": 3,
        "context_differs": 2,
    }
    texts = {p.a.text for p in result.pairs} | {p.b.text for p in result.pairs}
    assert "The monthly plan costs $12 per month." not in texts  # repeated, not contradicted
    assert "Our support team answers within 2 hours." not in texts


def test_without_a_judge_contradiction_pct_is_blank():
    table = r4_table(find_contradictions(site()))
    assert all(row["contradiction_pct"] is None for row in table.rows)
    assert {row["judge_status"] for row in table.rows} == {"disabled(no_local_provider)"}


def test_rows_are_ordered_by_bucket():
    buckets = [p.bucket for p in find_contradictions(site()).pairs]
    assert buckets == sorted(
        buckets, key=["context_matched", "context_unknown", "context_differs"].index
    )


def test_judge_reports_unflagged_pairs_above_threshold_and_keeps_both_directions():
    recs = [
        record("http://h/x", ["Premium members get free delivery on every order."]),
        record("http://h/y", ["Premium members pay for delivery on every order."]),
    ]
    a = recs[0].blocks[0].normalized_text
    b = recs[1].blocks[0].normalized_text
    judge = FakeJudge({(a, b): 0.4, (b, a): 0.82})
    result = find_contradictions(recs, judge=judge)
    (pair,) = result.pairs
    assert pair.flags == [] and pair.contradiction == pytest.approx(0.82)
    assert pair.bucket == "context_matched"  # same audience
    assert judge.calls == [(a, b), (b, a)]
    row = r4_table(result).rows[0]
    assert row["contradiction_pct"] == 82.0 and row["judge_model"] == "fake-nli"
    strict = ContradictionConfig(threshold=0.9)
    below = find_contradictions(recs, strict, judge=FakeJudge({(a, b): 0.82}))
    assert below.pairs == [] and below.judged == 1
    assert find_contradictions(recs).pairs == []  # no flags and no judge: nothing reported


def test_differing_context_is_not_judged_unless_asked():
    judge = FakeJudge({})
    result = find_contradictions(site(), judge=judge)
    judged_texts = {t for call in judge.calls for t in call}
    assert "Shipping is free for orders over $75 in the UK." not in judged_texts
    # 10 pairs checked, 2 in context_differs: the other 8 are judged, reported or not.
    assert result.candidates_checked == 10 and result.judged == 8
    everything = FakeJudge({})
    find_contradictions(site(), ContradictionConfig(judge_differing_context=True), judge=everything)
    assert len(everything.calls) > len(judge.calls)


def test_judge_budget_prefers_flagged_pairs():
    judge = FakeJudge({})
    result = find_contradictions(site(), ContradictionConfig(max_judged_pairs=1), judge=judge)
    assert result.judged == 1 and len(judge.calls) == 2
    first = judge.calls[0]
    assert "Effective from" in first[0]  # the matched-context flagged pair goes first


def test_confidence_band_is_gap_aware():
    ocr = block("res-o", 0, "The deposit is $500 for every booking.").model_copy(
        update={"method": "ocr_tesseract", "confidence": 0.62}
    )
    collapsed = block(
        "res-c", 0, "The deposit is $300 for every booking.", layer=ContentLayer.COLLAPSED_PUBLISHED
    ).model_copy(update={"status": EntityStatus.EXTRACTED_UNVERIFIED})
    recs = [
        record("http://h/o", blocks=[ocr]),
        record("http://h/c", blocks=[collapsed], badge=UrlBadge.PARTIAL),
    ]
    (pair,) = find_contradictions(recs).pairs
    assert pair.band == "low"
    assert "ocr_tesseract source, 0.62 confidence" in pair.band_reason
    assert "badge PARTIAL" in pair.band_reason and "layer collapsed_published" in pair.band_reason
    clean = find_contradictions(site()).pairs[0]
    assert clean.band == "high" and clean.band_reason == ""


def test_pair_ids_are_stable_across_runs_and_input_order():
    first = {p.pair_id for p in find_contradictions(site()).pairs}
    again = {p.pair_id for p in find_contradictions(list(reversed(site()))).pairs}
    assert first == again and len(first) == len(EXPECTED)


def test_reviewer_decisions_carry_forward(tmp_path: Path):
    result = find_contradictions(site())
    rows = r4_table(result).rows
    rows[0]["reviewer_decision"] = "Confirmed"
    rows[1]["reviewer_decision"] = "context_differs"
    path = tmp_path / "r4.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(R4_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    decisions = load_reviewer_decisions(path)
    assert decisions == {rows[0]["pair_id"]: "confirmed", rows[1]["pair_id"]: "context_differs"}
    carried = r4_table(find_contradictions(site()), decisions).rows
    assert [r["reviewer_decision"] for r in carried[:3]] == ["confirmed", "context_differs", ""]
    jsonl = tmp_path / "r4.jsonl"
    jsonl.write_text(
        json.dumps({"pair_id": "cp-1", "reviewer_decision": "not_a_contradiction"}) + "\n\n"
    )
    assert load_reviewer_decisions(jsonl) == {"cp-1": "not_a_contradiction"}


def test_unknown_reviewer_decision_names_the_line(tmp_path: Path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"pair_id": "cp-1", "reviewer_decision": "maybe"}) + "\n")
    with pytest.raises(ValueError, match=r"bad.jsonl:1: unknown reviewer_decision 'maybe'"):
        load_reviewer_decisions(path)


def test_r4_has_the_spec_columns_and_proof_fields():
    table = r4_table(find_contradictions(site()))
    assert table.columns == list(R4_COLUMNS)
    for spec in (
        "pair_id",
        "url_a",
        "quote_a",
        "url_b",
        "quote_b",
        "contradiction_pct",
        "deterministic_flags",
        "context_match",
        "confidence_band",
        "evidence_ids_a",
        "evidence_ids_b",
        "reviewer_decision",
    ):
        assert spec in table.columns
    row = next(r for r in table.rows if "negation_flip" in str(r["deterministic_flags"]))
    assert row["evidence_ids_a"] == "evidence:res-plans"
    assert "geography=unknown" in str(row["context_match"])
    assert row["captured_at_a"] == "2026-10-16T09:00:00+00:00"


def test_release_gate_every_quote_is_verbatim_block_text():
    """Zero fabricated proofs: each quote is the exact slice of its block."""
    records = site()
    blocks = {b.block_id: b for r in records for b in r.blocks}
    for pair in find_contradictions(records).pairs:
        for claim in (pair.a, pair.b):
            source = blocks[claim.block_id].normalized_text
            assert source[claim.start : claim.end] == claim.text
            assert claim.url == blocks[claim.block_id].resource_id.replace("res-", "http://h/")


class EveryPair:
    name = "every_pair"

    def pairs(self, claims: Sequence[Claim], config: ContradictionConfig):
        return [(i, j) for i in range(len(claims)) for j in range(i + 1, len(claims))]


def test_custom_source_and_pair_cap():
    capped = find_contradictions(site(), ContradictionConfig(max_pairs=5), sources=[EveryPair()])
    assert capped.pairs_capped and capped.candidates_checked <= 5
    full = find_contradictions(site(), sources=[EveryPair()])
    assert {p.basis for p in full.pairs} == {"every_pair"}
    assert _found(full) >= EXPECTED  # brute force finds every lexical result
    assert _found(find_contradictions(site())) <= _found(full)


def _args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    path = tmp_path / "records.jsonl"
    path.write_text("\n".join(r.model_dump_json() for r in site()) + "\n", encoding="utf-8")
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    return parser.parse_args(
        ["report", "--records", str(path), "--out", str(tmp_path / "out"), *extra]
    )


def test_report_command_writes_r4_and_refuses_remote_judges(tmp_path: Path, capsys):
    args = _args(tmp_path)
    assert args.func(args) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["contradictions"]["reported_pairs"] == len(EXPECTED)
    rows = list(
        csv.DictReader(
            report_path(tmp_path / "out", "r4_contradictions.csv")
            .read_text("utf-8-sig")
            .splitlines()
        )
    )
    assert len(rows) == len(EXPECTED)
    remote = _args(tmp_path, "--nli-endpoint", "https://openrouter.ai/api/v1", "--nli-model", "m")
    assert remote.func(remote) == 2
    assert "cost ceiling is $0" in capsys.readouterr().err
    no_model = _args(tmp_path, "--nli-endpoint", "http://localhost:11434/v1")
    assert no_model.func(no_model) == 2


def test_report_command_carries_decisions_and_uses_a_local_judge(tmp_path: Path, capsys):
    first = _args(tmp_path)
    assert first.func(first) == 0
    capsys.readouterr()
    rows = [
        json.loads(x)
        for x in report_path(tmp_path / "out", "r4_contradictions.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]
    decided = tmp_path / "decided.jsonl"
    decided.write_text(
        json.dumps({"pair_id": rows[0]["pair_id"], "reviewer_decision": "confirmed"}) + "\n"
    )
    # A local endpoint that is not listening: every call fails, is counted, and nothing breaks.
    second = _args(
        tmp_path,
        "--decisions",
        str(decided),
        "--nli-endpoint",
        "http://127.0.0.1:9/v1",
        "--nli-model",
        "m",
    )
    assert second.func(second) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["contradictions"]["judge_status"].startswith("enabled(m:errors=")
    again = [
        json.loads(x)
        for x in report_path(tmp_path / "out", "r4_contradictions.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]
    assert again[0]["reviewer_decision"] == "confirmed"
