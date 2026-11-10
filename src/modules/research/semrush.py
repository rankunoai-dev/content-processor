"""Semrush Analytics API v3 client and its unit budget.

Requests are `GET https://api.semrush.com/?type=<report>&key=...&export_columns=...`;
answers are text, one line per row, fields separated by `;`, with a header line. An
answer starting with `ERROR` is an error (`ERROR 50 :: NOTHING FOUND` means no rows).
Every call is priced at its worst case (the line limit times the report's units per
line, from `UNITS_PER_LINE`) and refused when that would cross the pull or monthly cap,
before anything is sent; the actual cost (lines returned) is logged afterwards.

The monthly cap holds across concurrent pulls (two browser tabs, the CLI and the app):
a call's worst case is **reserved** in `units.reserved.json` next to the log, under a
lock other processes respect, and every reservation counts the month's room until the
call is logged. A reservation left behind by a crashed pull expires after an hour.

Calls are **paced**: one `Pacer` shared by every pull of the app server waits so two
calls are at least `CP_SEMRUSH_MIN_INTERVAL_MS` apart (500 ms: two a second), well under
Semrush's limit, so pulls run steadily instead of in bursts. An HTTP 429 (too many
requests) is retried twice, after the server's `Retry-After` or a growing pause; the
units of a refused call are not charged.

The per-line costs below follow Semrush's documentation where it states them (organic
results and domain overview 10 units, keyword difficulty 50); check them against your
plan. An unknown report is priced at 50 units per line.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx

from src.core.schemas import StrictModel
from src.modules.analyst.policy import FileLock

__all__ = [
    "BASE_URL",
    "SHARED_PACER",
    "Pacer",
    "UNITS_PER_LINE",
    "SemrushClient",
    "SemrushError",
    "UnitBudget",
    "UnitEntry",
    "UnitLog",
]

BASE_URL: Final = "https://api.semrush.com/"
UNITS_PER_LINE: Final[dict[str, int]] = {
    "domain_ranks": 10,
    "domain_organic": 10,
    "phrase_this": 10,
    "phrase_organic": 10,
    "phrase_kdi": 50,
    "domain_organic_organic": 40,
}
_UNKNOWN_UNITS = 50
_LOCK = threading.Lock()
RESERVATION_TTL: Final = timedelta(hours=1)
"""A reservation older than this belongs to a pull that died: it no longer counts."""


_RETRIES: Final = 2
_BACKOFF_S: Final = 2.0
_MAX_RETRY_AFTER_S: Final = 60.0


class Pacer:
    """Keeps calls at least `interval_s` apart (thread-safe; shared by every pull)."""

    def __init__(
        self,
        interval_s: float = 0.5,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind to a minimum interval; `clock` and `sleep` are injectable for tests."""
        self.interval_s = interval_s
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()
        self.waited_s = 0.0
        self.calls = 0

    def wait(self) -> float:
        """Block until the next call may go; returns how long it waited."""
        with self._lock:
            now = self._clock()
            pause = max(0.0, self._next - now)
            if pause:
                self._sleep(pause)
            self._next = max(now, self._next) + self.interval_s
            self.waited_s += pause
            self.calls += 1
            return pause

    def hold(self, seconds: float) -> None:
        """Push the next call back (after a 429 or a Retry-After)."""
        with self._lock:
            self._next = max(self._next, self._clock() + seconds)


SHARED_PACER: Final = Pacer()


class SemrushError(RuntimeError):
    """Semrush refused or failed (key, units, network), or a cap would be crossed."""


class UnitEntry(StrictModel):
    """One Semrush call, as logged."""

    at: datetime
    project_id: str
    report: str
    target: str
    lines: int
    units: int


