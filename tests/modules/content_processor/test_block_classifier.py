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


@pytest.mark.parametrize(
    "title",
    [
        "Terrorism insurance: what it covers | Example",
        "Error handling guide for Python developers",
        "Trial and error: how we learned to ship",
        "Suite 1404, Market Street offices",
    ],
)
def test_error_like_words_in_a_real_title_are_not_a_soft_404(title: str):
    body = (
        f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
        "<p>A full article with a paragraph about the topic in the title. "
        "The page you requested could not be found is not written anywhere here.</p>"
        "</main></body></html>"
    ).encode()
    v = classify_response(status=200, headers={"content-type": "text/html"}, body_prefix=body)
    assert v.kind is BlockKind.NONE, (title, v)


def test_soft_404_title_alone_is_a_hint_not_a_verdict():
    body = (
        b"<html><head><title>Not found in any archive | Example</title></head><body>"
        b"<main><h1>The lost manuscripts</h1><p>An essay about documents nobody kept.</p></main>"
        b"</body></html>"
    )
    v = classify_response(status=200, headers={"content-type": "text/html"}, body_prefix=body)
    assert v.kind is BlockKind.NONE
    assert "soft404:title_only" in v.evidence


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


@pytest.mark.parametrize(
    "form",
    [
        '<form id="loginform" action="/wp-login.php" method="post">',
        '<form class="newsletter-signin" action="/subscribe">',
        '<form action="/login" method="post">',
    ],
)
def test_article_with_a_header_login_form_is_not_an_auth_wall(form: str):
    article = "<p>" + "A paragraph of the published article. " * 60 + "</p>"
    body = (
        "<html><head><title>How to brew coffee | Example</title></head><body><header>"
        f'{form}<input name="log"><input type="password" name="pwd">'
        "<button>Log in</button></form></header>"
        f"<main><article><h1>How to brew coffee</h1>{article}</article></main></body></html>"
    ).encode()
    v = classify_response(
        status=200,
        headers={"content-type": "text/html"},
        body_prefix=body,
        final_url="https://example.com/blog/how-to-brew-coffee",
    )
    assert v.kind is BlockKind.NONE, v


def test_http_401_is_an_auth_wall():
    body = b"<html><body><form action='/login'>Please sign in</form></body></html>"
    v = classify_response(status=401, headers={"content-type": "text/html"}, body_prefix=body)
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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (500, BlockKind.TRANSPORT),
        (503, BlockKind.TRANSPORT),
        (404, BlockKind.SOFT_404),
        (403, BlockKind.FINGERPRINT),
    ],
)
def test_vendor_cookie_does_not_turn_an_error_status_into_a_challenge(
    status: int, expected: BlockKind
):
    # Akamai sets bm_sz / _abck on every response of a fronted host, errors included.
    v = classify_response(
        status=status,
        headers={"content-type": "text/html", "set-cookie": "bm_sz=abc; Path=/, _abck=x; Path=/"},
        body_prefix=b"<html><body><h1>Internal server error</h1></body></html>",
    )
    assert v.kind is expected, v
    assert any(e.startswith("cookie:") for e in v.evidence)


@pytest.mark.parametrize("status", [404, 410])
def test_hard_not_found_is_never_content_whatever_the_title(status: int):
    body = (
        b"<html><head><title>Terrorism insurance explained | Example</title></head>"
        b"<body><h1>Terrorism insurance explained</h1><p>Full article text.</p></body></html>"
    )
    v = classify_response(status=status, headers={"content-type": "text/html"}, body_prefix=body)
    assert v.kind is BlockKind.SOFT_404
    assert f"status:{status}" in v.evidence


@pytest.mark.parametrize("status", [400, 405])
def test_client_errors_are_transient_failures_not_content(status: int):
    body = b"<html><head><title>Bad request</title></head><body>Bad request</body></html>"
    v = classify_response(status=status, headers={"content-type": "text/html"}, body_prefix=body)
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


_INLINE_JS = (
    "<script>window.__cfg = {label: \"I'm not a robot\", "
    + "x: 1, ".join(["y"] * 600)
    + "};</script>"
)


