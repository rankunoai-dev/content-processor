"""A GA4 stand-in: dummy landing-page figures in GA4's own formats, for trying the module.

Nothing here is real analytics data. It lets the analytics module run end to end
(imports, API pulls, rollups, decays, AI referrals) before a GA4 property is connected.

- `make_dataset` invents, for every page given, a period's sessions split over the
  default channel groups (Organic Search, Direct, Referral, Paid Search, Email) and
  their session sources, engaged sessions, engagement time, key events and revenue.
  A few pages get referrals from AI assistants (chatgpt.com, perplexity.ai,
  gemini.google.com). It is deterministic: the same seed and pages give the same
  numbers. `period` 0 is the latest 28 days, 1 the 28 days before, and so on; some
  pages **decay** (were 30-60% better in the earlier periods, in organic sessions or in
  key events), so period-over-period checks have something to find. A small
  "(not set)" landing page is included, as GA4 has one.
- `ga4_csv` writes the file a person downloads from GA4 (Reports, Engagement, Landing
  page, Download CSV): `#` comment lines with the dates, then the table by landing page
  and channel, "Grand total" first.
- `serve` answers the Data API on this computer only (127.0.0.1): `POST /token` gives a
  fake access token for any well-formed service-account assertion, and
  `POST /v1beta/properties/<id>:runReport` answers in the API's own shape, honouring
  the dimensions, metrics, `limit` and `offset` asked for; 401 without a token it gave
  out. `slow_down_every` answers every n-th report with HTTP 429.
- `write_service_account` writes a throwaway service-account key file with a freshly
  generated RSA key, to sign in to the stand-in.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import re
import secrets
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from src.modules.analytics.ga4 import SCOPE, TOKEN_URL

__all__ = [
    "CHANNELS",
    "DUMMY_NOTE",
    "Ga4Dataset",
    "Ga4Line",
    "ga4_csv",
    "make_dataset",
    "serve",
    "write_service_account",
]

DUMMY_NOTE: Final = (
    "DUMMY DATA from the GA4 stand-in: invented numbers for trying the module, not GA4 figures"
)
CHANNELS: Final[tuple[str, ...]] = ("Organic Search", "Direct", "Referral", "Paid Search", "Email")
AI_REFERRERS: Final[tuple[str, ...]] = ("chatgpt.com", "perplexity.ai", "gemini.google.com")
_OTHER_REFERRERS: Final[tuple[str, ...]] = ("linkedin.com", "reddit.com", "news.example.org")
_SOURCE: Final[dict[str, str]] = {
    "Organic Search": "google",
    "Direct": "(direct)",
    "Paid Search": "google",
    "Email": "newsletter",
}
_ANCHOR: Final = date(2026, 10, 31)
_DAYS: Final = 28
_GA4_DEFAULT_LIMIT: Final = 10_000
_GA4_MAX_LIMIT: Final = 250_000
_TOKENS_PER_DAY: Final = 200_000
_DIMENSIONS: Final[dict[str, str]] = {
    "landingPagePlusQueryString": "page",
    "landingPage": "page",
    "sessionDefaultChannelGroup": "channel",
    "sessionSource": "source",
}
_METRIC_TYPES: Final[dict[str, str]] = {
    "sessions": "TYPE_INTEGER",
    "engagedSessions": "TYPE_INTEGER",
    "engagementRate": "TYPE_FLOAT",
    "averageSessionDuration": "TYPE_SECONDS",
    "userEngagementDuration": "TYPE_SECONDS",
    "keyEvents": "TYPE_FLOAT",
    "totalRevenue": "TYPE_CURRENCY",
}
_REPORT_PATH: Final = re.compile(r"^/v1beta/properties/([^/:]+):runReport$")


class _Draw:
    """Deterministic numbers from a text (the same input always gives the same draw)."""

    def __init__(self, *parts: str) -> None:
        self._seed = hashlib.sha256("\x1f".join(parts).encode()).digest()
        self._n = 0

    def unit(self) -> float:
        """A number in [0, 1)."""
        self._n += 1
        digest = hashlib.sha256(self._seed + self._n.to_bytes(4, "big")).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def between(self, low: float, high: float) -> float:
        return low + (high - low) * self.unit()

    def log_between(self, low: float, high: float) -> float:
        return math.exp(self.between(math.log(low), math.log(high)))


@dataclass
class Ga4Line:
    """One landing page x channel x source over the period (the finest grain kept)."""

    page: str
    channel: str
    source: str
    sessions: int
    engaged: int
    seconds: float  # engagement time of all its sessions together
    events: float
    revenue: float


@dataclass
class Ga4Dataset:
    """One period of a website's invented GA4 figures."""

    website: str
    start: date
    end: date
    period: int
    lines: list[Ga4Line] = field(default_factory=list)
    decays: dict[str, str] = field(default_factory=dict)  # page path -> metric that decays
    note: str = DUMMY_NOTE

    def report(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """A runReport answer (HTTP status, JSON) for a request body."""
        dims = [str(d.get("name", "")) for d in body.get("dimensions") or []]
        mets = [str(m.get("name", "")) for m in body.get("metrics") or []]
        bad = [d for d in dims if d not in _DIMENSIONS] + [
            m for m in mets if m not in _METRIC_TYPES
        ]
        if bad or not mets:
            what = ", ".join(bad) if bad else "no metrics"
            return 400, _error(400, f"Field {what} is not a valid dimension or metric.")
        try:
            limit = int(body.get("limit") or _GA4_DEFAULT_LIMIT)
            offset = int(body.get("offset") or 0)
        except (TypeError, ValueError):
            return 400, _error(400, "limit and offset must be integers.")
        if limit < 1 or limit > _GA4_MAX_LIMIT or offset < 0:
            return 400, _error(400, "limit must be 1 to 250000 and offset at least 0.")
        groups: dict[tuple[str, ...], list[float]] = {}
        for line in self.lines:
            key = tuple(str(getattr(line, _DIMENSIONS[d])) for d in dims)
            agg = groups.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0.0])
            agg[0] += line.sessions
            agg[1] += line.engaged
            agg[2] += line.seconds
            agg[3] += line.events
            agg[4] += line.revenue
        ordered = sorted(groups.items(), key=lambda kv: (-kv[1][0], kv[0]))
        chosen = ordered[offset : offset + limit]
        rows = [
            {
                "dimensionValues": [{"value": v} for v in key],
                "metricValues": [{"value": _metric(m, agg)} for m in mets],
            }
            for key, agg in chosen
        ]
        answer: dict[str, Any] = {
            "dimensionHeaders": [{"name": d} for d in dims],
            "metricHeaders": [{"name": m, "type": _METRIC_TYPES[m]} for m in mets],
            "metadata": {"currencyCode": "USD", "timeZone": "America/New_York"},
            "kind": "analyticsData#runReport",
        }
        if rows:  # GA4 leaves rows and rowCount out of an empty report
            answer["rows"] = rows
            answer["rowCount"] = len(ordered)
        return 200, answer


