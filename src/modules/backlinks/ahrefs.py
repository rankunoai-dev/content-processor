"""Ahrefs API v3 client and its unit budget.

Requests are `GET https://api.ahrefs.com/v3/<endpoint>?target=...&output=json` with the
key in the header `Authorization: Bearer <key>` (never in the address); answers are JSON.
The module calls four Site Explorer endpoints: `domain-rating`, `backlinks-stats`,
`referring-domains` and `pages-by-backlinks`.

**Units.** Ahrefs bills every returned row by the fields it carries: most fields cost 1
unit, `refdomains`-type fields 5, traffic fields 10 (none is asked by default), and a
request costs at least 50 units. Every call is priced at its worst case,
max(50, limit x the sum of its field costs), and refused when that would cross the pull
or monthly cap, before anything is sent; the actual cost (rows returned, at least 50) is
logged afterwards in `<app data>/backlinks/units.jsonl`.

The budget, the reservations under a file lock (several pulls at once cannot pass the
monthly cap together), the log and the pacer are the Research module's (`semrush.py`),
priced here for Ahrefs. Calls are paced (`CP_AHREFS_MIN_INTERVAL_MS`, one pacer for
every pull of the app) and an HTTP 429 is retried twice after `Retry-After` or a growing
pause; a refused or failed call is not charged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from src.modules.backlinks.models import BacklinkStats, RefDomain, TopPage
from src.modules.research.semrush import Pacer, UnitBudget, UnitEntry, UnitLog

__all__ = [
    "AHREFS_PACER",
    "BASE_URL",
    "ENDPOINT_FIELDS",
    "MIN_UNITS",
    "AhrefsBudget",
    "AhrefsClient",
    "AhrefsError",
    "field_cost",
    "price",
    "row_cost",
]

BASE_URL: Final = "https://api.ahrefs.com/v3/"
MIN_UNITS: Final = 50
"""Ahrefs charges at least this many units for a request."""

DOMAIN_RATING: Final = "site-explorer/domain-rating"
BACKLINKS_STATS: Final = "site-explorer/backlinks-stats"
REFERRING_DOMAINS: Final = "site-explorer/referring-domains"
TOP_PAGES: Final = "site-explorer/pages-by-backlinks"

ENDPOINT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    DOMAIN_RATING: ("domain_rating", "ahrefs_rank"),
    BACKLINKS_STATS: ("live", "all_time", "live_refdomains", "all_time_refdomains"),
    REFERRING_DOMAINS: (
        "domain",
        "domain_rating",
        "dofollow_links",
        "links_to_target",
        "first_seen",
        "last_seen",
        "is_spam",
    ),
    TOP_PAGES: (
        "url_to",
        "title_target",
        "refdomains_target",
        "links_to_target",
        "url_rating_target",
        "http_code_target",
    ),
}
"""The fields each call selects (and is billed for)."""

_FIVE: Final = frozenset(
    {
        "refdomains",
        "dofollow_refdomains",
        "refdomains_target",
        "live_refdomains",
        "all_time_refdomains",
    }
)
_TEN: Final = frozenset({"traffic_domain", "traffic_page", "traffic_source"})
_UNKNOWN_ROW: Final = 50

_RETRIES: Final = 2
_BACKOFF_S: Final = 2.0
_MAX_RETRY_AFTER_S: Final = 60.0

AHREFS_PACER: Final = Pacer()
"""One pacer for every Ahrefs pull of the process (separate from Semrush's)."""


class AhrefsError(RuntimeError):
    """Ahrefs refused or failed (key, units, network), or a cap would be crossed."""


def field_cost(field: str) -> int:
    """Units one row costs for this field (1, 5 for referring-domain counts, 10 traffic)."""
    if field in _TEN:
        return 10
    if field in _FIVE:
        return 5
    return 1


def row_cost(endpoint: str) -> int:
    """Units one row of this call costs (its selected fields together)."""
    fields = ENDPOINT_FIELDS.get(endpoint)
    if fields is None:
        return _UNKNOWN_ROW
    return sum(field_cost(f) for f in fields)


def price(endpoint: str, rows: int) -> int:
    """Units of a call returning `rows` rows: at least 50."""
    return max(MIN_UNITS, rows * row_cost(endpoint))


class AhrefsBudget(UnitBudget):
    """The Research budget, priced the Ahrefs way and worded for Ahrefs."""

    def refusal(self) -> str | None:
        """Why a pull may not start, or None."""
        if not self.enabled:
            return "Ahrefs is switched off: set CP_AHREFS_ENABLED=true and AHREFS_API_KEY in .env"
        if not self.monthly_units:
            return "no monthly Ahrefs unit cap is set (CP_AHREFS_MONTHLY_UNITS=0 means unset)"
        if self.left <= 0:
            return "the Ahrefs unit cap for this month is used up"
        return None

    def reserve(self, report: str, lines: int) -> int:
        """Reserve the worst case of a call (`lines` = its limit); raises `AhrefsError`.

        Checked under the shared lock against this pull's cap and the month's room right
        now (the log plus every pull's outstanding reservations, this one's included).
        """
        worst = price(report, lines)
        with self.log.lock():
            pending = sum(self._pending)
            room = self.pull_units - self.spent - pending
            if self.monthly_units:
                used = self.log.used(self._month) + self.log.reserved(except_key=self.key)
                room = min(room, self.monthly_units - used - pending)
            room = max(0, room)
            if worst > room:
                msg = (
                    f"{report} for up to {lines} rows could use {worst} units; only {room} "
                    "are left for this pull (pull cap or monthly cap, counting pulls running "
                    "at the same time)"
                )
                raise AhrefsError(msg)
            self._pending.append(worst)
            self.log.set_reservation(self.key, pending + worst)
        return worst


def _retry_after(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after", "")
    try:
        return min(_MAX_RETRY_AFTER_S, max(0.0, float(raw)))
    except ValueError:
        return _BACKOFF_S * attempt


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    number = _float(value)
    return max(0, int(number)) if number is not None else 0


def _rating(value: Any) -> float | None:
    number = _float(value)
    return None if number is None else max(0.0, min(100.0, number))


def _date(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 and text[4] == "-" else None


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        found = data.get("error") or data.get("message") or ""
        return str(found)[:200]
    return ""


class AhrefsClient:
    """The Site Explorer calls the Backlinks module uses, each priced before it is sent."""

    def __init__(
        self,
        api_key: str,
        budget: AhrefsBudget,
        log: UnitLog,
        project_id: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
        pacer: Pacer | None = None,
    ) -> None:
        """Bind to a key, the pull's budget and the unit log (paced by `AHREFS_PACER`)."""
        self._key = api_key
        self.budget = budget
        self.log = log
        self.project_id = project_id
        self._client = client or httpx.Client(timeout=60.0)
        self._base = base_url.rstrip("/") + "/"
        self.pacer = pacer or AHREFS_PACER
        self.retries = 0

    def _get(self, endpoint: str, params: Mapping[str, str]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        attempt = 0
        while True:
            self.pacer.wait()
            try:
                response = self._client.get(
                    self._base + endpoint, params={**params, "output": "json"}, headers=headers
                )
            except httpx.HTTPError as exc:
                msg = f"could not reach Ahrefs for {endpoint}: {type(exc).__name__}"
                raise AhrefsError(msg) from exc
            if response.status_code == 429 and attempt < _RETRIES:
                attempt += 1
                self.retries += 1
                self.pacer.hold(_retry_after(response, attempt))
                continue
            if response.status_code >= 400:
                said = _error_text(response)
                msg = f"Ahrefs refused {endpoint} (HTTP {response.status_code})" + (
                    f": {said}" if said else ""
                )
                raise AhrefsError(msg)
            return response

    def _call(
        self, endpoint: str, params: Mapping[str, str], limit: int, target: str, rows_key: str
    ) -> Any:
        """One priced call: reserve, send, read, log the actual units, let the rest go."""
        self.budget.reserve(endpoint, limit)
        try:
            response = self._get(endpoint, params)
            try:
                data = response.json()
            except ValueError as exc:
                msg = f"Ahrefs answered {endpoint} with something that is not JSON"
                raise AhrefsError(msg) from exc
            found = data.get(rows_key) if isinstance(data, dict) else None
            if found is None:
                msg = f"Ahrefs answered {endpoint} without {rows_key!r}"
                raise AhrefsError(msg)
        except Exception:
            self.budget.charge(0)  # nothing was billed: its reservation goes
            raise
        rows = len(found) if isinstance(found, list) else 1
        units = price(endpoint, rows)
        self.log.add(
            UnitEntry(
                at=datetime.now(UTC),
                project_id=self.project_id,
                report=endpoint,
                target=target,
                lines=rows,
                units=units,
            )
        )
        self.budget.charge(units)
        return found

    def domain_rating(self, domain: str, date: str) -> tuple[float | None, int | None]:
        """The domain's Domain Rating (0-100) and Ahrefs rank."""
        found = self._call(
            DOMAIN_RATING, {"target": domain, "date": date}, 1, domain, "domain_rating"
        )
        if not isinstance(found, dict):
            return None, None
        rank = _float(found.get("ahrefs_rank"))
        return _rating(found.get("domain_rating")), int(rank) if rank else None

    def backlinks_stats(self, domain: str, date: str) -> BacklinkStats:
        """Live and all-time backlinks and referring domains of the domain."""
        found = self._call(
            BACKLINKS_STATS,
            {"target": domain, "date": date, "mode": "subdomains"},
            1,
            domain,
            "metrics",
        )
        found = found if isinstance(found, dict) else {}
        return BacklinkStats(**{k: _int(found.get(k)) for k in ENDPOINT_FIELDS[BACKLINKS_STATS]})

    def referring_domains(self, domain: str, limit: int) -> list[RefDomain]:
        """Live referring domains, strongest first (up to `limit` rows)."""
        found = self._call(
            REFERRING_DOMAINS,
            {
                "target": domain,
                "mode": "subdomains",
                "history": "live",
                "select": ",".join(ENDPOINT_FIELDS[REFERRING_DOMAINS]),
                "order_by": "domain_rating:desc",
                "limit": str(limit),
            },
            limit,
            domain,
            "refdomains",
        )
        out: list[RefDomain] = []
        for row in found if isinstance(found, list) else []:
            name = str(row.get("domain") or "").strip().casefold().removeprefix("www.")
            if not name:
                continue
            out.append(
                RefDomain(
                    domain=name,
                    domain_rating=_rating(row.get("domain_rating")),
                    dofollow_links=_int(row.get("dofollow_links")),
                    links_to_target=_int(row.get("links_to_target")),
                    first_seen=_date(row.get("first_seen")),
                    last_seen=_date(row.get("last_seen")),
                    is_spam=bool(row.get("is_spam")),
                )
            )
        return out

    def top_pages(self, domain: str, limit: int) -> list[TopPage]:
        """The domain's pages with the most referring domains (up to `limit` rows)."""
        found = self._call(
            TOP_PAGES,
            {
                "target": domain,
                "mode": "subdomains",
                "select": ",".join(ENDPOINT_FIELDS[TOP_PAGES]),
                "order_by": "refdomains_target:desc",
                "limit": str(limit),
            },
            limit,
            domain,
            "pages",
        )
        out: list[TopPage] = []
        for row in found if isinstance(found, list) else []:
            url = str(row.get("url_to") or "").strip()
            if not url:
                continue
            code = _float(row.get("http_code_target"))
            out.append(
                TopPage(
                    url=url,
                    title=str(row.get("title_target") or "").strip() or None,
                    referring_domains=_int(row.get("refdomains_target")),
                    links=_int(row.get("links_to_target")),
                    url_rating=_rating(row.get("url_rating_target")),
                    http_code=int(code) if code is not None else None,
                )
            )
        return out

    def close(self) -> None:
        """Close the HTTP client and drop any reservation the pull still holds."""
        self.budget.release()
        self._client.close()
