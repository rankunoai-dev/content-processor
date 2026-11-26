"""Analytics service: GA4 imports and pulls, and the report built from what the app holds."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from src.modules.analyst.playground import Playground
from src.modules.analytics.importer import aggregate, read_ga4_csv
from src.modules.analytics.insights import build_report
from src.modules.analytics.models import AnalyticsReport, AnalyticsSettings, Ga4Snapshot
from src.modules.analytics.store import AnalyticsStore, write_analytics_workbook
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.research.links import page_key

__all__ = [
    "analytics_store",
    "build_analytics_report",
    "client_website",
    "import_ga4_csv",
    "new_snapshot_id",
    "pull_ga4",
]

_LONG_PREFIX = "\\\\?\\"


def analytics_store(projects: ProjectStore) -> AnalyticsStore:
    """The store under the app data folder."""
    return AnalyticsStore(Path(projects.root) / "analytics")


def new_snapshot_id() -> tuple[datetime, str]:
    """Now, and a snapshot id made of it."""
    moment = datetime.now(UTC)
    return moment, "ga4-" + moment.strftime("%Y%m%d-%H%M%S-") + f"{moment.microsecond // 1000:03d}"


def client_website(projects: ProjectStore, project_id: str) -> str | None:
    """The client's main website (its shortest domain), from the project record."""
    project = projects.get(project_id)
    main = next((s for s in project.sites if s.role == "client" and s.domains), None)
    if main is None:
        return None
    return min((d.removeprefix("www.") for d in main.domains), key=len)


def import_ga4_csv(
    projects: ProjectStore, project_id: str, website: str | None, data: bytes
) -> Ga4Snapshot:
    """Keep a GA4 landing page export of the client's website ($0)."""
    site = (website or client_website(projects, project_id) or "").removeprefix("www.")
    if not site:
        raise ValueError("the project has no client website to attach the export to")
    now, snapshot_id = new_snapshot_id()
    snapshot = read_ga4_csv(
        data, project_id=project_id, website=site, now=now, snapshot_id=snapshot_id
    )
    store = analytics_store(projects)
    store.save(snapshot)
    settings = store.settings(project_id)
    if not settings.website:
        store.save_settings(project_id, settings.model_copy(update={"website": site}))
    return snapshot


def pull_ga4(
    projects: ProjectStore,
    project_id: str,
    *,
    client_factory: Callable[[], object] | None = None,
    end: date | None = None,
    days: int | None = None,
) -> list[Ga4Snapshot]:
    """Pull the latest period and the one before from the GA4 Data API, and keep both.

    Refused unless switched on, with a service-account key file and the project's GA4
    property set. Each period is a landing page report with channels, plus one with
    sources for AI referrals.
    """
    from src.core.config import get_settings
    from src.modules.analytics.ga4 import Ga4Client, Ga4Error, Ga4Settings, address_refusal

    s = Ga4Settings.from_env()
    store = analytics_store(projects)
    settings = store.settings(project_id)
    refusal = (
        None
        if s.enabled
        else "GA4 pulls are switched off: set CP_GA4_ENABLED=true in .env (the CSV import "
        "works without it)"
    )
    refusal = refusal or (
        None
        if s.credentials_file
        else "GA4_CREDENTIALS_FILE is not set: point it at the service account's key file"
    )
    refusal = refusal or (
        None if settings.property_id else "set the project's GA4 property id first"
    )
    refusal = refusal or address_refusal(s.base_url, s.token_url)
    if refusal:
        raise Ga4Error(refusal)
    site = settings.website or client_website(projects, project_id)
    if not site:
        raise Ga4Error("the project has no client website")
    period = days or get_settings().cp_ga4_period_days
    last = end or (datetime.now(UTC).date() - timedelta(days=1))
    client = (
        client_factory()
        if client_factory
        else Ga4Client(
            s.credentials_file,  # type: ignore[arg-type]
            base_url=s.base_url,
            token_url=s.token_url,
            max_requests=s.max_requests,
        )
    )
    saved = []
    try:
        for start, finish in (
            (last - timedelta(days=2 * period - 1), last - timedelta(days=period)),
            (last - timedelta(days=period - 1), last),
        ):
            records = client.landing_pages(  # type: ignore[attr-defined]
                settings.property_id, start, finish, channels=True, sources=True
            )
            now, snapshot_id = new_snapshot_id()
            snapshot = Ga4Snapshot(
                snapshot_id=snapshot_id,
                project_id=project_id,
                website=site,
                source="ga4_api",
                imported_at=now,
                start_date=start,
                end_date=finish,
                property_id=settings.property_id,
                rows=aggregate(records, site),
                has_channels=True,
                has_sources=True,
            )
            if "127.0.0.1" in s.base_url or "localhost" in s.base_url:
                snapshot.notes.append(
                    "DUMMY DATA from the GA4 stand-in: invented numbers for trying the module"
                )
            store.save(snapshot)
            saved.append(snapshot)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return saved


def build_analytics_report(
    projects: ProjectStore, access: KnowledgeAccess, project_id: str
) -> tuple[AnalyticsReport, dict[str, str]] | None:
    """The analytics report of the latest snapshot and its workbook (None without data)."""
    from src.modules.monitoring.service import monitor_store
    from src.modules.research.service import research_store
    from src.modules.theming.service import theme_store

    store = analytics_store(projects)
    current = store.latest(project_id)
    if current is None:
        return None
    previous = store.previous(project_id)
    research = research_store(projects)
    snapshot = research.latest(project_id)
    gsc = research.latest_gsc(project_id)
    stamps = {}
    play = Playground(projects, access, [project_id])
    for website in play.websites():
        if website.key.removeprefix("www.") == current.website and (
            data := play.site_data(website)
        ):
            stamps = play.stamps(data)
    changed = []
    check = monitor_store(projects).latest(project_id)
    if check is not None:
        for site in check.websites:
            if site.website.removeprefix("www.") == current.website:
                changed = list(site.changed_pages)
    report = build_report(
        current,
        previous=previous,
        theme_map=theme_store(projects).latest(project_id),
        demand=snapshot.demand if snapshot else [],
        stamps=stamps,
        gsc_clicks={page_key(r.page): r.clicks for r in gsc.pages if r.page} if gsc else {},
        changed=changed,
        now=datetime.now(UTC),
    )
    workbook = write_analytics_workbook(current, report, store.workbook(project_id))
    return report, {"workbook": str(workbook).removeprefix(_LONG_PREFIX)}


def set_property(
    projects: ProjectStore, project_id: str, property_id: str | None, website: str | None
) -> AnalyticsSettings:
    """Keep the project's GA4 property id and the website it measures."""
    store = analytics_store(projects)
    settings = store.settings(project_id)
    updated = AnalyticsSettings(
        property_id=property_id or settings.property_id,
        website=(website or settings.website or client_website(projects, project_id) or None),
    )
    return store.save_settings(project_id, updated)
