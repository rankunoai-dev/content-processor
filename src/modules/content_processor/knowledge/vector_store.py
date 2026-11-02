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
import re
import sqlite3
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
    "PageRow",
    "PgVectorStore",
    "SqliteVectorStore",
    "StoreChoice",
    "VectorStore",
    "open_store",
    "probe_pgvector",
]

StoreChoice = Literal["auto", "sqlite", "postgres"]
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_QUERY_TOKENS = 32


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
        self, vector: list[float], limit: int, offerings: set[str] | None
    ) -> list[tuple[str, float]]:
        """Chunk ids by cosine similarity, best first."""
        ...

    def keyword_search(
        self, query: str, limit: int, offerings: set[str] | None
    ) -> list[tuple[str, float]]:
        """Chunk ids by full-text match, best first."""
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
    embedding BLOB NOT NULL
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
    "evidence_ids, prev_chunk_id, next_chunk_id"
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
    )


class SqliteVectorStore:
    """Embedded store: one SQLite file, exact cosine search with numpy, FTS5 keywords."""

    kind = "sqlite"

    def __init__(self, path: Path) -> None:
        """Open or create the store file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.location = str(path)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SQLITE_SCHEMA)
        self._matrix: tuple[list[str], list[str | None], np.ndarray[Any, Any]] | None = None

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
        self._matrix = None

    def known_vectors(self, hashes: Iterable[str]) -> dict[str, list[float]]:
        """Vectors already stored for these hashes."""
        wanted = list(dict.fromkeys(hashes))
        found: dict[str, list[float]] = {}
        for start in range(0, len(wanted), 500):
            part = wanted[start : start + 500]
            marks = ",".join("?" * len(part))
            for row in self._db.execute(
                f"SELECT embed_hash, embedding FROM chunk_vectors WHERE embed_hash IN ({marks})",  # noqa: S608
                part,
            ):
                found[row["embed_hash"]] = np.frombuffer(row["embedding"], np.float32).tolist()
        return found

    def upsert_chunks(self, rows: Sequence[ChunkRow]) -> None:
        """Insert or replace chunks and their keyword index entries."""
        with self._db:
            for r in rows:
                self._db.execute("DELETE FROM chunk_fts WHERE chunk_id = ?", (r.chunk_id,))
                self._db.execute(
                    f"INSERT OR REPLACE INTO chunk_vectors ({_CHUNK_COLUMNS}, embedding) "  # noqa: S608
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        _unit(r.embedding).tobytes(),
                    ),
                )
                self._db.execute(
                    "INSERT INTO chunk_fts (chunk_id, body) VALUES (?, ?)",
                    (r.chunk_id, f"{r.context_header}\n{r.verbatim_text}"),
                )
        self._matrix = None

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
            self._matrix = None
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
                        _unit(p.embedding).tobytes(),
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

    def _load_matrix(self) -> tuple[list[str], list[str | None], np.ndarray[Any, Any]]:
        if self._matrix is None:
            ids: list[str] = []
            offerings: list[str | None] = []
            vectors: list[np.ndarray[Any, Any]] = []
            for row in self._db.execute(
                "SELECT chunk_id, offering_id, embedding FROM chunk_vectors ORDER BY chunk_id"
            ):
                ids.append(row["chunk_id"])
                offerings.append(row["offering_id"])
                vectors.append(np.frombuffer(row["embedding"], np.float32))
            matrix = np.vstack(vectors) if vectors else np.zeros((0, 1), np.float32)
            self._matrix = (ids, offerings, matrix)
        return self._matrix

    def vector_search(
        self, vector: list[float], limit: int, offerings: set[str] | None
    ) -> list[tuple[str, float]]:
        """Exact cosine similarity over every stored chunk (vectors are unit length)."""
        ids, owners, matrix = self._load_matrix()
        if not ids:
            return []
        scores = matrix @ _unit(vector)
        order = np.argsort(-scores, kind="stable")
        hits: list[tuple[str, float]] = []
        for i in order:
            if offerings is not None and owners[int(i)] not in offerings:
                continue
            hits.append((ids[int(i)], float(scores[int(i)])))
            if len(hits) >= limit:
                break
        return hits

    def keyword_search(
        self, query: str, limit: int, offerings: set[str] | None
    ) -> list[tuple[str, float]]:
        """FTS5 match on any query word (BM25), best first."""
        tokens = query_tokens(query)
        if not tokens:
            return []
        expression = " OR ".join(f'"{t}"' for t in tokens)
        sql = (
            "SELECT f.chunk_id AS chunk_id, bm25(chunk_fts) AS rank, c.offering_id AS offering_id "
            "FROM chunk_fts f JOIN chunk_vectors c ON c.chunk_id = f.chunk_id "
            "WHERE chunk_fts MATCH ? ORDER BY rank LIMIT ?"
        )
        fetch = limit if offerings is None else limit * 20
        hits = [
            (row["chunk_id"], -float(row["rank"]))
            for row in self._db.execute(sql, (expression, fetch))
            if offerings is None or row["offering_id"] in offerings
        ]
        return hits[:limit]

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
        """Every page with its type, offering and mean vector."""
        rows = self._db.execute(
            "SELECT page_key, run_id, url, title, page_type, offering_id, embedding"
            " FROM page_vectors ORDER BY url"
        )
        return [
            PageRow(
                page_key=r["page_key"],
                run_id=r["run_id"],
                url=r["url"],
                title=r["title"],
                page_type=r["page_type"],
                offering_id=r["offering_id"],
                embedding=np.frombuffer(r["embedding"], np.float32).tolist(),
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
        """Close the file."""
        self._db.close()


# -- Postgres + pgvector ----------------------------------------------------------------


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
                "%s::jsonb,%s,%s,%s::vector)"
                " ON CONFLICT (tenant, chunk_id) DO UPDATE SET"
                " run_id = EXCLUDED.run_id, url = EXCLUDED.url, page_key = EXCLUDED.page_key,"
                " order_no = EXCLUDED.order_no, kind = EXCLUDED.kind,"
                " offering_id = EXCLUDED.offering_id, chunk_role = EXCLUDED.chunk_role,"
                " label_status = EXCLUDED.label_status, labels = EXCLUDED.labels,"
                " heading_path = EXCLUDED.heading_path, context_header = EXCLUDED.context_header,"
                " verbatim_text = EXCLUDED.verbatim_text, embed_text = EXCLUDED.embed_text,"
                " embed_hash = EXCLUDED.embed_hash, block_ids = EXCLUDED.block_ids,"
                " evidence_ids = EXCLUDED.evidence_ids, prev_chunk_id = EXCLUDED.prev_chunk_id,"
                " next_chunk_id = EXCLUDED.next_chunk_id, embedding = EXCLUDED.embedding",
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

    def vector_search(
        self, vector: list[float], limit: int, offerings: set[str] | None
    ) -> list[tuple[str, float]]:
        """Nearest chunks by cosine distance through the HNSW index."""
        rows = self._conn.execute(
            "SELECT chunk_id, 1 - (embedding <=> %s::vector) AS score"
            " FROM knowledge.chunk_vectors WHERE tenant = %s"
            " AND (%s::text[] IS NULL OR offering_id = ANY(%s::text[]))"
            " ORDER BY embedding <=> %s::vector LIMIT %s",
            (
                _vector_literal(vector),
                self.tenant,
                sorted(offerings) if offerings is not None else None,
                sorted(offerings) if offerings is not None else None,
                _vector_literal(vector),
                limit,
            ),
        ).fetchall()
        return [(r["chunk_id"], float(r["score"])) for r in rows]

    def keyword_search(
        self, query: str, limit: int, offerings: set[str] | None
    ) -> list[tuple[str, float]]:
        """`tsvector` match on any query word, ranked by `ts_rank_cd`."""
        tokens = query_tokens(query)
        if not tokens:
            return []
        rows = self._conn.execute(
            "SELECT chunk_id, ts_rank_cd(tsv, q) AS score"
            " FROM knowledge.chunk_vectors, to_tsquery('english', %s) AS q"
            " WHERE tenant = %s AND tsv @@ q"
            " AND (%s::text[] IS NULL OR offering_id = ANY(%s::text[]))"
            " ORDER BY score DESC, chunk_id LIMIT %s",
            (
                " | ".join(tokens),
                self.tenant,
                sorted(offerings) if offerings is not None else None,
                sorted(offerings) if offerings is not None else None,
                limit,
            ),
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
    ok, detail = probe_pgvector(db_url)
    if ok:
        return PgVectorStore(db_url, tenant, dims), f"postgres: {detail}"
    if choice == "postgres":
        raise RuntimeError(detail)
    return SqliteVectorStore(sqlite_path), f"sqlite fallback: postgres {detail}"
