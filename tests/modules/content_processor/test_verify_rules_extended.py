"""Rules R6, R7, R8, R11, R12, R13, volatility (V-6) and evidence integrity."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.adapters.ocr import OcrResult, OcrSpan
from src.modules.content_processor.contracts import ContentBlock, Resource, RunEvent
from src.modules.content_processor.enums import ContentLayer, EntityStatus, GapType, UrlBadge
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.verify.detectors import (
    DetectedRegion,
    EvidenceProblem,
    OcrRegionDetector,
    PdfRegionRef,
    default_detector,
    evidence_problems,
    pdf_regions_from_evidence,
    screenshot_sha,
)
from src.modules.content_processor.verify.record import assemble_url_record, url_records_for_run
from src.modules.content_processor.verify.rules_extended import (
    main_word_count,
    rule_evidence_integrity,
    rule_r6_visual_regions,
    rule_r7_channel_agreement,
    rule_r8_pdf_regions,
    rule_r11_degradation,
    rule_r12_limits,
    rule_r13_layer_hygiene,
    rule_volatility,
)

S = EntityStatus
RUN = "run-x"


def _res(**kw: Any) -> Resource:
    base: dict[str, Any] = {
        "resource_id": "res-1",
        "run_id": RUN,
        "requested_url": "http://h/page",
        "final_url": "http://h/page",
        "schedule_key": "http://h/page",
        "sniffed_mime": "text/html",
        "payload_sha256": "a" * 64,
        "status": S.VERIFIED_EXTRACTED,
        "status_reason": "rendered;discovery_closed_within_scope",
    }
    base.update(kw)
    return Resource(**base)


def _block(
    n: int,
    text: str,
    *,
    state: str = "run|rendered",
    layer: ContentLayer = ContentLayer.RENDERED_VISIBLE,
    role: Any = "main",
    method: str = "html_source",
    page: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    evidence: str = "evidence:" + "a" * 64,
) -> ContentBlock:
    return ContentBlock(
        block_id=f"b{n}",
        block_key=f"k{n}",
        resource_id="res-1",
        state_id=state,
        region_id=f"r{n}",
        reading_order=n,
        page_index=page,
        bbox=bbox,
        layer=layer,
        role=role,
        raw_text=text,
        normalized_text=text,
        method=method,
        tool_version="1",
        confidence=1.0,
        status=S.VERIFIED_EXTRACTED,
        evidence_ids=[evidence],
    )


def _types(gaps: list[Any]) -> list[tuple[GapType, EntityStatus]]:
    return [(g.gap_type, g.status) for g in gaps]


def _region(text: str, conf: float = 0.9) -> DetectedRegion:
    return DetectedRegion(text=text, bbox=(0, 0, 10, 10), confidence=conf)


# -- R6 ----------------------------------------------------------------------------


def test_r6_regions_found_in_any_block_map_and_unknown_text_is_a_gap():
    blocks = [
        _block(1, "Eligibility applies only to annual plans."),
        _block(2, "Chat with us now!", layer=ContentLayer.THIRD_PARTY_CHROME),
    ]
    mapped = [_region("Eligibility applies only"), _region("Chat with us"), _region("Menu")]
    assert rule_r6_visual_regions(RUN, _res(), regions=mapped, blocks=blocks) == []
    unknown = [
        _region("Limited offer ends Friday"),
        _region("Free shipping for members"),
        _region("annual plans only"),
    ]
    (gap,) = rule_r6_visual_regions(RUN, _res(), regions=unknown, blocks=blocks)
    assert (gap.gap_type, gap.status) == (GapType.LOW_CONFIDENCE, S.EXTRACTED_UNVERIFIED)
    assert gap.reason.startswith("visual_text_unextracted:regions=2:Limited offer")
    lone = [_region("Limited offer ends Friday")]
    assert rule_r6_visual_regions(RUN, _res(), regions=lone, blocks=blocks) == []
    assert rule_r6_visual_regions(RUN, _res(), regions=None, blocks=blocks) == []


def test_ocr_region_detector_uses_sparse_mode_and_filters_confidence():
    seen: dict[str, Any] = {}

    def runner(image: bytes, *, packs: Any, psm: int) -> OcrResult:
        seen["psm"] = psm
        return OcrResult(
            text="x",
            confidence=0.8,
            spans=[
                OcrSpan(text="Hello world", bbox=(1, 2, 3, 4), confidence=0.9),
                OcrSpan(text="smudge", bbox=(0, 0, 1, 1), confidence=0.2),
                OcrSpan(text="  ", bbox=(0, 0, 1, 1), confidence=0.99),
            ],
        )

    regions = OcrRegionDetector(ocr_runner=runner).detect(b"img")
    assert seen["psm"] == 11
    assert [r.text for r in regions or []] == ["Hello world"]

    def missing(image: bytes, *, packs: Any, psm: int) -> OcrResult:
        return OcrResult(text="", confidence=0.0, engine_available=False)

    assert OcrRegionDetector(ocr_runner=missing).detect(b"img") is None


def test_default_detector_follows_engine_availability(monkeypatch):
    from src.modules.content_processor.verify import detectors

    monkeypatch.setattr(detectors, "is_ocr_available", lambda: False)
    assert default_detector() is None
    monkeypatch.setattr(detectors, "is_ocr_available", lambda: True)
    detector = default_detector()
    assert isinstance(detector, OcrRegionDetector) and detector.name == "tesseract_psm11"


def test_screenshot_sha_reads_the_latest_rendered_event():
    def event(reason: str, ids: list[str], entity: str = "res-1") -> RunEvent:
        return RunEvent(
            run_id=RUN,
            entity_type="resource",
            entity_id=entity,
            old_state=None,
            new_state="VERIFIED_EXTRACTED",
            reason=reason,
            evidence_ids=ids,
        )

    ids = ["evidence:" + c * 64 for c in "123"]
    later = ["evidence:" + c * 64 for c in "456"]
    history = [
        event("rendered;x", ids),
        event("source_extracted", later),
        event("rendered;y", later),
        event("rendered;z", ids, entity="other"),
    ]
    assert screenshot_sha(history, "res-1") == "6" * 64
    assert screenshot_sha([event("rendered", ids[:2])], "res-1") is None


# -- R7 ----------------------------------------------------------------------------


def test_r7_obfuscated_font_output_is_a_disagreement():
    glyphs = "  plain"
    (gap,) = rule_r7_channel_agreement(RUN, _res(), blocks=[_block(1, glyphs)])
    assert gap.gap_type is GapType.FIDELITY_DISAGREEMENT
    assert gap.reason == "obfuscated_font_suspected:pua_chars=5"
    assert rule_r7_channel_agreement(RUN, _res(), blocks=[_block(1, "Plain words.")]) == []


def test_r7_source_text_missing_from_render():
    source_text = " ".join(["Detailed coverage terms apply here."] * 6)
    blocks = [
        _block(1, source_text, state="run|source"),
        _block(2, "Short intro.", state="run|source"),
        _block(3, "Short intro."),
    ]
    (gap,) = rule_r7_channel_agreement(RUN, _res(), blocks=blocks)
    assert gap.reason.startswith("source_text_not_rendered:words=30/32:")
    moved = [*blocks, _block(4, source_text)]
    assert rule_r7_channel_agreement(RUN, _res(), blocks=moved) == []
    no_render = [b for b in blocks if b.state_id.endswith("|source")]
    assert rule_r7_channel_agreement(RUN, _res(), blocks=no_render) == []


# -- R8 ----------------------------------------------------------------------------

PAGE_REGION = PdfRegionRef(page_index=0, bbox=(0, 0, 300, 300), width=400, height=400)
SMALL_REGION = PdfRegionRef(page_index=0, bbox=(0, 0, 20, 20), width=32, height=32)


def test_r8_uncovered_region_is_a_gap_only_when_ocr_could_run():
    pdf = _res(sniffed_mime="application/pdf")
    native = [_block(1, "Native header", method="pymupdf/native", page=0, bbox=(0, 400, 50, 420))]
    regions = [PAGE_REGION, SMALL_REGION]
    (gap,) = rule_r8_pdf_regions(RUN, pdf, regions=regions, blocks=native, ocr_available=True)
    assert gap.reason == "pdf_image_regions_not_ocred:regions=1:pages=[1]"
    assert rule_r8_pdf_regions(RUN, pdf, regions=regions, blocks=native, ocr_available=False) == []
    assert rule_r8_pdf_regions(RUN, pdf, regions=None, blocks=native, ocr_available=True) == []


def test_r8_native_and_ocr_disagreement():
    pdf = _res(sniffed_mime="application/pdf")
    native = _block(
        1, "Premium is 120 dollars", method="pymupdf/native", page=0, bbox=(0, 0, 100, 20)
    )
    agree = _block(
        2, "Premium is 120 dollars", method="tesseract/ocr", page=0, bbox=(0, 0, 100, 20)
    )
    stale = _block(3, "Lorem ipsum dolor sit", method="tesseract/ocr", page=0, bbox=(0, 0, 100, 20))
    regions = [PAGE_REGION]
    assert (
        rule_r8_pdf_regions(RUN, pdf, regions=regions, blocks=[native, agree], ocr_available=True)
        == []
    )
    (gap,) = rule_r8_pdf_regions(
        RUN, pdf, regions=regions, blocks=[native, stale], ocr_available=True
    )
    assert gap.reason == "pdf_native_ocr_disagree:pages=[1]"


def _image_pdf() -> bytes:
    png = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(png, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Native heading text")
    page.insert_image(pymupdf.Rect(72, 100, 472, 500), stream=png.getvalue())
    data: bytes = doc.tobytes()
    doc.close()
    return data


def test_pdf_regions_are_reread_from_evidence(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path, "t")
    ref = store.put(_image_pdf(), "application/pdf")
    read = pdf_regions_from_evidence(store)
    regions = read(_res(sniffed_mime="application/pdf", payload_sha256=ref.sha256))
    assert regions and regions[0].page_index == 0 and regions[0].width >= 200
    assert read(_res(sniffed_mime="text/html", payload_sha256=ref.sha256)) is None
    assert read(_res(sniffed_mime="application/pdf", payload_sha256="0" * 64)) is None


# -- R11, R12, R13, V-6 ------------------------------------------------------------


def test_r11_flags_thin_pages_only_against_a_real_baseline():
    thin = {"main_words": 12, "host_median": 400.0, "host_pages": 20}
    (gap,) = rule_r11_degradation(RUN, _res(), **thin)
    assert (gap.gap_type, gap.reason) == (
        GapType.SHADOW_BLOCK_SUSPECTED,
        "main_words=12:host_median=400:host_pages=20",
    )
    assert rule_r11_degradation(RUN, _res(), **{**thin, "host_pages": 5}) == []
    assert rule_r11_degradation(RUN, _res(), **{**thin, "host_median": 60.0}) == []
    assert rule_r11_degradation(RUN, _res(), **{**thin, "main_words": 90}) == []
    assert rule_r11_degradation(RUN, _res(status=S.BLOCKED), **thin) == []


def test_main_word_count_uses_the_main_corpus():
    blocks = [
        _block(1, "one two three"),
        _block(2, "nav words here", role="nav"),
        _block(3, "hidden words", layer=ContentLayer.HIDDEN),
    ]
    assert main_word_count(blocks) == 3


def test_r12_page_budgets_from_summaries():
    summaries = {"explore": {"exploration": {"stop_reason": "action_budget_exhausted"}}}
    (gap,) = rule_r12_limits(RUN, _res(), summaries=summaries)
    assert (gap.gap_type, gap.status, gap.reason) == (
        GapType.LIMIT_HIT,
        S.PARTIAL,
        "page_budget:action_budget_exhausted",
    )
    done = {"explore": {"stop_reason": "all_visited", "states": [{"stop_reason": "x"}]}}
    assert rule_r12_limits(RUN, _res(), summaries=done) == []


def test_r13_icon_artifacts_and_chrome_leaks():
    blocks = [
        _block(1, "arrow_forward"),
        _block(2, ""),
        _block(3, "Chat with us now!"),
        _block(4, "Chat with us now!", layer=ContentLayer.THIRD_PARTY_CHROME, role="chrome"),
        _block(5, "Real content about max_tokens settings."),
    ]
    gaps = rule_r13_layer_hygiene(RUN, _res(), blocks=blocks)
    assert [g.reason for g in gaps] == [
        "layer_leak:icon_artifact:blocks=2:b1,b2",
        "layer_leak:third_party_chrome:blocks=1:b3",
    ]
    assert rule_r13_layer_hygiene(RUN, _res(), blocks=blocks[4:]) == []


def test_volatility_only_when_the_page_never_settled():
    unstable = {"render": {"settle": {"stable": False, "polls": 40}}}
    (gap,) = rule_volatility(RUN, _res(), summaries=unstable)
    assert (gap.gap_type, gap.reason) == (GapType.VOLATILE, "settle_window_exhausted:polls=40")
    assert rule_volatility(RUN, _res(), summaries={"render": {"settle": {"stable": True}}}) == []
    assert rule_volatility(RUN, _res(), summaries={"render": {}}) == []


# -- evidence integrity --------------------------------------------------------------


def test_evidence_problems_and_their_gaps(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path, "t")
    ref = store.put(b"<html>page</html>", "text/html")
    res = _res(payload_sha256=ref.sha256)
    blocks = [_block(1, "x", evidence=f"evidence:{ref.sha256}")]
    assert evidence_problems(store, res, blocks) == []
    gone = "f" * 64
    assert evidence_problems(store, res, [_block(2, "y", evidence=f"evidence:{gone}")]) == [
        EvidenceProblem(kind="missing", sha256=gone)
    ]
    path = tmp_path / "t" / "raw" / ref.sha256[:2] / ref.sha256[2:4] / ref.sha256
    if not path.exists():  # layout guard: find the stored file wherever the store puts it
        path = next(p for p in tmp_path.rglob(ref.sha256))
    path.write_bytes(b"tampered")
    assert evidence_problems(store, res, []) == [
        EvidenceProblem(kind="hash_mismatch", sha256=ref.sha256)
    ]
    assert evidence_problems(store, _res(payload_sha256="e" * 64), []) == [
        EvidenceProblem(kind="missing", sha256="e" * 64)
    ]
    gaps = rule_evidence_integrity(
        RUN,
        res,
        problems=[
            EvidenceProblem(kind="hash_mismatch", sha256=ref.sha256),
            EvidenceProblem(kind="missing", sha256=gone),
        ],
    )
    assert _types(gaps) == [
        (GapType.FIDELITY_DISAGREEMENT, S.EXTRACTED_UNVERIFIED),
        (GapType.LOW_CONFIDENCE, S.EXTRACTED_UNVERIFIED),
    ]


# -- record wiring -------------------------------------------------------------------


def _record(**kw: Any):
    base: dict[str, Any] = {
        "run_id": RUN,
        "target": _res(),
        "recorded_gaps": [],
        "blocks": [_block(1, "Clean content words."), _block(2, "More clean words.")],
        "summaries": {"render": {}},
        "frames": [],
    }
    base.update(kw)
    return assemble_url_record(**base)


def test_missing_detector_warns_and_does_not_block():
    rec = _record()
    assert rec.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert "r6:detector_unavailable" in rec.warnings
    ran = _record(visual_regions=[_region("Clean content words")], detector_name="fake")
    assert (
        "r6:detector_unavailable" not in ran.warnings
        and ran.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    )
    shot_missing = _record(detector_name="fake")
    assert "r6:screenshot_unavailable" in shot_missing.warnings


def test_r6_gap_lowers_the_badge_when_a_detector_ran():
    two = [_region("Unextracted promotional banner text"), _region("Another hidden offer line")]
    rec = _record(visual_regions=two, detector_name="f")
    assert rec.badge is UrlBadge.NEEDS_REVIEW
    one = _record(visual_regions=two[:1], detector_name="f")
    assert one.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert "r6:unmapped_line:Unextracted promotional banner text" in one.warnings


def test_r8_without_ocr_warns_and_with_ocr_reviews():
    pdf = _res(sniffed_mime="application/pdf")
    blocks = [_block(1, "Native text.", method="pymupdf/native", page=0, bbox=(0, 400, 50, 420))]
    quiet = _record(target=pdf, blocks=blocks, summaries={}, pdf_regions=[PAGE_REGION])
    assert "r8:ocr_unavailable:regions=1" in quiet.warnings
    assert quiet.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    loud = _record(
        target=pdf, blocks=blocks, summaries={}, pdf_regions=[PAGE_REGION], ocr_available=True
    )
    assert loud.badge is UrlBadge.NEEDS_REVIEW


def test_evidence_issue_on_a_blocked_page_is_still_reported():
    rec = _record(
        target=_res(status=S.BLOCKED, status_reason="js_challenge:cf"),
        blocks=[],
        evidence_issues=[EvidenceProblem(kind="missing", sha256="a" * 64)],
    )
    assert any(g.reason.startswith("evidence_missing") for g in rec.gaps)


def test_r11_needs_no_other_explanation():
    thin = _record(blocks=[_block(1, "Tiny page.")], host_baseline=(400.0, 20))
    assert thin.badge is UrlBadge.NEEDS_REVIEW
    assert any(g.gap_type is GapType.SHADOW_BLOCK_SUSPECTED for g in thin.gaps)
    volatile = _record(
        blocks=[_block(1, "Tiny page.")],
        host_baseline=(400.0, 20),
        summaries={"render": {"settle": {"stable": False, "polls": 3}}},
    )
    assert not any(g.gap_type is GapType.SHADOW_BLOCK_SUSPECTED for g in volatile.gaps)


class FakeDetector:
    name = "fake"

    def __init__(self, regions: list[DetectedRegion]) -> None:
        self.regions = regions
        self.images: list[bytes] = []

    def detect(self, image: bytes) -> list[DetectedRegion] | None:
        self.images.append(image)
        return self.regions


def test_run_level_wiring_reads_screenshots_and_builds_baselines(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path, "t")
    ledger = MemoryLedger()
    long_text = " ".join(["word"] * 300)
    shot = store.put(b"jpeg-bytes", "image/jpeg")
    for i in range(12):
        payload = store.put(f"<html>{i}</html>".encode(), "text/html")
        res = _res(
            resource_id=f"res-{i}",
            requested_url=f"http://h/p{i}",
            final_url=f"http://h/p{i}",
            schedule_key=f"http://h/p{i}",
            payload_sha256=payload.sha256,
        )
        ledger.upsert_resource(res, "h")
        text = "Thin page." if i == 0 else long_text
        ledger.upsert_blocks(
            res.resource_id,
            [
                _block(1, text, evidence=f"evidence:{payload.sha256}").model_copy(
                    update={"resource_id": res.resource_id}
                )
            ],
        )
        ledger.record_summary(res.resource_id, "render", {})
        ledger.append_event(
            RunEvent(
                run_id=RUN,
                entity_type="resource",
                entity_id=res.resource_id,
                old_state="EXTRACTED_UNVERIFIED",
                new_state="VERIFIED_EXTRACTED",
                reason="rendered;all_rendered_blocks_verified",
                evidence_ids=[
                    f"evidence:{payload.sha256}",
                    "evidence:" + "0" * 64,
                    f"evidence:{shot.sha256}",
                ],
            )
        )
    detector = FakeDetector([_region("word word word")])
    records = url_records_for_run(
        ledger,
        run_id=RUN,
        frames_for=lambda _r: [],
        evidence=store,
        detector=detector,
        ocr_available=False,
    )
    by_url = {r.requested_url: r for r in records}
    assert len(detector.images) == 12 and set(detector.images) == {b"jpeg-bytes"}
    assert by_url["http://h/p0"].badge is UrlBadge.NEEDS_REVIEW  # R11: thin vs a 300-word host
    assert all(
        by_url[f"http://h/p{i}"].badge is UrlBadge.VERIFIED_WITHIN_SCOPE for i in range(1, 12)
    )
    assert all("r6:detector_unavailable" not in r.warnings for r in records)


def test_r6_ignores_ocr_noise_consent_text_and_clipped_words():
    from src.modules.content_processor.verify.rules_extended import unmapped_visual_lines

    blocks = [
        _block(1, "Individual annuities and contact us for financial professionals."),
        _block(2, "John Hancock 2026"),
    ]
    regions = [
        _region("Individual annuiti Contac us"),  # OCR-clipped words of page text
        _region("1026 John Hanca"),  # digits ignored, clipped name: under 3 real words
        _region("cookies that are strictly necessary for"),  # consent banner
        _region("garbled smudge text here", conf=0.5),  # low-confidence OCR line
    ]
    assert unmapped_visual_lines(regions, blocks) == []


def test_r13_ignores_accessibility_hints_and_non_main_blocks():
    blocks = [
        _block(1, "(opens in a new tab)"),
        _block(2, "(opens in a new tab)", layer=ContentLayer.THIRD_PARTY_CHROME, role="chrome"),
        _block(3, "Chat with us now!", role="unknown"),
        _block(4, "Chat with us now!", layer=ContentLayer.THIRD_PARTY_CHROME, role="chrome"),
    ]
    assert rule_r13_layer_hygiene(RUN, _res(), blocks=blocks) == []
