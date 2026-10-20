"""Group-scoped asset cache (Bit 2): rules without a browser, then one browser run."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from playwright.async_api import async_playwright

from fixtures.server import app
from src.modules.content_processor.grouping.asset_cache import AssetCache, install

CSS_HEADERS = {"content-type": "text/css"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server_url():
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


# -- rules, no browser -----------------------------------------------------------


def test_static_get_is_stored_then_served_from_cache() -> None:
    cache = AssetCache(group_id="g")
    assert cache.lookup("http://h/a.css", "stylesheet", "GET") is None
    cache.record_origin("http://h/a.css", "stylesheet", "GET", 200, CSS_HEADERS, b"body{}")
    hit = cache.lookup("http://h/a.css", "stylesheet", "GET")
    assert hit is not None and hit.body == b"body{}"
    assert (cache.stats.origin_requests, cache.stats.stored, cache.stats.cache_hits) == (1, 1, 1)


def test_documents_and_api_calls_are_never_cached() -> None:
    cache = AssetCache(group_id="g")
    cache.record_origin("http://h/page", "document", "GET", 200, {}, b"<html>")
    cache.record_origin("http://h/api", "fetch", "GET", 200, {}, b"{}")
    assert len(cache) == 0
    assert cache.stats.not_cacheable == 2
    assert cache.lookup("http://h/page", "document", "GET") is None


def test_post_is_never_cached_even_for_a_static_type() -> None:
    cache = AssetCache(group_id="g")
    cache.record_origin("http://h/a.js", "script", "POST", 200, {}, b"x")
    assert len(cache) == 0


def test_no_store_and_error_responses_are_not_kept() -> None:
    cache = AssetCache(group_id="g")
    cache.record_origin("http://h/a.js", "script", "GET", 200, {"Cache-Control": "no-store"}, b"x")
    cache.record_origin("http://h/b.js", "script", "GET", 404, {}, b"missing")
    assert len(cache) == 0
    assert cache.stats.not_cacheable == 2


def test_size_limit_skips_storage_without_failing() -> None:
    cache = AssetCache(group_id="g", max_bytes=10)
    cache.record_origin("http://h/a.js", "script", "GET", 200, {}, b"0123456789")
    cache.record_origin("http://h/b.js", "script", "GET", 200, {}, b"x")
    assert len(cache) == 1
    assert cache.stats.skipped_too_large == 1


# -- browser -----------------------------------------------------------------------


async def test_group_cache_fetches_each_static_asset_once(server_url: str) -> None:
    pages = [
        f"{server_url}/services/{s}"
        for s in ("wealth-planning", "retirement-income", "estate-planning", "tax-strategies")
    ]
    cache = AssetCache(group_id="grp-services")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for url in pages:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await install(ctx, cache)
            await page.goto(url, wait_until="load")
            heading = await page.locator("h1").inner_text()
            assert heading
            await ctx.close()
        await browser.close()

    assert cache.stats.origin_requests == 4 + 5  # four documents, five static assets
    assert cache.stats.cache_hits == 15  # 5 assets x 3 later pages
    assert len(cache) == 5
