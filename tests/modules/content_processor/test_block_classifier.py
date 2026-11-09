from __future__ import annotations

from pathlib import Path

import pytest

from src.modules.content_processor.enums import BlockKind
from src.modules.content_processor.governor.block_classifier import classify_response

PAGES = Path(__file__).resolve().parents[3] / "fixtures" / "pages"


def _page(name: str) -> bytes:
    return (PAGES / f"{name}.html").read_bytes()


def test_clean_html_is_not_a_block():
    v = classify_response(
        status=200, headers={"Content-Type": "text/html"}, body_prefix=_page("static_ok")
    )
    assert v.kind is BlockKind.NONE


def test_http_200_cloudflare_challenge_is_blocked():
    v = classify_response(
        status=200,
        headers={"content-type": "text/html", "cf-mitigated": "challenge", "server": "cloudflare"},
        body_prefix=_page("challenge_cf"),
    )
    assert v.kind is BlockKind.JS_CHALLENGE
    assert v.vendor == "cloudflare"
    assert any(e.startswith("header:cf-mitigated") for e in v.evidence)


def test_akamai_access_denied_403():
    body = (
        b"<html><body><h1>Access Denied</h1>"
        b"You don't have permission. Reference #18.4f3c</body></html>"
    )
    v = classify_response(status=403, headers={"set-cookie": "_abck=x; Path=/"}, body_prefix=body)
    assert v.kind is BlockKind.FINGERPRINT
    assert v.vendor == "akamai"


def test_soft_404_from_title_and_body():
    v = classify_response(
        status=200, headers={"content-type": "text/html"}, body_prefix=_page("soft_404")
    )
    assert v.kind is BlockKind.SOFT_404
    assert v.confidence >= 0.7


def test_html_for_pdf_request_is_wrong_payload():
    v = classify_response(
        status=200,
        headers={"content-type": "text/html"},
        body_prefix=b"<html><body>Please sign in to view this document</body></html>",
        requested_mime_family="application/pdf",
    )
    assert v.kind is BlockKind.WRONG_PAYLOAD


def test_rate_limited_429():
    v = classify_response(status=429, headers={"Retry-After": "7"}, body_prefix=b"slow down")
    assert v.kind is BlockKind.RATE


def test_pay_per_crawl_402():
    v = classify_response(status=402, headers={"crawler-price": "0.01"}, body_prefix=b"")
    assert v.kind is BlockKind.PAY_PER_CRAWL


def test_login_redirect_is_auth_wall():
    v = classify_response(
        status=200,
        headers={"content-type": "text/html"},
        body_prefix=b"<html><body><form action='/login' method='post'>Sign in</form></body></html>",
        final_url="https://example.com/login?next=/gated",
    )
    assert v.kind is BlockKind.AUTH_WALL


def test_vendor_cookie_alone_on_clean_200_is_not_a_block():
    v = classify_response(
        status=200,
        headers={"content-type": "text/html", "set-cookie": "bm_sz=abc; Path=/"},
        body_prefix=_page("static_ok"),
    )
    assert v.kind is BlockKind.NONE
    assert any(e.startswith("cookie:bm_sz") for e in v.evidence)


@pytest.mark.parametrize("status", [500, 502, 504])
def test_5xx_is_transport(status: int):
    v = classify_response(status=status, headers={}, body_prefix=b"error")
    assert v.kind is BlockKind.TRANSPORT


def test_turnstile_is_interactive_captcha():
    body = b'<html><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></html>'
    v = classify_response(status=403, headers={"server": "cloudflare"}, body_prefix=body)
    assert v.kind is BlockKind.INTERACTIVE_CAPTCHA


def _content_page(widget: str) -> bytes:
    """A real article (like GEP's blog) whose footer form loads a CAPTCHA widget."""
    article = (
        "<p>" + "Omnichannel transformation helps pharma companies reach doctors. " * 80 + "</p>"
    )
    return (
        "<!doctype html><html><head><title>A Guide to Omnichannel Transformation | GEP Blog</title>"
        f'<script src="{widget}"></script></head><body><article><h1>Omnichannel</h1>{article}'
        '</article><form class="newsletter"><div class="g-recaptcha"></div></form></body></html>'
    ).encode()


@pytest.mark.parametrize(
    "widget",
    [
        "https://www.google.com/recaptcha/api.js?render=explicit",
        "https://js.hcaptcha.com/1/api.js",
        "https://challenges.cloudflare.com/turnstile/v0/api.js",
    ],
)
def test_captcha_widget_on_a_content_page_is_not_a_block(widget: str):
    v = classify_response(
        status=200, headers={"content-type": "text/html"}, body_prefix=_content_page(widget)
    )
    assert v.kind is BlockKind.NONE
    assert any(e.startswith("captcha_widget_on_content_page") for e in v.evidence)


def test_large_page_cut_to_its_first_64kb_is_not_minimal():
    # A page whose first 64 KB is scripts and styles: little text in the prefix, but
    # the whole page is 300 KB, so it is not a CAPTCHA wall.
    head = b"<html><head><script src='https://www.google.com/recaptcha/api.js'></script><style>"
    prefix = (head + b"x" * 70000)[:65536]
    v = classify_response(status=200, headers={}, body_prefix=prefix, body_size=309_000)
    assert v.kind is BlockKind.NONE
    v = classify_response(
        status=200, headers={}, body_prefix=prefix[:2000] + b"</style></head></html>"
    )
    assert v.kind is BlockKind.INTERACTIVE_CAPTCHA, "the same page when that is all there is"


@pytest.mark.parametrize(
    ("status", "body", "why"),
    [
        (403, _content_page("https://www.google.com/recaptcha/api.js"), "status:403"),
        (429, _content_page("https://www.google.com/recaptcha/api.js"), "status:429"),
        (
            200,
            b"<html><head><title>Verify you are human</title>"
            b"<script src='https://www.google.com/recaptcha/api.js'></script></head><body>"
            + b"<p>Some text.</p>" * 400
            + b"</body></html>",
            "title:human_check",
        ),
        (
            200,
            b"<html><head><script src='https://js.hcaptcha.com/1/api.js'></script></head><body>"
            b"<h1>Please confirm you are not a robot</h1>"
            b"<div class='h-captcha'></div></body></html>",
            "text:human_check",
        ),
        (
            200,
            b"<html><head><script src='https://www.google.com/recaptcha/api.js'></script></head>"
            b"<body><div class='g-recaptcha'></div></body></html>",
            "minimal_content",
        ),
    ],
)
def test_captcha_wall_is_still_a_challenge(status: int, body: bytes, why: str):
    v = classify_response(status=status, headers={}, body_prefix=body, body_size=len(body))
    assert v.kind in (BlockKind.INTERACTIVE_CAPTCHA, BlockKind.RATE)
    if v.kind is BlockKind.INTERACTIVE_CAPTCHA:
        assert any(why in e for e in v.evidence), v.evidence
