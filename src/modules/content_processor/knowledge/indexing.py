"""Load a run's chunks into the vector store (RFC 0002, Stage D): the `index` command.

Reads `<run>.chunks.jsonl`, `<run>.stamps.jsonl`, `<run>.catalog.json` (optional)
and `<run>.structured.jsonl` (optional, for page types). For every chunk:

1. The embed text is hashed. A vector already stored for the same text and
   model is reused, so a recrawl only embeds new or changed sections.
2. New texts are embedded in batches by the local model (or the offline fallback).
3. Role and offering are labelled from independent signals (`labels.py`).
4. The chunk is stored with its verbatim text, headings, block ids and evidence ids.

A page's chunks that are no longer in the run are removed; pages of other runs
stay. Each page gets one vector, the mean of its main-content chunks.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.content_processor.knowledge.catalog_models import Catalog
from src.modules.content_processor.knowledge.embedders import Embedder, is_semantic
from src.modules.content_processor.knowledge.labels import LABELS_VERSION, ChunkLabel, Labeller
from src.modules.content_processor.knowledge.models import Chunk, PageStamp
from src.modules.content_processor.knowledge.numeric import np
from src.modules.content_processor.knowledge.vector_store import ChunkRow, PageRow, VectorStore

__all__ = [
    "EMBED_BATCH",
    "IndexReport",
    "ModelMismatchError",
    "RunInputs",
    "embed_hash",
    "index_run",
    "load_run",
    "write_label_review",
]

EMBED_BATCH = 32
_WRITE_BATCH = 200
_SCHEMA_PAGE_TYPES = (
    ("FAQPage", "faq"),
    ("Product", "product"),
    ("NewsArticle", "article"),
    ("Article", "article"),
    ("BlogPosting", "article"),
    ("Event", "event"),
)


class ModelMismatchError(RuntimeError):
    """The store holds vectors of another embedding model; they cannot be mixed."""


@dataclass
class RunInputs:
    """Everything `index` reads for one run."""

    run_name: str
    folder: Path
    chunks: list[Chunk]
    stamps: dict[str, PageStamp]
    catalog: Catalog | None
    page_types: dict[str, str] = field(default_factory=dict)


@dataclass
class IndexReport:
    """What indexing did, in counts."""

    chunks: int = 0
    pages: int = 0
    embedded: int = 0
    reused: int = 0
    removed: int = 0
    embed_calls: int = 0
    roles: dict[str, int] = field(default_factory=dict)
    role_status: dict[str, int] = field(default_factory=dict)
    offerings: dict[str, int] = field(default_factory=dict)
    offering_status: dict[str, int] = field(default_factory=dict)
    labels: list[ChunkLabel] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """JSON-ready counts (without the per-chunk labels)."""
        return {k: v for k, v in vars(self).items() if k != "labels"}


def embed_hash(text: str) -> str:
    """Key of an embed text: same text, same vector (per model)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_run(folder: Path, run_name: str) -> RunInputs:
    """Read a run's files; raises `FileNotFoundError` naming the missing one."""
    chunks_path = folder / f"{run_name}.chunks.jsonl"
    stamps_path = folder / f"{run_name}.stamps.jsonl"
    for needed in (chunks_path, stamps_path):
        if not needed.exists():
            msg = f"{needed}: not found (run `stamp` first)"
            raise FileNotFoundError(msg)
    chunks = [Chunk.model_validate(d) for d in _jsonl(chunks_path)]
    stamps = {s.page_key: s for s in (PageStamp.model_validate(d) for d in _jsonl(stamps_path))}
    catalog_path = folder / f"{run_name}.catalog.json"
    catalog = (
        Catalog.model_validate_json(catalog_path.read_text(encoding="utf-8"))
        if catalog_path.exists()
        else None
    )
    page_types: dict[str, str] = {}
    structured_path = folder / f"{run_name}.structured.jsonl"
    if structured_path.exists():
        for page in _jsonl(structured_path):
            label = page.get("page_type")
            if isinstance(label, dict) and page.get("page_key"):
                page_types[str(page["page_key"])] = str(label.get("page_type"))
    return RunInputs(run_name, folder, chunks, stamps, catalog, page_types)


def _stamp_page_type(stamp: PageStamp | None) -> str | None:
    if stamp is None:
        return None
    types = set(stamp.schema_org.types)
    for schema_type, page_type in _SCHEMA_PAGE_TYPES:
        if schema_type in types:
            return page_type
    if stamp.page_format in ("pdf", "office"):
        return "document"
    return None


