from __future__ import annotations

import httpx
import pytest

from fixtures.server import app
from src.integrations.http_client import FetchLimits, HttpFetcher


def _allow_all(_host: str) -> tuple[bool, str]:
    return True, "test"


@pytest.fixture
async def fetcher():
    f = HttpFetcher(
        host_checker=_allow_all,
        user_agent="test-agent/1.0",
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=10_000, total_timeout_s=5),
    )
    yield f
    await f.aclose()


async def test_static_page(fetcher: HttpFetcher):
    r = await fetcher.fetch("http://testserver/pages/static_ok")
    assert r.acquired and r.http_status == 200
    assert r.sniffed_mime == "text/html" and r.declared_mime == "text/html"
    assert r.sha256 and len(r.sha256) == 64
    assert b"Eligibility applies only to annual plans." in r.body
    assert r.hops == [] and r.final_url == "http://testserver/pages/static_ok"
    assert not r.truncated and not r.limit_hit


async def test_http_redirect_chain_is_typed(fetcher: HttpFetcher):
    r = await fetcher.fetch("http://testserver/gated")
    assert r.http_status == 200
    assert [h.kind for h in r.hops] == ["http"]
    assert r.hops[0].http_status == 302
    assert r.final_url == "http://testserver/login?next=/gated"


async def test_meta_refresh_is_followed_as_a_meta_hop(fetcher: HttpFetcher):
    r = await fetcher.fetch("http://testserver/meta-refresh")
    assert [h.kind for h in r.hops] == ["meta"]
    assert r.final_url == "http://testserver/pages/static_ok"
    assert b"Eligibility" in r.body


async def test_egress_denied_before_connecting():
    calls: list[str] = []

    def deny(host: str) -> tuple[bool, str]:
        calls.append(host)
        return False, "denied:PRIVATE"

    f = HttpFetcher(host_checker=deny, user_agent="t", transport=httpx.ASGITransport(app=app))
    try:
        r = await f.fetch("http://testserver/pages/static_ok")
    finally:
        await f.aclose()
    assert r.egress_denied == "denied:PRIVATE" and r.http_status is None
    assert calls == ["testserver"]


async def test_body_cap_sets_limit_hit():
    f = HttpFetcher(
        host_checker=_allow_all,
        user_agent="t",
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=50),
    )
    try:
        r = await f.fetch("http://testserver/pages/static_ok")
    finally:
        await f.aclose()
    assert r.limit_hit and r.received_bytes == 50


async def test_declared_length_mismatch_is_truncated():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "40", "content-type": "text/plain"},
            content=b"only half",
        )

    f = HttpFetcher(host_checker=_allow_all, user_agent="t", transport=httpx.MockTransport(handler))
    try:
        r = await f.fetch("http://example.com/truncated")
    finally:
        await f.aclose()
    assert r.truncated and r.declared_length == 40 and r.received_bytes == 9


async def test_transport_error_is_reported_not_raised():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    f = HttpFetcher(host_checker=_allow_all, user_agent="t", transport=httpx.MockTransport(handler))
    try:
        r = await f.fetch("http://example.com/x")
    finally:
        await f.aclose()
    assert r.transport_error is not None and r.transport_error.startswith("ConnectError")
    assert not r.acquired


async def test_redirect_loop_stops_at_max_hops():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    f = HttpFetcher(
        host_checker=_allow_all,
        user_agent="t",
        transport=httpx.MockTransport(handler),
        limits=FetchLimits(max_hops=3),
    )
    try:
        r = await f.fetch("http://example.com/loop")
    finally:
        await f.aclose()
    assert r.transport_error == "too_many_hops" and len(r.hops) == 4


async def test_canonical_hint_recorded_not_followed():
    def handler(_request: httpx.Request) -> httpx.Response:
        body = b'<html><head><link rel="canonical" href="/canon"></head><body>x</body></html>'
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body)

    f = HttpFetcher(host_checker=_allow_all, user_agent="t", transport=httpx.MockTransport(handler))
    try:
        r = await f.fetch("http://example.com/page?v=1")
    finally:
        await f.aclose()
    assert r.final_url == "http://example.com/page?v=1"
    assert [h.kind for h in r.hops] == ["canonical_hint"]
    assert r.hops[0].to_url == "http://example.com/canon"
