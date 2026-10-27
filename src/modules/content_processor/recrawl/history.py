"""Global URL history: every capture of every run, and the latest good one per URL.

One SQLite file per tenant, kept next to that tenant's evidence
(`<evidence root>/<tenant>/history.sqlite`), so a reused capture never points at
evidence that is not there. Two tables:

- `captures`: one row per URL per run (audit: when, what verdict, changed or not).
- `latest`: for each URL key (schedule key, requested and final URL), the newest
  capture worth reusing, with its full `UrlRecord` stored compressed so a later
  run can reuse the extraction even if the run's export files are gone.

Rules that keep it safe:

- A failed, blocked or never-acquired capture is logged but never replaces a good
  one (a bad day does not erase what is known).
- An older run imported after a newer one never overwrites the newer capture.
- Importing the same run twice changes nothing.
"""

from __future__ import annotations

import sqlite3
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.recrawl.baseline import Baseline, extraction_time, url_key

__all__ = [
    "HISTORY_FILE",
    "Capture",
    "HistoryBaseline",
    "HistoryImport",
    "HistoryIndex",
    "history_path",
]

HISTORY_FILE = "history.sqlite"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    requested_url TEXT NOT NULL,
    final_url TEXT,
    captured_at TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    badge TEXT NOT NULL,
    change_status TEXT,
    change_basis TEXT,
    content_fingerprint TEXT,
    payload_sha256 TEXT,
    page_modified_at TEXT,
    reusable INTEGER NOT NULL,
    record BLOB,
    UNIQUE (run_id, requested_url)
);
CREATE TABLE IF NOT EXISTS latest (
    url_key TEXT PRIMARY KEY,
    capture_id INTEGER NOT NULL REFERENCES captures(id)
);
CREATE INDEX IF NOT EXISTS captures_url ON captures(requested_url);
"""


def history_path(evidence_root: Path, tenant: str) -> Path:
    """Where a tenant's history lives: beside its evidence."""
    return evidence_root / tenant / HISTORY_FILE


@dataclass(frozen=True)
class Capture:
    """One past capture of a URL (audit row)."""

    run_id: str
    requested_url: str
    final_url: str | None
    captured_at: datetime
    extracted_at: datetime
    badge: str
    change_status: str | None
    change_basis: str | None
    page_modified_at: datetime | None
    reusable: bool
    is_latest: bool


@dataclass
class HistoryImport:
    """What one import did."""

    captures_added: int = 0
    already_known: int = 0
    latest_updated: int = 0
    kept_newer: int = 0
    not_reusable: int = 0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _keys(record: UrlRecord) -> set[str]:
    return {
        k
        for k in (record.schedule_key, url_key(record.requested_url), url_key(record.final_url))
        if k
    }


