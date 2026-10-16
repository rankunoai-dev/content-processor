"""I1 URL Content Explorer as one self-contained, offline HTML file.

The explorer lists every requested URL of a run with its badge, filters by
badge, text and role (boilerplate hidden by default), and shows one URL at a
time: status vector, gaps with their evidence ids, and the corpus blocks in
reading order coloured by layer and role, under their headings. Links to the
run's downloadable reports sit in the header.

Page text is third-party content. It travels as JSON inside a
`<script type="application/json">` element with `<`, `>`, `&` and line
separators escaped, and the page script renders it only with `textContent`,
never as markup. A Content-Security-Policy allows no network access and only
the page's own script, pinned by its SHA-256 hash, so even a mistake in
escaping cannot run injected code.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.export.content import whole_content

__all__ = ["explorer_data", "render_explorer", "write_explorer"]

_BOILERPLATE_ROLES = frozenset({"nav", "chrome", "footer"})

_ASSETS = Path(__file__).parent
_STYLE = (_ASSETS / "explorer.css").read_text(encoding="utf-8")
_SCRIPT = (_ASSETS / "explorer.js").read_text(encoding="utf-8")


def _record_data(record: UrlRecord) -> dict[str, Any]:
    blocks = sorted(record.blocks, key=lambda b: b.reading_order)
    return {
        "url": record.requested_url,
        "final_url": record.final_url,
        "aliases": list(record.aliases),
        "badge": record.badge.value,
        "captured_at": record.captured_at.isoformat(),
        "tier": record.tier_reached.value if record.tier_reached else None,
        "warnings": list(record.warnings),
        "words": len(whole_content(record.blocks).split()),
        "text": whole_content(record.blocks),
        "vector": {
            "acquisition": record.status_vector.acquisition.value,
            "fidelity": record.status_vector.fidelity.value,
            "state_exploration": record.status_vector.state_exploration.value,
            "discovery_closed": record.status_vector.discovery_closed,
            "reasons": list(record.status_vector.reasons),
        },
        "gaps": [
            {
                "type": g.gap_type.value,
                "status": g.status.value,
                "reason": g.reason,
                "evidence": list(g.evidence_ids),
            }
            for g in record.gaps
        ],
        "blocks": [
            {
                "id": b.block_id,
                "text": b.normalized_text,
                "layer": b.layer.value,
                "role": b.role,
                "boiler": b.role in _BOILERPLATE_ROLES,
                "heading": list(b.heading_path),
                "evidence": list(b.evidence_ids),
            }
            for b in blocks
            if b.normalized_text and b.dedupe_of is None
        ],
    }


def explorer_data(records: Iterable[UrlRecord], *, run_id: str) -> dict[str, Any]:
    """The JSON the explorer page embeds."""
    return {"run_id": run_id, "records": [_record_data(r) for r in records]}


def _embed_json(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_explorer(
    records: Sequence[UrlRecord],
    *,
    run_id: str,
    downloads: Mapping[str, str] | None = None,
) -> str:
    """Return the explorer page. `downloads` maps a label to a relative file name."""
    script_hash = base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode()
    csp = (
        "default-src 'none'; "
        f"script-src 'sha256-{script_hash}'; "
        "style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    links = " · ".join(
        f'<a href="{html.escape(path, quote=True)}" download>{html.escape(label)}</a>'
        for label, path in (downloads or {}).items()
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=True)}">'
        f"<title>URL Content Explorer</title><style>{_STYLE}</style></head><body>"
        "<header><div><h1>URL Content Explorer</h1>"
        f'<div class="muted">Run {html.escape(run_id)} · {len(records)} requested URLs</div></div>'
        '<div class="chips" id="chips"></div>'
        '<label>Search <input type="search" id="q" placeholder="URL or text"></label>'
        '<label><input type="checkbox" id="boiler"> Show boilerplate (nav, chrome, footer)</label>'
        f'<div class="muted">{links}</div></header>'
        '<main><nav id="list" aria-label="URLs"></nav><section id="detail"></section></main>'
        '<script type="application/json" id="data">'
        f"{_embed_json(explorer_data(records, run_id=run_id))}</script>"
        f"<script>{_SCRIPT}</script></body></html>\n"
    )


def write_explorer(
    path: Path,
    records: Sequence[UrlRecord],
    *,
    run_id: str,
    downloads: Mapping[str, str] | None = None,
) -> None:
    """Write the explorer page to `path`."""
    path.write_text(render_explorer(records, run_id=run_id, downloads=downloads), encoding="utf-8")
