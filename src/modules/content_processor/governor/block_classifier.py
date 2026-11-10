"""Block classifier (playbook §4): runs on every response at every tier.

HTTP 200 is never a pass. The classifier looks at status, headers, cookies and
the first bytes of the body, and returns a `BlockVerdict` whose evidence list
names the exact signals that fired, so the ledger can show why.

A CAPTCHA script (reCAPTCHA, hCaptcha, Turnstile) is not a block by itself: many
sites protect their contact or newsletter forms with one, on every page. It counts
as a challenge only when the response is an error, the page asks the visitor to
prove they are human, or the page has almost no content of its own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from src.modules.content_processor.contracts import BlockVerdict
from src.modules.content_processor.enums import BlockKind

__all__ = ["classify_response"]

_HTML_TYPES = ("text/html", "application/xhtml")

# (vendor, kind, header-name regex, header-value regex)
_HEADER_SIGNALS: tuple[tuple[str, BlockKind, str, str], ...] = (
    ("cloudflare", BlockKind.JS_CHALLENGE, r"^cf-mitigated$", r"challenge"),
    ("datadome", BlockKind.JS_CHALLENGE, r"^x-datadome$", r"."),
    ("kasada", BlockKind.JS_CHALLENGE, r"^x-kpsdk-", r"."),
    ("aws_waf", BlockKind.JS_CHALLENGE, r"^x-amzn-waf-action$", r"challenge"),
    ("sucuri", BlockKind.FINGERPRINT, r"^x-sucuri-id$", r"."),
    ("cloudflare", BlockKind.PAY_PER_CRAWL, r"^crawler-price$", r"."),
)

# (vendor, kind, cookie-name regex)
_COOKIE_SIGNALS: tuple[tuple[str, BlockKind, str], ...] = (
    ("akamai", BlockKind.JS_CHALLENGE, r"^(_abck|bm_sz|ak_bmsc|bm_mi)$"),
    ("human_perimeterx", BlockKind.JS_CHALLENGE, r"^_px(3|hd|vid)$"),
    ("datadome", BlockKind.JS_CHALLENGE, r"^datadome$"),
    ("imperva", BlockKind.JS_CHALLENGE, r"^(visid_incap_|incap_ses_)"),
    ("aws_waf", BlockKind.JS_CHALLENGE, r"^aws-waf-token$"),
    ("anubis", BlockKind.JS_CHALLENGE, r"anubis"),
)

# (vendor, kind, body regex) — matched case-insensitively on the first 64 KB
_BODY_SIGNALS: tuple[tuple[str, BlockKind, str], ...] = (
    ("cloudflare", BlockKind.JS_CHALLENGE, r"just a moment|cf-chl|challenge-platform|cf_chl_opt"),
    ("cloudflare", BlockKind.FINGERPRINT, r"attention required!|error code:?\s*10(06|08|10|20)"),
    ("cloudflare", BlockKind.RATE, r"error code:?\s*1015"),
    ("akamai", BlockKind.FINGERPRINT, r"access denied.{0,200}reference #18\."),
    ("human_perimeterx", BlockKind.JS_CHALLENGE, r"px-captcha|\"blockScript\"|_pxAppId"),
    ("datadome", BlockKind.JS_CHALLENGE, r"geo\.captcha-delivery\.com|\"dd\"\s*:\s*\{"),
    ("imperva", BlockKind.JS_CHALLENGE, r"incapsula incident id|_incapsula_resource"),
    ("kasada", BlockKind.JS_CHALLENGE, r"kpsdk"),
    ("anubis", BlockKind.JS_CHALLENGE, r"making sure you're not a bot|anubis"),
    ("generic", BlockKind.FINGERPRINT, r"unusual traffic|automated access|bot detection"),
    ("generic", BlockKind.GEO_GATE, r"not available in your (country|region)"),
)
# No body pattern for login forms: a header sign-in box, a WordPress `#loginform`
# or a `newsletter-signin` class sits on ordinary article pages. An auth wall is an
# HTTP 401, a redirect to a login URL, or `gates.login_wall` (password input and
# almost no main text).

# (vendor, body regex): CAPTCHA widgets, a challenge only with one of the conditions below.
_CAPTCHA_WIDGETS: tuple[tuple[str, str], ...] = (
    ("cloudflare", r"challenges\.cloudflare\.com/turnstile"),
    ("recaptcha", r"google\.com/recaptcha|grecaptcha"),
    ("hcaptcha", r"hcaptcha\.com"),
)
_HUMAN_CHECK = re.compile(
    r"verify (that )?you are (a )?human|are you (a )?(human|robot)|"
    r"confirm (that )?you are (a )?human|(you are|you're|i am|i'm) not a robot|"
    r"attention required|complete the security check|checking your browser",
    re.IGNORECASE,
)
MINIMAL_TEXT_CHARS = 2000
"""A page with less visible text than this has no content of its own besides a CAPTCHA."""
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>([^<]*)", re.IGNORECASE)

# Both are whole-word matches: "Terrorism insurance", "Trial and error" and a street
# number ending in 404 are not error pages. A soft 404 needs the title AND the body.
_SOFT404_BODY = re.compile(
    r"page (you requested )?(was |could )?not (be )?found|\b404\b|no longer available|"
    r"this page doesn.t exist|oops!? .{0,40}(wrong|missing)",
    re.IGNORECASE,
)
_SOFT404_TITLE = re.compile(
    r"<title[^>]*>[^<]*\b(404|not found|page not found)\b[^<]*</title>", re.IGNORECASE
)
_LOGIN_PATH = re.compile(r"/(login|signin|sign-in|auth|sso)(/|\?|$)", re.IGNORECASE)


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _cookie_names(headers: dict[str, str]) -> list[str]:
    raw = headers.get("set-cookie", "")
    names: list[str] = []
    for part in re.split(r",(?=[^;,]+=)|\n", raw):
        name = part.strip().split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


def _visible_text(html: str) -> str:
    return " ".join(_TAG.sub(" ", _SCRIPT_STYLE.sub(" ", html)).split())


def _captcha_challenge(status: int, body: str, *, whole_body: bool) -> str | None:
    """Why a page carrying a CAPTCHA widget is a challenge; None when it is real content."""
    if status >= 400:
        return f"status:{status}"
    title = _TITLE.search(body)
    if title and _HUMAN_CHECK.search(title.group(1)):
        return "title:human_check"
    text = _visible_text(body)
    if _HUMAN_CHECK.search(text[:5000]):
        return "text:human_check"
    if whole_body and len(text) < MINIMAL_TEXT_CHARS:
        return f"minimal_content:{len(text)}_chars"
    return None


def classify_response(
    *,
    status: int,
    headers: Mapping[str, str],
    body_prefix: bytes,
    requested_mime_family: str = "text/html",
    final_url: str | None = None,
    body_size: int | None = None,
) -> BlockVerdict:
    """Classify one HTTP response.

    Args:
        status: HTTP status code.
        headers: Response headers (any case).
        body_prefix: First bytes of the body (up to 64 KB is enough).
        requested_mime_family: What the caller expected (e.g. `application/pdf`).
        final_url: URL after redirects, used to detect login redirects.
        body_size: Size of the whole body in bytes, when only a prefix is passed (a
            large page is never "minimal content").

    Returns:
        A verdict with `kind == NONE` only when nothing fired.
    """
    h = _lower_headers(headers)
    body = body_prefix[:65536].decode("utf-8", errors="replace")
    body_lc = body.lower()
    content_type = h.get("content-type", "").lower()
    evidence: list[str] = []
    vendor: str | None = None
    kind = BlockKind.NONE
    confidence = 0.0

    def fire(v: str, k: BlockKind, note: str, conf: float) -> None:
        nonlocal vendor, kind, confidence
        evidence.append(note)
        if conf >= confidence:
            vendor, kind, confidence = v, k, conf

    for v, k, name_re, value_re in _HEADER_SIGNALS:
        for name, value in h.items():
            if re.search(name_re, name) and re.search(value_re, value, re.IGNORECASE):
                fire(v, k, f"header:{name}", 0.95)

    for v, k, name_re in _COOKIE_SIGNALS:
        for cookie in _cookie_names(h):
            if re.search(name_re, cookie, re.IGNORECASE):
                # A vendor cookie alone is weak evidence; status/body decide.
                fire(v, k, f"cookie:{cookie}", 0.35)

    for v, k, pattern in _BODY_SIGNALS:
        if re.search(pattern, body_lc):
            fire(v, k, f"body:{pattern[:40]}", 0.8)

    for v, pattern in _CAPTCHA_WIDGETS:
        if re.search(pattern, body_lc):
            whole = body_size is None or body_size <= min(len(body_prefix), 65536)
            why = _captcha_challenge(status, body, whole_body=whole)
            if why is None:
                evidence.append(f"captcha_widget_on_content_page:{v}")
            else:
                fire(v, BlockKind.INTERACTIVE_CAPTCHA, f"captcha:{v}:{why}", 0.8)
            break

    if status == 402:
        fire("unknown", BlockKind.PAY_PER_CRAWL, "status:402", 0.95)
    elif status == 429 or (status == 503 and "retry-after" in h):
        fire(vendor or "generic", BlockKind.RATE, f"status:{status}", 0.9)
    elif status == 451:
        fire("generic", BlockKind.GEO_GATE, "status:451", 0.9)
    elif status == 401:
        fire("generic", BlockKind.AUTH_WALL, "status:401", 0.9)
    elif status in (404, 410):
        # A hard "not found" is never content, whatever the title says.
        fire("generic", BlockKind.SOFT_404, f"status:{status}", 0.9)
    elif status in (400, 405):
        # The server refused the request itself: nothing to keep, retry later.
        fire("generic", BlockKind.TRANSPORT, f"status:{status}", 0.5)
    elif status in (403, 406) and kind is BlockKind.NONE:
        fire(vendor or "generic", BlockKind.FINGERPRINT, f"status:{status}", 0.6)
    elif status == 503 and kind is BlockKind.NONE:
        fire("generic", BlockKind.TRANSPORT, "status:503", 0.5)
    elif status >= 500 and kind is BlockKind.NONE:
        fire("generic", BlockKind.TRANSPORT, f"status:{status}", 0.5)

    if final_url and _LOGIN_PATH.search(final_url) and kind is BlockKind.NONE:
        fire("generic", BlockKind.AUTH_WALL, "redirect:login_path", 0.7)

    html_returned = content_type.startswith(_HTML_TYPES) or body_lc.lstrip().startswith(
        ("<!doctype html", "<html")
    )
    if (
        status < 400
        and kind is BlockKind.NONE
        and not requested_mime_family.startswith(_HTML_TYPES)
        and html_returned
    ):
        fire("generic", BlockKind.WRONG_PAYLOAD, f"html_for:{requested_mime_family}", 0.7)

    if status < 400 and kind is BlockKind.NONE and html_returned:
        title_hit = _SOFT404_TITLE.search(body)
        body_hit = _SOFT404_BODY.search(body_lc[:20000])
        if title_hit and body_hit:
            fire("generic", BlockKind.SOFT_404, "soft404:title+body", 0.75)
        elif title_hit:
            # Title alone is a hint for the ledger, never a verdict.
            evidence.append("soft404:title_only")

    # Weak cookie-only evidence with a clean 2xx and a non-challenge body is not a block.
    if kind is not BlockKind.NONE and confidence < 0.5 and status < 400:
        return BlockVerdict(kind=BlockKind.NONE, vendor=vendor, confidence=0.0, evidence=evidence)

    return BlockVerdict(kind=kind, vendor=vendor, confidence=confidence, evidence=evidence)
