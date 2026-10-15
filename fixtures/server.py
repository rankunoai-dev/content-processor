"""Local adversarial fixture server (Stage 0).

Serves the pages under `fixtures/pages/` plus synthetic endpoints that
reproduce access and payload edge cases without touching any real host.
`fixtures/manifest.json` records the expected verdict for each path.

Run: `python -m uvicorn fixtures.server:app --port 8765`
"""

from __future__ import annotations

import asyncio
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
