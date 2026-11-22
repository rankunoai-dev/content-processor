"""Where chunk and page vectors are kept (RFC 0002, Stage D).

Two stores with the same methods:

- `PgVectorStore`: Postgres with the `pgvector` extension, tables in the
  `knowledge` schema (never the ledger's tables). Vector search through an HNSW
  cosine index, keyword search through a generated `tsvector` column and GIN index.
- `SqliteVectorStore`: one SQLite file per tenant, needing nothing installed.
  Vectors are float32 blobs searched exactly with numpy; keywords through FTS5.
  This is the fallback when Postgres with pgvector is not configured.

Every row keeps the verbatim text, block ids and evidence ids, so a search result
can be quoted and traced back to the stored page.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from src.modules.content_processor.knowledge.numeric import np

__all__ = [
    "ChunkRow",
    "DateRange",
    "PageRow",
    "PgVectorStore",
    "SqliteVectorStore",
    "StoreChoice",
    "VectorStore",
    "clear_vector_cache",
    "keyword_terms",
    "open_store",
    "probe_pgvector",
]

StoreChoice = Literal["auto", "sqlite", "postgres"]
# a publication-date filter: (from, to), ISO dates (YYYY-MM-DD), either end open with None
DateRange = tuple[str | None, str | None]
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_QUERY_TOKENS = 32
# question words that match nearly every passage: left out of keyword search (BM25 gives
# them almost no weight, but they pull every chunk into the candidate set)
_KEYWORD_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


@dataclass
class ChunkRow:
    """One chunk as stored: text for quoting, ids for evidence, labels, vector."""

    chunk_id: str
    run_id: str
    url: str
    page_key: str
    order: int
    kind: str
    heading_path: list[str]
    context_header: str
    verbatim_text: str
    embed_text: str
    embed_hash: str
    block_ids: list[str]
    evidence_ids: list[str]
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    offering_id: str | None = None
    chunk_role: str | None = None
    label_status: str = "unlabelled"
    labels: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)
    published_at: str | None = None  # the page's publication date, YYYY-MM-DD, when known


@dataclass
class PageRow:
    """One page: its type, offering and the mean vector of its main chunks."""

    page_key: str
    run_id: str
    url: str
    title: str | None
    page_type: str | None
    offering_id: str | None
    embedding: list[float]


class VectorStore(Protocol):
    """The methods indexing and search need, whatever the backend."""

    kind: str
    location: str

    def meta(self) -> dict[str, str]:
        """Stored settings: `model` and `dims` of the vectors."""
        ...

    def set_meta(self, values: dict[str, str]) -> None:
        """Record settings (the embedding model and its size)."""
        ...

    def reset(self) -> None:
        """Delete every chunk, page, offering and setting of this tenant."""
        ...

    def known_vectors(self, hashes: Iterable[str]) -> dict[str, list[float]]:
        """Vectors already stored for these embed-text hashes."""
        ...

    def upsert_chunks(self, rows: Sequence[ChunkRow]) -> None:
        """Insert or replace chunks."""
        ...

    def prune_page(self, page_key: str, keep: set[str]) -> int:
        """Delete the page's chunks that are not in `keep`; returns how many."""
        ...

    def upsert_pages(self, rows: Sequence[PageRow]) -> None:
        """Insert or replace pages."""
        ...

    def upsert_offerings(self, rows: Sequence[tuple[str, str | None, str]]) -> None:
        """Insert or replace offerings as (id, parent id, name)."""
        ...

    def offering_parents(self) -> dict[str, str | None]:
        """Each offering's parent id."""
        ...

    def vector_search(
        self,
        vector: list[float],
        limit: int,
        offerings: set[str] | None,
        published: DateRange | None = None,
    ) -> list[tuple[str, float]]:
        """Chunk ids by cosine similarity, best first, among chunks that pass the filters."""
        ...

    def keyword_search(
        self,
        query: str,
        limit: int,
        offerings: set[str] | None,
        published: DateRange | None = None,
    ) -> list[tuple[str, float]]:
        """Chunk ids by full-text match, best first, among chunks that pass the filters."""
        ...

    def vectors(self, ids: Iterable[str]) -> dict[str, list[float]]:
        """The stored vectors of these chunks (for diversity between results)."""
        ...

    def get_chunks(self, ids: Iterable[str]) -> dict[str, ChunkRow]:
        """Chunks by id (without their vectors)."""
        ...

    def page_chunks(self, url: str) -> list[ChunkRow]:
        """Every chunk of one page, in page order (without vectors)."""
        ...

    def pages(self) -> list[PageRow]:
        """Every page with its type, offering and mean vector."""
        ...

    def counts(self) -> dict[str, int]:
        """Rows per table."""
        ...

    def close(self) -> None:
        """Close the connection."""
        ...


def keyword_terms(query: str) -> list[str]:
    """The query words worth matching: question and filler words dropped.

    All words are kept when nothing else is left, so "what is it" still searches.
    """
    tokens = query_tokens(query)
    kept = [t for t in tokens if t not in _KEYWORD_STOP]
    return kept or tokens


