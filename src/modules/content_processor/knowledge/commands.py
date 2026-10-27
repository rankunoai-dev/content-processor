"""`stamp` and `catalog` subcommands (RFC 0002, steps 1 and 2).

    python -m src.modules.content_processor.cli stamp --records out/jh.records.jsonl \
        --tenant rankuno-dev
    python -m src.modules.content_processor.cli catalog --stamps out/jh.stamps.jsonl

`stamp` writes `<run>.stamps.jsonl`, `<run>.chunks.jsonl` and `<run>.site.json`
next to the records (or into `--out`). Page HTML is read back from the evidence
store by its payload hash; nothing is fetched and no model is called.

`catalog` reads those three files and writes `<run>.catalog.json` and
`<run>_offering_catalog.xlsx`. With `--embed-endpoint`/`--llm-endpoint` (local or
private-network only, for example Ollama at `http://localhost:11434/v1`) it adds
checked synonym merges and card attributes; without them it is code only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.export.commands import load_records, run_name_for
from src.modules.content_processor.knowledge.assist import (
    AssistReport,
    LocalChat,
    LocalEmbedder,
    LocalOnlyError,
    extract_attributes,
    propose_merges,
)
from src.modules.content_processor.knowledge.catalog import build_catalog
from src.modules.content_processor.knowledge.catalog_report import write_catalog
from src.modules.content_processor.knowledge.models import Chunk, PageStamp, SiteStamp
from src.modules.content_processor.knowledge.stamp import PayloadLoader, StampResult, stamp_records

__all__ = ["cmd_catalog", "cmd_stamp", "register", "store_loader", "write_stamps"]


def store_loader(store: LocalEvidenceStore) -> PayloadLoader:
    """Read payloads from the store; a missing or unreadable one gives None."""

    def load(sha256: str) -> bytes | None:
        try:
            return store.get(sha256)
        except (OSError, ValueError):
            return None

    return load


def write_stamps(result: StampResult, out_dir: Path, run_name: str) -> dict[str, str]:
    """Write the three stamp files and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "stamps": out_dir / f"{run_name}.stamps.jsonl",
        "chunks": out_dir / f"{run_name}.chunks.jsonl",
        "site": out_dir / f"{run_name}.site.json",
    }
    with paths["stamps"].open("w", encoding="utf-8") as handle:
        for stamp in result.stamps:
            handle.write(stamp.model_dump_json() + "\n")
    with paths["chunks"].open("w", encoding="utf-8") as handle:
        for chunk in result.chunks:
            handle.write(chunk.model_dump_json() + "\n")
    paths["site"].write_text(result.site.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}


def cmd_stamp(args: argparse.Namespace) -> int:
    """Stamp a run's records and write stamps, chunks and site facts."""
    records_path = Path(args.records)
    records = load_records(records_path)
    if not records:
        sys.stderr.write(f"{records_path}: no records\n")
        return 2
    root = Path(args.evidence_root) if args.evidence_root else get_settings().cp_evidence_root
    result = stamp_records(records, store_loader(LocalEvidenceStore(root, args.tenant)))
    run_name = args.run_name or run_name_for(records_path)
    out_dir = Path(args.out) if args.out else records_path.parent
    files = write_stamps(result, out_dir, run_name)
    sizes = [c.token_estimate for c in result.chunks]
    summary: dict[str, Any] = {
        "run_id": result.site.run_id,
        "site": result.site.site,
        "site_name": result.site.site_name,
        "pages": result.site.pages,
        "knowledge_pages": result.site.knowledge_pages,
        "excluded": result.site.excluded,
        "chunks": len(sizes),
        "chunk_tokens": {
            "min": min(sizes, default=0),
            "max": max(sizes, default=0),
            "mean": round(sum(sizes) / len(sizes), 1) if sizes else 0,
        },
        "menus": len(result.site.menus),
        "metadata_coverage": result.site.metadata_coverage,
        "files": files,
    }
    sys.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return 0


