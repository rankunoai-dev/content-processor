"""Google Analytics 4 Data API v1 client: landing pages by channel and source ($0).

**Access.** A Google Cloud service account reads the property: its JSON key file
(`GA4_CREDENTIALS_FILE`) holds `client_email` and `private_key`. The client signs a
short JWT with that key (RS256, scope `analytics.readonly`, one hour) and trades it at
Google's token endpoint for an access token, kept until a minute before it expires.
No Google library is used; `cryptography` signs. The key file's contents are never
logged, echoed or put in an error message, and the signed JWT and the access token
go only to Google itself or to the stand-in on this computer (`address_refusal`).
The service account must be added to the GA4 property as a **Viewer** (Admin,
Property access management); GA4 answers HTTP 403 until it is.

**Reports.** `POST {base}v1beta/properties/{id}:runReport` with the landing page
(`landingPagePlusQueryString`) and, when asked, `sessionDefaultChannelGroup` and/or
`sessionSource`, and the metrics sessions, engagedSessions, engagementRate,
averageSessionDuration, keyEvents and totalRevenue. Pages of up to 100,000 rows are
read until `rowCount`. Every request counts against `max_requests` for the pull and
is refused before it is sent once the cap is reached; HTTP 429 and 503 are retried
twice after the server's `Retry-After`; `quota` holds the last `propertyQuota`.

**Records** (what `importer.aggregate` reads): `page`, `channel`, `source`,
`sessions`, `engaged`, `rate` (0-1), `time` (seconds per session), `events`,
`revenue`.

**Channels and sources together.** Asking GA4 for page x channel x source would
split every page's figures over many more rows. With both, two reports run instead:

1. page x channel: every metric. These records carry the page totals (`source` "").
2. page x source, sessions only. Only rows whose source is an AI assistant
   (`importer.ai_source`) are kept; the others add nothing and are dropped. Each kept
   record has `channel` "", `sessions` **0** and every other metric 0, so it adds
   nothing to the page's totals, and the AI sessions in an extra key `ai_sessions`.

With sources only, one page x source report runs and its records carry real sessions
(no `ai_sessions` key).
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from src.modules.analytics.importer import ai_source
from src.modules.research.semrush import Pacer

__all__ = [
    "BASE_URL",
    "METRICS",
    "SCOPE",
    "SHARED_PACER",
    "TOKEN_URL",
    "Ga4Client",
    "Ga4Error",
    "Ga4Settings",
    "address_refusal",
    "make_jwt",
]

BASE_URL: Final = "https://analyticsdata.googleapis.com/"
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - an address
SCOPE: Final = "https://www.googleapis.com/auth/analytics.readonly"
METRICS: Final[tuple[str, ...]] = (
    "sessions",
    "engagedSessions",
    "engagementRate",
    "averageSessionDuration",
    "keyEvents",
    "totalRevenue",
)
PAGE: Final = "landingPagePlusQueryString"
CHANNEL: Final = "sessionDefaultChannelGroup"
SOURCE: Final = "sessionSource"
PAGE_SIZE: Final = 100_000

_RETRIES: Final = 2
_BACKOFF_S: Final = 2.0
_MAX_RETRY_AFTER_S: Final = 60.0
_TOKEN_MARGIN_S: Final = 60.0
_JWT_LIFETIME_S: Final = 3600
_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_PROPERTY: Final = re.compile(r"^\d{1,20}$")

SHARED_PACER: Final = Pacer()
"""One pacer for every GA4 pull of the process (500 ms apart unless set otherwise)."""


class Ga4Error(RuntimeError):
    """GA4 refused or failed (key, access, property, quota, network), or a cap was reached."""


def address_refusal(base_url: str, token_url: str) -> str | None:
    """Why GA4 may not be called at these addresses (None when it may).

    The signed key assertion and the access token travel with the calls, so they go only
    to Google itself (https) or to a server on this computer: the stand-in.
    """
    for setting, url, real, paths in (
        ("CP_GA4_BASE_URL", base_url, BASE_URL, ("", "/")),
        ("CP_GA4_TOKEN_URL", token_url, TOKEN_URL, ("/token",)),
    ):
        parts = urlsplit(url)
        try:
            host = (parts.hostname or "").casefold()
            port = parts.port
        except ValueError:
            host, port = "", None
        google = urlsplit(real).hostname
        if (
            parts.scheme == "https"
            and host == google
            and port in (None, 443)
            and parts.path in paths
            and not parts.query
            and not parts.username
        ):
            continue
        if parts.scheme in ("http", "https") and host in _LOCAL_HOSTS:
            continue
        return (
            f"{setting} {url!r} is refused: the service account's credentials are sent only "
            f"to {real} or to the stand-in on this computer (http://127.0.0.1:...)"
        )
    return None


@dataclass(frozen=True)
class Ga4Settings:
    """The GA4 settings from the environment (`.env`)."""

    credentials_file: Path | None = None
    enabled: bool = False
    base_url: str = BASE_URL
    token_url: str = TOKEN_URL
    max_requests: int = 20
    min_interval_ms: int = 500

    @classmethod
    def from_env(cls, settings: object | None = None) -> Ga4Settings:
        """Read `GA4_CREDENTIALS_FILE` and the `CP_GA4_*` settings (defaults when unset)."""
        if settings is None:
            from src.core.config import get_settings

            settings = get_settings()
        raw = getattr(settings, "ga4_credentials_file", None)
        return cls(
            credentials_file=Path(raw) if raw else None,
            enabled=bool(getattr(settings, "cp_ga4_enabled", False)),
            base_url=str(getattr(settings, "cp_ga4_base_url", BASE_URL)),
            token_url=str(getattr(settings, "cp_ga4_token_url", TOKEN_URL)),
            max_requests=int(getattr(settings, "cp_ga4_max_requests", 20)),
            min_interval_ms=int(getattr(settings, "cp_ga4_min_interval_ms", 500)),
        )

    def refusal(self) -> str | None:
        """Why a pull may not start, or None."""
        if not self.enabled:
            return "GA4 is switched off: set CP_GA4_ENABLED=true and GA4_CREDENTIALS_FILE in .env"
        if self.credentials_file is None:
            return "no GA4 service account key is set (GA4_CREDENTIALS_FILE in .env)"
        if not self.credentials_file.is_file():
            return f"the GA4 service account key file {str(self.credentials_file)!r} is missing"
        if self.max_requests < 1:
            return "CP_GA4_MAX_REQUESTS is 0: no GA4 request may be sent"
        return address_refusal(self.base_url, self.token_url)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(client_email: str, private_key_pem: str, audience: str, now: float) -> str:
    """The RS256-signed assertion a service account trades for an access token."""
    try:
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover - cryptography is a dependency
        msg = "the cryptography library is needed to sign in to GA4 (pip install cryptography)"
        raise Ga4Error(msg) from exc
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        msg = "the GA4 service account key file has a private key that cannot be read"
        raise Ga4Error(msg) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise Ga4Error("the GA4 service account key is not an RSA key")
    iat = int(now)
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": client_email,
        "scope": SCOPE,
        "aud": audience,
        "iat": iat,
        "exp": iat + _JWT_LIFETIME_S,
    }
    signing_input = ".".join(
        _b64(json.dumps(part, separators=(",", ":")).encode("utf-8")) for part in (header, claims)
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


def _load_key(path: Path) -> tuple[str, str, str]:
    """client_email, private_key and token_uri of a key file (contents never echoed)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise Ga4Error(f"the GA4 service account key file {str(path)!r} cannot be read") from None
    except ValueError:
        msg = f"the GA4 service account key file {str(path)!r} is not JSON"
        raise Ga4Error(msg) from None
    if not isinstance(data, dict):
        raise Ga4Error(f"the GA4 service account key file {str(path)!r} is not a key file")
    email = data.get("client_email")
    key = data.get("private_key")
    if not isinstance(email, str) or not email or not isinstance(key, str) or not key:
        msg = (
            f"the file {str(path)!r} is not a service account key: it needs client_email and "
            "private_key (Google Cloud, IAM, Service accounts, Keys, Add key, JSON)"
        )
        raise Ga4Error(msg)
    uri = data.get("token_uri")
    return email, key, uri if isinstance(uri, str) and uri else TOKEN_URL


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """The server's Retry-After (seconds), else a growing pause."""
    raw = response.headers.get("retry-after", "")
    try:
        return min(_MAX_RETRY_AFTER_S, max(0.0, float(raw)))
    except ValueError:
        return _BACKOFF_S * attempt


