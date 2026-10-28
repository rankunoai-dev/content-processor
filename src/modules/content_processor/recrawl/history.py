"""Global URL history: every capture of every run, and the latest good one per URL.

One SQLite file per tenant, `<CP_URL_HISTORY_ROOT>/<tenant>/history.sqlite`. Keep
that root next to the evidence root: a reused capture cites evidence stored there
(and a capture whose evidence is gone is not reused). Two tables:

- `captures`: one row per URL per run (audit: when, what verdict, changed or not).
- `latest`: for each URL key (schedule key, requested and final URL), the newest
  capture worth reusing, with its full `UrlRecord` stored compressed so a later
  run can reuse the extraction even if the run's export files are gone.

Rules that keep it safe:

- A failed, blocked or never-acquired capture is logged but never replaces a good
  one (a bad day does not erase what is known).
- An older run imported after a newer one never overwrites the newer capture.
- Importing the same run twice changes nothing.

`UrlHistory` is the tenant-level entry point the CLI uses (`<root>, <tenant>`). The
first time it opens a tenant that still has the earlier file layout (`index.jsonl`
plus `records/<aa>/<sha>.json.gz`), it imports those captures into SQLite and
renames `index.jsonl` to `index.jsonl.migrated`; nothing is deleted.
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.schemas import StrictModel
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.recrawl.baseline import Baseline, extraction_time, url_key

__all__ = [
    "HISTORY_FILE",
    "Capture",
    "HistoryBaseline",
    "HistoryEntry",
    "HistoryImport",
    "HistoryIndex",
    "UrlHistory",
    "describe",
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
    run_name TEXT,
    UNIQUE (run_id, requested_url)
);
CREATE TABLE IF NOT EXISTS latest (
    url_key TEXT PRIMARY KEY,
    capture_id INTEGER NOT NULL REFERENCES captures(id)
);
CREATE INDEX IF NOT EXISTS captures_url ON captures(requested_url);
"""


def history_path(root: Path, tenant: str) -> Path:
    """Where a tenant's history file lives under the history root."""
    return root / tenant / HISTORY_FILE


