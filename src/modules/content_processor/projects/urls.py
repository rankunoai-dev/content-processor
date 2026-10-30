"""Read URLs from pasted text or an imported file (.txt, .csv, .xlsx) and merge them.

Every candidate is checked with the scraper's own normaliser, so "the same URL"
means the same thing here as in a run. Nothing is dropped silently: the result
counts what was added, what was already there and what was rejected, with the
reason for each rejection.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from src.modules.content_processor.scope.normalize import InvalidInputUrl, normalize_url

__all__ = [
    "IMPORT_MAX_BYTES",
    "MAX_PROJECT_URLS",
    "ImportResult",
    "UnreadableFileError",
    "candidates_from_file",
    "candidates_from_text",
    "merge_urls",
]

IMPORT_MAX_BYTES = 20 * 1024 * 1024
MAX_PROJECT_URLS = 100_000
_URLISH = re.compile(r"https?://[^\s\"'<>,;]+", re.IGNORECASE)
_REASONS = {
    "empty": "empty line",
    "too_long": "address is too long",
    "contains_whitespace": "address contains spaces",
    "missing_host": "no website name in the address",
    "credentials_in_url": "address contains a user name or password",
    "invalid_idn_host": "website name is not valid",
    "invalid_port": "port number is not valid",
}


def _reason(code: str) -> str:
    if code.startswith("unsupported_scheme"):
        return "only http and https addresses can be scraped"
    return _REASONS.get(code, code)


class UnreadableFileError(ValueError):
    """The imported file could not be read; the message says why, in plain words."""


@dataclass
class ImportResult:
    """What a paste or an import did to the project's URL list."""

    added: list[str] = field(default_factory=list)
    already_present: int = 0
    duplicates_in_input: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    over_limit: int = 0

    def summary(self) -> dict[str, object]:
        """Counts plus up to 20 rejected lines with their reasons."""
        return {
            "added": len(self.added),
            "already_present": self.already_present,
            "duplicates_in_input": self.duplicates_in_input,
            "rejected": len(self.rejected),
            "rejected_examples": [{"value": v, "reason": r} for v, r in self.rejected[:20]],
            "over_limit": self.over_limit,
        }


def candidates_from_text(text: str) -> list[str]:
    """One candidate per non-empty line (a line may hold several URLs separated by spaces)."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip().strip("﻿")
        if not line:
            continue
        found = _URLISH.findall(line)
        out.extend(found if found else [line])
    return out


def _from_csv(text: str) -> list[str]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    out: list[str] = []
    for row in csv.reader(io.StringIO(text), dialect):
        for cell in row:
            out.extend(_URLISH.findall(cell))
    return out


def _from_xlsx(data: bytes) -> list[str]:
    try:
        from openpyxl import load_workbook  # type: ignore[import-untyped]  # no stubs
    except ImportError as exc:  # pragma: no cover - the office extra is installed in dev
        raise UnreadableFileError("Excel files need the 'office' extra (openpyxl).") from exc
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any parse failure is an unreadable file
        raise UnreadableFileError("This file could not be read as an Excel workbook.") from exc
    out: list[str] = []
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                for cell in row:
                    if isinstance(cell, str):
                        out.extend(_URLISH.findall(cell))
    finally:
        workbook.close()
    return out


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def candidates_from_file(name: str, data: bytes) -> list[str]:
    """URLs found in an imported file; the kind is taken from the file name."""
    if len(data) > IMPORT_MAX_BYTES:
        raise UnreadableFileError("The file is larger than 20 MB.")
    lower = name.lower()
    if lower.endswith(".xlsx"):
        return _from_xlsx(data)
    if lower.endswith((".xls", ".xlsm", ".numbers", ".ods")):
        raise UnreadableFileError("Save the sheet as .xlsx or .csv and import that file.")
    text = _decode(data)
    if lower.endswith((".csv", ".tsv")):
        return _from_csv(text)
    if lower.endswith((".txt", ".text", ".list", ".md")) or "." not in lower:
        return candidates_from_text(text)
    raise UnreadableFileError("Import a .txt, .csv or .xlsx file.")


def merge_urls(existing: Iterable[str], candidates: Iterable[str]) -> ImportResult:
    """Add the valid, new candidates to the list; count everything else."""
    known = set()
    for url in existing:
        try:
            known.add(normalize_url(url).schedule_key)
        except InvalidInputUrl:
            known.add(url)
    room = MAX_PROJECT_URLS - len(known)
    result = ImportResult()
    seen_now: set[str] = set()
    for raw in candidates:
        value = raw.strip()
        try:
            key = normalize_url(value).schedule_key
        except InvalidInputUrl as exc:
            result.rejected.append((value[:300], _reason(exc.reason)))
            continue
        if key in known:
            result.already_present += 1
        elif key in seen_now:
            result.duplicates_in_input += 1
        elif len(result.added) >= room:
            result.over_limit += 1
        else:
            seen_now.add(key)
            result.added.append(value)
    return result