class HistoryIndex:
    """The tenant's URL history in SQLite."""

    def __init__(self, path: Path) -> None:
        """Open (and create if needed) the history file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, isolation_level=None)  # transactions are explicit
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the file."""
        self._db.close()

    def __enter__(self) -> HistoryIndex:
        """Use as a context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on exit."""
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._db.cursor()
        try:
            cur.execute("BEGIN")
            yield cur
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def add_records(self, records: Iterable[UrlRecord]) -> HistoryImport:
        """Log every record's capture and keep the newest reusable one per URL."""
        result = HistoryImport()
        with self._tx() as cur:
            for record in records:
                self._add(cur, record, result)
            # Only the captures `latest` points at keep their full record.
            cur.execute(
                "UPDATE captures SET record = NULL WHERE record IS NOT NULL "
                "AND id NOT IN (SELECT capture_id FROM latest)"
            )
        return result

    def _add(self, cur: sqlite3.Cursor, record: UrlRecord, result: HistoryImport) -> None:
        reusable = Baseline.reusable(record)
        extracted = extraction_time(record)
        blob = zlib.compress(record.model_dump_json().encode("utf-8")) if reusable else None
        cur.execute(
            "INSERT OR IGNORE INTO captures (run_id, requested_url, final_url, captured_at, "
            "extracted_at, badge, change_status, change_basis, content_fingerprint, "
            "payload_sha256, page_modified_at, reusable, record) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.run_id,
                record.requested_url,
                record.final_url,
                record.captured_at.isoformat(),
                extracted.isoformat(),
                str(record.badge),
                str(record.change_status) if record.change_status else None,
                record.change_basis,
                record.content_fingerprint,
                record.payload_sha256,
                _iso(record.page_modified_at),
                int(reusable),
                blob,
            ),
        )
        if cur.rowcount == 0:
            result.already_known += 1
            return
        result.captures_added += 1
        capture_id = cur.lastrowid
        if not reusable:
            result.not_reusable += 1
            return
        updated = False
        for key in sorted(_keys(record)):
            row = cur.execute(
                "SELECT c.captured_at FROM latest l JOIN captures c ON c.id = l.capture_id "
                "WHERE l.url_key = ?",
                (key,),
            ).fetchone()
            if row is not None and datetime.fromisoformat(row[0]) > record.captured_at:
                result.kept_newer += 1
                continue
            cur.execute(
                "INSERT INTO latest (url_key, capture_id) VALUES (?, ?) "
                "ON CONFLICT(url_key) DO UPDATE SET capture_id = excluded.capture_id",
                (key, capture_id),
            )
            updated = True
        if updated:
            result.latest_updated += 1

    def lookup(self, *urls: str | None) -> UrlRecord | None:
        """The newest reusable capture for any of these URLs or schedule keys."""
        for url in urls:
            for key in dict.fromkeys(k for k in (url, url_key(url)) if k):
                row = self._db.execute(
                    "SELECT c.record FROM latest l JOIN captures c ON c.id = l.capture_id "
                    "WHERE l.url_key = ?",
                    (key,),
                ).fetchone()
                if row is not None and row[0] is not None:
                    return UrlRecord.model_validate_json(zlib.decompress(row[0]))
        return None

    def captures(self, url: str) -> list[Capture]:
        """Every logged capture of a URL, newest first."""
        key = url_key(url) or url
        latest_ids = {
            r[0]
            for r in self._db.execute(
                "SELECT capture_id FROM latest WHERE url_key IN (?, ?)", (key, url)
            )
        }
        rows = self._db.execute(
            "SELECT id, run_id, requested_url, final_url, captured_at, extracted_at, badge, "
            "change_status, change_basis, page_modified_at, reusable FROM captures "
            "WHERE requested_url = ? OR final_url = ? OR id IN "
            "(SELECT capture_id FROM latest WHERE url_key IN (?, ?)) "
            "ORDER BY captured_at DESC",
            (url, url, key, url),
        ).fetchall()
        return [
            Capture(
                run_id=r[1],
                requested_url=r[2],
                final_url=r[3],
                captured_at=datetime.fromisoformat(r[4]),
                extracted_at=datetime.fromisoformat(r[5]),
                badge=r[6],
                change_status=r[7],
                change_basis=r[8],
                page_modified_at=datetime.fromisoformat(r[9]) if r[9] else None,
                reusable=bool(r[10]),
                is_latest=r[0] in latest_ids,
            )
            for r in rows
        ]

    def stats(self) -> dict[str, int]:
        """Counts for a quick look."""
        one = self._db.execute
        return {
            "captures": int(one("SELECT COUNT(*) FROM captures").fetchone()[0]),
            "runs": int(one("SELECT COUNT(DISTINCT run_id) FROM captures").fetchone()[0]),
            "urls_reusable": int(
                one("SELECT COUNT(DISTINCT capture_id) FROM latest").fetchone()[0]
            ),
            "url_keys": int(one("SELECT COUNT(*) FROM latest").fetchone()[0]),
        }


class HistoryBaseline(Baseline):
    """A baseline backed by the tenant's history instead of one previous run."""

    def __init__(
        self,
        index: HistoryIndex,
        *,
        reuse_since: datetime | None = None,
        evidence_exists: Callable[[str], bool] | None = None,
    ) -> None:
        """Look captures up in `index`; reuse only those extracted at or after `reuse_since`.

        With `evidence_exists`, a capture whose stored payload is gone is ignored
        (the page is then treated as new and captured again).
        """
        super().__init__([], reuse_since=reuse_since)
        self._index = index
        self._evidence_exists = evidence_exists
        self.missing_evidence = 0

    def __len__(self) -> int:
        """Distinct URLs with a reusable capture."""
        return self._index.stats()["urls_reusable"]

    def lookup(self, *urls: str | None) -> UrlRecord | None:
        """The newest reusable capture from any earlier run whose evidence is still stored."""
        prior = self._index.lookup(*urls)
        if (
            prior is not None
            and self._evidence_exists is not None
            and prior.payload_sha256
            and not self._evidence_exists(prior.payload_sha256)
        ):
            self.missing_evidence += 1
            return None
        return prior
