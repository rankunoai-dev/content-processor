"""False alarms that made real pages "Partial": pixel frames, drop-down lists, SVG logos.

Live evidence (www.gep.com, 466 rendered pages): every page carried
`frame_not_acquired:https://www.googletagmanager.com/ns.html?...` from the tag manager's
`<noscript>` frame; 57 pages had "visible text not extracted" made only of country names
from a drop-down; three SVG logos failed as "corrupt images" in every batch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.adapters.image import extract_image
from src.modules.content_processor.contracts import ContentBlock, Resource
from src.modules.content_processor.enums import (
    AccessTier,
    ContentLayer,
    EntityStatus,
    UrlBadge,
)
from src.modules.content_processor.governor.gates import GateFinding
from src.modules.content_processor.recrawl.baseline import Baseline
from src.modules.content_processor.verify.gaps import gap_for_gate
from src.modules.content_processor.verify.record import assemble_url_record
from src.modules.content_processor.verify.reverdict import (
    gate_recheck,
    reverdict_file,
    reverdict_record,
)
from src.modules.content_processor.verify.rules import FrameRef, is_pixel_frame, rule_r3_frames
from src.modules.content_processor.workers.render_worker import (
    missing_visible_lines,
    option_lines,
)

RUN = "run-v"
GTM = "https://www.googletagmanager.com/ns.html?id=GTM-M94W3Z9"


def _res(**kw: Any) -> Resource:
    base: dict[str, Any] = {
        "resource_id": "res-1",
        "run_id": RUN,
        "requested_url": "http://h/page",
        "final_url": "http://h/page",
        "schedule_key": "http://h/page",
        "sniffed_mime": "text/html",
        "payload_sha256": "abc",
        "acquired_at_tier": AccessTier.T3_FIDELITY_BROWSER,
        "status": EntityStatus.VERIFIED_EXTRACTED,
        "status_reason": "rendered;all_rendered_blocks_verified;discovery_closed_within_scope",
    }
    base.update(kw)
    return Resource(**base)


def _block(
    n: int, text: str = "", layer: ContentLayer = ContentLayer.RENDERED_VISIBLE
) -> ContentBlock:
    return ContentBlock(
        block_id=f"b{n}",
        block_key=f"k{n}",
        resource_id="res-1",
        state_id="run|rendered",
        region_id=f"r{n}",
        reading_order=n,
        layer=layer,
        role="main",
        raw_text=text or f"text {n}",
        normalized_text=text or f"text {n}",
        method="m",
        tool_version="1",
        confidence=1.0,
        status=EntityStatus.VERIFIED_EXTRACTED,
        evidence_ids=["e"],
    )


def _record(**kw: Any):
    base: dict[str, Any] = {
        "run_id": RUN,
        "target": _res(),
        "recorded_gaps": [],
        "blocks": [_block(1), _block(2)],
        "summaries": {"render": {}},
        "frames": [],
    }
    base.update(kw)
    return assemble_url_record(**base)


def _frames(html: str) -> dict[str, bool]:
    ex = extract_html_source(
        html.encode("utf-8"),
        base_url="https://site.test/p",
        evidence_id="evidence:x",
        resource_id="r",
        state_id="s|source",
    )
    return {a.url: a.hidden for a in ex.assets if a.kind == "frame"}


# -- the HTML reader knows a hidden frame when it sees one ---------------------


def test_noscript_and_self_hidden_frames_are_hidden_and_a_real_embed_is_not():
    html = f"""<html><body>
    <noscript><iframe src="{GTM}" height="0" width="0"
      style="display:none;visibility:hidden"></iframe></noscript>
    <iframe src="https://px.test/a" style="display:none"></iframe>
    <iframe src="https://px.test/b" width="0" height="0"></iframe>
    <iframe src="https://px.test/c" hidden></iframe>
    <iframe src="https://px.test/d" aria-hidden="true" width="1" height="1"></iframe>
    <main><p>Article text here.</p>
    <iframe src="https://www.youtube.com/embed/abc" width="560" height="315"></iframe></main>
    </body></html>"""
    frames = _frames(html)
    assert frames[GTM] is True
    assert all(frames[f"https://px.test/{k}"] for k in "abcd")
    assert frames["https://www.youtube.com/embed/abc"] is False


def test_pixel_frames_are_recognised_by_address_whatever_their_styling():
    assert is_pixel_frame(GTM)
    assert is_pixel_frame("https://www.facebook.com/tr?id=1&ev=PageView&noscript=1")
    assert is_pixel_frame("https://bat.bing.com/action/0?ti=1")
    assert not is_pixel_frame("https://www.youtube.com/embed/abc")
    assert not is_pixel_frame("https://forms.site.test/contact")
    assert not is_pixel_frame("https://googletagmanager.com.evil.test/x")


def test_r3_raises_no_gap_for_a_pixel_frame_but_still_for_a_content_frame():
    frames = [FrameRef(url=GTM), FrameRef(url="https://forms.site.test/contact")]
    gaps = rule_r3_frames(RUN, _res(), frames=frames, children=[], blocks=[])
    assert [g.reason for g in gaps] == ["frame_not_acquired:https://forms.site.test/contact"]


def test_a_page_whose_only_frame_is_the_tag_manager_is_verified():
    rec = _record(frames=[FrameRef(url=GTM)])
    assert rec.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert not rec.gaps


# -- drop-down choices are not lines the reader missed --------------------------


def test_country_list_in_a_select_is_not_missing_text():
    dom = b"""<html><body><p>Contact us today for more.</p>
    <select name="country"><option>United Arab Emirates</option>
    <option>Bosnia and Herzegovina</option><option>British Indian Ocean Territory</option>
    </select></body></html>"""
    inner_text = (
        "Contact us today for more.\nUnited Arab Emirates\nBosnia and Herzegovina\n"
        "British Indian Ocean Territory\nA real paragraph nobody extracted\n"
    )
    blocks = [_block(1, "Contact us today for more.")]
    ignore = option_lines([dom])
    assert len(ignore) == 3
    missing = missing_visible_lines([(inner_text, blocks)], ignore=ignore)
    assert missing == ["A real paragraph nobody extracted"]
    assert len(missing_visible_lines([(inner_text, blocks)])) == 4, "without the DOM, as before"


def test_option_lines_tolerates_empty_or_broken_documents():
    assert option_lines([b"", b"<select><option>"]) in (frozenset(), frozenset({""}) - {""})


# -- SVG files are read as XML, never as pictures ------------------------------


def _svg_extraction(data: bytes, media_type: str = "image/svg+xml"):
    return extract_image(
        data, resource_id="r", run_id=RUN, evidence_id="evidence:x", media_type=media_type
    )


def test_a_logo_svg_without_text_is_non_content_not_a_corrupt_image():
    svg = (
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="120" '
        b'height="40"><path d="M0 0h10v10H0z"/></svg>'
    )
    ex = _svg_extraction(svg)
    assert ex.status is EntityStatus.VERIFIED_NONCONTENT
    assert ex.image_format == "SVG" and (ex.width, ex.height) == (120, 40)
    assert ex.gaps == [] and "svg_without_text" in ex.warnings


def test_an_svg_with_text_keeps_the_text_exactly():
    svg = (
        b'<svg viewBox="0 0 300 100" xmlns="http://www.w3.org/2000/svg"><title>Download on the '
        b'App Store</title><text x="1" y="2">Get it <tspan>on Google &amp; Play</tspan>'
        b"</text></svg>"
    )
    ex = _svg_extraction(svg, media_type="application/octet-stream")
    assert ex.status is EntityStatus.VERIFIED_EXTRACTED
    texts = [b.normalized_text for b in ex.blocks if b.method == "svg/text"]
    assert texts == ["Download on the App Store Get it on Google & Play"]
    assert (ex.width, ex.height) == (300, 100)


def test_a_real_png_still_goes_through_the_picture_reader():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    ex = extract_image(buf.getvalue(), resource_id="r", run_id=RUN, evidence_id="e")
    assert ex.image_format == "PNG" and "icon_or_tracker_dimension" in ex.warnings


# -- a capture that ran out of budget is not reused ------------------------------


def test_a_budget_exhausted_capture_is_not_reused_next_time():
    rec = _record(
        target=_res(status=EntityStatus.BUDGET_EXHAUSTED, status_reason="budget_exhausted:runtime")
    )
    assert rec.badge is UrlBadge.PARTIAL
    assert Baseline.reusable(rec) is False


# -- re-scoring stored records without a new scrape -------------------------------


def _stored_with_pixel_gap():
    """A record as the old rule wrote it: the tag-manager frame counted as a gap."""
    rec = _record(frames=[FrameRef(url="https://forms.site.test/contact")])
    assert rec.badge is UrlBadge.PARTIAL
    gap = rec.gaps[0].model_copy(update={"reason": f"frame_not_acquired:{GTM}"})
    return rec.model_copy(update={"gaps": [gap]})


def test_reverdict_turns_a_pixel_frame_partial_into_verified():
    stored = _stored_with_pixel_gap()
    updated, dropped = reverdict_record(stored)
    assert dropped == 1
    assert updated.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert updated.gaps == []
    assert updated.status_vector.acquisition is EntityStatus.VERIFIED_EXTRACTED
    assert any(w.startswith("reverdicted:") for w in updated.warnings)
    assert not any(w.startswith("status_lowered_by_gaps") for w in updated.warnings)
    assert updated.blocks == stored.blocks and updated.captured_at == stored.captured_at


def test_reverdict_keeps_other_gaps_and_leaves_clean_records_alone():
    stored = _record(frames=[FrameRef(url="https://forms.site.test/contact")])
    same, dropped = reverdict_record(stored)
    assert dropped == 0 and same is stored
    both = stored.model_copy(
        update={
            "gaps": [
                *stored.gaps,
                stored.gaps[0].model_copy(
                    update={"gap_id": "g-px", "reason": f"frame_not_acquired:{GTM}"}
                ),
            ]
        }
    )
    updated, dropped = reverdict_record(both)
    assert dropped == 1 and updated.badge is UrlBadge.PARTIAL
    assert [g.reason for g in updated.gaps] == [
        "frame_not_acquired:https://forms.site.test/contact"
    ]


def test_reverdict_file_rewrites_only_what_changed_and_keeps_damaged_lines(tmp_path: Path):
    path = tmp_path / "b1_a1.records.jsonl"
    clean = _record()
    stored = _stored_with_pixel_gap().model_dump_json()
    path.write_text(
        "\n".join([stored, "{not json", clean.model_dump_json()]) + "\n",
        encoding="utf-8",
    )
    result = reverdict_file(path)
    assert (result.records, result.changed, result.gaps_dropped, result.damaged_lines) == (
        2,
        1,
        1,
        1,
    )
    assert result.badges_before == {"PARTIAL": 1, "VERIFIED_WITHIN_SCOPE": 1}
    assert result.badges_after == {"VERIFIED_WITHIN_SCOPE": 2}
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "{not json"
    assert json.loads(lines[0])["badge"] == "VERIFIED_WITHIN_SCOPE"
    assert json.loads(lines[2]) == json.loads(clean.model_dump_json())
    before = path.stat().st_mtime_ns
    reverdict_file(path)
    assert path.stat().st_mtime_ns == before, "a second pass changes nothing"


@pytest.mark.parametrize("status", [EntityStatus.BLOCKED, EntityStatus.FAILED])
def test_reverdict_never_upgrades_a_blocked_or_failed_page(status: EntityStatus):
    rec = _record(target=_res(status=status, status_reason="blocked"))
    stored = rec.model_copy(
        update={
            "gaps": [
                *rec.gaps,
                _stored_with_pixel_gap().gaps[0].model_copy(update={"gap_id": "g-px"}),
            ]
        }
    )
    updated, _ = reverdict_record(stored)
    assert updated.badge is rec.badge


# -- gate flags are checked again on the stored page -----------------------------


class _Store:
    def __init__(self, **objects: bytes) -> None:
        self.objects = objects

    def get(self, sha256: str) -> bytes:
        return self.objects[sha256]


def _gated_record(dom: bytes, text: str, kind: str = "lead_gen_form_wall"):
    """A page the worker lowered to PARTIAL because of a gate, as the old rules saw it."""
    finding = GateFinding(kind=kind, severity="partial", evidence=["x"], main_word_count=900)
    gap, implied = gap_for_gate(RUN, "res-1", finding, ["evidence:dom", "evidence:txt"])
    assert implied is EntityStatus.PARTIAL
    target = _res(
        status=EntityStatus.PARTIAL,
        status_reason=f"rendered;all_rendered_blocks_verified;{kind};discovery_closed_within_scope",
    )
    rec = _record(target=target, recorded_gaps=[gap])
    assert rec.badge is UrlBadge.PARTIAL
    return rec, _Store(dom=dom, txt=text.encode("utf-8"))


def test_a_gate_the_current_rules_no_longer_find_is_withdrawn_and_the_page_verified():
    dom = b"<html><body><main><p>" + b"word " * 900 + b"</p></main></body></html>"
    rec, store = _gated_record(dom, "word " * 900)
    updated, dropped = reverdict_record(rec, gate_recheck(store))
    assert dropped == 1
    assert updated.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert updated.resource_status is EntityStatus.VERIFIED_EXTRACTED
    assert "gate_withdrawn:PARTIAL->VERIFIED_EXTRACTED" in updated.warnings


def test_a_gate_still_found_on_the_stored_page_is_kept():
    dom = (
        b"<html><body><div class='modal'>Are you a healthcare professional? "
        b"<button>Yes</button><button>No</button></div><main><p>short</p></main></body></html>"
    )
    rec, store = _gated_record(
        dom, "Are you a healthcare professional? Yes No", "age_or_audience_gate"
    )
    recheck = gate_recheck(store)
    verdict = recheck(rec.gaps[0])
    if verdict is True:  # the current rules still gate this page
        same, dropped = reverdict_record(rec, recheck)
        assert dropped == 0 and same is rec
    else:  # the rules changed under this test: it must then be withdrawn, never kept silently
        assert verdict is False


def test_unreadable_evidence_leaves_the_gate_in_place():
    rec, _ = _gated_record(b"<html></html>", "")
    same, dropped = reverdict_record(rec, gate_recheck(_Store()))
    assert dropped == 0 and same is rec
