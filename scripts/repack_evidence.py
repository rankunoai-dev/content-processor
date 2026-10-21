"""Repack evidence written before K20 into the packed form, in place.

    python scripts/repack_evidence.py                 # every tenant under the evidence root
    python scripts/repack_evidence.py --root evidence --tenant rankuno-dev --dry-run

Each raw object is read, hash-checked, packed (shared chunks for inlined
fonts and stylesheets, the rest compressed), read back and compared byte for
byte before the raw file is removed. Objects that would not shrink by 10 %,
already-compressed media, damaged objects and objects already packed are left
as they are. Safe to stop and re-run at any point.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import get_settings  # noqa: E402
from src.integrations.evidence_store import LocalEvidenceStore  # noqa: E402

_SKIP_PREFIXES = (b"\xff\xd8", b"\x89PNG", b"%PDF", b"PK\x03\x04", b"GIF8", b"RIFF", b"\x1f\x8b")


def repack_tenant(root: Path, tenant: str, *, dry_run: bool) -> tuple[int, int, int]:
    """Repack one tenant; returns (objects seen, objects packed, bytes saved)."""
    store = LocalEvidenceStore(root, tenant)
    seen = packed = saved = 0
    for path in sorted((root / tenant / "raw").rglob("*")):
        if not path.is_file() or path.suffix == ".cpz" or path.name.startswith(".tmp-"):
            continue
        seen += 1
        with path.open("rb") as handle:
            if handle.read(8).startswith(_SKIP_PREFIXES):
                continue
        if dry_run:
            continue
        gained = store.repack(path.name)
        if gained:
            packed += 1
            saved += gained
    return seen, packed, saved


def main(argv: list[str] | None = None) -> int:
    """Repack every tenant (or one) under the evidence root."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="evidence root (default: CP_EVIDENCE_ROOT)")
    parser.add_argument("--tenant", default=None, help="one tenant only")
    parser.add_argument("--dry-run", action="store_true", help="count objects, change nothing")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else get_settings().cp_evidence_root
    tenants = [args.tenant] if args.tenant else sorted(p.name for p in root.iterdir() if p.is_dir())
    total_saved = 0
    for tenant in tenants:
        seen, packed, saved = repack_tenant(root, tenant, dry_run=args.dry_run)
        total_saved += saved
        sys.stdout.write(f"{tenant}: {seen} objects, {packed} packed, {saved / 1e6:.1f} MB saved\n")
    sys.stdout.write(f"total saved: {total_saved / 1e6:.1f} MB\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
