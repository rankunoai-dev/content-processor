"""Pull, import and read backlink snapshots (shared by the CLI, the API and the tools)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from src.modules.analyst.playground import Playground
from src.modules.backlinks.ahrefs import (
    AHREFS_PACER,
    BACKLINKS_STATS,
    DOMAIN_RATING,
    MIN_UNITS,
    REFERRING_DOMAINS,
    TOP_PAGES,
    AhrefsBudget,
    AhrefsClient,
    AhrefsError,
    price,
    row_cost,
)
from src.modules.backlinks.importer import (
    BacklinksImportError,
    ExportKind,
    read_pages_csv,
    read_refdomains_csv,
)
from src.modules.backlinks.insights import build_insights, with_history
from src.modules.backlinks.models import BacklinkSnapshot, WebsiteBacklinks
from src.modules.backlinks.store import BacklinksStore, write_backlinks_workbook
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.projects.websites import Website
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.research.semrush import Pacer, UnitLog

__all__ = [
    "MIN_PAGES",
    "MIN_REFDOMAINS",
    "BacklinksSettings",
    "address_refusal",
    "backlinks_store",
    "estimate_units",
    "fair_limits",
    "import_csv",
    "pull_project",
    "site_units",
    "unit_log",
]

_LONG_PREFIX = "\\\\?\\"  # Windows extended-length path prefix, not shown to people
REAL_HOST = "api.ahrefs.com"
MIN_REFDOMAINS = 10
MIN_PAGES = 5


@dataclass(frozen=True)
class BacklinksSettings:
    """The `CP_AHREFS_*` settings (passed in so tests can set them)."""

    enabled: bool = False
    api_key: str | None = None
    monthly_units: int = 0
    pull_units: int = 10_000
    refdomains_limit: int = 500
    pages_limit: int = 100
    min_interval_ms: int = 500
    base_url: str = "https://api.ahrefs.com/v3/"

    @classmethod
    def from_env(cls) -> BacklinksSettings:
        """Read the settings from `.env`."""
        from src.core.config import get_settings

        s = get_settings()
        return cls(
            enabled=s.cp_ahrefs_enabled,
            api_key=s.ahrefs_api_key.get_secret_value() if s.ahrefs_api_key else None,
            monthly_units=s.cp_ahrefs_monthly_units,
            pull_units=s.cp_ahrefs_pull_units,
            refdomains_limit=s.cp_ahrefs_refdomains_limit,
            pages_limit=s.cp_ahrefs_pages_limit,
            min_interval_ms=s.cp_ahrefs_min_interval_ms,
            base_url=s.cp_ahrefs_base_url,
        )


def address_refusal(base_url: str) -> str | None:
    """Why the API may not be called at this address (None when it may).

    The key travels with every call, so it goes only to Ahrefs itself (https) or to a
    server on this computer: the stand-in that serves dummy data for trying the module.
    """
    parts = urlsplit(base_url)
    host = (parts.hostname or "").casefold()
    if parts.scheme == "https" and host == REAL_HOST:
        return None
    if parts.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1"):
        return None
    return (
        f"CP_AHREFS_BASE_URL {base_url!r} is refused: the API key is sent only to "
        "https://api.ahrefs.com/v3/ or to the stand-in on this computer (http://127.0.0.1:...)"
    )


def _is_real(base_url: str) -> bool:
    return (urlsplit(base_url).hostname or "").casefold() == REAL_HOST


def backlinks_store(projects: ProjectStore) -> BacklinksStore:
    """The backlinks store next to the projects (`<app data>/backlinks`)."""
    return BacklinksStore(Path(str(projects.root)) / "backlinks")


def unit_log(projects: ProjectStore) -> UnitLog:
    """The log of every Ahrefs call (`<app data>/backlinks/units.jsonl`)."""
    return UnitLog(Path(str(projects.root)) / "backlinks")


def site_units(refdomains: int, pages: int) -> int:
    """Worst-case units of one website: rating, stats, referring domains, top pages."""
    return (
        price(DOMAIN_RATING, 1)
        + price(BACKLINKS_STATS, 1)
        + price(REFERRING_DOMAINS, refdomains)
        + price(TOP_PAGES, pages)
    )


def estimate_units(websites: int, settings: BacklinksSettings) -> int:
    """The worst-case units of a pull of `websites` websites at the configured limits."""
    return websites * site_units(settings.refdomains_limit, settings.pages_limit)


def fair_limits(share: int, settings: BacklinksSettings) -> tuple[int, int]:
    """(referring domains, top pages) to ask for one website that may use `share` units.

    Both lists shrink in proportion when the full lists would not fit; never below 10
    referring domains and 5 pages.
    """
    room = share - 2 * MIN_UNITS
    want = settings.refdomains_limit * row_cost(
        REFERRING_DOMAINS
    ) + settings.pages_limit * row_cost(TOP_PAGES)
    if room >= want:
        return settings.refdomains_limit, settings.pages_limit
    part = max(0.0, room / want)
    return (
        max(MIN_REFDOMAINS, int(settings.refdomains_limit * part)),
        max(MIN_PAGES, int(settings.pages_limit * part)),
    )


def _new_id() -> tuple[datetime, str]:
    moment = datetime.now(UTC)
    stamp = moment.strftime("%Y%m%d-%H%M%S-") + f"{moment.microsecond // 1000:03d}"
    return moment, "backlinks-" + stamp


def _role(play: Playground, website: Website) -> str:
    return "client" if play.role_in_run(website) == "client" else "competitor"


def _order(websites: list[WebsiteBacklinks]) -> list[WebsiteBacklinks]:
    return sorted(websites, key=lambda w: (w.role != "client", w.website))


def _save(
    projects: ProjectStore, snapshot: BacklinkSnapshot
) -> tuple[BacklinkSnapshot, dict[str, str]]:
    """Work out the insights, save the snapshot and write its workbook."""
    done = snapshot.model_copy(update={"insights": build_insights(snapshot.websites)})
    store = backlinks_store(projects)
    path = store.save(done)
    workbook = write_backlinks_workbook(done, store.workbook(done))
    files = {"snapshot": path, "workbook": workbook}
    return done, {k: str(v).removeprefix(_LONG_PREFIX) for k, v in files.items()}


def _pull_website(
    client: AhrefsClient,
    website: Website,
    role: str,
    limits: tuple[int, int],
    settings: BacklinksSettings,
    date: str,
) -> WebsiteBacklinks:
    key = website.key
    rating, rank = client.domain_rating(key, date)
    stats = client.backlinks_stats(key, date)
    refdomains = client.referring_domains(key, limits[0])
    pages = client.top_pages(key, limits[1])
    cut = []
    if limits[0] < settings.refdomains_limit:
        cut.append(f"{limits[0]} referring domains")
    if limits[1] < settings.pages_limit:
        cut.append(f"{limits[1]} top pages")
    return WebsiteBacklinks(
        website=key,
        name=website.name,
        role="client" if role == "client" else "competitor",
        source="ahrefs_api",
        pulled_at=datetime.now(UTC),
        domain_rating=rating,
        ahrefs_rank=rank,
        stats=stats,
        refdomains=refdomains,
        refdomains_asked=limits[0],
        top_pages=pages,
        note=f"Asked for {' and '.join(cut)} to share the pull cap fairly" if cut else "",
    )


def pull_project(
    projects: ProjectStore,
    access: KnowledgeAccess,
    project_id: str,
    settings: BacklinksSettings,
    *,
    http: Callable[[], httpx.Client] | None = None,
    pacer: Pacer | None = None,
) -> tuple[BacklinkSnapshot, dict[str, str]]:
    """Pull every website of the project from Ahrefs (refused unless switched on and capped).

    Websites go in turn, client first; each may use an equal share of what is left, so
    the lists shrink fairly when the pull cap is small. A website that fails keeps its
    previous data (with a note); the pull fails only when every website did.
    """
    log = unit_log(projects)
    budget = AhrefsBudget(
        log,
        enabled=settings.enabled and bool(settings.api_key),
        monthly_units=settings.monthly_units,
        pull_units=settings.pull_units,
    )
    refusal = budget.refusal() or address_refusal(settings.base_url)
    if refusal or not settings.api_key:
        raise AhrefsError(refusal or "AHREFS_API_KEY is not set in .env")
    play = Playground(projects, access, [project_id])
    websites = sorted(play.websites(), key=lambda w: (_role(play, w) != "client", w.name.lower()))
    if not websites:
        msg = "the project has no websites to pull"
        raise AhrefsError(msg)
    need = site_units(MIN_REFDOMAINS, MIN_PAGES)
    if budget.left // len(websites) < need:
        msg = (
            f"the pull may use {budget.left} units; each of the {len(websites)} websites would "
            f"get {budget.left // len(websites)}, and at least {need} are needed (its rating, "
            f"counts, {MIN_REFDOMAINS} referring domains and {MIN_PAGES} pages): raise "
            "CP_AHREFS_PULL_UNITS or the monthly cap"
        )
        raise AhrefsError(msg)
    pace = pacer or AHREFS_PACER
    pace.interval_s = settings.min_interval_ms / 1000
    client = AhrefsClient(
        settings.api_key,
        budget,
        log,
        project_id,
        client=http() if http else None,
        base_url=settings.base_url,
        pacer=pace,
    )
    store = backlinks_store(projects)
    previous = store.latest(project_id)
    before = {w.website: w for w in (previous.websites if previous else [])}
    created, snapshot_id = _new_id()
    date = created.date().isoformat()
    pulled: list[WebsiteBacklinks] = []
    notes: list[str] = []
    failed = 0
    try:
        for i, website in enumerate(websites):
            limits = fair_limits(budget.left // (len(websites) - i), settings)
            try:
                site = _pull_website(client, website, _role(play, website), limits, settings, date)
            except AhrefsError as exc:
                failed += 1
                notes.append(f"{website.name} could not be pulled: {exc}")
                if website.key in before:
                    kept = before[website.key]
                    pulled.append(kept.model_copy(update={"note": "Kept from the last snapshot"}))
                continue
            pulled.append(with_history(before.get(website.key), site))
    finally:
        client.close()
    if failed == len(websites):
        raise AhrefsError("; ".join(notes))
    if not _is_real(settings.base_url):
        from src.modules.backlinks.standin import DUMMY_NOTE

        notes.insert(0, DUMMY_NOTE)
    if client.retries:
        notes.append(
            f"Ahrefs asked to slow down {client.retries} time(s); the pull waited and went on"
        )
    snapshot = BacklinkSnapshot(
        snapshot_id=snapshot_id,
        project_id=project_id,
        created_at=created,
        websites=_order(pulled),
        units_spent=budget.spent,
        notes=[f"Pulled {len(pulled) - failed} website(s) from Ahrefs", *notes],
    )
    return _save(projects, snapshot)


def import_csv(
    projects: ProjectStore,
    access: KnowledgeAccess,
    project_id: str,
    website: str,
    kind: ExportKind,
    data: bytes,
) -> tuple[BacklinkSnapshot, dict[str, str]]:
    """Import one website's Ahrefs export ($0): its referring domains or its top pages.

    The other websites, and the other part of this website, are kept from the latest
    snapshot. Raises `BacklinksImportError` (not the export) or `ValueError` (website).
    """
    play = Playground(projects, access, [project_id])
    found = play.website(website)
    store = backlinks_store(projects)
    previous = store.latest(project_id)
    others = [w for w in (previous.websites if previous else []) if w.website != found.key]
    last = next(
        (w for w in (previous.websites if previous else []) if w.website == found.key), None
    )
    created, snapshot_id = _new_id()
    base = last or WebsiteBacklinks(
        website=found.key,
        name=found.name,
        role="client" if _role(play, found) == "client" else "competitor",
        source="csv_import",
        pulled_at=created,
    )
    if kind == "refdomains":
        rows = read_refdomains_csv(data)
        if not rows:
            raise BacklinksImportError("the export has no referring domains")
        site = with_history(
            last,
            base.model_copy(
                update={
                    "refdomains": rows,
                    "refdomains_asked": 0,
                    "source": "csv_import",
                    "pulled_at": created,
                    "note": "",
                }
            ),
        )
        note = f"{found.name}: {len(rows)} referring domains imported from an Ahrefs export"
    else:
        pages = read_pages_csv(data)
        if not pages:
            raise BacklinksImportError("the export has no pages")
        site = base.model_copy(
            update={"top_pages": pages, "source": "csv_import", "pulled_at": created, "note": ""}
        )
        note = f"{found.name}: {len(pages)} top pages imported from an Ahrefs export"
    snapshot = BacklinkSnapshot(
        snapshot_id=snapshot_id,
        project_id=project_id,
        created_at=created,
        websites=_order([*others, site]),
        notes=[note],
    )
    return _save(projects, snapshot)
