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


_ARTICLE = " ".join(
    [
        "Procurement leaders are rethinking category strategies as supply risk rises.",
        "Download our latest report to read the full analysis of 2026 sourcing trends.",
    ]
    * 60
)


def test_contact_form_under_a_long_article_is_not_a_lead_gen_wall():
    # Modelled on www.gep.com blog pages: 1,000+ words of article, the words "report"
    # and "download" in the body, and a separate contact form with an email field.
    html = (
        "<html><body><main>"
        f"<article><h1>Category strategy in 2026</h1><p>{_ARTICLE}</p></article>"
        "<section class='contact-us'><h2>Talk to us</h2>"
        "<form action='/contact'><input type='email' name='email'>"
        "<input name='company'><button>Submit</button></form></section>"
        "</main></body></html>"
    ).encode()
    assert detect_gates(html) == []


def test_contact_form_beside_the_article_in_main_is_not_a_lead_gen_wall():
    # Same page, form directly under <main> next to the article (no section of its own).
    html = (
        "<html><body><main>"
        f"<article><h1>Category strategy in 2026</h1><p>{_ARTICLE}</p></article>"
        "<div class='contact-us'><h2>Talk to us</h2><form action='/contact'>"
        "<input type='email' name='email'><button>Submit</button></form></div>"
        "</main></body></html>"
    ).encode()
    assert detect_gates(html) == []


def test_real_wall_with_the_gate_phrase_in_the_form_section_is_detected():
    html = (
        b"<html><body><main><h1>2026 Sourcing Report</h1>"
        b"<p>A short teaser about the findings.</p>"
        b"<section class='gate'><p>To download the report, fill in the form below.</p>"
        b"<form><input type='email' name='email'><button>Get the report</button></form>"
        b"</section></main></body></html>"
    )
    (finding,) = detect_gates(html)
    assert finding.kind == "lead_gen_form_wall"
    assert any(e.startswith("phrase:") for e in finding.evidence)
    assert any(e.startswith("asset:report") for e in finding.evidence)


def test_audience_disclaimer_in_the_footer_is_not_a_gate():
    html = (
        "<html><body><main><h1>Dosage guidance</h1><p>"
        + "Published clinical guidance paragraph. " * 150
        + "</p></main><footer><p>This website is intended for healthcare professionals "
        "in the United Kingdom.</p><a href='/privacy'>Privacy</a></footer></body></html>"
    ).encode()
    assert detect_gates(html) == []


def test_audience_gate_in_a_dialog_with_a_confirm_button_is_detected():
    html = (
        "<html><body><div class='modal' role='dialog'><p>Are you a healthcare professional?"
        "</p><button>I am a healthcare professional</button><button>No</button></div>"
        "<main hidden><p>" + "Prescribing information. " * 150 + "</p></main></body></html>"
    ).encode()
    (finding,) = detect_gates(html)
    assert finding.kind == "age_or_audience_gate"
    assert "overlay:confirm_control" in finding.evidence


def test_audience_phrase_in_an_overlay_without_a_confirm_control_is_not_a_gate():
    html = (
        "<html><body><div class='modal'><p>Content for healthcare professionals.</p></div>"
        "<main><p>" + "Prescribing information. " * 150 + "</p></main></body></html>"
    ).encode()
    assert detect_gates(html) == []


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