def _google_message(response: httpx.Response) -> str:
    """Google's own error words (`error.message` or `error_description`), short."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        text = err.get("message") or err.get("status") or ""
    else:
        text = body.get("error_description") or err or ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text[:200]


def _number(value: object) -> float:
    try:
        return float(str(value))
    except ValueError:
        return 0.0


class Ga4Client:
    """Reads a GA4 property's landing pages for one pull (`max_requests` calls at most)."""

    def __init__(
        self,
        credentials_file: Path,
        *,
        base_url: str = BASE_URL,
        token_url: str | None = None,
        client: httpx.Client | None = None,
        pacer: Pacer | None = None,
        max_requests: int = 20,
        page_size: int = PAGE_SIZE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Load the key file and check the addresses; nothing is sent yet.

        `token_url` defaults to the key file's `token_uri` (Google's endpoint). Raises
        `Ga4Error` for an unreadable key or a refused address.
        """
        self.client_email, self._private_key, file_token_url = _load_key(credentials_file)
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.token_url = token_url or file_token_url
        if refusal := address_refusal(self.base_url, self.token_url):
            raise Ga4Error(refusal)
        self._client = client or httpx.Client(timeout=60.0)
        self.pacer = pacer or SHARED_PACER
        self.max_requests = max_requests
        self.page_size = max(1, min(PAGE_SIZE, page_size))
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = threading.Lock()
        self.requests = 0
        self.retries = 0
        self.token_requests = 0
        self.quota: dict[str, Any] | None = None

    def __repr__(self) -> str:
        """Names the service account only (never the key)."""
        return f"Ga4Client({self.client_email!r}, base_url={self.base_url!r})"

    # -- sign-in ------------------------------------------------------------

    def access_token(self) -> str:
        """A valid access token: the cached one, or a new one from the token endpoint."""
        with self._token_lock:
            now = self._clock()
            if self._token and now < self._token_expires - _TOKEN_MARGIN_S:
                return self._token
            assertion = make_jwt(self.client_email, self._private_key, self.token_url, now)
            self.token_requests += 1
            try:
                response = self._client.post(
                    self.token_url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
            except httpx.HTTPError as exc:
                msg = f"could not reach Google's sign-in for GA4: {type(exc).__name__}"
                raise Ga4Error(msg) from None
            if response.status_code != 200:
                said = _google_message(response)
                msg = (
                    f"Google refused the GA4 service account key (HTTP {response.status_code}"
                    f"{': ' + said if said else ''}): check that the key file is current and "
                    "not deleted in Google Cloud, and that this computer's clock is right"
                )
                raise Ga4Error(msg)
            try:
                body = response.json()
                token = str(body["access_token"])
                lifetime = float(body.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError):
                raise Ga4Error("Google's sign-in answered without an access token") from None
            self._token = token
            self._token_expires = now + lifetime
            return token

    def _forget_token(self) -> None:
        with self._token_lock:
            self._token = None
            self._token_expires = 0.0

    # -- reports ------------------------------------------------------------

    def _post_report(self, property_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}v1beta/properties/{property_id}:runReport"
        attempt = 0
        reauthed = False
        while True:
            if self.requests >= self.max_requests:
                msg = (
                    f"the GA4 request cap for this pull ({self.max_requests}, "
                    "CP_GA4_MAX_REQUESTS) is reached: nothing more was sent"
                )
                raise Ga4Error(msg)
            token = self.access_token()
            self.pacer.wait()
            self.requests += 1
            try:
                response = self._client.post(
                    url, json=dict(body), headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                msg = f"could not reach GA4 for property {property_id}: {type(exc).__name__}"
                raise Ga4Error(msg) from None
            status = response.status_code
            if status in (429, 503) and attempt < _RETRIES:
                attempt += 1
                self.retries += 1
                self.pacer.hold(_retry_after(response, attempt))
                continue
            if status == 401 and not reauthed:
                reauthed = True
                self._forget_token()
                continue
            if status != 200:
                raise Ga4Error(self._status_message(status, property_id, response))
            try:
                data = response.json()
            except ValueError:
                raise Ga4Error(f"GA4 answered property {property_id} with no JSON") from None
            if not isinstance(data, dict):
                raise Ga4Error(f"GA4 answered property {property_id} with no report")
            if isinstance(data.get("propertyQuota"), dict):
                self.quota = data["propertyQuota"]
            return data

    def _status_message(self, status: int, property_id: str, response: httpx.Response) -> str:
        said = _google_message(response)
        tail = f" GA4 said: {said}" if said else ""
        if status == 403:
            return (
                f"GA4 refused access to property {property_id} (HTTP 403): add the service "
                f"account {self.client_email} to the GA4 property as a Viewer (GA4 Admin, "
                f"Property access management), and check the Google Analytics Data API is "
                f"enabled in its Google Cloud project.{tail}"
            )
        if status in (400, 404):
            return (
                f"GA4 does not know property {property_id!r} (HTTP {status}): use the numeric "
                f"property id from GA4 Admin, Property details (not the G-... measurement "
                f"id).{tail}"
            )
        if status == 401:
            return (
                f"GA4 refused the access token for property {property_id} (HTTP 401) even "
                f"after signing in again: check the service account key.{tail}"
            )
        if status in (429, 503):
            return (
                f"GA4 is busy or the property's quota is used up (HTTP {status}, still after "
                f"{_RETRIES} retries): try again later.{tail}"
            )
        return f"GA4 failed for property {property_id} (HTTP {status}).{tail}"

    def _report(
        self,
        property_id: str,
        start: date,
        end: date,
        dimensions: Sequence[str],
        metrics: Sequence[str],
    ) -> list[tuple[list[str], list[str]]]:
        """Every row (dimension values, metric values) of one report, page by page."""
        out: list[tuple[list[str], list[str]]] = []
        offset = 0
        while True:
            body = {
                "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
                "dimensions": [{"name": d} for d in dimensions],
                "metrics": [{"name": m} for m in metrics],
                "limit": self.page_size,
                "offset": offset,
                "returnPropertyQuota": True,
            }
            data = self._post_report(property_id, body)
            rows = data.get("rows") or []
            for row in rows:
                dims = [str(v.get("value", "")) for v in row.get("dimensionValues") or []]
                mets = [str(v.get("value", "0")) for v in row.get("metricValues") or []]
                out.append((dims, mets))
            offset += len(rows)
            total = int(_number(data.get("rowCount", 0)))
            if not rows or offset >= total:
                return out

    def landing_pages(
        self,
        property_id: str,
        start: date,
        end: date,
        *,
        channels: bool = True,
        sources: bool = True,
    ) -> list[dict[str, object]]:
        """Landing page records for `importer.aggregate` (see the module notes)."""
        property_id = property_id.strip().removeprefix("properties/")
        if not _PROPERTY.match(property_id):
            msg = (
                f"{property_id!r} is not a GA4 property id: use the number from GA4 Admin, "
                "Property details (not the G-... measurement id)"
            )
            raise Ga4Error(msg)
        if start > end:
            raise Ga4Error(f"the GA4 period starts ({start}) after it ends ({end})")
        records: list[dict[str, object]] = []
        if channels or not sources:
            dims = [PAGE, CHANNEL] if channels else [PAGE]
            for values, mets in self._report(property_id, start, end, dims, METRICS):
                m = dict(zip(METRICS, mets, strict=False))
                records.append(
                    {
                        "page": values[0] if values else "",
                        "channel": values[1] if channels and len(values) > 1 else "",
                        "source": "",
                        "sessions": int(_number(m.get("sessions", 0))),
                        "engaged": int(_number(m.get("engagedSessions", 0))),
                        "rate": _number(m.get("engagementRate", 0)),
                        "time": _number(m.get("averageSessionDuration", 0)),
                        "events": _number(m.get("keyEvents", 0)),
                        "revenue": _number(m.get("totalRevenue", 0)),
                    }
                )
        if sources:
            merged = channels  # page totals came from the channel report
            metrics = ("sessions",) if merged else METRICS
            for values, mets in self._report(property_id, start, end, [PAGE, SOURCE], metrics):
                m = dict(zip(metrics, mets, strict=False))
                page = values[0] if values else ""
                source = values[1] if len(values) > 1 else ""
                sessions = int(_number(m.get("sessions", 0)))
                if merged:
                    if not ai_source(source):
                        continue
                    records.append(
                        {
                            "page": page,
                            "channel": "",
                            "source": source,
                            "sessions": 0,
                            "ai_sessions": sessions,
                            "engaged": 0,
                            "rate": 0.0,
                            "time": 0.0,
                            "events": 0.0,
                            "revenue": 0.0,
                        }
                    )
                else:
                    records.append(
                        {
                            "page": page,
                            "channel": "",
                            "source": source,
                            "sessions": sessions,
                            "engaged": int(_number(m.get("engagedSessions", 0))),
                            "rate": _number(m.get("engagementRate", 0)),
                            "time": _number(m.get("averageSessionDuration", 0)),
                            "events": _number(m.get("keyEvents", 0)),
                            "revenue": _number(m.get("totalRevenue", 0)),
                        }
                    )
        return records

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()
