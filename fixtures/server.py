"""Local adversarial fixture server (Stage 0).

Serves the pages under `fixtures/pages/` plus synthetic endpoints that
reproduce access and payload edge cases without touching any real host.
`fixtures/manifest.json` records the expected verdict for each path.

Run: `python -m uvicorn fixtures.server:app --port 8765`
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

PAGES = Path(__file__).with_name("pages")

app = FastAPI(title="content-processor fixtures")


@app.get("/pages/{name}")
def page(name: str) -> Response:
    """Serve a static fixture page."""
    target = PAGES / f"{name}.html"
    if not target.is_file():
        return PlainTextResponse("no such fixture", status_code=404)
    return FileResponse(target, media_type="text/html; charset=utf-8")


@app.get("/robots.txt")
def robots() -> Response:
    """robots.txt with a disallowed prefix and a crawl-delay (case 84)."""
    return PlainTextResponse("User-agent: *\nDisallow: /private/\nCrawl-delay: 1\n")


@app.get("/private/{name}")
def private_page(name: str) -> Response:
    """Disallowed by robots.txt; must be EXCLUDED under the respect policy."""
    return HTMLResponse(f"<html><body><p>Private {name}</p></body></html>")


@app.get("/challenge-200")
def challenge_200() -> Response:
    """HTTP 200 Cloudflare-style challenge page (case 79)."""
    body = (PAGES / "challenge_cf.html").read_text(encoding="utf-8")
    return HTMLResponse(body, status_code=200, headers={"cf-mitigated": "challenge"})


@app.get("/challenge-403")
def challenge_403() -> Response:
    """Akamai-style access denied with reference id."""
    body = (
        "<html><body><h1>Access Denied</h1>"
        "You don't have permission. Reference #18.4f3c</body></html>"
    )
    return HTMLResponse(body, status_code=403, headers={"set-cookie": "_abck=abc; Path=/"})


@app.get("/soft-404")
def soft_404() -> Response:
    """HTTP 200 'page not found' template (case 80)."""
    return FileResponse(PAGES / "soft_404.html", media_type="text/html; charset=utf-8")


@app.get("/pdf-but-html")
def pdf_but_html() -> Response:
    """A PDF URL that returns HTML (case 106)."""
    return HTMLResponse("<html><body>Please sign in to view this document</body></html>")


@app.get("/rate-limited")
def rate_limited() -> Response:
    """429 with Retry-After in seconds (case 77)."""
    return PlainTextResponse("slow down", status_code=429, headers={"Retry-After": "7"})


@app.get("/pay-per-crawl")
def pay_per_crawl() -> Response:
    """HTTP 402 with a crawler price header (case 183)."""
    return PlainTextResponse("payment required", status_code=402, headers={"crawler-price": "0.01"})


@app.get("/meta-refresh")
def meta_refresh() -> Response:
    """Non-HTTP redirect (case 97)."""
    return HTMLResponse(
        '<html><head><meta http-equiv="refresh" content="0; url=/pages/static_ok"></head>'
        "<body>Redirecting…</body></html>"
    )


@app.get("/slow")
async def slow(request: Request) -> Response:
    """Body that takes several seconds to finish (case 111)."""
    await asyncio.sleep(float(request.query_params.get("s", "3")))
    return PlainTextResponse("finally")


@app.get("/truncated")
def truncated() -> Response:
    """Declared Content-Length larger than the body sent (case 104)."""
    return Response(content=b"only half", media_type="text/plain", headers={"Content-Length": "40"})


@app.get("/login")
def login() -> Response:
    """Login wall target for redirect tests (case 81)."""
    return HTMLResponse(
        '<html><body><form action="/login" method="post">Sign in</form></body></html>'
    )


@app.get("/gated")
def gated() -> Response:
    """Redirects to the login page."""
    return Response(status_code=302, headers={"Location": "/login?next=/gated"})


# -- Lane L1: depth-1 follow-ups (A-1), expected type from the link (K15), T2 (A-2) --

_TINY_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)


@app.get("/downloads")
def downloads() -> Response:
    """A page whose embedded assets and linked downloads are followed to depth 1.

    Expected follow-ups: the typed PDF link (served as HTML: must fail closed as
    WRONG_PAYLOAD, K15), the brochure PDF and its byte-identical mirror (one
    payload, deduped by hash), the untyped `download` link, the logo image, the
    hot-link-blocked image (HTML for an image: WRONG_PAYLOAD) and the frame.
    Expected skips: hidden trap link, out-of-scope CDN image, video, zip,
    stylesheet and script; the plain page link is not a download.
    """
    return HTMLResponse(
        """<!doctype html><html><head><title>Downloads</title>