def _metric(name: str, agg: list[float]) -> str:
    sessions, engaged, seconds, events, revenue = agg
    if name == "sessions":
        return str(int(sessions))
    if name == "engagedSessions":
        return str(int(engaged))
    if name == "engagementRate":
        return repr(round(engaged / sessions, 6)) if sessions else "0"
    if name == "averageSessionDuration":
        return repr(round(seconds / sessions, 6)) if sessions else "0"
    if name == "userEngagementDuration":
        return str(round(seconds))
    if name == "keyEvents":
        return repr(round(events, 2))
    return repr(round(revenue, 2))


def _error(code: int, message: str) -> dict[str, Any]:
    status = {
        400: "INVALID_ARGUMENT",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        429: "RESOURCE_EXHAUSTED",
    }
    return {"error": {"code": code, "message": message, "status": status.get(code, "UNKNOWN")}}


def _path(page: str) -> str:
    """A landing page as GA4 shows it: path and query, no host."""
    parts = urlsplit(page.strip())
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return f"{path}?{parts.query}" if parts.query else path


def _shares(draw: _Draw) -> dict[str, float]:
    raw = {
        "Organic Search": draw.between(0.3, 0.7),
        "Direct": draw.between(0.1, 0.3),
        "Referral": draw.between(0.03, 0.1),
        "Paid Search": draw.between(0.0, 0.15) if draw.unit() < 0.6 else 0.0,
        "Email": draw.between(0.0, 0.08) if draw.unit() < 0.5 else 0.0,
    }
    total = sum(raw.values())
    return {c: v / total for c, v in raw.items()}


