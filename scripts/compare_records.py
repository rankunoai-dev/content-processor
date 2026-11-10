"""Compare two `*.records.jsonl` files of the same scope, ignoring what must differ between runs.

Usage:
    python scripts/compare_records.py A.records.jsonl B.records.jsonl [--ignore key ...] [--verbose]

Two runs of the same scope capture the same content; what legitimately differs
is timing (`captured_at`, `extracted_at`, `acquired_at`, `elapsed_*`, `settle`,
event timestamps), tool versions and every identifier derived from the run id
(`run_id` itself, `res-<hash>` resource ids, and the block and gap ids hashed
from them). This script normalises those and reports every other difference,
record by record, as a JSON path with both values. Exit code 0 means the
records are identical after normalisation; 1 means a behaviour change.

Normalisation:
- the fields named in `TIMING_KEYS` (and `--ignore`) are dropped wherever they occur;
- each record's `run_id` becomes `RUN`;
- each record's resource id (`res-` + sha256(run_id, schedule_key)[:16]) becomes
  `res:<schedule_key>` everywhere in the file, so cross references
  (`duplicate_payload_of:`, `parent_resource_id`, `dedupe_of`) still line up;
- `block_id`, `gap_id` and `dedupe_of` are hashes of the resource id and the content:
  they are replaced by the stable key they were hashed from (`block_key`, `reason`)
  so the block set is still compared, just not through a run-specific digest;
- lists of blocks and gaps are compared in order (order is part of the record).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

TIMING_KEYS = frozenset(
    {
        "captured_at",
        "extracted_at",
        "acquired_at",
        "at",
        "fetched_at",
        "settle",
        "tool_versions",
        "tool_version",
        "elapsed_s",
        "elapsed_ms",
        "previous_captured_at",
    }
)
_RES = re.compile(r"res-[0-9a-f]{16}")


def _resource_id_for(run_id: str, schedule_key: str) -> str:
    return "res-" + hashlib.sha256(f"{run_id}\x1f{schedule_key}".encode()).hexdigest()[:16]


def _strip(value: Any, ignore: frozenset[str]) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in ignore or key.startswith("elapsed_"):
                continue
            if key in ("block_id", "gap_id", "dedupe_of") and isinstance(item, str):
                out[key] = "<derived>"
                continue
            out[key] = _strip(item, ignore)
        return out
    if isinstance(value, list):
        return [_strip(v, ignore) for v in value]
    return value


def load(path: Path, ignore: frozenset[str]) -> dict[str, Any]:
    """Records keyed by requested URL, normalised; raises on duplicate URLs."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    aliases: dict[str, str] = {}
    for row in rows:
        key = row.get("schedule_key") or row["requested_url"]
        aliases[_resource_id_for(row["run_id"], key)] = f"res:{key}"
        aliases[row["run_id"]] = "RUN"
    text = json.dumps(rows, sort_keys=True)
    for old, new in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(old, new)
    leftovers = sorted(set(_RES.findall(text)))
    if leftovers:
        # Resource ids of another run (a baseline) or of resources without a record.
        for old in leftovers:
            text = text.replace(old, "res:<foreign>")
    out: dict[str, Any] = {}
    for row in json.loads(text):
        url = row["requested_url"]
        if url in out:
            msg = f"{path}: two records for {url}"
            raise SystemExit(msg)
        out[url] = _strip(row, ignore)
    return out


def diff(a: Any, b: Any, path: str, out: list[tuple[str, Any, Any]]) -> None:
    """Append (path, left, right) for every leaf that differs."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append((f"{path}.{key}", "<missing>", b[key]))
            elif key not in b:
                out.append((f"{path}.{key}", a[key], "<missing>"))
            else:
                diff(a[key], b[key], f"{path}.{key}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((f"{path}[len]", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            diff(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append((path, a, b))


def main(argv: list[str] | None = None) -> int:
    """Compare two record files; print the differences; exit 1 when there are any."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--ignore", nargs="*", default=[], help="more field names to drop")
    parser.add_argument("--verbose", action="store_true", help="print every differing path")
    args = parser.parse_args(argv)
    ignore = TIMING_KEYS | frozenset(args.ignore)
    left = load(args.left, ignore)
    right = load(args.right, ignore)
    problems: list[tuple[str, Any, Any]] = []
    for url in sorted(set(left) | set(right)):
        if url not in left:
            problems.append((url, "<no record>", "record"))
        elif url not in right:
            problems.append((url, "record", "<no record>"))
        else:
            diff(left[url], right[url], url, problems)
    out = sys.stdout
    out.write(f"records: left={len(left)} right={len(right)} differences={len(problems)}\n")
    shown = problems if args.verbose else problems[:40]
    for path, a, b in shown:
        sa, sb = json.dumps(a, default=str), json.dumps(b, default=str)
        out.write(f"  {path}\n    left:  {sa[:300]}\n    right: {sb[:300]}\n")
    if len(problems) > len(shown):
        out.write(f"  ... {len(problems) - len(shown)} more (use --verbose)\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
