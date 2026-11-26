"""GA4 Data API client: addresses, service-account sign-in, reports, and the stand-in."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.modules.analytics.ga4 import (
    SCOPE,
    Ga4Client,
    Ga4Error,
    Ga4Settings,
    address_refusal,
    make_jwt,
)
from src.modules.analytics.importer import aggregate, ai_source, read_ga4_csv
from src.modules.analytics.standin import (
    Ga4Dataset,
    ga4_csv,
    make_dataset,
    serve,
    write_service_account,
)
from src.modules.research.semrush import Pacer

PAGES = [f"https://www.example.com/{p}" for p in ("", "pricing", "blog/a", "blog/b", "about")]
PAGES += [f"https://www.example.com/guide/{n}" for n in range(30)]
START, END = date(2026, 10, 4), date(2026, 10, 31)


def _pacer() -> Pacer:
    return Pacer(0.0, sleep=lambda _s: None)


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


@pytest.fixture(scope="module")
def key_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_service_account(tmp_path_factory.mktemp("ga4") / "sa.json")


@pytest.fixture(scope="module")
def dataset() -> Ga4Dataset:
    return make_dataset("www.example.com", PAGES, seed="p1")


@pytest.fixture
def standin(dataset: Ga4Dataset) -> Iterator[tuple[str, Any]]:
    server = serve(dataset, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", server.RequestHandlerClass
    finally:
        server.shutdown()
        server.server_close()


def _client(key_file: Path, base: str, **kw: Any) -> Ga4Client:
    return Ga4Client(key_file, base_url=base, token_url=base + "token", pacer=_pacer(), **kw)


# -- addresses ----------------------------------------------------------------


def test_only_google_or_this_computer_may_receive_the_credentials() -> None:
    google = ("https://analyticsdata.googleapis.com/", "https://oauth2.googleapis.com/token")
    assert address_refusal(*google) is None
    assert address_refusal("https://analyticsdata.googleapis.com", google[1]) is None
    for local in ("http://127.0.0.1:8791/", "http://localhost:9/", "https://[::1]:8443/"):
        assert address_refusal(local, local + "token") is None
    for base, token in (
        ("http://analyticsdata.googleapis.com/", google[1]),
        ("https://analyticsdata.googleapis.com.evil.io/", google[1]),
        ("https://evil.io/", google[1]),
        (google[0], "https://evil.io/token"),
        (google[0], "https://oauth2.googleapis.com/other"),
        (google[0], "http://oauth2.googleapis.com/token"),
        ("http://10.0.0.5/", google[1]),
    ):
        reason = address_refusal(base, token)
        assert reason and "refused" in reason


def test_the_client_refuses_a_foreign_address_before_sending(key_file: Path) -> None:
    with pytest.raises(Ga4Error, match="CP_GA4_TOKEN_URL"):
        Ga4Client(key_file, token_url="https://evil.io/token")


def test_settings_have_defaults_before_config_knows_them(tmp_path: Path) -> None:
    s = Ga4Settings.from_env(object())
    assert s.base_url == "https://analyticsdata.googleapis.com/"
    assert s.token_url == "https://oauth2.googleapis.com/token"
    assert (s.max_requests, s.min_interval_ms, s.enabled) == (20, 500, False)
    assert s.refusal() and "switched off" in (s.refusal() or "")

    class Env:
        ga4_credentials_file = tmp_path / "missing.json"
        cp_ga4_enabled = True

    refusal = Ga4Settings.from_env(Env()).refusal()
    assert refusal and "missing" in refusal


# -- sign-in -----------------------------------------------------------------


def test_the_jwt_is_rs256_and_verifies_with_the_public_key(key_file: Path) -> None:
    data = json.loads(key_file.read_text(encoding="utf-8"))
    jwt = make_jwt(data["client_email"], data["private_key"], "https://aud/token", 1_000_000.7)
    head, body, sig = jwt.split(".")
    assert json.loads(_unb64(head)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_unb64(body)) == {
        "iss": data["client_email"],
        "scope": SCOPE,
        "aud": "https://aud/token",
        "iat": 1_000_000,
        "exp": 1_003_600,
    }
    key = serialization.load_pem_private_key(data["private_key"].encode(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    key.public_key().verify(
        _unb64(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_a_bad_key_file_is_named_without_its_contents(tmp_path: Path) -> None:
    bad = tmp_path / "sa.json"
    bad.write_text(json.dumps({"client_email": "x@y", "private_key": "SECRET-NOT-PEM"}))
    client = Ga4Client(bad, client=httpx.Client(transport=httpx.MockTransport(lambda r: None)))  # type: ignore[arg-type]
    with pytest.raises(Ga4Error) as caught:
        client.access_token()
    assert "SECRET" not in str(caught.value)
    assert "SECRET" not in repr(client)
    bad.write_text("not json SECRET")
    with pytest.raises(Ga4Error) as caught:
        Ga4Client(bad)
    assert "SECRET" not in str(caught.value)


def test_the_access_token_is_cached_until_shortly_before_it_expires(key_file: Path) -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": f"t{len(seen)}", "expires_in": 3600})

    now = [1000.0]
    client = Ga4Client(
        key_file,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    assert client.token_url == "https://oauth2.googleapis.com/token"  # from the key file
    assert client.access_token() == "t1"
    now[0] += 3000
    assert client.access_token() == "t1"
    now[0] += 560  # within a minute of expiry
    assert client.access_token() == "t2"
    assert client.token_requests == 2
    assert seen[0]["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assert seen[0]["assertion"][0].count(".") == 2


def test_a_refused_key_is_reported_plainly(key_file: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
        )

    client = Ga4Client(key_file, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(Ga4Error, match=r"refused the GA4 service account key.*Invalid JWT"):
        client.access_token()


# -- reports against the stand-in --------------------------------------------


def test_landing_pages_by_channel_page_through_the_rows(
    key_file: Path, dataset: Ga4Dataset, standin: tuple[str, Any]
) -> None:
    base, handler = standin
    client = _client(key_file, base, page_size=25)
    records = client.landing_pages("123456", START, END, channels=True, sources=False)
    pairs = {(x.page, x.channel) for x in dataset.lines}
    assert len(records) == len(pairs) > 25
    assert len(handler.calls) == -(-len(pairs) // 25)  # every page of 25 rows asked once
    assert [c["offset"] for c in handler.calls] == [25 * i for i in range(len(handler.calls))]
    assert handler.calls[0]["dimensions"] == [
        {"name": "landingPagePlusQueryString"},
        {"name": "sessionDefaultChannelGroup"},
    ]
    assert handler.calls[0]["returnPropertyQuota"] is True
    assert set(records[0]) == {
        "page", "channel", "source", "sessions", "engaged", "rate", "time", "events", "revenue",
    }  # fmt: skip
    assert sum(int(str(r["sessions"])) for r in records) == sum(x.sessions for x in dataset.lines)
    assert client.quota and client.quota["tokensPerDay"]["remaining"] > 0
    assert client.token_requests == 1


def test_channels_and_sources_merge_without_double_counting(
    key_file: Path, dataset: Ga4Dataset, standin: tuple[str, Any]
) -> None:
    base, handler = standin
    client = _client(key_file, base)
    records = client.landing_pages("123456", START, END, channels=True, sources=True)
    assert len(handler.calls) == 2
    assert handler.calls[1]["metrics"] == [{"name": "sessions"}]
    by_source = [r for r in records if r["source"]]
    assert by_source and all(r["sessions"] == 0 and ai_source(str(r["source"])) for r in by_source)
    want_ai = sum(x.sessions for x in dataset.lines if ai_source(x.source))
    assert sum(int(str(r["ai_sessions"])) for r in by_source) == want_ai
    rows = aggregate(records, "example.com")
    assert sum(r.sessions for r in rows) == sum(
        x.sessions for x in dataset.lines if x.page != "(not set)"
    )
    assert sum(r.organic_sessions for r in rows) == sum(
        x.sessions for x in dataset.lines if x.channel == "Organic Search"
    )


def test_sources_alone_give_real_sessions(
    key_file: Path, dataset: Ga4Dataset, standin: tuple[str, Any]
) -> None:
    base, _ = standin
    records = _client(key_file, base).landing_pages(
        "123456", START, END, channels=False, sources=True
    )
    rows = aggregate(records, "example.com")
    want = sum(x.sessions for x in dataset.lines if ai_source(x.source))
    assert want and sum(r.ai_sessions for r in rows) == want
    assert all("ai_sessions" not in r for r in records)


def test_the_request_cap_refuses_before_sending(key_file: Path, standin: tuple[str, Any]) -> None:
    base, handler = standin
    client = _client(key_file, base, page_size=10, max_requests=2)
    with pytest.raises(Ga4Error, match=r"request cap.*\(2, CP_GA4_MAX_REQUESTS\)"):
        client.landing_pages("123456", START, END, channels=True, sources=False)
    assert len(handler.calls) == 2


def test_a_429_is_retried_after_retry_after(key_file: Path, dataset: Ga4Dataset) -> None:
    server = serve(dataset, 0, slow_down_every=2)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/"
        slept: list[float] = []
        client = Ga4Client(
            key_file,
            base_url=base,
            token_url=base + "token",
            pacer=Pacer(0.0, sleep=slept.append),
        )
        records = client.landing_pages("123456", START, END, channels=True, sources=True)
        assert records and client.retries == 1 and client.requests == 3
        assert slept and max(slept) > 0.5  # held for the server's Retry-After: 1
    finally:
        server.shutdown()
        server.server_close()


def test_the_standin_wants_its_bearer_token(standin: tuple[str, Any]) -> None:
    base, _ = standin
    url = base + "v1beta/properties/1:runReport"
    assert httpx.post(url, json={}).status_code == 401
    assert httpx.post(url, json={}, headers={"Authorization": "Bearer made-up"}).status_code == 401


def _mock(status: int, message: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(status, json={"error": {"code": status, "message": message}})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("status", "words"),
    [
        (403, r"add the service account standin@.* as a Viewer"),
        (400, r"does not know property '123456'.*numeric property id"),
        (401, r"refused the access token.*after signing in again"),
    ],
)
def test_refusals_are_explained_in_plain_words(key_file: Path, status: int, words: str) -> None:
    base = "http://127.0.0.1:9/"
    client = _client(key_file, base, client=_mock(status, "Google says no"))
    with pytest.raises(Ga4Error, match=words) as caught:
        client.landing_pages("123456", START, END, channels=True, sources=False)
    assert "Google says no" in str(caught.value)
    if status == 401:
        assert client.token_requests == 2  # signed in again once


def test_a_property_id_that_is_not_a_number_is_refused_before_sending(key_file: Path) -> None:
    client = _client(key_file, "http://127.0.0.1:9/", client=_mock(200, ""))
    with pytest.raises(Ga4Error, match="not a GA4 property id"):
        client.landing_pages("G-ABC123", START, END)
    assert client.requests == 0


# -- the stand-in's data -----------------------------------------------------


def test_the_dataset_is_deterministic_and_the_earlier_period_has_decays() -> None:
    a = make_dataset("example.com", PAGES, seed="p1")
    assert a == make_dataset("example.com", PAGES, seed="p1")
    assert a != make_dataset("example.com", PAGES, seed="p2")
    assert {x.channel for x in a.lines} == {
        "Organic Search", "Direct", "Referral", "Paid Search", "Email",
    }  # fmt: skip
    assert {"chatgpt.com", "perplexity.ai", "gemini.google.com"} <= {x.source for x in a.lines}
    before = make_dataset("example.com", PAGES, seed="p1", period=1)
    assert (before.start, before.end) == (date(2026, 9, 6), date(2026, 10, 3))
    assert a.decays

    def organic(d: Ga4Dataset, page: str) -> int:
        return sum(x.sessions for x in d.lines if x.page == page and x.channel == "Organic Search")

    for page, metric in a.decays.items():
        if metric == "organic_sessions":
            assert organic(a, page) < 0.8 * organic(before, page)


def test_the_csv_reads_back_with_the_same_totals(dataset: Ga4Dataset) -> None:
    data = ga4_csv(dataset)
    text = data.decode()
    assert "# Start date: 20261004" in text and "# End date: 20261031" in text
    assert text.splitlines()[11].startswith("Grand total,")
    assert "https://" not in text.split("# End date")[1]
    snap = read_ga4_csv(
        data, project_id="p1", website="example.com", now=datetime.now(UTC), snapshot_id="s1"
    )
    assert (snap.start_date, snap.end_date) == (START, END)
    lines = [x for x in dataset.lines if x.page != "(not set)"]
    assert snap.sessions == sum(x.sessions for x in lines)
    assert sum(r.engaged_sessions for r in snap.rows) == sum(x.engaged for x in lines)
    assert sum(r.key_events for r in snap.rows) == pytest.approx(sum(x.events for x in lines))
    assert sum(r.revenue for r in snap.rows) == pytest.approx(
        sum(x.revenue for x in lines), abs=0.05
    )
    assert sum(r.organic_sessions for r in snap.rows) == sum(
        x.sessions for x in lines if x.channel == "Organic Search"
    )
    assert len(snap.rows) == len({x.page for x in lines})
