"""T2 impersonated fetcher (A-2).

Unit tests drive the shared chain through an injected session. The live tests
use the real curl_cffi against loopback servers only, and skip when the
optional `unblock` extra is not installed.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from src.integrations import impersonated_client as ic
from src.integrations.http_client import FetchLimits
from src.integrations.impersonated_client import (
    ImpersonatedFetcher,
    ImpersonationUnavailableError,
    impersonation_available,
)


class FakeHeaders:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self._pairs)


class FakeResponse:
    def __init__(self, status: int, headers: list[tuple[str, str]], chunks: list[Any]) -> None:
        self.status_code = status
        self.headers = FakeHeaders(headers)
        self._chunks = chunks

    async def aiter_content(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class FakeSession:
    def __init__(self, routes: dict[str, FakeResponse | Exception]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: Any) -> AsyncIterator[FakeResponse]:
        self.calls.append((url, kwargs))
        target = self.routes[url]
        if isinstance(target, Exception):
            raise target
        yield target

    async def close(self) -> None:
        self.closed = True


class PartialFile(Exception):
    code = 18


def _fetcher(routes, **kwargs) -> tuple[ImpersonatedFetcher, FakeSession]:
    session = FakeSession(routes)
    fetcher = ImpersonatedFetcher(
        host_checker=lambda host: (host != "blocked.test", "metadata"),
        session=session,
        **kwargs,
    )
    return fetcher, session


async def test_follows_redirects_and_passes_impersonation_options():
    fetcher, session = _fetcher(
        {
            "https://a.test/start": FakeResponse(302, [("Location", "/end")], []),
            "https://a.test/end": FakeResponse(
                200,
                [
                    ("Content-Type", "text/html"),
                    ("Set-Cookie", "a=1"),
                    ("Set-Cookie", "b=2"),
                ],
                [b"<html><body>", b"ok</body></html>"],
            ),
        },
        impersonate="safari",
        accept_language="de-DE,de;q=0.9",
        limits=FetchLimits(connect_timeout_s=3, read_timeout_s=7),
    )
    result = await fetcher.fetch("https://a.test/start")
    assert result.http_status == 200 and result.final_url == "https://a.test/end"
    assert [h.kind for h in result.hops] == ["http"]
    assert result.headers["set-cookie"] == "a=1, b=2"
    assert result.sniffed_mime == "text/html"
    _, opts = session.calls[0]
    assert opts == {
        "impersonate": "safari",
        "allow_redirects": False,
        "headers": {"Accept-Language": "de-DE,de;q=0.9"},
        "timeout": (3.0, 7.0),
    }
    assert fetcher.tier_name == "T2_IMPERSONATED_HTTP"
    await fetcher.aclose()
    assert session.closed


async def test_egress_is_checked_on_every_hop():
    fetcher, _ = _fetcher(
        {"https://a.test/x": FakeResponse(301, [("location", "https://blocked.test/")], [])}
    )
    result = await fetcher.fetch("https://a.test/x")
    assert result.egress_denied == "metadata" and result.final_url == "https://blocked.test/"


async def test_transport_errors_become_data_and_other_errors_raise():
    fetcher, _ = _fetcher(
        {"https://a.test/down": ConnectionRefusedError("refused"), "https://a.test/bug": KeyError()}
    )
    result = await fetcher.fetch("https://a.test/down")
    assert result.transport_error == "ConnectionRefusedError:refused"
    with pytest.raises(KeyError):
        await fetcher.fetch("https://a.test/bug")


async def test_partial_body_is_marked_truncated():
    fetcher, _ = _fetcher(
        {"https://a.test/t": FakeResponse(200, [], [b"only half", PartialFile("closed")])}
    )
    result = await fetcher.fetch("https://a.test/t")
    assert result.truncated and result.body == b"only half" and result.transport_error is None


async def test_body_cap_applies():
    fetcher, _ = _fetcher(
        {"https://a.test/big": FakeResponse(200, [], [b"x" * 6, b"y" * 6])},
        limits=FetchLimits(max_bytes=8),
    )
    result = await fetcher.fetch("https://a.test/big")
    assert result.limit_hit and result.body == b"xxxxxxyy"


def test_missing_curl_cffi_is_reported(monkeypatch):
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    assert impersonation_available() is False
    with pytest.raises(ImpersonationUnavailableError, match="unblock"):
        ImpersonatedFetcher(host_checker=lambda _h: (True, "t"))


def test_plain_items_headers_are_flattened():
    assert ic._flatten_headers({"X-A": "1"}) == {"x-a": "1"}


# -- live: real curl_cffi on loopback -----------------------------------------


@pytest.fixture(scope="module")
def fixture_server() -> Iterator[str]:
    pytest.importorskip("curl_cffi")
    uvicorn = pytest.importorskip("uvicorn")
    from fixtures.server import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _live() -> ImpersonatedFetcher:
    return ImpersonatedFetcher(
        host_checker=lambda _h: (True, "loopback"),
        limits=FetchLimits(connect_timeout_s=5, read_timeout_s=5, total_timeout_s=15),
    )


async def test_live_impersonation_passes_the_client_hints_wall(fixture_server: str):
    assert impersonation_available()
    fetcher = _live()
    try:
        result = await fetcher.fetch(f"{fixture_server}/fingerprint-wall")
        gated = await fetcher.fetch(f"{fixture_server}/gated")
    finally:
        await fetcher.aclose()
    assert result.http_status == 200 and b"Eligibility applies" in result.body
    assert gated.final_url.endswith("/login?next=/gated") and [h.kind for h in gated.hops] == [
        "http"
    ]


async def test_live_truncated_body_is_detected():
    pytest.importorskip("curl_cffi")

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(4096)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 40\r\n\r\nonly half"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    fetcher = _live()
    try:
        result = await fetcher.fetch(f"http://127.0.0.1:{port}/x")
    finally:
        await fetcher.aclose()
        server.close()
    assert result.truncated and result.body == b"only half"
