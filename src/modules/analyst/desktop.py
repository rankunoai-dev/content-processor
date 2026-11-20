"""Keep answers written in Claude Desktop in the analyst run history.

Claude Desktop reads a project through the MCP server and writes its answer in the chat;
`save_deliverable` hands that answer back to the web app, which checks it like any card
deliverable (citations must be ones a tool returned for the project, quotes verbatim,
facts cited) and stores it as a run of kind `claude_desktop`: the readable text with
numbered sources, the check, the Sources table, the author (Claude Desktop and the
connection's module scope), the time and the text as Claude sent it.

Nothing is sent anywhere: the text is already in Claude, and the app only writes files
on this computer, so a confidential project can save too.

Saving the same title again for the same project adds a **version** to the same record
(`versions`), instead of a second run; every version's files stay in the run folder.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import UTC, datetime
from typing import Final

from src.modules.analyst.models import ActionCard, DeliverableVersion, RunRecord, Verification
from src.modules.analyst.playground import EvidenceLedger
from src.modules.analyst.runs import RunStore
from src.modules.analyst.verify import REF, readable_version, sources_section, verify_deliverable

__all__ = [
    "DESKTOP_AUTHOR",
    "DESKTOP_CARD_ID",
    "MAX_TITLE_CHARS",
    "deliverable_text",
    "find_saved",
    "save_deliverable",
    "same_title",
]

DESKTOP_AUTHOR: Final = "Claude Desktop"
DESKTOP_CARD_ID: Final = "claude-desktop"
"""The `card_id` of a saved deliverable that names no card."""
MAX_TITLE_CHARS: Final = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SAVE_LOCK = threading.Lock()


def _clean_title(title: str) -> str:
    return " ".join(_CONTROL.sub(" ", title).split())[:MAX_TITLE_CHARS]


def same_title(a: str, b: str) -> bool:
    """Whether two titles name the same deliverable (case and spacing aside)."""
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def find_saved(runs: RunStore, project_id: str, title: str) -> RunRecord | None:
    """The project's saved Claude Desktop deliverable with this title, if any."""
    for record in runs.all():
        if (
            record.backend == "claude_desktop"
            and project_id in record.project_ids
            and record.title is not None
            and same_title(record.title, title)
        ):
            return record
    return None


def save_deliverable(
    runs: RunStore,
    *,
    project_id: str,
    title: str,
    markdown: str,
    ledger: EvidenceLedger,
    author: str = DESKTOP_AUTHOR,
    card: ActionCard | None = None,
    module: str | None = None,
) -> tuple[RunRecord, Verification, str]:
    """Check `markdown` against `ledger` and store it; returns (record, check, readable text).

    A record with the same title for this project (case and spacing aside) gets a new
    version and keeps its first spelling; otherwise a new run is created. The check never
    blocks the save: a deliverable that needs review is stored with that result, so the
    person sees it in the app.
    """
    title = _clean_title(title)
    if not title:
        msg = "title is required"
        raise ValueError(msg)
    check = verify_deliverable(markdown, ledger)
    readable = readable_version(markdown, ledger)
    deliverable = readable.rstrip() + "\n" + sources_section(markdown, ledger, check)
    cited = EvidenceLedger()
    for ref in dict.fromkeys(REF.findall(markdown)):
        entry = ledger.entries.get(ref)
        if entry is not None:
            cited.entries[ref] = entry
    now = datetime.now(UTC)
    with _SAVE_LOCK:
        existing = find_saved(runs, project_id, title)
        version = existing.version + 1 if existing is not None else 1
        run_id = existing.run_id if existing is not None else f"run-{uuid.uuid4().hex[:12]}"
        folder = runs.folder(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        file_name = f"deliverable.v{version}.md"
        draft_name = f"draft.v{version}.md"
        (folder / file_name).write_text(deliverable, encoding="utf-8")
        (folder / draft_name).write_text(markdown, encoding="utf-8")
        (folder / "ledger.json").write_text(cited.to_json(), encoding="utf-8")
        saved = DeliverableVersion(
            version=version, saved_at=now, passed=check.passed, file=file_name, draft=draft_name
        )
        message = (
            f"saved from {author}; the check passed"
            if check.passed
            else f"saved from {author}; the check needs review"
        )
        if version > 1:
            message += f" (version {version})"
        update = {
            "status": "finished",
            "finished_at": now,
            "message": message,
            "verification": check,
            "files": {"deliverable": file_name, "draft": draft_name, "ledger": "ledger.json"},
            "title": existing.title if existing is not None else title,
            "author": author,
            "module": module,
            "version": version,
            "versions": [*(existing.versions if existing is not None else []), saved],
        }
        base = existing or RunRecord(
            run_id=run_id,
            card_id=card.id if card is not None else DESKTOP_CARD_ID,
            card_version=card.version if card is not None else 0,
            backend="claude_desktop",
            status="finished",
            created_at=now,
            inputs={"title": title},
            project_ids=[project_id],
        )
        record = base.model_copy(update=update)
        runs.save(record)
        runs.event(
            run_id,
            {
                "type": "saved",
                "version": version,
                "author": author,
                "status": "passed" if check.passed else "needs review",
            },
        )
        runs.event(run_id, {"type": "end", "status": "finished", "message": message})
    return record, check, readable


def deliverable_text(runs: RunStore, record: RunRecord, version: int | None = None) -> str | None:
    """A run's readable result (or one saved version of a Claude Desktop deliverable)."""
    folder = runs.folder(record.run_id)
    name: str | None
    if version is not None:
        found = next((v for v in record.versions if v.version == version), None)
        if found is None:
            return None
        name = found.file
    else:
        name = record.files.get("deliverable") or record.files.get("context_pack")
    if not name or not (folder / name).exists():
        return None
    return (folder / name).read_text(encoding="utf-8")