def test_script_text_is_not_visible_text_and_never_a_human_check():
    article = "<p>" + "The article the visitor actually reads. " * 80 + "</p>"
    body = (
        "<html><head><title>Guide</title>"
        '<script src="https://www.google.com/recaptcha/api.js"></script></head><body>'
        f"{_INLINE_JS}<main>{article}</main></body></html>"
    ).encode()
    v = classify_response(status=200, headers={}, body_prefix=body, body_size=len(body))
    assert v.kind is BlockKind.NONE, v.evidence
    assert not any("human_check" in e for e in v.evidence)


def test_inline_script_does_not_count_towards_minimal_content():
    # Over 2000 characters of JavaScript and one short line of real text: a shell.
    body = (
        "<html><head><title>App</title>"
        '<script src="https://www.google.com/recaptcha/api.js"></script></head><body>'
        f"{_INLINE_JS}<style>{'.a{color:red}' * 200}</style><div id='root'>Loading</div>"
        "</body></html>"
    ).encode()
    v = classify_response(status=200, headers={}, body_prefix=body, body_size=len(body))
    assert v.kind is BlockKind.JS_CHALLENGE
    assert any("minimal_content" in e for e in v.evidence), v.evidence


_SPA_SHELL = (
    b"<!doctype html><html><head><title>Portal</title>"
    b"<script src='https://www.google.com/recaptcha/api.js?render=6Lc_site_key'></script>"
    b"<script src='/static/js/main.3f2a.js'></script></head>"
    b"<body><noscript>You need to enable JavaScript to run this app.</noscript>"
    b"<div id='root'></div></body></html>"
)


def test_spa_shell_with_a_global_recaptcha_goes_to_the_browser_not_a_captcha_wall():
    # Server HTML of a JavaScript app: almost no text plus a site-wide reCAPTCHA
    # script. At T1/T2 that is a page the browser must render, not a CAPTCHA wall
    # (which would escalate towards paid tiers and end BLOCKED).
    v = classify_response(
        status=200,
        headers={"content-type": "text/html"},
        body_prefix=_SPA_SHELL,
        body_size=len(_SPA_SHELL),
    )
    assert v.kind is BlockKind.JS_CHALLENGE, v
    assert any(e.endswith(":app_shell") for e in v.evidence)


def test_rendered_dom_that_is_still_only_a_captcha_is_a_captcha_wall():
    body = (
        b"<html><head><script src='https://www.google.com/recaptcha/api.js'></script></head>"
        b"<body><div class='g-recaptcha'><iframe title='reCAPTCHA'></iframe></div></body></html>"
    )
    v = classify_response(
        status=200, headers={}, body_prefix=body, body_size=len(body), rendered=True
    )
    assert v.kind is BlockKind.INTERACTIVE_CAPTCHA, v


def test_large_page_cut_to_its_first_64kb_is_not_minimal():
    # A page whose first 64 KB is scripts and styles: little text in the prefix, but
    # the whole page is 300 KB, so it is not a CAPTCHA wall.
    head = b"<html><head><script src='https://www.google.com/recaptcha/api.js'></script><style>"
    prefix = (head + b"x" * 70000)[:65536]
    v = classify_response(status=200, headers={}, body_prefix=prefix, body_size=309_000)
    assert v.kind is BlockKind.NONE
    shell = prefix[:2000] + b"</style></head></html>"
    v = classify_response(status=200, headers={}, body_prefix=shell)
    assert v.kind is BlockKind.JS_CHALLENGE, "the same page when that is all the server sends"
    v = classify_response(status=200, headers={}, body_prefix=shell, rendered=True)
    assert v.kind is BlockKind.INTERACTIVE_CAPTCHA, "and a CAPTCHA wall once the browser ran it"


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
    ],
)
def test_captcha_wall_is_still_a_challenge(status: int, body: bytes, why: str):
    v = classify_response(status=status, headers={}, body_prefix=body, body_size=len(body))
    assert v.kind in (BlockKind.INTERACTIVE_CAPTCHA, BlockKind.RATE)
    if v.kind is BlockKind.INTERACTIVE_CAPTCHA:
        assert any(why in e for e in v.evidence), v.evidence


def test_minimal_content_with_a_widget_is_a_challenge_for_the_browser():
    body = (
        b"<html><head><script src='https://www.google.com/recaptcha/api.js'></script></head>"
        b"<body><div class='g-recaptcha'></div></body></html>"
    )
    v = classify_response(status=200, headers={}, body_prefix=body, body_size=len(body))
    assert v.kind is BlockKind.JS_CHALLENGE
    assert any("minimal_content" in e for e in v.evidence), v.evidence