<link rel="stylesheet" href="/files/site.css"><script src="/files/app.js"></script></head>
<body><main><h1>Downloads</h1>
<p>Product documents for the current year.</p>
<ul>
<li><a href="/pdf-but-html" type="application/pdf">Annual report</a></li>
<li><a href="/files/brochure.pdf">Brochure</a></li>
<li><a href="/files/brochure-copy.pdf">Brochure (mirror)</a></li>
<li><a href="/files/report" download>Policy wording</a></li>
<li><a href="/files/archive.zip">All documents (zip)</a></li>
<li><a href="/pages/static_ok">Back to the overview</a></li>
<li><a href="/trap/terms.pdf" style="display:none">Terms</a></li>
</ul>
<img src="/files/logo.png" alt="Logo">
<img src="/files/logo.png" alt="Logo again">
<img src="/files/hotlink.png" alt="Chart">
<img src="https://cdn.example.net/banner.png" alt="Banner">
<video src="/files/intro.mp4"></video>
<iframe src="/pages/static_ok" title="Overview"></iframe>
</main></body></html>"""
    )


@app.get("/files/brochure.pdf")
def brochure_pdf() -> Response:
    """A real (tiny) PDF."""
    return Response(content=_TINY_PDF, media_type="application/pdf")


@app.get("/files/brochure-copy.pdf")
def brochure_copy_pdf() -> Response:
    """The same bytes at another URL: deduplicated by content hash."""
    return Response(content=_TINY_PDF, media_type="application/pdf")


@app.get("/files/report")
def report_download() -> Response:
    """An extension-less download that really is a PDF."""
    return Response(
        content=_TINY_PDF,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="policy-wording.pdf"'},
    )


@app.get("/files/logo.png")
def logo_png() -> Response:
    """A real 1x1 PNG."""
    return Response(content=_TINY_PNG, media_type="image/png")


@app.get("/files/hotlink.png")
def hotlink_png() -> Response:
    """An image URL that answers with an HTML page (hot-link protection)."""
    return HTMLResponse("<html><body><p>Hot-linking is not allowed.</p></body></html>")


@app.get("/fingerprint-wall")
def fingerprint_wall(request: Request) -> Response:
    """Blocks clients without browser client hints (T1); a Chrome impersonation passes (T2)."""
    if "sec-ch-ua" not in request.headers:
        return HTMLResponse(
            "<html><body><h1>Blocked</h1><p>Unusual traffic from your network.</p></body></html>",
            status_code=403,
        )
    return FileResponse(PAGES / "static_ok.html", media_type="text/html; charset=utf-8")


# -- K17: documents and images reach the doc worker ------------------------------


def _text_pdf(lines: list[str]) -> bytes:
    """A minimal valid one-page PDF whose text layer holds `lines` (xref offsets exact)."""
    text_ops = " ".join(
        f"BT /F1 12 Tf 72 {720 - 18 * i} Td ({line}) Tj ET" for i, line in enumerate(lines)
    )
    stream = text_ops.encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj".encode() + body + b"endobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


_ANNUAL_REPORT_PDF = _text_pdf(
    ["Annual report 2026", "Premiums grew in every region.", "Claims are paid within 30 days."]
)


@app.get("/docs-hub")
def docs_hub() -> Response:
    """Links a text PDF and embeds an image: both must be extracted by the doc worker."""
    return HTMLResponse(
        "<!doctype html><html><head><title>Reports</title></head><body><main>"
        "<h1>Reports</h1><p>Download the annual report.</p>"
        '<a href="/files/annual-report.pdf">Annual report (PDF)</a>'
        '<img src="/files/logo.png" alt="Logo">'
        "</main></body></html>"
    )


@app.get("/files/annual-report.pdf")
def annual_report_pdf() -> Response:
    """A PDF with a real text layer."""
    return Response(content=_ANNUAL_REPORT_PDF, media_type="application/pdf")
