from __future__ import annotations

from pathlib import Path

from src.modules.content_processor.governor.gates import detect_gates

PAGES = Path(__file__).resolve().parents[3] / "fixtures" / "pages"


def _page(name: str) -> bytes:
    return (PAGES / f"{name}.html").read_bytes()


def test_lead_gen_form_wall_detected_with_evidence():
    findings = detect_gates(_page("gated_whitepaper"))
    kinds = [f.kind for f in findings]
    assert kinds == ["lead_gen_form_wall"]
    f = findings[0]
    assert f.severity == "blocks_main_content"
    assert f.selector_hint == "#gate-form"
    assert any(e.startswith("cta:") for e in f.evidence)
    assert any(e.startswith("asset:") for e in f.evidence)


def test_newsletter_form_is_not_a_wall():
    assert detect_gates(_page("newsletter_only")) == []


def test_pincode_modal_detected_as_overlay():
    findings = detect_gates(
        _page("pincode_modal"), inner_text="Enter your pincode to check delivery availability"
    )
    assert [f.kind for f in findings] == ["geo_pincode_modal"]
    assert "overlay" in findings[0].evidence
    assert findings[0].severity == "partial"


def test_product_unavailable_detected():
    findings = detect_gates(_page("product_unavailable"))
    assert [f.kind for f in findings] == ["product_unavailable"]


def test_age_gate_detected_and_blocks_hidden_main():
    findings = detect_gates(
        _page("age_gate"), inner_text="Are you over 18? Please confirm your age to enter. Yes No"
    )
    assert [f.kind for f in findings] == ["age_or_audience_gate"]
    assert findings[0].severity == "blocks_main_content"


def test_login_wall_detected():
    html = (
        b"<html><body><main><form action='/login'><input name='user'>"
        b"<input type='password' name='pw'><button>Sign in</button></form></main></body></html>"
    )
    findings = detect_gates(html)
    assert [f.kind for f in findings] == ["login_wall"]


def test_clean_pages_have_no_gates():
    for name in ("static_ok", "icon_font", "details_collapsed", "third_party_widget"):
        assert detect_gates(_page(name)) == [], name
