"""Gate rules that feed verification: lead-gen teaser measurement and sign-in prompts."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fixtures.server import app
from src.modules.content_processor.enums import EntityStatus
from src.modules.content_processor.governor.gates import detect_gates
from src.modules.content_processor.verify.gaps import TEASER_MIN_WORDS, gap_for_gate

PAGES = Path(__file__).resolve().parents[3] / "fixtures" / "pages"

BARE_WALL = (
    b"<html><body><main><h1>2026 Outlook Report</h1>"
    b"<p>Fill out the form to download the full report.</p>"
    b"<form><input type='email' name='email'><button>Download the report</button></form>"
    b"</main></body></html>"
)


def _teaser(evidence: list[str]) -> int:
    return int(next(e for e in evidence if e.startswith("teaser_words=")).split("=")[1])


def test_whitepaper_teaser_is_measured_and_kept_as_partial():
    (finding,) = detect_gates((PAGES / "gated_whitepaper.html").read_bytes())
    assert finding.severity == "blocks_main_content"
    assert _teaser(finding.evidence) >= TEASER_MIN_WORDS
    gap, status = gap_for_gate("r", "res", finding, ["e"])
    assert status is EntityStatus.PARTIAL and gap.status is EntityStatus.PARTIAL


def test_wall_with_nothing_but_the_form_is_blocked():
    (finding,) = detect_gates(BARE_WALL)
    assert finding.kind == "lead_gen_form_wall"
    assert _teaser(finding.evidence) == 0
    assert gap_for_gate("r", "res", finding, [])[1] is EntityStatus.BLOCKED


def test_sign_in_notice_for_a_document_is_a_login_wall():
    body = TestClient(app).get("/pdf-but-html").content
    (finding,) = detect_gates(body)
    assert finding.kind == "login_wall" and finding.severity == "blocks_main_content"
    assert "no_password_input" in finding.evidence
    assert gap_for_gate("r", "res", finding, [])[1] is EntityStatus.BLOCKED


def test_sign_in_phrase_on_a_real_page_is_not_a_wall():
    filler = " ".join(["Plenty of published article text."] * 12)
    html = f"<html><body><main><p>Sign in to view your saved items.</p><p>{filler}</p></main>"
    assert detect_gates((html + "</body></html>").encode()) == []


def test_password_form_is_reported_once():
    html = (
        b"<html><body><main><p>Please sign in to continue.</p><form action='/login'>"
        b"<input type='password' name='pw'><button>Sign in</button></form></main></body></html>"
    )
    findings = detect_gates(html)
    assert [(f.kind, f.evidence[0]) for f in findings] == [("login_wall", "form:password_input")]
