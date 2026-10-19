"""Lane 5 outputs over the real fixture suite (records from L4's release-gate harness).

The harness runs the frontier, fetch worker and render worker in-process
against the fixture app, then `url_records_for_run`. The report bundle built
from those records must keep the denominator, never claim more than the
records, and quote only verbatim block text.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.export.bundle import write_run_reports
from tests.modules.content_processor.test_verify_release_gate import _run_suite


@pytest.fixture(scope="module")
async def bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[list[UrlRecord], Path, dict]:
    work = tmp_path_factory.mktemp("l5-suite")
    records = (await _run_suite(work, render=True))["recorded"]
    out = work / "reports"
    manifest = write_run_reports(records, out)
    return records, out, manifest


async def test_every_requested_url_has_one_r1_row_with_its_record_badge(bundle):
    records, out, manifest = bundle
    rows = [json.loads(x) for x in (out / "r1_url_content.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == len(records) == manifest["requested_urls"]
    assert {r["url"]: r["badge"] for r in rows} == {r.requested_url: r.badge.value for r in records}


async def test_review_queue_covers_every_record_needing_a_decision(bundle):
    records, out, _ = bundle
    queue = list(csv.DictReader((out / "review_queue.csv").open(encoding="utf-8-sig")))
    need = {
        r.requested_url
        for r in records
        if r.badge.value in {"FAILED", "BLOCKED", "PARTIAL", "NEEDS_REVIEW"}
    }
    assert {row["url"] for row in queue} == need
    assert any(row["url"].endswith("/pdf-but-html") and row["badge"] == "BLOCKED" for row in queue)


async def test_overlap_quotes_are_verbatim_block_text(bundle):
    records, out, manifest = bundle
    texts = {r.requested_url: [b.normalized_text for b in r.blocks] for r in records}
    pairs = list(csv.DictReader((out / "r2b_overlap_pairs.csv").open(encoding="utf-8-sig")))
    assert pairs, "the meta-refresh fixture duplicates static_ok"
    assert len(pairs) == manifest["overlap"]["pairs"]
    for pair in pairs:
        for quote in pair["shared_spans"].split(" || "):
            for line in quote.removesuffix("…").split("\n"):
                assert any(line in t for t in texts[pair["url_a"]]), line


async def test_contradiction_quotes_are_verbatim_block_text(bundle):
    """I4 release gate on the real fixture suite: zero fabricated proofs."""
    records, out, manifest = bundle
    texts = {r.requested_url: [b.normalized_text for b in r.blocks] for r in records}
    rows = [
        json.loads(x) for x in (out / "r4_contradictions.jsonl").read_text("utf-8").splitlines()
    ]
    assert len(rows) == manifest["contradictions"]["reported_pairs"]
    assert manifest["contradictions"]["judge_status"] == "disabled(no_local_provider)"
    for row in rows:
        for side in ("a", "b"):
            assert any(row[f"quote_{side}"] in t for t in texts[row[f"url_{side}"]]), row