def _embed_all(
    embedder: Embedder,
    texts: dict[str, str],
    report: IndexReport,
    progress: Callable[[int, int], None] | None,
) -> dict[str, list[float]]:
    """Vectors for {hash: text}, in batches; one retry per batch, then a clear error."""
    keys = list(texts)
    out: dict[str, list[float]] = {}
    for start in range(0, len(keys), EMBED_BATCH):
        part = keys[start : start + EMBED_BATCH]
        vectors = None
        for _attempt in range(2):
            report.embed_calls += 1
            vectors = embedder.embed([texts[k] for k in part])
            if vectors is not None:
                break
        if vectors is None:
            msg = f"embedding failed twice for a batch of {len(part)} texts ({embedder.model})"
            raise RuntimeError(msg)
        out.update(zip(part, vectors, strict=True))
        if progress is not None:
            progress(min(start + EMBED_BATCH, len(keys)), len(keys))
    return out


def _mean(vectors: Sequence[Sequence[float]]) -> list[float]:
    matrix = np.asarray(vectors, dtype=np.float32)
    return [float(x) for x in matrix.mean(axis=0)]


def index_run(
    run: RunInputs,
    store: VectorStore,
    embedder: Embedder,
    labeller: Labeller,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> IndexReport:
    """Embed (or reuse), label and store every chunk and page of a run."""
    stored_model = store.meta().get("model")
    if stored_model and stored_model != embedder.model:
        msg = (
            f"the store holds {stored_model} vectors and this run would add {embedder.model} "
            "vectors; they cannot be compared. Re-run with --rebuild to start the store again"
        )
        raise ModelMismatchError(msg)
    report = IndexReport()
    chunks = [
        c
        for c in run.chunks
        if (stamp := run.stamps.get(c.page_key)) is None or stamp.knowledge_eligible
    ]
    hashes = {c.chunk_id: embed_hash(c.embed_text) for c in chunks}
    known = store.known_vectors(hashes.values())
    missing = {h: c.embed_text for c in chunks if (h := hashes[c.chunk_id]) not in known}
    fresh = _embed_all(embedder, missing, report, progress)
    vectors = {**known, **fresh}
    report.embedded = len(fresh)
    report.reused = sum(1 for c in chunks if hashes[c.chunk_id] in known)

    if is_semantic(embedder.model) and labeller.offering_texts:
        ids = [i for i, _ in labeller.offering_texts]
        offering_vectors = embedder.embed([t for _, t in labeller.offering_texts])
        report.embed_calls += 1
        if offering_vectors is not None:
            labeller.set_offering_vectors(ids, offering_vectors)
    by_chunk = {c.chunk_id: vectors[hashes[c.chunk_id]] for c in chunks}
    labels = {lab.chunk_id: lab for lab in labeller.label(chunks, by_chunk)}

    rows: list[ChunkRow] = []
    for c in chunks:
        lab = labels[c.chunk_id]
        rows.append(
            ChunkRow(
                chunk_id=c.chunk_id,
                run_id=c.run_id,
                url=c.url,
                page_key=c.page_key,
                order=c.order,
                kind=c.kind,
                heading_path=list(c.heading_path),
                context_header=c.context_header,
                verbatim_text=c.text,
                embed_text=c.embed_text,
                embed_hash=hashes[c.chunk_id],
                block_ids=list(c.block_ids),
                evidence_ids=list(c.evidence_ids),
                prev_chunk_id=c.prev_chunk_id,
                next_chunk_id=c.next_chunk_id,
                offering_id=lab.offering.value,
                chunk_role=lab.role.value,
                label_status=lab.status,
                labels=lab.as_dict(),
                embedding=by_chunk[c.chunk_id],
            )
        )
    for start in range(0, len(rows), _WRITE_BATCH):
        store.upsert_chunks(rows[start : start + _WRITE_BATCH])

    by_page: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_page.setdefault(c.page_key, []).append(c)
    pages: list[PageRow] = []
    for page_key, members in by_page.items():
        report.removed += store.prune_page(page_key, {c.chunk_id for c in members})
        main = [c for c in members if "main_content" in c.ui_regions] or members
        offered = Counter(
            labels[c.chunk_id].offering.value for c in members if labels[c.chunk_id].offering.value
        )
        stamp = run.stamps.get(page_key)
        pages.append(
            PageRow(
                page_key=page_key,
                run_id=members[0].run_id,
                url=members[0].url,
                title=stamp.title if stamp else None,
                page_type=run.page_types.get(page_key) or _stamp_page_type(stamp),
                offering_id=offered.most_common(1)[0][0] if offered else None,
                embedding=_mean([by_chunk[c.chunk_id] for c in main]),
            )
        )
    store.upsert_pages(pages)
    if run.catalog is not None:
        store.upsert_offerings(
            [(o.offering_id, o.parent_id, o.name) for o in run.catalog.offerings]
        )
    dims = len(next(iter(by_chunk.values()))) if by_chunk else 0
    store.set_meta(
        {
            "model": embedder.model,
            "dims": str(dims),
            "labels_version": LABELS_VERSION,
            "last_run": run.run_name,
        }
    )

    report.chunks = len(rows)
    report.pages = len(pages)
    report.labels = list(labels.values())
    report.roles = dict(Counter(lab.role.value for lab in report.labels if lab.role.value))
    report.role_status = dict(Counter(lab.role.status for lab in report.labels))
    report.offerings = dict(
        Counter(lab.offering.value for lab in report.labels if lab.offering.value)
    )
    report.offering_status = dict(Counter(lab.offering.status for lab in report.labels))
    return report


_REVIEW_COLUMNS = [
    "Chunk",
    "URL",
    "Headings",
    "Text (start)",
    "Role",
    "Role status",
    "Role signals",
    "Offering",
    "Offering status",
    "Offering signals",
]


def write_label_review(
    labels: Sequence[ChunkLabel], chunks: Sequence[Chunk], out_dir: Path, run_name: str
) -> dict[str, str]:
    """`<run>.labels.jsonl` and `<run>_chunk_labels.xlsx` (all labels; those needing review)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    by_id = {c.chunk_id: c for c in chunks}
    jsonl = out_dir / f"{run_name}.labels.jsonl"
    with jsonl.open("w", encoding="utf-8") as handle:
        for lab in labels:
            handle.write(json.dumps({"chunk_id": lab.chunk_id, **lab.as_dict()}) + "\n")
    rows: list[dict[str, Cell]] = []
    for lab in labels:
        chunk = by_id.get(lab.chunk_id)
        rows.append(
            {
                "Chunk": lab.chunk_id,
                "URL": chunk.url if chunk else "",
                "Headings": " > ".join(chunk.heading_path) if chunk else "",
                "Text (start)": chunk.text[:300] if chunk else "",
                "Role": lab.role.value or "",
                "Role status": lab.role.status,
                "Role signals": ", ".join(f"{k}: {v}" for k, v in lab.role_votes.items()),
                "Offering": lab.offering.value or "",
                "Offering status": lab.offering.status,
                "Offering signals": ", ".join(f"{k}: {v}" for k, v in lab.offering_votes.items()),
            }
        )
    review = [r for r in rows if "review" in (r["Role status"], r["Offering status"])]
    widths = {"URL": 50, "Headings": 40, "Text (start)": 80, "Role signals": 40}
    xlsx = out_dir / f"{run_name}_chunk_labels.xlsx"
    write_xlsx(
        xlsx,
        [
            Table("Needs review", _REVIEW_COLUMNS, review, frozenset({"Text (start)"}), widths),
            Table("All chunks", _REVIEW_COLUMNS, rows, frozenset({"Text (start)"}), widths),
            Table(
                "How to read",
                ["Column", "Meaning"],
                [
                    {"Column": k, "Meaning": v}
                    for k, v in (
                        ("Role", "What the passage does; set only when two signals agree."),
                        (
                            "Role status",
                            "accepted: two signals agree. single_signal: only one signal, "
                            "not used. review: signals disagree, a person decides. "
                            "unlabelled: no signal.",
                        ),
                        (
                            "Role signals",
                            "structure (markup and headings), text (the words), "
                            "model (local AI model, when one was used).",
                        ),
                        (
                            "Offering",
                            "The product or service the passage is about; set only when two "
                            "signals agree (a sub-offering supports its parent).",
                        ),
                        (
                            "Offering signals",
                            "site (catalog mapping, breadcrumb or URL section), text (name "
                            "in the headings, or twice in the text), meaning (embedding "
                            "similarity, only with a real embedding model), model.",
                        ),
                    )
                ],
                widths={"Meaning": 100},
            ),
        ],
    )
    return {"labels": str(jsonl), "review": str(xlsx)}