def make_dataset(
    website: str,
    pages: Sequence[str],
    *,
    seed: str,
    period: int = 0,
    end: date | None = None,
) -> Ga4Dataset:
    """Invented GA4 figures for the pages (URLs or paths) of a website over one period.

    `end` is the last day of the latest period (31 October 2026 unless given); period n
    is the 28 days ending 28 x n days before it.
    """
    last = (end or _ANCHOR) - timedelta(days=_DAYS * period)
    data = Ga4Dataset(
        website=website.removeprefix("www."),
        start=last - timedelta(days=_DAYS - 1),
        end=last,
        period=period,
    )
    paths = list(dict.fromkeys(_path(p) for p in pages))
    for path in paths:
        base = _Draw(seed, "page", path)
        top = 20000 if path == "/" else 4000
        sessions = base.log_between(20, top)
        shares = _shares(base)
        rate = base.between(0.35, 0.8)
        avg_s = base.between(20, 180)
        event_rate = 0.0 if base.unit() < 0.25 else base.between(0.002, 0.05)
        order_value = base.between(20, 300) if base.unit() < 0.3 else 0.0
        ai = base.unit() < 0.3
        ai_share = base.between(0.2, 0.7) if ai else 0.0

        decay = _Draw(seed, "decay", path)
        kind = ""
        if decay.unit() < 0.25:
            kind = "organic_sessions" if decay.unit() < 0.6 else "key_events"
            data.decays[path] = kind
        drop = decay.between(0.3, 0.6)
        if period:  # earlier periods: a little different, and better before a decay
            jitter = _Draw(seed, "period", str(period), path).between(0.9, 1.1)
            sessions *= jitter
            if kind == "organic_sessions":
                organic = shares["Organic Search"] / (1 - drop)
                shares = {c: (organic if c == "Organic Search" else s) for c, s in shares.items()}
                sessions *= sum(shares.values())
                total = sum(shares.values())
                shares = {c: s / total for c, s in shares.items()}
            elif kind == "key_events":
                event_rate /= 1 - drop

        for channel in CHANNELS:
            n = round(sessions * shares[channel])
            if n <= 0:
                continue
            if channel == "Referral":
                split = _referrals(_Draw(seed, "referral", path), n, ai_share)
            else:
                split = [(_SOURCE[channel], n)]
            for source, count in split:
                if count <= 0:
                    continue
                d = _Draw(seed, "line", str(period), path, channel, source)
                engaged = min(count, round(count * rate * d.between(0.85, 1.15)))
                events = float(round(count * event_rate * d.between(0.8, 1.2)))
                data.lines.append(
                    Ga4Line(
                        page=path,
                        channel=channel,
                        source=source,
                        sessions=count,
                        engaged=engaged,
                        seconds=round(count * avg_s * d.between(0.8, 1.2), 1),
                        events=events,
                        revenue=round(events * order_value, 2),
                    )
                )
    if paths:  # GA4 always has a few sessions with no landing page
        n = 3 + int(_Draw(seed, "not set", str(period)).between(0, 40))
        data.lines.append(Ga4Line("(not set)", "Direct", "(direct)", n, 0, 0.0, 0.0, 0.0))
    return data


