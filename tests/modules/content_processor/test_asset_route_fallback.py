"""The group asset cache never fails a page: a fetch that dies falls back or is dropped."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Error as PlaywrightError

from src.integrations.browser_client import serve_through_asset_store


class _Request:
    url = "https://x.example/app.js"
    resource_type = "script"
    method = "GET"


class _Store:
    def __init__(self, hit: Any = None) -> None:
        self.hit = hit
        self.recorded: list[str] = []

    def lookup(self, *_: object) -> Any:
        return self.hit

    def record_origin(self, url: str, *_: object) -> None:
        self.recorded.append(url)


class _Route:
    def __init__(self, *, fetch_fails: bool = False, continue_fails: bool = False) -> None:
        self.fetch_fails = fetch_fails
        self.continue_fails = continue_fails
        self.calls: list[str] = []

    async def fetch(self) -> Any:
        self.calls.append("fetch")
        if self.fetch_fails:
            raise PlaywrightError("Route.fetch: Request context disposed.")
        return self

    async def body(self) -> bytes:
        return b"x"

    status = 200
    headers: dict[str, str] = {}

    async def fulfill(self, **_: object) -> None:
        self.calls.append("fulfill")

    async def continue_(self) -> None:
        self.calls.append("continue")
        if self.continue_fails:
            raise PlaywrightError("Target page, context or browser has been closed")


async def test_fetch_and_record_on_a_miss() -> None:
    route, store = _Route(), _Store()
    await serve_through_asset_store(route, _Request(), store)
    assert route.calls == ["fetch", "fulfill"] and store.recorded == [_Request.url]


async def test_disposed_context_falls_back_to_a_normal_request() -> None:
    route, store = _Route(fetch_fails=True), _Store()
    await serve_through_asset_store(route, _Request(), store)  # must not raise
    assert route.calls == ["fetch", "continue"] and store.recorded == []


async def test_closed_page_drops_the_request_quietly() -> None:
    route = _Route(fetch_fails=True, continue_fails=True)
    await serve_through_asset_store(route, _Request(), _Store())  # must not raise
    assert route.calls == ["fetch", "continue"]
