"""`stamp` subcommand: page stamps and chunks for a run (RFC 0002, step 1).

    python -m src.modules.content_processor.cli stamp --records out/jh.records.jsonl \
        --tenant rankuno-dev

Writes `<run>.stamps.jsonl`, `<run>.chunks.jsonl` and `<run>.site.json` next to
the records (or into `--out`). Page HTML is read back from the evidence store by
its payload hash; nothing is fetched and no model is called.
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
from src.modules.content_processor.knowledge.stamp import PayloadLoader, StampResult, stamp_records

__all__ = ["cmd_stamp", "register", "store_loader", "write_stamps"]


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


def register(subparsers: Any) -> None:
    """Add the `stamp` subcommand to the CLI's subparsers."""
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