def _referrals(draw: _Draw, sessions: int, ai_share: float) -> list[tuple[str, int]]:
    """Referral sessions split between AI assistants (for some pages) and other sites."""
    out: list[tuple[str, int]] = []
    left = sessions
    if ai_share:
        ai_total = max(1, round(sessions * ai_share))
        weights = [draw.between(0.2, 1.0) for _ in AI_REFERRERS]
        for source, w in zip(AI_REFERRERS, weights, strict=True):
            n = min(left, round(ai_total * w / sum(weights)))
            out.append((source, n))
            left -= n
    for i, source in enumerate(_OTHER_REFERRERS):
        n = left if i == len(_OTHER_REFERRERS) - 1 else round(left * draw.between(0.2, 0.6))
        out.append((source, n))
        left -= n
    return out


def _num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def ga4_csv(dataset: Ga4Dataset) -> bytes:
    """The Landing page report as GA4 downloads it (page x channel, paths, Grand total)."""
    groups: dict[tuple[str, str], list[float]] = {}
    for line in dataset.lines:
        agg = groups.setdefault((line.page, line.channel), [0.0, 0.0, 0.0, 0.0, 0.0])
        agg[0] += line.sessions
        agg[1] += line.engaged
        agg[2] += line.seconds
        agg[3] += line.events
        agg[4] += line.revenue
    total = [sum(a[i] for a in groups.values()) for i in range(5)]
    out = io.StringIO()
    out.write(
        "# ----------------------------------------\n"
        "# Landing page\n"
        "# Account: Stand-in\n"
        f"# Property: {dataset.website} (stand-in)\n"
        f"# {dataset.note}\n"
        "# ----------------------------------------\n"
        "#\n"
        "# All Users\n"
        f"# Start date: {dataset.start:%Y%m%d}\n"
        f"# End date: {dataset.end:%Y%m%d}\n"
    )
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        [
            "Landing page + query string",
            "Session default channel group",
            "Sessions",
            "Engaged sessions",
            "Engagement rate",
            "Average engagement time per session",
            "Key events",
            "Total revenue",
        ]
    )

    def row(page: str, channel: str, agg: list[float]) -> list[str]:
        sessions, engaged, seconds, events, revenue = agg
        return [
            page,
            channel,
            str(int(sessions)),
            str(int(engaged)),
            f"{engaged / sessions:.4f}" if sessions else "0",
            f"{seconds / sessions:.4f}" if sessions else "0",
            _num(events),
            _num(round(revenue, 2)),
        ]

    writer.writerow(row("Grand total", "", total))
    for (page, channel), agg in sorted(groups.items(), key=lambda kv: (-kv[1][0], kv[0])):
        writer.writerow(row(page, channel, agg))
    return out.getvalue().encode("utf-8")