class HistoryEntry(StrictModel):
    """What the history knows about one URL without opening its record."""

    key: str
    url: str
    last_scraped_at: datetime
    content_captured_at: datetime | None = None
    run_id: str
    run_name: str | None = None
    badge: str
    page_modified_at: datetime | None = None
    page_modified_source: str | None = None
    content_fingerprint: str | None = None


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
        columns = {r[1] for r in self._db.execute("PRAGMA table_info(captures)")}
        if "run_name" not in columns:  # files written before run names were kept
            self._db.execute("ALTER TABLE captures ADD COLUMN run_name TEXT")

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

    def add_records(
        self, records: Iterable[UrlRecord], *, run_name: str | None = None
    ) -> HistoryImport:
        """Log every record's capture and keep the newest reusable one per URL."""
        result = HistoryImport()
        with self._tx() as cur:
            for record in records:
                self._add(cur, record, result, run_name)
            # Only the captures `latest` points at keep their full record.
            cur.execute(
                "UPDATE captures SET record = NULL WHERE record IS NOT NULL "
                "AND id NOT IN (SELECT capture_id FROM latest)"
            )
        return result

    def _add(
        self, cur: sqlite3.Cursor, record: UrlRecord, result: HistoryImport, run_name: str | None
    ) -> None:
        reusable = Baseline.reusable(record)
        extracted = extraction_time(record)
        blob = zlib.compress(record.model_dump_json().encode("utf-8")) if reusable else None
        cur.execute(
            "INSERT OR IGNORE INTO captures (run_id, requested_url, final_url, captured_at, "
            "extracted_at, badge, change_status, change_basis, content_fingerprint, "
            "payload_sha256, page_modified_at, reusable, record, run_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                run_name,
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

    def entry(self, url: str) -> HistoryEntry | None:
        """Summary of the newest reusable capture of a URL (any spelling of the page)."""
        key = url_key(url)
        for k in dict.fromkeys(x for x in (key, url) if x):
            row = self._db.execute(
                "SELECT c.requested_url, c.captured_at, c.extracted_at, c.run_id, c.run_name, "
                "c.badge, c.page_modified_at, c.content_fingerprint FROM latest l "
                "JOIN captures c ON c.id = l.capture_id WHERE l.url_key = ?",
                (k,),
            ).fetchone()
            if row is not None:
                return HistoryEntry(
                    key=key or url,
                    url=row[0],
                    last_scraped_at=datetime.fromisoformat(row[1]),
                    content_captured_at=datetime.fromisoformat(row[2]),
                    run_id=row[3],
                    run_name=row[4],
                    badge=row[5],
                    page_modified_at=datetime.fromisoformat(row[6]) if row[6] else None,
                    content_fingerprint=row[7],
                )
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


class UrlHistory:
    """The tenant's URL history under a root folder (SQLite, earlier files imported once)."""

    def __init__(self, root: Path, tenant_id: str) -> None:
        """Bind to `<root>/<tenant_id>/`; nothing is created until the history is used."""
        self._base = Path(root) / tenant_id
        self.path = self._base / HISTORY_FILE
        self._index: HistoryIndex | None = None
        self.migrated = 0

    def __enter__(self) -> UrlHistory:
        """Use as a context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on exit."""
        self.close()

    def close(self) -> None:
        """Close the SQLite file if it was opened."""
        if self._index is not None:
            self._index.close()
            self._index = None

    @property
    def _legacy_index(self) -> Path:
        return self._base / "index.jsonl"

    def exists(self) -> bool:
        """Whether anything was ever recorded for this tenant."""
        return self.path.exists() or self._legacy_index.exists()

    def index(self) -> HistoryIndex:
        """The SQLite index, opened (and created, and the earlier files imported) on first use."""
        if self._index is None:
            self._index = HistoryIndex(self.path)
            self.migrated = self._import_legacy(self._index)
        return self._index

    def _legacy_record(self, key: str) -> UrlRecord | None:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self._base / "records" / digest[:2] / f"{digest}.json.gz"
        try:
            return UrlRecord.model_validate_json(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            return None

    def _import_legacy(self, index: HistoryIndex) -> int:
        """Import the earlier `index.jsonl` + `records/` layout once; return captures added."""
        if not self._legacy_index.exists():
            return 0
        added = 0
        for line in self._legacy_index.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = HistoryEntry.model_validate_json(line)
            except ValueError:
                continue
            record = self._legacy_record(entry.key)
            if record is not None:
                added += index.add_records([record], run_name=entry.run_name).captures_added
        self._legacy_index.replace(self._legacy_index.with_name("index.jsonl.migrated"))
        return added

    def __len__(self) -> int:
        """How many URLs have a reusable capture."""
        return self.index().stats()["urls_reusable"] if self.exists() else 0

    def lookup(self, url: str) -> HistoryEntry | None:
        """The entry for a URL in any spelling that normalizes to the same key."""
        return self.index().entry(url) if self.exists() else None

    def baseline_for(self, urls: Iterable[str]) -> Baseline:
        """A `Baseline` of the stored records for these URLs (unknown ones are absent)."""
        if not self.exists():
            return Baseline([])
        records = [r for u in urls if (r := self.index().lookup(u)) is not None]
        return Baseline(records)

    def add_records(
        self, records: Iterable[UrlRecord], *, run_name: str | None = None
    ) -> HistoryImport:
        """Add a run's records (see `HistoryIndex.add_records`)."""
        return self.index().add_records(records, run_name=run_name)

    def remember(self, records: Iterable[UrlRecord], *, run_name: str | None = None) -> int:
        """Add a run's records; return how many became the newest capture of their URL."""
        return self.add_records(records, run_name=run_name).latest_updated


def describe(entry: HistoryEntry | None) -> dict[str, str]:
    """Plain columns for the pre-scrape check (`cli history`)."""
    if entry is None:
        return {"Scraped before": "No", "Last scraped": "", "Site last modified": "", "Run": ""}
    when = entry.content_captured_at or entry.last_scraped_at
    updated = entry.page_modified_at.date().isoformat() if entry.page_modified_at else ""
    return {
        "Scraped before": "Yes",
        "Last scraped": when.date().isoformat(),
        "Site last modified": updated,
        "Run": entry.run_name or entry.run_id,
    }