def _stamps_base(path: Path) -> tuple[Path, str]:
    name = path.name
    run = name[: -len(".stamps.jsonl")] if name.endswith(".stamps.jsonl") else path.stem
    return path.parent, run


def cmd_catalog(args: argparse.Namespace) -> int:
    """Build the offering catalog from a run's stamps, chunks and site file."""
    folder, run_name = _stamps_base(Path(args.stamps))
    run_name = args.run_name or run_name
    site_path = folder / f"{run_name}.site.json"
    chunks_path = folder / f"{run_name}.chunks.jsonl"
    for needed in (Path(args.stamps), site_path, chunks_path):
        if not needed.exists():
            sys.stderr.write(f"{needed}: not found (run `stamp` first)\n")
            return 2
    for endpoint, model, flag in (
        (args.embed_endpoint, args.embed_model, "--embed-endpoint"),
        (args.llm_endpoint, args.llm_model, "--llm-endpoint"),
    ):
        if endpoint and not model:
            sys.stderr.write(f"{flag} needs its model (--embed-model / --llm-model)\n")
            return 2
    site = SiteStamp.model_validate_json(site_path.read_text(encoding="utf-8"))
    with Path(args.stamps).open(encoding="utf-8") as handle:
        stamps = [PageStamp.model_validate_json(line) for line in handle if line.strip()]
    with chunks_path.open(encoding="utf-8") as handle:
        chunks = [Chunk.model_validate_json(line) for line in handle if line.strip()]
    catalog = build_catalog(site, stamps, chunks)
    report = AssistReport()
    try:
        if args.embed_endpoint:
            embedder = LocalEmbedder(args.embed_endpoint, args.embed_model)
            try:
                propose_merges(catalog, embedder, report)
            finally:
                embedder.close()
        if args.llm_endpoint:
            chat = LocalChat(args.llm_endpoint, args.llm_model)
            try:
                extract_attributes(catalog, {c.chunk_id: c for c in chunks}, chat, report)
            finally:
                chat.close()
    except LocalOnlyError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    catalog.stats.update({f"assist_{k}": v for k, v in vars(report).items() if v})
    files = write_catalog(catalog, Path(args.out) if args.out else folder, run_name)
    summary = {
        "run_id": catalog.run_id,
        "site_name": catalog.site_name,
        "stats": catalog.stats,
        "engines": catalog.engines,
        "files": files,
    }
    sys.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return 0


def register(subparsers: Any) -> None:
    """Add the `stamp` and `catalog` subcommands to the CLI's subparsers."""
    parser = subparsers.add_parser("stamp", help="stamp pages and build context-preserving chunks")
    parser.add_argument("--records", required=True, help="UrlRecord JSONL file")
    parser.add_argument("--tenant", required=True, help="evidence tenant, e.g. rankuno-dev")
    parser.add_argument(
        "--evidence-root", default=None, help="evidence store root (default: CP_EVIDENCE_ROOT)"
    )
    parser.add_argument("--out", default=None, help="output directory (default: next to records)")
    parser.add_argument(
        "--run-name",
        default=None,
        help="prefix of the output files (default: the records file name, x.records.jsonl -> x)",
    )
    parser.set_defaults(func=cmd_stamp)

    cat = subparsers.add_parser("catalog", help="build the site's offering catalog from stamps")
    cat.add_argument("--stamps", required=True, help="<run>.stamps.jsonl written by `stamp`")
    cat.add_argument("--out", default=None, help="output directory (default: next to stamps)")
    cat.add_argument("--run-name", default=None, help="prefix of the input and output files")
    cat.add_argument("--embed-endpoint", default=None, help="local embeddings URL (optional)")
    cat.add_argument("--embed-model", default=None, help="embedding model, e.g. bge-m3")
    cat.add_argument("--llm-endpoint", default=None, help="local chat URL (optional)")
    cat.add_argument("--llm-model", default=None, help="instruct model, e.g. qwen2.5:14b")
    cat.set_defaults(func=cmd_catalog)
