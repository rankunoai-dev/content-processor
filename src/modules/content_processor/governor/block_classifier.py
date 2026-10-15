"""Block classifier (playbook §4): runs on every response at every tier.

HTTP 200 is never a pass. The classifier looks at status, headers, cookies and
the first bytes of the body, and returns a `BlockVerdict` whose evidence list
names the exact signals that fired, so the ledger can show why.
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
    ("cloudflare", BlockKind.INTERACTIVE_CAPTCHA, r"challenges\.cloudflare\.com/turnstile"),
    ("akamai", BlockKind.FINGERPRINT, r"access denied.{0,200}reference #18\."),
    ("human_perimeterx", BlockKind.JS_CHALLENGE, r"px-captcha|\"blockScript\"|_pxAppId"),
    ("datadome", BlockKind.JS_CHALLENGE, r"geo\.captcha-delivery\.com|\"dd\"\s*:\s*\{"),
    ("imperva", BlockKind.JS_CHALLENGE, r"incapsula incident id|_incapsula_resource"),
    ("kasada", BlockKind.JS_CHALLENGE, r"kpsdk"),
    ("anubis", BlockKind.JS_CHALLENGE, r"making sure you're not a bot|anubis"),
    ("recaptcha", BlockKind.INTERACTIVE_CAPTCHA, r"google\.com/recaptcha|grecaptcha"),
    ("hcaptcha", BlockKind.INTERACTIVE_CAPTCHA, r"hcaptcha\.com"),
    ("generic", BlockKind.FINGERPRINT, r"unusual traffic|automated access|bot detection"),
    ("generic", BlockKind.GEO_GATE, r"not available in your (country|region)"),
    ("generic", BlockKind.AUTH_WALL, r"<form[^>]+(login|signin|sign-in)"),
)

_SOFT404_BODY = re.compile(
    r"page (you requested )?(was |could )?not (be )?found|404|no longer available|"
    r"this page doesn.t exist|oops!? .{0,40}(wrong|missing)",
    re.IGNORECASE,
)
_SOFT404_TITLE = re.compile(r"<title>[^<]*(not found|404|error)[^<]*</title>", re.IGNORECASE)
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


def classify_response(
    *,
    status: int,
    headers: Mapping[str, str],
    body_prefix: bytes,
    requested_mime_family: str = "text/html",
    final_url: str | None = None,
) -> BlockVerdict:
    """Classify one HTTP response.

    Args:
        status: HTTP status code.
        headers: Response headers (any case).
        body_prefix: First bytes of the body (up to 64 KB is enough).
        requested_mime_family: What the caller expected (e.g. `application/pdf`).
        final_url: URL after redirects, used to detect login redirects.

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

    if status == 402:
        fire("unknown", BlockKind.PAY_PER_CRAWL, "status:402", 0.95)
    elif status == 429 or (status == 503 and "retry-after" in h):
        fire(vendor or "generic", BlockKind.RATE, f"status:{status}", 0.9)
    elif status == 451:
        fire("generic", BlockKind.GEO_GATE, "status:451", 0.9)
    elif status == 401:
        fire("generic", BlockKind.AUTH_WALL, "status:401", 0.9)
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
            fire("generic", BlockKind.SOFT_404, "soft404:title", 0.5)

    # Weak cookie-only evidence with a clean 2xx and a non-challenge body is not a block.
    if kind is not BlockKind.NONE and confidence < 0.5 and status < 400:
        return BlockVerdict(kind=BlockKind.NONE, vendor=vendor, confidence=0.0, evidence=evidence)

    return BlockVerdict(kind=kind, vendor=vendor, confidence=confidence, evidence=evidence)
