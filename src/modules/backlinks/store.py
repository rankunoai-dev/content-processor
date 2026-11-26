"""Backlink snapshots on disk and the backlinks workbook.

`<app data>/backlinks/<project>/`: every snapshot as `<snapshot id>.json`, the newest as
`latest.json`, each snapshot's workbook; `<app data>/backlinks/units.jsonl` logs every
Ahrefs call.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from src.modules.backlinks.models import BacklinkSnapshot
from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx

__all__ = ["BacklinksStore", "backlink_tables", "write_backlinks_workbook"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_SNAPSHOT_ID = re.compile(r"^backlinks-\d{8}-\d{6}-\d{3}$")


class BacklinksStore:
    """Backlink snapshots of every project under `<root>`."""

    def __init__(self, root: Path) -> None:
        """Bind to the folder (usually `<app data>/backlinks`)."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, project_id: str) -> Path:
        """The project's folder (the id is checked, so it cannot escape the root)."""
        if not _ID.match(project_id):
            raise KeyError(project_id)
        return self.root / project_id

    def save(self, snapshot: BacklinkSnapshot) -> Path:
        """Write the snapshot and make it the latest."""
        folder = self.folder(snapshot.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        text = snapshot.model_dump_json(indent=1)
        path = folder / f"{snapshot.snapshot_id}.json"
        with self._lock:
            path.write_text(text, encoding="utf-8")
            tmp = folder / "latest.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(folder / "latest.json")
        return path

    def latest(self, project_id: str) -> BacklinkSnapshot | None:
        """The newest snapshot, or None."""
        path = self.folder(project_id) / "latest.json"
        if not path.exists():
            return None
        with self._lock:
            return BacklinkSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def get(self, project_id: str, snapshot_id: str) -> BacklinkSnapshot:
        """One snapshot; raises `KeyError`."""
        if not _SNAPSHOT_ID.match(snapshot_id):
            raise KeyError(snapshot_id)
        path = self.folder(project_id) / f"{snapshot_id}.json"
        if not path.exists():
            raise KeyError(snapshot_id)
        return BacklinkSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def history(self, project_id: str) -> list[str]:
        """Every snapshot's id, newest first."""
        folder = self.folder(project_id)
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("backlinks-*.json")), reverse=True)

    def workbook(self, snapshot: BacklinkSnapshot) -> Path:
        """Where a snapshot's workbook is written."""
        return self.folder(snapshot.project_id) / f"{snapshot.snapshot_id}_backlinks.xlsx"


_HOW_TO_READ: list[tuple[str, str]] = [
    (
        "Source",
        "ahrefs_api: pulled with the Ahrefs API (units logged); csv_import: an Ahrefs export "
        "(Referring domains or Best by links) imported by hand.",
    ),
    (
        "Domain Rating (DR)",
        "Ahrefs' 0-100 score of how strong a website's links are; higher is stronger. An "
        "export carries no DR of the website itself, only of the websites linking to it.",
    ),
    ("Ahrefs rank", "The website's place among all websites by links; 1 is the strongest."),
    ("Referring domains", "Different websites linking to it (live now)."),
    (
        "Link intersect",
        "Websites linking to two or more competitors (the one competitor, when there is "
        "only one) and not to the client, strongest first. They already link to pages like "
        "the client's: the likeliest to link to it too. Spam domains are left out.",
    ),
    (
        "Authority gap",
        "The competitor's DR minus the client's (their referring domains when a DR is not "
        "known). Positive: the competitor is stronger.",
    ),
    (
        "Lost",
        "Referring domains in the previous snapshot that are gone now (only those that "
        "would have been in the new list).",
    ),
    (
        "Cut lists",
        "A pull asks for the strongest referring domains up to a limit; when a list reached "
        "it, weaker linking websites may be missing.",
    ),
]


def backlink_tables(snapshot: BacklinkSnapshot) -> list[Table]:
    """The workbook's sheets."""
    sites: list[dict[str, Cell]] = [
        {
            "Website": w.name,
            "Domain": w.website,
            "Role": w.role,
            "Source": w.source,
            "Domain Rating": w.domain_rating,
            "Ahrefs rank": w.ahrefs_rank,
            "Live backlinks": w.stats.live if w.stats else None,
            "Live referring domains": w.stats.live_refdomains if w.stats else None,
            "Referring domains in this snapshot": len(w.refdomains),
            "Asked for": w.refdomains_asked or "all (export)",
            "New since last": w.new_domains,
            "Lost since last": len(w.lost_domains) if w.new_domains is not None else None,
            "Top pages": len(w.top_pages),
            "Note": w.note,
        }
        for w in snapshot.websites
    ]
    refdomains: list[dict[str, Cell]] = [
        {
            "Website": w.website,
            "Referring domain": r.domain,
            "Domain Rating": r.domain_rating,
            "Links to target": r.links_to_target,
            "Dofollow links": r.dofollow_links,
            "First seen": r.first_seen or "",
            "Last seen": r.last_seen or "",
            "Spam": "yes" if r.is_spam else "",
        }
        for w in snapshot.websites
        for r in w.refdomains
    ]
    pages: list[dict[str, Cell]] = [
        {
            "Website": w.website,
            "Page": p.url,
            "Title": p.title or "",
            "Referring domains": p.referring_domains,
            "Links": p.links,
            "URL Rating": p.url_rating,
            "HTTP code": p.http_code,
        }
        for w in snapshot.websites
        for p in w.top_pages
    ]
    intersect: list[dict[str, Cell]] = [
        {
            "Referring domain": i.domain,
            "Domain Rating": i.domain_rating,
            "Links to competitors": len(i.competitors),
            "Competitors": ", ".join(i.competitors),
            "Links": i.links,
        }
        for i in snapshot.insights.link_intersect
    ]
    gaps: list[dict[str, Cell]] = [
        {
            "Competitor": g.competitor,
            "Compared by": "Domain Rating" if g.metric == "domain_rating" else "Referring domains",
            "Client": g.client,
            "Competitor value": g.competitor_value,
            "Gap": g.gap,
        }
        for g in snapshot.insights.authority
    ]
    notes = [*snapshot.notes, *snapshot.insights.notes]
    reading: list[dict[str, Cell]] = [{"Column": k, "Meaning": v} for k, v in _HOW_TO_READ]
    reading += [{"Column": "Note", "Meaning": n} for n in notes]
    return [
        Table("Websites", list(sites[0]) if sites else ["Website"], sites),
        Table(
            "Referring domains",
            [
                "Website",
                "Referring domain",
                "Domain Rating",
                "Links to target",
                "Dofollow links",
                "First seen",
                "Last seen",
                "Spam",
            ],
            refdomains,
            widths={"Referring domain": 40},
        ),
        Table(
            "Top pages",
            ["Website", "Page", "Title", "Referring domains", "Links", "URL Rating", "HTTP code"],
            pages,
            widths={"Page": 60, "Title": 40},
        ),
        Table(
            "Link intersect",
            ["Referring domain", "Domain Rating", "Links to competitors", "Competitors", "Links"],
            intersect,
            widths={"Referring domain": 40, "Competitors": 60},
        ),
        Table(
            "Authority gap",
            ["Competitor", "Compared by", "Client", "Competitor value", "Gap"],
            gaps,
        ),
        Table(
            "How to read",
            ["Column", "Meaning"],
            reading,
            widths={"Meaning": 110},
        ),
    ]


def write_backlinks_workbook(snapshot: BacklinkSnapshot, path: Path) -> Path:
    """The snapshot's workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, backlink_tables(snapshot))
    return path