class UnitLog:
    """Append-only JSON lines of every Semrush call (`<root>/units.jsonl`), and the reservations.

    `units.reserved.json` holds, per running pull, the worst-case units of its calls
    that are not logged yet; `units.lock` serialises every look at both files, across
    threads and processes.
    """

    def __init__(self, root: Path) -> None:
        """Bind to the research folder."""
        self.path = root / "units.jsonl"
        self.reserved_path = root / "units.reserved.json"
        self.lock_path = root / "units.lock"

    def lock(self) -> FileLock:
        """The lock to hold while reading the month's use and writing a reservation."""
        return FileLock(self.lock_path)

    def add(self, entry: UnitEntry) -> None:
        """Log one call."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")

    def _reservations(self) -> dict[str, dict[str, Any]]:
        if not self.reserved_path.exists():
            return {}
        try:
            data = json.loads(self.reserved_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def reserved(self, *, except_key: str = "", now: datetime | None = None) -> int:
        """Units reserved by other running pulls (reservations older than an hour expire)."""
        moment = now or datetime.now(UTC)
        total = 0
        for key, item in self._reservations().items():
            if key == except_key:
                continue
            try:
                at = datetime.fromisoformat(str(item["at"]))
                units = int(item["units"])
            except (KeyError, TypeError, ValueError):
                continue
            if moment - at <= RESERVATION_TTL:
                total += units
        return total

    def set_reservation(self, key: str, units: int, now: datetime | None = None) -> None:
        """Write one pull's outstanding reservation (0 removes it). Call under `lock()`."""
        data = self._reservations()
        if units > 0:
            data[key] = {"units": units, "at": (now or datetime.now(UTC)).isoformat()}
        else:
            data.pop(key, None)
        self.reserved_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.reserved_path.with_name(f"{self.reserved_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.reserved_path)

    def used(self, since: datetime) -> int:
        """Units used since a moment."""
        if not self.path.exists():
            return 0
        total = 0
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    entry = UnitEntry.model_validate(json.loads(line))
                    if entry.at >= since:
                        total += entry.units
        return total


class UnitBudget:
    """What one pull may use: the smaller of its own cap and the month's room.

    The month's room is checked again, under the shared lock, every time a call is
    reserved: the log plus every other pull's outstanding reservations. So two pulls
    running at once cannot both pass the cap with the same units.
    """

    def __init__(
        self,
        log: UnitLog,
        *,
        enabled: bool,
        monthly_units: int,
        pull_units: int,
        now: datetime | None = None,
    ) -> None:
        """Read this month's use once, at the start of the pull."""
        moment = now or datetime.now(UTC)
        self._month = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        self.log = log
        self.key = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.enabled = enabled
        self.monthly_units = monthly_units
        self.month_left = 0
        if monthly_units:
            with log.lock():
                self.month_left = max(0, monthly_units - log.used(self._month) - log.reserved())
        self.pull_units = pull_units
        self.spent = 0
        self._pending: list[int] = []

    @property
    def left(self) -> int:
        """Units this pull may still use (from the month's room as it was at the start)."""
        return max(0, min(self.pull_units, self.month_left) - self.spent)

    def refusal(self) -> str | None:
        """Why a pull may not start, or None."""
        if not self.enabled:
            return (
                "Semrush is switched off: set CP_SEMRUSH_ENABLED=true and SEMRUSH_API_KEY in .env"
            )
        if not self.monthly_units:
            return "no monthly Semrush unit cap is set (CP_SEMRUSH_MONTHLY_UNITS=0 means unset)"
        if self.left <= 0:
            return "the Semrush unit cap for this month is used up"
        return None

    def reserve(self, report: str, lines: int) -> int:
        """Reserve the worst-case units of a call; raises `SemrushError` if it does not fit.

        Checked under the shared lock against this pull's cap and the month's room right
        now (the log plus every pull's outstanding reservations, this one's included);
        the reservation is written before the lock is released.
        """
        worst = lines * UNITS_PER_LINE.get(report, _UNKNOWN_UNITS)
        with self.log.lock():
            pending = sum(self._pending)
            room = self.pull_units - self.spent - pending
            if self.monthly_units:
                used = self.log.used(self._month) + self.log.reserved(except_key=self.key)
                room = min(room, self.monthly_units - used - pending)
            room = max(0, room)
            if worst > room:
                msg = (
                    f"{report} for up to {lines} lines could use {worst} units; only {room} "
                    "are left for this pull (pull cap or monthly cap, counting pulls running "
                    "at the same time)"
                )
                raise SemrushError(msg)
            self._pending.append(worst)
            self.log.set_reservation(self.key, pending + worst)
        return worst

    def charge(self, units: int) -> None:
        """Count a call's actual units and let its reservation go (the log holds it now)."""
        self.spent += units
        with self.log.lock():
            if self._pending:
                self._pending.pop(0)
            self.log.set_reservation(self.key, sum(self._pending))

    def release(self) -> None:
        """Drop whatever this pull still had reserved (at the end of the pull)."""
        self._pending.clear()
        with self.log.lock():
            self.log.set_reservation(self.key, 0)


def parse_rows(text: str) -> list[dict[str, str]]:
    """Rows of a Semrush answer (header line, `;` separated); [] for NOTHING FOUND."""
    stripped = text.strip()
    if stripped.startswith("ERROR"):
        if "NOTHING FOUND" in stripped.upper():
            return []
        raise SemrushError(f"Semrush answered: {stripped[:200]}")
    if not stripped:
        return []
    reader = csv.DictReader(io.StringIO(stripped), delimiter=";")
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """The server's Retry-After (seconds), else a growing pause."""
    raw = response.headers.get("retry-after", "")
    try:
        return min(_MAX_RETRY_AFTER_S, max(0.0, float(raw)))
    except ValueError:
        return _BACKOFF_S * attempt


class SemrushClient:
    """The Analytics API reports the Research module uses, each priced before it is sent."""

    def __init__(
        self,
        api_key: str,
        budget: UnitBudget,
        log: UnitLog,
        project_id: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
        pacer: Pacer | None = None,
    ) -> None:
        """Bind to a key, the pull's budget and the unit log (paced by `SHARED_PACER`)."""
        self._key = api_key
        self.budget = budget
        self.log = log
        self.project_id = project_id
        self._client = client or httpx.Client(timeout=60.0)
        self._base = base_url
        self.pacer = pacer or SHARED_PACER
        self.retries = 0

    def _call(
        self, report: str, params: Mapping[str, str], lines: int, target: str
    ) -> list[dict[str, str]]:
        self.budget.reserve(report, lines)
        query = {"type": report, "key": self._key, "display_limit": str(lines), **params}
        attempt = 0
        try:
            while True:
                self.pacer.wait()
                try:
                    response = self._client.get(self._base, params=query)
                    if response.status_code == 429 and attempt < _RETRIES:
                        attempt += 1
                        self.retries += 1
                        self.pacer.hold(_retry_after(response, attempt))
                        continue
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    msg = f"could not reach Semrush for {report}: {type(exc).__name__}"
                    if isinstance(exc, httpx.HTTPStatusError):
                        msg += f" (HTTP {exc.response.status_code})"
                    raise SemrushError(msg) from exc
                break
            rows = parse_rows(response.text)
        except Exception:
            self.budget.charge(0)  # the call used nothing: its reservation goes
            raise
        units = len(rows) * UNITS_PER_LINE.get(report, _UNKNOWN_UNITS)
        # Logged first, then the reservation is let go: the units are never uncounted.
        self.log.add(
            UnitEntry(
                at=datetime.now(UTC),
                project_id=self.project_id,
                report=report,
                target=target,
                lines=len(rows),
                units=units,
            )
        )
        self.budget.charge(units)
        return rows

    def domain_overview(self, domain: str, database: str) -> list[dict[str, str]]:
        """Rank, organic keywords, traffic and traffic cost of a domain (1 line)."""
        return self._call(
            "domain_ranks",
            {"domain": domain, "database": database, "export_columns": "Db,Dn,Rk,Or,Ot,Oc"},
            1,
            domain,
        )

    def domain_organic(self, domain: str, database: str, limit: int) -> list[dict[str, str]]:
        """The keywords a domain ranks for, by traffic (up to `limit` lines)."""
        return self._call(
            "domain_organic",
            {
                "domain": domain,
                "database": database,
                "display_sort": "tr_desc",
                "export_columns": "Ph,Po,Pp,Nq,Cp,Ur,Tr,Co",
            },
            limit,
            domain,
        )

    def phrase_this(self, phrase: str, database: str) -> list[dict[str, str]]:
        """Volume, CPC, competition and result count of a keyword (1 line)."""
        return self._call(
            "phrase_this",
            {"phrase": phrase, "database": database, "export_columns": "Ph,Nq,Cp,Co,Nr"},
            1,
            phrase,
        )

    def phrase_organic(self, phrase: str, database: str, limit: int = 10) -> list[dict[str, str]]:
        """Who ranks for a keyword (domain and URL, up to `limit` lines)."""
        return self._call(
            "phrase_organic",
            {"phrase": phrase, "database": database, "export_columns": "Dn,Ur"},
            limit,
            phrase,
        )

    def organic_competitors(
        self, domain: str, database: str, limit: int = 20
    ) -> list[dict[str, str]]:
        """The domain's competitors in organic search, closest first (up to `limit` lines)."""
        return self._call(
            "domain_organic_organic",
            {"domain": domain, "database": database, "export_columns": "Dn,Cr,Np,Or,Ot"},
            limit,
            domain,
        )

    def close(self) -> None:
        """Close the HTTP client and drop any reservation the pull still holds."""
        self.budget.release()
        self._client.close()