def query_tokens(query: str) -> list[str]:
    """Words of a query, de-duplicated, in order (safe to put in a search expression)."""
    seen: list[str] = []
    for token in _TOKEN.findall(query.casefold()):
        if token not in seen:
            seen.append(token)
    return seen[:_MAX_QUERY_TOKENS]


def _unit(vector: Sequence[float]) -> np.ndarray[Any, np.dtype[np.float32]]:
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return array / norm if norm else array


# -- SQLite ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    url TEXT NOT NULL,
    page_key TEXT NOT NULL,
    order_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    offering_id TEXT,
    chunk_role TEXT,
    label_status TEXT NOT NULL,
    labels TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    context_header TEXT NOT NULL,
    verbatim_text TEXT NOT NULL,
    embed_text TEXT NOT NULL,
    embed_hash TEXT NOT NULL,
    block_ids TEXT NOT NULL,
    evidence_ids TEXT NOT NULL,
    prev_chunk_id TEXT,
    next_chunk_id TEXT,
    embedding BLOB NOT NULL,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS chunk_vectors_offering ON chunk_vectors (offering_id);
CREATE INDEX IF NOT EXISTS chunk_vectors_page ON chunk_vectors (page_key);
CREATE INDEX IF NOT EXISTS chunk_vectors_hash ON chunk_vectors (embed_hash);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    chunk_id UNINDEXED, body, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS page_vectors (
    page_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    page_type TEXT,
    offering_id TEXT,
    embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS offerings (
    offering_id TEXT PRIMARY KEY, parent_id TEXT, name TEXT NOT NULL
);
"""

_CHUNK_COLUMNS = (
    "chunk_id, run_id, url, page_key, order_no, kind, offering_id, chunk_role, label_status, "
    "labels, heading_path, context_header, verbatim_text, embed_text, embed_hash, block_ids, "
    "evidence_ids, prev_chunk_id, next_chunk_id, published_at"
)


def _row_from(data: dict[str, Any] | sqlite3.Row) -> ChunkRow:
    def as_list(value: Any) -> list[str]:
        return [str(v) for v in (json.loads(value) if isinstance(value, str) else value or [])]

    labels = data["labels"]
    return ChunkRow(
        chunk_id=data["chunk_id"],
        run_id=data["run_id"],
        url=data["url"],
        page_key=data["page_key"],
        order=int(data["order_no"]),
        kind=data["kind"],
        heading_path=as_list(data["heading_path"]),
        context_header=data["context_header"],
        verbatim_text=data["verbatim_text"],
        embed_text=data["embed_text"],
        embed_hash=data["embed_hash"],
        block_ids=as_list(data["block_ids"]),
        evidence_ids=as_list(data["evidence_ids"]),
        prev_chunk_id=data["prev_chunk_id"],
        next_chunk_id=data["next_chunk_id"],
        offering_id=data["offering_id"],
        chunk_role=data["chunk_role"],
        label_status=data["label_status"],
        labels=json.loads(labels) if isinstance(labels, str) else dict(labels or {}),
        published_at=data["published_at"] if "published_at" in data.keys() else None,  # noqa: SIM118 - sqlite3.Row,
    )


def _blob(vector: Sequence[float]) -> bytes:
    """A unit vector as float32 bytes, trailing zeros dropped.

    A smaller model padded to the store's width keeps only its own numbers on disk;
    reads pad it back.
    """
    array = _unit(vector)
    nonzero = np.flatnonzero(array)
    width = int(nonzero[-1]) + 1 if len(nonzero) else 1
    return np.ascontiguousarray(array[:width]).tobytes()


def _padded(blob: bytes, dims: int) -> np.ndarray[Any, np.dtype[np.float32]]:
    """A stored vector back at the store's width (zeros added after its own numbers)."""
    array = np.frombuffer(blob, np.float32)
    if len(array) >= dims:
        return array
    out = np.zeros(dims, dtype=np.float32)
    out[: len(array)] = array
    return out


@dataclass
class _Matrix:
    """A store's chunk vectors in memory, for exact search: one row per chunk."""

    ids: list[str]
    offerings: list[str | None]
    published: list[str | None]
    matrix: np.ndarray[Any, np.dtype[np.float32]]
    row_of: dict[str, int]

    @property
    def nbytes(self) -> int:
        return int(self.matrix.nbytes) + 200 * len(self.ids)


_MATRICES: OrderedDict[str, tuple[tuple[int, int], _Matrix]] = OrderedDict()
_MATRICES_LOCK = threading.Lock()


def _cache_limit() -> int:
    """Bytes of chunk vectors kept in memory across searches (`CP_VECTOR_CACHE_MB`)."""
    try:
        from src.core.config import get_settings

        return int(get_settings().cp_vector_cache_mb) * 1024 * 1024
    except Exception:  # noqa: BLE001 - settings unavailable (scripts, tests): a safe default
        return 256 * 1024 * 1024


def clear_vector_cache() -> None:
    """Forget every store's vectors held in memory (tests, or after a large re-index)."""
    with _MATRICES_LOCK:
        _MATRICES.clear()


def _file_version(path: str) -> tuple[int, int]:
    try:
        st = os.stat(path)
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _in_range(value: str | None, published: DateRange | None) -> bool:
    if published is None:
        return True
    if value is None:
        return False
    start, end = published
    return (start is None or value >= start) and (end is None or value <= end)


class SqliteVectorStore:
    """Embedded store: one SQLite file, exact cosine search with numpy, FTS5 keywords.

    The chunk vectors are read into memory once and shared by every search (and every
    open of the same file) until the file changes, within `CP_VECTOR_CACHE_MB`.
    """

    kind = "sqlite"

    def __init__(self, path: Path) -> None:
        """Open or create the store file (adding columns newer versions need)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.location = str(path)
        self._key = str(path.resolve())
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        columns = {r[1] for r in self._db.execute("PRAGMA table_info(chunk_vectors)")}
        if columns and "published_at" not in columns:
            with self._db:
                self._db.execute("ALTER TABLE chunk_vectors ADD COLUMN published_at TEXT")
        self._db.executescript(_SQLITE_SCHEMA)

    def _changed(self) -> None:
        with _MATRICES_LOCK:
            _MATRICES.pop(self._key, None)

    def _dims(self) -> int:
        row = self._db.execute("SELECT value FROM kb_meta WHERE key = 'dims'").fetchone()
        try:
            return int(row["value"]) if row else 0
        except ValueError:
            return 0

    def meta(self) -> dict[str, str]:
        """Stored settings."""
        return {r["key"]: r["value"] for r in self._db.execute("SELECT key, value FROM kb_meta")}

    def set_meta(self, values: dict[str, str]) -> None:
        """Record settings."""
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO kb_meta (key, value) VALUES (?, ?)", values.items()
            )

    def reset(self) -> None:
        """Delete everything."""
        with self._db:
            for table in ("kb_meta", "chunk_vectors", "chunk_fts", "page_vectors", "offerings"):
                self._db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
        self._changed()

    def known_vectors(self, hashes: Iterable[str]) -> dict[str, list[float]]:
        """Vectors already stored for these hashes (at the store's width)."""
        wanted = list(dict.fromkeys(hashes))
        dims = self._dims()
        found: dict[str, list[float]] = {}
        for start in range(0, len(wanted), 500):
            part = wanted[start : start + 500]
            marks = ",".join("?" * len(part))
            for row in self._db.execute(
                f"SELECT embed_hash, embedding FROM chunk_vectors WHERE embed_hash IN ({marks})",  # noqa: S608
                part,
            ):
                found[row["embed_hash"]] = _padded(row["embedding"], dims).tolist()
        return found

    def upsert_chunks(self, rows: Sequence[ChunkRow]) -> None:
        """Insert or replace chunks and their keyword index entries."""
        with self._db:
            for r in rows:
                self._db.execute("DELETE FROM chunk_fts WHERE chunk_id = ?", (r.chunk_id,))
                self._db.execute(
                    f"INSERT OR REPLACE INTO chunk_vectors ({_CHUNK_COLUMNS}, embedding) "  # noqa: S608
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r.chunk_id,
                        r.run_id,
                        r.url,
                        r.page_key,
                        r.order,
                        r.kind,
                        r.offering_id,
                        r.chunk_role,
                        r.label_status,
                        json.dumps(r.labels, ensure_ascii=False),
                        json.dumps(r.heading_path, ensure_ascii=False),
                        r.context_header,
                        r.verbatim_text,
                        r.embed_text,
                        r.embed_hash,
                        json.dumps(r.block_ids),
                        json.dumps(r.evidence_ids),
                        r.prev_chunk_id,
                        r.next_chunk_id,
                        r.published_at,
                        _blob(r.embedding),
                    ),
                )
                self._db.execute(
                    "INSERT INTO chunk_fts (chunk_id, body) VALUES (?, ?)",
                    (r.chunk_id, f"{r.context_header}\n{r.verbatim_text}"),
                )
        self._changed()

    def prune_page(self, page_key: str, keep: set[str]) -> int:
        """Delete the page's chunks that are not in `keep`."""
        stale = [
            row["chunk_id"]
            for row in self._db.execute(
                "SELECT chunk_id FROM chunk_vectors WHERE page_key = ?", (page_key,)
            )
            if row["chunk_id"] not in keep
        ]
        with self._db:
            for chunk_id in stale:
                self._db.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,))
                self._db.execute("DELETE FROM chunk_fts WHERE chunk_id = ?", (chunk_id,))
        if stale:
            self._changed()
        return len(stale)

    def upsert_pages(self, rows: Sequence[PageRow]) -> None:
        """Insert or replace pages."""
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO page_vectors "
                "(page_key, run_id, url, title, page_type, offering_id, embedding) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        p.page_key,
                        p.run_id,
                        p.url,
                        p.title,
                        p.page_type,
                        p.offering_id,
                        _blob(p.embedding),
                    )
                    for p in rows
                ],
            )

    def upsert_offerings(self, rows: Sequence[tuple[str, str | None, str]]) -> None:
        """Insert or replace offerings."""
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO offerings (offering_id, parent_id, name) VALUES (?,?,?)",
                rows,
            )

    def offering_parents(self) -> dict[str, str | None]:
        """Each offering's parent id."""
        return {
            r["offering_id"]: r["parent_id"]
            for r in self._db.execute("SELECT offering_id, parent_id FROM offerings")
        }

    def _load_matrix(self) -> _Matrix:
        """The chunk vectors in memory: shared across opens until the file changes."""
        version = _file_version(self.location)
        with _MATRICES_LOCK:
            cached = _MATRICES.get(self._key)
            if cached is not None and cached[0] == version:
                _MATRICES.move_to_end(self._key)
                return cached[1]
        ids: list[str] = []
        offerings: list[str | None] = []
        published: list[str | None] = []
        blobs: list[np.ndarray[Any, Any]] = []
        for row in self._db.execute(
            "SELECT chunk_id, offering_id, published_at, embedding FROM chunk_vectors"
            " ORDER BY chunk_id"
        ):
            ids.append(row["chunk_id"])
            offerings.append(row["offering_id"])
            published.append(row["published_at"])
            blobs.append(np.frombuffer(row["embedding"], np.float32))
        # only the columns some vector uses: a 384-number model padded to 1,024 (older
        # stores kept the zeros on disk) is searched at 384 numbers per row
        width = 1
        for blob in blobs:
            nonzero = np.flatnonzero(blob)
            if len(nonzero):
                width = max(width, int(nonzero[-1]) + 1)
        matrix = np.zeros((len(blobs), width), dtype=np.float32)
        for i, blob in enumerate(blobs):
            part = blob[:width]
            matrix[i, : len(part)] = part
        loaded = _Matrix(ids, offerings, published, matrix, {c: i for i, c in enumerate(ids)})
        limit = _cache_limit()
        with _MATRICES_LOCK:
            _MATRICES.pop(self._key, None)
            if loaded.nbytes <= limit:
                _MATRICES[self._key] = (version, loaded)
                total = sum(m.nbytes for _v, m in _MATRICES.values())
                while total > limit and len(_MATRICES) > 1:
                    _key, (_v, dropped) = _MATRICES.popitem(last=False)
                    total -= dropped.nbytes
        return loaded

    def vector_search(
        self,
        vector: list[float],
        limit: int,
        offerings: set[str] | None,
        published: DateRange | None = None,
    ) -> list[tuple[str, float]]:
        """Exact cosine similarity over the chunks that pass the filters (unit vectors).

        Filters are applied before scoring (pre-filtering), so a narrow filter still
        returns its best `limit` chunks.
        """
        m = self._load_matrix()
        if not m.ids:
            return []
        query = _unit(vector)
        width = m.matrix.shape[1]
        if len(query) >= width:
            query = query[:width]
        else:
            query = np.concatenate([query, np.zeros(width - len(query), dtype=np.float32)])
        rows = np.arange(len(m.ids))
        if offerings is not None or published is not None:
            rows = np.fromiter(
                (
                    i
                    for i in range(len(m.ids))
                    if (offerings is None or m.offerings[i] in offerings)
                    and _in_range(m.published[i], published)
                ),
                dtype=np.int64,
            )
            if not len(rows):
                return []
        scores = m.matrix[rows] @ query
        take = min(limit, len(rows))
        best = (
            np.argpartition(-scores, take - 1)[:take] if take < len(rows) else np.arange(len(rows))
        )
        best = best[np.lexsort((rows[best], -scores[best]))]
        return [(m.ids[int(rows[i])], float(scores[i])) for i in best]

    def vectors(self, ids: Iterable[str]) -> dict[str, list[float]]:
        """The vectors of these chunks (as searched), for diversity and reranking."""
        m = self._load_matrix()
        return {c: m.matrix[m.row_of[c]].tolist() for c in ids if c in m.row_of}

    def keyword_search(
        self,
        query: str,
        limit: int,
        offerings: set[str] | None,
        published: DateRange | None = None,
    ) -> list[tuple[str, float]]:
        """FTS5 match on any meaningful query word (BM25), best first, filters in the query."""
        tokens = keyword_terms(query)
        if not tokens:
            return []
        expression = " OR ".join(f'"{t}"' for t in tokens)
        where = ["chunk_fts MATCH ?"]
        params: list[Any] = [expression]
        if offerings is not None:
            if not offerings:
                return []
            where.append(f"c.offering_id IN ({','.join('?' * len(offerings))})")
            params.extend(sorted(offerings))
        if published is not None:
            where.append("c.published_at IS NOT NULL")
            if published[0] is not None:
                where.append("c.published_at >= ?")
                params.append(published[0])
            if published[1] is not None:
                where.append("c.published_at <= ?")
                params.append(published[1])
        statement = (
            "SELECT f.chunk_id AS chunk_id, bm25(chunk_fts) AS rank "  # noqa: S608 - fixed parts
            "FROM chunk_fts f JOIN chunk_vectors c ON c.chunk_id = f.chunk_id "
            f"WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?"
        )
        params.append(limit)
        return [
            (row["chunk_id"], -float(row["rank"])) for row in self._db.execute(statement, params)
        ]

    def get_chunks(self, ids: Iterable[str]) -> dict[str, ChunkRow]:
        """Chunks by id."""
        wanted = list(dict.fromkeys(ids))
        found: dict[str, ChunkRow] = {}
        for start in range(0, len(wanted), 500):
            part = wanted[start : start + 500]
            marks = ",".join("?" * len(part))
            for row in self._db.execute(
                f"SELECT {_CHUNK_COLUMNS} FROM chunk_vectors WHERE chunk_id IN ({marks})",  # noqa: S608
                part,
            ):
                found[row["chunk_id"]] = _row_from(row)
        return found

    def page_chunks(self, url: str) -> list[ChunkRow]:
        """Every chunk of one page, in page order."""
        rows = self._db.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM chunk_vectors WHERE url = ? ORDER BY order_no",  # noqa: S608
            (url,),
        )
        return [_row_from(row) for row in rows]

    def pages(self) -> list[PageRow]:
        """Every page with its type, offering and mean vector (all at one width)."""
        rows = list(
            self._db.execute(
                "SELECT page_key, run_id, url, title, page_type, offering_id, embedding"
                " FROM page_vectors ORDER BY url"
            )
        )
        dims = max([self._dims(), *(len(r["embedding"]) // 4 for r in rows)], default=0)
        return [
            PageRow(
                page_key=r["page_key"],
                run_id=r["run_id"],
                url=r["url"],
                title=r["title"],
                page_type=r["page_type"],
                offering_id=r["offering_id"],
                embedding=_padded(r["embedding"], dims).tolist(),
            )
            for r in rows
        ]

    def counts(self) -> dict[str, int]:
        """Rows per table."""
        return {
            table: int(self._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608
            for table in ("chunk_vectors", "page_vectors", "offerings")
        }

    def close(self) -> None:
        """Close the file (the vectors stay in memory for the next open)."""
        self._db.close()


# -- Postgres + pgvector ----------------------------------------------------------------


def _version_at_least(version: str, wanted: tuple[int, int]) -> bool:
    parts = re.findall(r"\d+", version)[:2]
    return len(parts) == 2 and (int(parts[0]), int(parts[1])) >= wanted


def _ef_search() -> int:
    """HNSW search depth (`CP_HNSW_EF_SEARCH`); pgvector's own default is 40."""
    try:
        from src.core.config import get_settings

        return int(get_settings().cp_hnsw_ef_search)
    except Exception:  # noqa: BLE001 - settings unavailable: a safe default
        return 100


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in _unit(vector).tolist()) + "]"


def _pg_schema(dims: int) -> list[str]:
    return [
        "CREATE SCHEMA IF NOT EXISTS knowledge",
        "CREATE TABLE IF NOT EXISTS knowledge.kb_meta ("
        " tenant TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
        " PRIMARY KEY (tenant, key))",
        "CREATE TABLE IF NOT EXISTS knowledge.chunk_vectors ("
        " tenant TEXT NOT NULL, chunk_id TEXT NOT NULL, run_id TEXT NOT NULL,"
        " url TEXT NOT NULL, page_key TEXT NOT NULL, order_no INTEGER NOT NULL,"
        " kind TEXT NOT NULL, offering_id TEXT, chunk_role TEXT, label_status TEXT NOT NULL,"
        " labels JSONB NOT NULL, heading_path TEXT[] NOT NULL, context_header TEXT NOT NULL,"
        " verbatim_text TEXT NOT NULL, embed_text TEXT NOT NULL, embed_hash TEXT NOT NULL,"
        " block_ids JSONB NOT NULL, evidence_ids JSONB NOT NULL,"
        " prev_chunk_id TEXT, next_chunk_id TEXT,"
        f" embedding vector({dims}) NOT NULL,"
        " tsv tsvector GENERATED ALWAYS AS"
        " (to_tsvector('english', context_header || ' ' || verbatim_text)) STORED,"
        " PRIMARY KEY (tenant, chunk_id))",
        "ALTER TABLE knowledge.chunk_vectors ADD COLUMN IF NOT EXISTS published_at TEXT",
        "CREATE INDEX IF NOT EXISTS chunk_vectors_published"
        " ON knowledge.chunk_vectors (tenant, published_at)",
        "CREATE INDEX IF NOT EXISTS chunk_vectors_hnsw ON knowledge.chunk_vectors"
        " USING hnsw (embedding vector_cosine_ops)",
        "CREATE INDEX IF NOT EXISTS chunk_vectors_tsv ON knowledge.chunk_vectors USING gin (tsv)",
        "CREATE INDEX IF NOT EXISTS chunk_vectors_offering"
        " ON knowledge.chunk_vectors (tenant, offering_id)",
        "CREATE INDEX IF NOT EXISTS chunk_vectors_page"
        " ON knowledge.chunk_vectors (tenant, page_key)",
        "CREATE INDEX IF NOT EXISTS chunk_vectors_hash"
        " ON knowledge.chunk_vectors (tenant, embed_hash)",
        "CREATE TABLE IF NOT EXISTS knowledge.page_vectors ("
        " tenant TEXT NOT NULL, page_key TEXT NOT NULL, run_id TEXT NOT NULL, url TEXT NOT NULL,"
        " title TEXT, page_type TEXT, offering_id TEXT,"
        f" embedding vector({dims}) NOT NULL, PRIMARY KEY (tenant, page_key))",
        "CREATE TABLE IF NOT EXISTS knowledge.offerings ("
        " tenant TEXT NOT NULL, offering_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL,"
        " PRIMARY KEY (tenant, offering_id))",
    ]


def probe_pgvector(url: str) -> tuple[bool, str]:
    """Whether Postgres at `url` answers and offers pgvector; never raises."""
    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT default_version, installed_version FROM pg_available_extensions"
                " WHERE name = 'vector'"
            ).fetchone()
    except psycopg.Error as exc:
        return False, f"cannot connect: {type(exc).__name__}"
    if row is None:
        return False, "connected, but the pgvector extension is not installed on this server"
    installed = f", enabled {row[1]}" if row[1] else ", not yet enabled in this database"
    return True, f"pgvector {row[0]} available{installed}"


class PgVectorStore:
    """Postgres with pgvector: HNSW cosine search and a tsvector keyword index."""

    kind = "postgres"

    def __init__(self, url: str, tenant: str, dims: int) -> None:
        """Connect, enable pgvector and create the `knowledge` tables if needed."""
        self._conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
        self.tenant = tenant
        self.location = f"postgres (schema knowledge, tenant {tenant})"
        self._conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        for statement in _pg_schema(dims):
            self._conn.execute(sql.SQL(statement))
        row = self._conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        self.pgvector_version = str(row["extversion"]) if row else ""
        # pgvector 0.8+ keeps walking the HNSW graph until enough rows pass the filters
        # (every search filters by tenant, as all websites share one index)
        self.iterative_scan = _version_at_least(self.pgvector_version, (0, 8))
        self.ef_search = _ef_search()

    def meta(self) -> dict[str, str]:
        """Stored settings."""
        rows = self._conn.execute(
            "SELECT key, value FROM knowledge.kb_meta WHERE tenant = %s", (self.tenant,)
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_meta(self, values: dict[str, str]) -> None:
        """Record settings."""
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO knowledge.kb_meta (tenant, key, value) VALUES (%s, %s, %s)"
                " ON CONFLICT (tenant, key) DO UPDATE SET value = EXCLUDED.value",
                [(self.tenant, k, v) for k, v in values.items()],
            )

    def reset(self) -> None:
        """Delete this tenant's rows."""
        for table in ("kb_meta", "chunk_vectors", "page_vectors", "offerings"):
            self._conn.execute(
                sql.SQL("DELETE FROM knowledge.{} WHERE tenant = %s").format(sql.Identifier(table)),
                (self.tenant,),
            )

    def known_vectors(self, hashes: Iterable[str]) -> dict[str, list[float]]:
        """Vectors already stored for these hashes."""
        rows = self._conn.execute(
            "SELECT DISTINCT ON (embed_hash) embed_hash, embedding::text AS v"
            " FROM knowledge.chunk_vectors WHERE tenant = %s AND embed_hash = ANY(%s)",
            (self.tenant, list(dict.fromkeys(hashes))),
        ).fetchall()
        return {r["embed_hash"]: [float(x) for x in r["v"].strip("[]").split(",")] for r in rows}

    def upsert_chunks(self, rows: Sequence[ChunkRow]) -> None:
        """Insert or replace chunks."""
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO knowledge.chunk_vectors (tenant, "  # noqa: S608 - fixed column list
                + _CHUNK_COLUMNS
                + ", embedding)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb,"
                "%s::jsonb,%s,%s,%s,%s::vector)"
                " ON CONFLICT (tenant, chunk_id) DO UPDATE SET"
                " run_id = EXCLUDED.run_id, url = EXCLUDED.url, page_key = EXCLUDED.page_key,"
                " order_no = EXCLUDED.order_no, kind = EXCLUDED.kind,"
                " offering_id = EXCLUDED.offering_id, chunk_role = EXCLUDED.chunk_role,"
                " label_status = EXCLUDED.label_status, labels = EXCLUDED.labels,"
                " heading_path = EXCLUDED.heading_path, context_header = EXCLUDED.context_header,"
                " verbatim_text = EXCLUDED.verbatim_text, embed_text = EXCLUDED.embed_text,"
                " embed_hash = EXCLUDED.embed_hash, block_ids = EXCLUDED.block_ids,"
                " evidence_ids = EXCLUDED.evidence_ids, prev_chunk_id = EXCLUDED.prev_chunk_id,"
                " next_chunk_id = EXCLUDED.next_chunk_id, published_at = EXCLUDED.published_at,"
                " embedding = EXCLUDED.embedding",
                [
                    (
                        self.tenant,
                        r.chunk_id,
                        r.run_id,
                        r.url,
                        r.page_key,
                        r.order,
                        r.kind,
                        r.offering_id,
                        r.chunk_role,
                        r.label_status,
                        json.dumps(r.labels, ensure_ascii=False),
                        r.heading_path,
                        r.context_header,
                        r.verbatim_text,
                        r.embed_text,
                        r.embed_hash,
                        json.dumps(r.block_ids),
                        json.dumps(r.evidence_ids),
                        r.prev_chunk_id,
                        r.next_chunk_id,
                        r.published_at,
                        _vector_literal(r.embedding),
                    )
                    for r in rows
                ],
            )

    def prune_page(self, page_key: str, keep: set[str]) -> int:
        """Delete the page's chunks that are not in `keep`."""
        cur = self._conn.execute(
            "DELETE FROM knowledge.chunk_vectors"
            " WHERE tenant = %s AND page_key = %s AND NOT (chunk_id = ANY(%s))",
            (self.tenant, page_key, sorted(keep)),
        )
        return cur.rowcount

    def upsert_pages(self, rows: Sequence[PageRow]) -> None:
        """Insert or replace pages."""
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO knowledge.page_vectors"
                " (tenant, page_key, run_id, url, title, page_type, offering_id, embedding)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)"
                " ON CONFLICT (tenant, page_key) DO UPDATE SET run_id = EXCLUDED.run_id,"
                " url = EXCLUDED.url, title = EXCLUDED.title, page_type = EXCLUDED.page_type,"
                " offering_id = EXCLUDED.offering_id, embedding = EXCLUDED.embedding",
                [
                    (
                        self.tenant,
                        p.page_key,
                        p.run_id,
                        p.url,
                        p.title,
                        p.page_type,
                        p.offering_id,
                        _vector_literal(p.embedding),
                    )
                    for p in rows
                ],
            )

    def upsert_offerings(self, rows: Sequence[tuple[str, str | None, str]]) -> None:
        """Insert or replace offerings."""
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO knowledge.offerings (tenant, offering_id, parent_id, name)"
                " VALUES (%s,%s,%s,%s) ON CONFLICT (tenant, offering_id)"
                " DO UPDATE SET parent_id = EXCLUDED.parent_id, name = EXCLUDED.name",
                [(self.tenant, *row) for row in rows],
            )

    def offering_parents(self) -> dict[str, str | None]:
        """Each offering's parent id."""
        rows = self._conn.execute(
            "SELECT offering_id, parent_id FROM knowledge.offerings WHERE tenant = %s",
            (self.tenant,),
        ).fetchall()
        return {r["offering_id"]: r["parent_id"] for r in rows}

    def _filters(
        self, offerings: set[str] | None, published: DateRange | None
    ) -> tuple[str, list[Any]]:
        where = " AND (%s::text[] IS NULL OR offering_id = ANY(%s::text[]))"
        chosen = sorted(offerings) if offerings is not None else None
        params: list[Any] = [chosen, chosen]
        if published is not None:
            where += " AND published_at IS NOT NULL"
            if published[0] is not None:
                where += " AND published_at >= %s"
                params.append(published[0])
            if published[1] is not None:
                where += " AND published_at <= %s"
                params.append(published[1])
        return where, params

    def vector_search(
        self,
        vector: list[float],
        limit: int,
        offerings: set[str] | None,
        published: DateRange | None = None,
    ) -> list[tuple[str, float]]:
        """Nearest chunks by cosine distance through the HNSW index, filters applied.

        HNSW returns at most `hnsw.ef_search` rows and filters after walking the graph,
        so the search depth is raised to cover `limit` (`CP_HNSW_EF_SEARCH`, at least
        twice the limit) and, on pgvector 0.8+, the walk continues until enough rows pass
        the filters. On older pgvector a filtered search that comes back short is run
        again exactly (index off), so a small website never loses its results.
        """
        where, params = self._filters(offerings, published)
        literal = _vector_literal(vector)
        statement = (
            "SELECT chunk_id, 1 - (embedding <=> %s::vector) AS score"  # noqa: S608 - placeholders only
            " FROM knowledge.chunk_vectors WHERE tenant = %s"
            + where
            + " ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        values = [literal, self.tenant, *params, literal, limit]
        depth = max(self.ef_search, limit * 2)

        def run(exact: bool) -> list[dict[str, Any]]:
            with self._conn.transaction():
                self._conn.execute(
                    sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(depth))
                )
                if exact:
                    self._conn.execute("SET LOCAL enable_indexscan = off")
                elif self.iterative_scan:
                    self._conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                return self._conn.execute(statement, values).fetchall()

        rows = run(exact=False)
        if len(rows) < limit and not self.iterative_scan:
            rows = run(exact=True)
        hits = [(r["chunk_id"], float(r["score"])) for r in rows]
        # relaxed order may return near-ties out of order: put them back in score order
        return sorted(hits, key=lambda h: (-h[1], h[0]))[:limit]

    def vectors(self, ids: Iterable[str]) -> dict[str, list[float]]:
        """The stored vectors of these chunks."""
        rows = self._conn.execute(
            "SELECT chunk_id, embedding::text AS v FROM knowledge.chunk_vectors"
            " WHERE tenant = %s AND chunk_id = ANY(%s)",
            (self.tenant, list(dict.fromkeys(ids))),
        ).fetchall()
        return {r["chunk_id"]: [float(x) for x in r["v"].strip("[]").split(",")] for r in rows}

    def keyword_search(
        self,
        query: str,
        limit: int,
        offerings: set[str] | None,
        published: DateRange | None = None,
    ) -> list[tuple[str, float]]:
        """`tsvector` match on any meaningful query word, ranked by `ts_rank_cd`."""
        tokens = keyword_terms(query)
        if not tokens:
            return []
        where, params = self._filters(offerings, published)
        statement = (
            "SELECT chunk_id, ts_rank_cd(tsv, q) AS score"  # noqa: S608 - placeholders only
            " FROM knowledge.chunk_vectors, to_tsquery('english', %s) AS q"
            " WHERE tenant = %s AND tsv @@ q" + where + " ORDER BY score DESC, chunk_id LIMIT %s"
        )
        rows = self._conn.execute(
            statement,
            (" | ".join(tokens), self.tenant, *params, limit),
        ).fetchall()
        return [(r["chunk_id"], float(r["score"])) for r in rows]

    def get_chunks(self, ids: Iterable[str]) -> dict[str, ChunkRow]:
        """Chunks by id."""
        rows = self._conn.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM knowledge.chunk_vectors"  # noqa: S608
            " WHERE tenant = %s AND chunk_id = ANY(%s)",
            (self.tenant, list(dict.fromkeys(ids))),
        ).fetchall()
        return {r["chunk_id"]: _row_from(r) for r in rows}

    def page_chunks(self, url: str) -> list[ChunkRow]:
        """Every chunk of one page, in page order."""
        rows = self._conn.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM knowledge.chunk_vectors"  # noqa: S608
            " WHERE tenant = %s AND url = %s ORDER BY order_no",
            (self.tenant, url),
        ).fetchall()
        return [_row_from(r) for r in rows]

    def pages(self) -> list[PageRow]:
        """Every page with its type, offering and mean vector."""
        rows = self._conn.execute(
            "SELECT page_key, run_id, url, title, page_type, offering_id, embedding::text AS v"
            " FROM knowledge.page_vectors WHERE tenant = %s ORDER BY url",
            (self.tenant,),
        ).fetchall()
        return [
            PageRow(
                page_key=r["page_key"],
                run_id=r["run_id"],
                url=r["url"],
                title=r["title"],
                page_type=r["page_type"],
                offering_id=r["offering_id"],
                embedding=[float(x) for x in r["v"].strip("[]").split(",")],
            )
            for r in rows
        ]

    def counts(self) -> dict[str, int]:
        """Rows per table."""
        out: dict[str, int] = {}
        for table in ("chunk_vectors", "page_vectors", "offerings"):
            row = self._conn.execute(
                sql.SQL("SELECT count(*) AS n FROM knowledge.{} WHERE tenant = %s").format(
                    sql.Identifier(table)
                ),
                (self.tenant,),
            ).fetchone()
            out[table] = int(row["n"]) if row else 0
        return out

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()


