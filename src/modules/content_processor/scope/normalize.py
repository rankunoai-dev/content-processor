"""URL identity (case 91/92/94): schedule keys that never discard the requested URL.

Two keys are produced:

* `schedule_key` decides what is fetched once. It is conservative: path case
  and trailing slashes are preserved because servers treat them as distinct.
* `near_duplicate_key` is looser and is used only for grouping and reporting.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from src.core.schemas import StrictModel

__all__ = ["InvalidInputUrl", "NormalizedUrl", "TRACKING_PARAMS", "normalize_url"]

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
        "hsCtaTracking",
        "mkt_tok",
        "vero_id",
        "ref_src",
        "spm",
    }
)
_TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "matomo_")
_FRAGMENT_ROUTE = re.compile(r"^(!|/)")


class InvalidInputUrl(ValueError):
    """The input cannot be scheduled; the reason is preserved for the ledger."""

    def __init__(self, raw: str, reason: str) -> None:
        """Keep the raw string and a stable reason code."""
        self.raw = raw
        self.reason = reason
        super().__init__(f"{reason}: {raw[:120]}")


class NormalizedUrl(StrictModel):
    """Identity triple for one requested URL."""

    requested: str
    schedule_key: str
    near_duplicate_key: str
    host: str
    fragment_route: str | None
    dropped_params: list[str]


def _is_tracking(name: str) -> bool:
    return name in TRACKING_PARAMS or name.lower().startswith(_TRACKING_PREFIXES)


def _normalize_host(host: str, raw: str) -> str:
    host = host.strip().rstrip(".").lower()
    if not host:
        raise InvalidInputUrl(raw, "missing_host")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidInputUrl(raw, "invalid_idn_host") from exc


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    # Decode then re-encode so %7E and ~ compare equal; keep case and slashes.
    return quote(unquote(path), safe="/:@!$&'()*+,;=-._~%")


def normalize_url(raw: str) -> NormalizedUrl:
    """Validate and normalize one input URL.

    Raises:
        InvalidInputUrl: For unsupported schemes, missing hosts, over-long or
            malformed inputs. The caller records the reason as an exclusion.
    """
    candidate = raw.strip()
    if not candidate:
        raise InvalidInputUrl(raw, "empty")
    if len(candidate) > MAX_URL_LENGTH:
        raise InvalidInputUrl(raw, "too_long")
    if any(ch.isspace() for ch in candidate):
        raise InvalidInputUrl(raw, "contains_whitespace")

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidInputUrl(raw, f"unsupported_scheme:{scheme or 'none'}")
    host = _normalize_host(parts.hostname or "", raw)

    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidInputUrl(raw, "invalid_port") from exc
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
    if parts.username or parts.password:
        raise InvalidInputUrl(raw, "credentials_in_url")

    path = _normalize_path(parts.path)

    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if _is_tracking(name):
            dropped.append(name)
        else:
            kept.append((name, value))
    kept.sort()
    query = urlencode(kept, doseq=True)

    fragment_route = (
        parts.fragment if parts.fragment and _FRAGMENT_ROUTE.match(parts.fragment) else None
    )

    schedule_key = urlunsplit((scheme, netloc, path, query, ""))

    loose_path = path.lower()
    if loose_path.endswith(("/index.html", "/index.htm", "/index.php")):
        loose_path = loose_path.rsplit("/", 1)[0] + "/"
    if len(loose_path) > 1 and loose_path.endswith("/"):
        loose_path = loose_path.rstrip("/")
    loose_host = host[4:] if host.startswith("www.") else host
    near_duplicate_key = urlunsplit(("https", loose_host, loose_path, query, ""))

    return NormalizedUrl(
        requested=candidate,
        schedule_key=schedule_key,
        near_duplicate_key=near_duplicate_key,
        host=host,
        fragment_route=fragment_route,
        dropped_params=dropped,
    )