def write_service_account(
    path: Path,
    *,
    client_email: str = "standin@standin-project.iam.gserviceaccount.com",
    token_uri: str = TOKEN_URL,
) -> Path:
    """Write a throwaway service-account key file with a new RSA key (for the stand-in)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    data = {
        "type": "service_account",
        "project_id": "standin-project",
        "private_key_id": secrets.token_hex(20),
        "private_key": pem,
        "client_email": client_email,
        "client_id": str(secrets.randbelow(10**20)),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": token_uri,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _claims(assertion: str) -> dict[str, Any] | None:
    """The claims of a JWT (not verified: the stand-in does not know the public key)."""
    parts = assertion.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(raw)
    except ValueError:
        return None
    return claims if isinstance(claims, dict) else None


class _Handler(BaseHTTPRequestHandler):
    datasets: list[Ga4Dataset]
    slow_down_every: int = 0
    calls: list[dict[str, Any]]
    tokens: set[str]
    state: dict[str, int]
    lock: threading.Lock

    def _send(
        self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self) -> None:  # noqa: N802 - the http.server name
        path = urlsplit(self.path).path
        raw = self._body()
        if path == "/token":
            self._token(raw)
            return
        found = _REPORT_PATH.match(path)
        if not found:
            self._send(404, _error(404, f"{path} is not a stand-in address"))
            return
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        with self.lock:
            known = token in self.tokens
        if not known:
            msg = "Request is missing required authentication credential (stand-in)."
            self._send(401, _error(401, msg))
            return
        if not found.group(1).isdigit():
            self._send(400, _error(400, f"Invalid property: {found.group(1)}."))
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send(400, _error(400, "The body is not JSON."))
            return
        if not isinstance(body, dict):
            self._send(400, _error(400, "The body is not a report request."))
            return
        with self.lock:
            self.calls.append(body)
            count = len(self.calls)
        if self.slow_down_every and count % self.slow_down_every == 0:
            msg = "Exhausted concurrent requests quota (stand-in)."
            self._send(429, _error(429, msg), {"Retry-After": "1"})
            return
        status, answer = self._dataset(body).report(body)
        if status == 200:
            with self.lock:
                cost = 1 + len(answer.get("rows", [])) // 1000
                self.state["tokens"] += cost
                used = self.state["tokens"]
            answer["propertyQuota"] = {
                "tokensPerDay": {"consumed": cost, "remaining": max(0, _TOKENS_PER_DAY - used)},
                "tokensPerHour": {"consumed": cost, "remaining": max(0, 40_000 - used)},
                "concurrentRequests": {"consumed": 0, "remaining": 10},
                "serverErrorsPerProjectPerHour": {"consumed": 0, "remaining": 10},
                "potentiallyThresholdedRequestsPerHour": {"consumed": 0, "remaining": 120},
                "tokensPerProjectPerHour": {"consumed": cost, "remaining": max(0, 14_000 - used)},
            }
        self._send(status, answer)

    def _dataset(self, body: dict[str, Any]) -> Ga4Dataset:
        """The period the request's date range ends on (else the first one)."""
        ranges = body.get("dateRanges") or [{}]
        wanted = str(ranges[0].get("endDate", "")) if isinstance(ranges[0], dict) else ""
        return next((d for d in self.datasets if d.end.isoformat() == wanted), self.datasets[0])

    def _token(self, raw: bytes) -> None:
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        claims = _claims(form.get("assertion", ""))
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer" or not claims:
            self._send(400, {"error": "invalid_grant", "error_description": "Invalid JWT."})
            return
        if claims.get("scope") != SCOPE or not claims.get("iss"):
            msg = "Invalid JWT: scope or issuer missing."
            self._send(400, {"error": "invalid_scope", "error_description": msg})
            return
        token = "standin-" + secrets.token_hex(16)
        with self.lock:
            self.tokens.add(token)
            self.state["token_calls"] += 1
        self._send(200, {"access_token": token, "expires_in": 3599, "token_type": "Bearer"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature
        """Quiet: the module reports what it asked for itself."""


def serve(
    dataset: Ga4Dataset | Sequence[Ga4Dataset], port: int = 8791, *, slow_down_every: int = 0
) -> ThreadingHTTPServer:
    """A stand-in Data API on 127.0.0.1 (only this computer); call `serve_forever()` on it.

    Give several periods (`make_dataset(..., period=0)`, `period=1`, ...) to answer each
    request with the period its date range ends on. The handler class keeps `calls` (the
    report request bodies) and `state` (token calls, quota tokens used) for inspection.
    """
    datasets = [dataset] if isinstance(dataset, Ga4Dataset) else list(dataset)
    if not datasets:
        raise ValueError("the stand-in needs at least one dataset")
    handler = type(
        "Ga4StandinHandler",
        (_Handler,),
        {
            "datasets": datasets,
            "slow_down_every": slow_down_every,
            "calls": [],
            "tokens": set(),
            "state": {"tokens": 0, "token_calls": 0},
            "lock": threading.Lock(),
        },
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
