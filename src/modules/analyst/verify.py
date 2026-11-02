"""Check a deliverable against the run's evidence ledger, and append its sources.

Three checks, all by code:

1. **References resolve.** Every `[chunk:...]` / `[fact:...]` must be one the run's
   tools handed out. An unknown reference means a made-up citation.
2. **Quotes are verbatim.** Text in double quotes followed by a `[chunk:...]`
   reference must appear word for word (spacing and case aside) in that passage.
3. **Claims are cited.** A line that states numbers, or a long sentence, without
   any reference is listed as uncited (headings, the "Limits" and
   "Recommendations" sections and table headers are not claims).

The run passes when no reference is unknown, no quote fails, and at most a few
lines are uncited. Then code appends a "Sources" table that resolves every cited
reference to its website, page, headings and evidence hashes, so the hashes in
the deliverable always come from the store, never from the model.
"""

from __future__ import annotations

import re

from src.modules.analyst.models import Verification
from src.modules.analyst.playground import EvidenceLedger
from src.modules.content_processor.knowledge.assist import quote_in

__all__ = ["MAX_UNCITED", "REF", "sources_section", "verify_deliverable"]

REF = re.compile(r"\[((?:chunk|fact):[A-Za-z0-9_.:\-]+)\]")
MAX_UNCITED = 3
_QUOTED = re.compile(r"[\"“]([^\"“”]{12,600})[\"”][^\[\n]{0,40}\[(chunk:[A-Za-z0-9_.:\-]+)\]")
_NUMBER = re.compile(r"\d")
_EXEMPT_SECTIONS = re.compile(
    r"^#+\s*(limits|recommend|implementation|action plan|content to create|counter-moves|"
    r"five priorities|quick wins|what makes|internal link|outline|title tag|target length|"
    r"intent|faq|required elements|rewrites)",
    re.IGNORECASE,
)
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}")
_MIN_CLAIM_WORDS = 14


def _claims(text: str) -> list[str]:
    """Lines that state something checkable, outside exempt sections and table headers."""
    out: list[str] = []
    exempt = False
    previous_was_header_row = False
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("#"):
            exempt = bool(_EXEMPT_SECTIONS.match(line))
            continue
        if not line or exempt or line.startswith(("```", ">")) or _TABLE_RULE.match(line):
            continue
        is_table = line.startswith("|")
        next_is_rule = i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1].strip())
        if is_table and next_is_rule:
            previous_was_header_row = True
            continue
        previous_was_header_row = previous_was_header_row and is_table
        words = len(line.split())
        if _NUMBER.search(line) or words >= _MIN_CLAIM_WORDS:
            out.append(line)
    return out


def verify_deliverable(text: str, ledger: EvidenceLedger) -> Verification:
    """Resolve references, check quotes and find uncited claims."""
    refs = REF.findall(text)
    unknown = sorted({r for r in refs if r not in ledger.entries})
    failed: list[str] = []
    checked = 0
    for quote, ref in _QUOTED.findall(text):
        entry = ledger.entries.get(ref)
        if entry is None:
            continue
        checked += 1
        if not quote_in(quote, entry.text):
            failed.append(f'"{quote[:120]}" [{ref}]')
    uncited = [line[:200] for line in _claims(text) if not REF.search(line)]
    return Verification(
        references=len(refs),
        resolved=len(refs) - sum(1 for r in refs if r in unknown),
        unknown_refs=unknown,
        quotes_checked=checked,
        quotes_failed=failed,
        uncited_lines=uncited,
        passed=not unknown and not failed and len(uncited) <= MAX_UNCITED,
    )


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def sources_section(text: str, ledger: EvidenceLedger, check: Verification) -> str:
    """The "Verification" and "Sources" sections code appends to the deliverable."""
    cited = list(dict.fromkeys(r for r in REF.findall(text) if r in ledger.entries))
    status = "passed" if check.passed else "needs review"
    lines = [
        "",
        "---",
        "",
        "## Verification (by code)",
        "",
        f"- Result: **{status}**. {check.resolved} of {check.references} citations resolve to "
        f"data shown in this run; {check.quotes_checked - len(check.quotes_failed)} of "
        f"{check.quotes_checked} quotes match their passage word for word; "
        f"{len(check.uncited_lines)} line(s) state facts without a citation.",
    ]
    if check.unknown_refs:
        lines.append(f"- Unknown citations (not from this run): {', '.join(check.unknown_refs)}")
    for failure in check.quotes_failed:
        lines.append(f"- Quote not found in its passage: {failure}")
    for line in check.uncited_lines[:10]:
        lines.append(f"- Uncited: {line}")
    lines += [
        "",
        "## Sources",
        "",
        "| Ref | Website | Page | What | Evidence |",
        "|---|---|---|---|---|",
    ]
    for ref in cited:
        entry = ledger.entries[ref]
        evidence = ", ".join(e.removeprefix("evidence:")[:16] for e in entry.evidence_ids) or "-"
        lines.append(
            f"| `{ref}` | {_cell(entry.website)} | {_cell(entry.url or '-')} | "
            f"{_cell(entry.title or entry.kind)[:120]} | {evidence} |"
        )
    if not cited:
        lines.append("| - | - | - | no citations | - |")
    return "\n".join(lines) + "\n"