_PROBES: dict[str, tuple[float, tuple[bool, str]]] = {}
_PROBE_SECONDS = 300.0


def _probe_cached(url: str) -> tuple[bool, str]:
    """`probe_pgvector`, remembered for five minutes.

    Every search opens each website's store; a second connection per open only to ask the
    same question would be waste.
    """
    now = time.monotonic()
    found = _PROBES.get(url)
    if found is not None and now - found[0] < _PROBE_SECONDS:
        return found[1]
    result = probe_pgvector(url)
    _PROBES[url] = (now, result)
    return result


def open_store(
    choice: StoreChoice,
    *,
    tenant: str,
    sqlite_root: Path,
    db_url: str | None,
    dims: int,
) -> tuple[VectorStore, str]:
    """The store to use and a plain note on why.

    `auto` uses Postgres when a database URL is set and pgvector is available there,
    otherwise the SQLite file `<sqlite_root>/<tenant>/vectors.sqlite`. `postgres`
    insists and raises `RuntimeError` with the reason when it cannot.
    """
    sqlite_path = sqlite_root / tenant / "vectors.sqlite"
    if choice == "sqlite":
        return SqliteVectorStore(sqlite_path), "sqlite (asked for)"
    if not db_url:
        if choice == "postgres":
            msg = "no database URL: set CP_DATABASE_URL in .env or pass --db-url"
            raise RuntimeError(msg)
        return SqliteVectorStore(sqlite_path), "sqlite fallback: CP_DATABASE_URL is not set"
    ok, detail = _probe_cached(db_url)
    if ok:
        return PgVectorStore(db_url, tenant, dims), f"postgres: {detail}"
    if choice == "postgres":
        raise RuntimeError(detail)
    return SqliteVectorStore(sqlite_path), f"sqlite fallback: postgres {detail}"
