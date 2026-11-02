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
from urllib.parse import unquote, urlsplit

from src.modules.content_processor.scope.normalize import InvalidInputUrl, normalize_url

__all__ = [
    "IMPORT_MAX_BYTES",
    "MAX_PROJECT_URLS",
    "ImportResult",
    "UnreadableFileError",
    "address_of",
    "address_score",
    "addresses_overlap",
    "belongs_to",
    "candidates_from_file",
    "candidates_from_text",
    "domain_of",
    "host_key",
    "merge_urls",
    "split_address",
    "url_domain",
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


# -- websites and their domains --------------------------------------------------


def host_key(host: str) -> str:
    """A host as a website key: lower case, without a leading 'www.'."""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def domain_of(website: str) -> str:
    """The domain of what a person typed for a website ('https://www.flynns.com/x' -> 'flynns.com').

    Raises:
        ValueError: The text is not a web address or a domain.
    """
    text = website.strip()
    if not text:
        raise ValueError("empty website")
    if "://" not in text:
        text = "https://" + text
    try:
        host = normalize_url(text).host
    except InvalidInputUrl as exc:
        raise ValueError(f"'{website}' is not a website address") from exc
    key = host_key(host)
    if "." not in key:
        raise ValueError(f"'{website}' is not a website address")
    return key


def belongs_to(url: str, domains: Iterable[str]) -> bool:
    """True when the URL's host is one of the domains or a subdomain of one."""
    try:
        host = host_key(normalize_url(url).host)
    except InvalidInputUrl:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def url_domain(url: str) -> str | None:
    """The website key of a URL's host, or None when it is not a usable URL."""
    try:
        return host_key(normalize_url(url).host)
    except InvalidInputUrl:
        return None


# -- website addresses: a domain, or a section of one ------------------------------
#
# A website's address is usually a domain ("flynns.com", subdomains included). When
# one domain holds several websites (the client's brands at "brand.client.com" and
# "client.com", or sections such as "client.com/brand-a"), each website has its own,
# more specific address, and a URL belongs to the most specific address that covers it.


def split_address(address: str) -> tuple[str, str]:
    """('client.com/brand-a') -> ('client.com', '/brand-a'); a plain domain has path ''."""
    host, _, path = address.partition("/")
    return host, f"/{path}" if path else ""


def address_of(website: str, *, keep_path: bool) -> str:
    """The address of what a person typed: a domain, or with `keep_path` a domain and section.

    'https://www.client.com/Brand-A/' -> 'client.com' (or 'client.com/brand-a' with keep_path).

    Raises:
        ValueError: The text is not a web address or a domain.
    """
    host = domain_of(website)
    if not keep_path:
        return host
    text = website.strip()
    if "://" not in text:
        text = "https://" + text
    path = _path_of(normalize_url(text).schedule_key).strip("/")
    return f"{host}/{path}" if path else host


def _path_of(normalized: str) -> str:
    return unquote(urlsplit(normalized).path).lower()


def _host_path(url_or_address: str) -> tuple[str, str] | None:
    text = url_or_address.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        parts = normalize_url(text)
    except InvalidInputUrl:
        return None
    return host_key(parts.host), _path_of(parts.schedule_key).rstrip("/")


def address_score(url: str, address: str) -> tuple[int, int] | None:
    """How specifically an address covers a URL (higher is more specific), or None.

    The host must be the address's domain or a subdomain of it; an address with a
    section covers only the URLs under that section.
    """
    found = _host_path(url)
    if found is None:
        return None
    host, path = found
    a_host, a_path = split_address(address)
    if not (host == a_host or host.endswith("." + a_host)):
        return None
    if a_path and not (path == a_path or path.startswith(a_path + "/")):
        return None
    return a_host.count(".") + 1, a_path.count("/")


def addresses_overlap(a: str, b: str) -> bool:
    """True when one address covers the other (equal, a subdomain, or a section of it)."""
    return a == b or address_score(b, a) is not None or address_score(a, b) is not None
