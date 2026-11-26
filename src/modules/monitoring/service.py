"""Run and save checks (CLI, API, cards), and the background watcher of the app server.

The watcher wakes up every few minutes and, for every project:

- **auto-check** (on by default, local only): when a website of the project finished
  a new scrape since the last check, it runs a check. Nothing is fetched.
- **scheduled re-scrape** (off by default): when `rescrape_every_days` is set and the
  project's last scrape is older than that, it queues a scrape of every website.
  That contacts the client's and competitors' websites, so it is an opt-in per
  project, set by someone who unlocked the project. With `smart_recrawl` (the
  default) the pass follows a recrawl plan (`recrawl.py`): core pages and pages
  the sitemap shows changed are checked, the others only once a month; pages not
  due are carried forward without a request. The plan is saved with the pass.

Every check also reads the project's newest research and backlink snapshots (pulled
or imported in their modules; nothing is pulled here) against the ones the previous
check read, and emails a digest of its new alerts when the project asked for one.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, TypeVar

import httpx

from src.core.config import get_settings
from src.core.logger import get_logger
from src.modules.analyst.playground import Playground
from src.modules.analyst.policy import DataPolicy
from src.modules.content_processor.analysis.semantic import VectorCache
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.notify import Notifier, notifier_from_settings
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.monitoring.check import _alert, run_check
from src.modules.monitoring.digest import send_digest
from src.modules.monitoring.models import Alert, MonitorCheck, RecrawlPlan, SiteDiff
from src.modules.monitoring.store import MonitorStore, write_check_workbook

__all__ = ["MonitorWatcher", "monitor_store", "recrawl_project", "run_project_check"]

_logger = get_logger("rankuno.monitoring")
WAKE_S = 300.0
_ACTIVE = {"queued", "running"}
_EARLIER_CHECKS = 12
_T = TypeVar("_T", covariant=True)


def monitor_store(projects: ProjectStore) -> MonitorStore:
    """The monitoring store next to the projects (`<app data>/monitoring`)."""
    return MonitorStore(Path(str(projects.root)) / "monitoring")


class _Snapshots(Protocol[_T]):
    def latest(self, project_id: str) -> _T | None: ...
    def get(self, project_id: str, snapshot_id: str) -> _T: ...
    def history(self, project_id: str) -> list[str]: ...


def _snapshot_pair(
    store: _Snapshots[_T], project_id: str, used_before: str | None
) -> tuple[_T | None, _T | None]:
    """(the snapshot the previous check read, else the one before the newest; the newest)."""
    try:
        latest = store.latest(project_id)
    except (KeyError, ValueError):
        return None, None
    if latest is None:
        return None, None
    if used_before:
        try:
            return store.get(project_id, used_before), latest
        except (KeyError, ValueError):
            pass  # deleted since: fall back to the snapshot before the newest
    history = store.history(project_id)
    if len(history) < 2:
        return None, latest
    try:
        return store.get(project_id, history[1]), latest
    except (KeyError, ValueError):
        return None, latest


def _earlier_diffs(store: MonitorStore, project_id: str, since: datetime | None) -> list[SiteDiff]:
    """The client websites' comparisons of recent checks (for matching ranking drops)."""
    if since is None:
        return []
    out: list[SiteDiff] = []
    for check_id in store.history(project_id)[:_EARLIER_CHECKS]:
        try:
            check = store.get(project_id, check_id)
        except (KeyError, ValueError):
            continue
        if check.created_at < since:
            break
        out += [
            d for d in check.websites if d.role == "client" and d.changed_pages + d.removed_pages
        ]
    return out


def _deliver_digest(
    projects: ProjectStore, store: MonitorStore, check: MonitorCheck, notifier: Notifier | None
) -> None:
    """Email the check's new alerts when the project asked for it (never fails the check)."""
    project_id = check.project_id
    settings = store.watch(project_id)
    book = store.digest(project_id)
    if settings.digest_level == "off" or not book.to:
        return
    try:
        record = projects.get(project_id)
        confidential = (
            DataPolicy(Path(str(projects.root)) / "analyst", projects).data_class(project_id)
            == "client_confidential"
        )
        book, note = send_digest(
            check,
            settings.digest_level,
            book,
            notifier or notifier_from_settings(Path(str(projects.root))),
            project_name=record.name,
            confidential=confidential,
        )
    except Exception as exc:  # noqa: BLE001 - a digest problem never loses the check
        _logger.warning("monitor_digest_failed", extra={"project": project_id, "error": str(exc)})
        note = f"the digest could not be prepared ({type(exc).__name__})"
    if not note:
        return
    store.set_digest(project_id, book)
    store.set_watch(
        project_id,
        settings.model_copy(update={"last_digest_at": datetime.now(UTC), "last_digest_note": note}),
    )


_DECAY_ALERTS = 10


def analytics_alerts(projects: ProjectStore, project_id: str) -> list[Alert]:
    """GA4 decays of the client's pages as alerts.

    Organic sessions or conversions down against the period before: rankings can look flat
    while visits fall, or a form can stop working.
    """
    from src.modules.analytics.insights import build_report
    from src.modules.analytics.service import analytics_store

    store = analytics_store(projects)
    current = store.latest(project_id)
    previous = store.previous(project_id)
    if current is None or previous is None:
        return []
    report = build_report(current, previous=previous, now=datetime.now(UTC))
    out: list[Alert] = []
    for d in report.decays[:_DECAY_ALERTS]:
        conversions = d.metric == "key_events"
        drop = abs(d.change)
        out.append(
            _alert(
                "client_conversion_drop" if conversions else "client_traffic_decay",
                "high" if drop >= 0.5 else "medium",
                current.website,
                f"{'Conversions' if conversions else 'Organic visits'} down {drop:.0%} on {d.url}",
                f"GA4: {d.before:,.0f} to {d.after:,.0f} "
                f"{'key events' if conversions else d.metric.replace('_', ' ')} against the "
                f"period before ({report.period or 'latest import'}). Check rankings and the "
                "search results for the same dates; if they held, test the page's form and "
                "call to action.",
                urls=[d.url],
            )
        )
    return out


def run_project_check(
    projects: ProjectStore,
    access: KnowledgeAccess,
    project_id: str,
    *,
    trigger: Literal["manual", "auto"] = "manual",
    notifier: Notifier | None = None,
) -> tuple[MonitorCheck, dict[str, str]]:
    """Run a check, save it and its workbook, send the digest if asked; returns both."""
    from src.modules.backlinks.service import backlinks_store
    from src.modules.research.service import research_store

    store = monitor_store(projects)
    play = Playground(projects, access, [project_id])
    settings = store.watch(project_id)
    previous = store.latest(project_id)
    signals = previous.signals if previous is not None else None
    research = _snapshot_pair(
        research_store(projects), project_id, signals.research_after if signals else None
    )
    backlinks = _snapshot_pair(
        backlinks_store(projects), project_id, signals.backlinks_after if signals else None
    )
    since = research[0].created_at if research[0] is not None else None
    app = get_settings()
    # line vectors for meaning scores, shared by every project's checks (keyed by model)
    cache = (
        VectorCache(Path(str(projects.root)) / "monitoring" / "meaning.sqlite")
        if app.cp_monitor_meaning
        else None
    )
    try:
        check = run_check(
            play,
            project_id,
            previous,
            push_threshold=settings.push_threshold,
            trigger=trigger,
            meaning_cache=cache,
            topic_drift=app.cp_monitor_topic_drift,
            research=research,
            backlinks=backlinks,
            earlier_diffs=_earlier_diffs(store, project_id, since),
            rank_min_volume=settings.rank_min_volume,
        )
    finally:
        if cache is not None:
            cache.close()
    extra = analytics_alerts(projects, project_id)
    if extra:
        check = check.model_copy(update={"alerts": [*check.alerts, *extra]})
    path = store.save(check)
    workbook = write_check_workbook(check, store.workbook(check))
    _deliver_digest(projects, store, check, notifier)
    prefix = "\\\\?\\"  # Windows extended-length path prefix, not shown to people
    return check, {
        k: str(v).removeprefix(prefix) for k, v in {"check": path, "workbook": workbook}.items()
    }


def recrawl_project(
    projects: ProjectStore,
    runner: ScrapeRunner | None,
    project_id: str,
    *,
    trigger: str = "preview",
    http: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
) -> RecrawlPlan:
    """Plan a recrawl and (unless a preview) queue a scrape per website with its skips."""
    from src.modules.lite_scan.service import latest_lite_run, start_lite_scan
    from src.modules.monitoring.recrawl import plan_project
    from src.modules.theming.service import theme_store

    store = monitor_store(projects)
    settings = store.watch(project_id)
    plan = plan_project(
        projects,
        project_id,
        settings,
        theme_map=theme_store(projects).latest(project_id),
        http=http,
        now=now,
        trigger=trigger,
    )
    if trigger != "preview" and runner is not None:
        sites = []
        for site in plan.sites:
            if latest_lite_run(projects.runs(project_id, site.site_id)) is not None:
                # a website followed by lite scans is refreshed with another lite scan
                try:
                    found, state = start_lite_scan(
                        projects,
                        runner,
                        project_id,
                        site.site_id,
                        started_by=f"{trigger} recrawl",
                        http=http,
                    )
                except ValueError as exc:
                    sites.append(site.model_copy(update={"note": str(exc)}))
                    continue
                sites.append(
                    site.model_copy(
                        update={
                            "run_id": state.run_id,
                            "note": f"lite scan repeated: {found.basis()}",
                        }
                    )
                )
                continue
            if site.check == 0:
                sites.append(site.model_copy(update={"note": "nothing due: no scrape queued"}))
                continue
            skips = [p.url for p in site.pages if p.action == "skip"]
            try:
                state = runner.submit(
                    project_id,
                    site.site_id,
                    started_by=f"{trigger} recrawl",
                    skip_fetch=skips,
                    skip_reason="not due in the recrawl plan",
                )
            except ValueError as exc:  # already being scraped, or no addresses
                sites.append(site.model_copy(update={"note": str(exc)}))
                continue
            sites.append(site.model_copy(update={"run_id": state.run_id}))
        plan = plan.model_copy(update={"sites": sites})
    store.save_plan(plan)
    return plan


def _finished(runs: list[RunState]) -> datetime | None:
    done = [
        r.finished_at for r in runs if r.finished_at is not None and r.knowledge_status == "ready"
    ]
    return max(done) if done else None


class MonitorWatcher:
    """The background watcher (one per app server)."""

    def __init__(
        self,
        projects: ProjectStore,
        access: KnowledgeAccess,
        runner: ScrapeRunner | None,
        *,
        wake_s: float = WAKE_S,
        http: Callable[[], httpx.Client] | None = None,
    ) -> None:
        """Bind to the projects; `runner` queues scheduled re-scrapes (None: never)."""
        self.projects = projects
        self.access = access
        self.runner = runner
        self.store = monitor_store(projects)
        self.wake_s = wake_s
        self.http = http  # sitemap reads of smart recrawls (tests pass a fake)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background thread."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="monitor-watcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop after the current pass."""
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the watcher must survive one bad project
                _logger.exception("monitor_tick_failed")
            self._stop.wait(self.wake_s)

    def tick(self, now: datetime | None = None) -> dict[str, list[str]]:
        """One pass over every project; returns what it did (for logs and tests)."""
        moment = now or datetime.now(UTC)
        done: dict[str, list[str]] = {"checked": [], "rescraped": []}
        for project in self.projects.projects():
            pid = project.project_id
            settings = self.store.watch(pid)
            runs = self.projects.runs(pid)
            if any(r.status in _ACTIVE for r in runs):
                continue
            last_scrape = _finished(runs)
            latest = self.store.latest(pid)
            if (
                settings.auto_check
                and last_scrape is not None
                and (latest is None or last_scrape > latest.created_at)
            ):
                run_project_check(self.projects, self.access, pid, trigger="auto")
                # read again: the check may have saved its digest note meanwhile
                self.store.set_watch(
                    pid, self.store.watch(pid).model_copy(update={"last_auto_check_at": moment})
                )
                done["checked"].append(pid)
                settings = self.store.watch(pid)
            if (
                self.runner is not None
                and settings.rescrape_every_days > 0
                and last_scrape is not None
                and moment - last_scrape >= timedelta(days=settings.rescrape_every_days)
                and (
                    settings.last_rescrape_at is None
                    or moment - settings.last_rescrape_at
                    >= timedelta(days=settings.rescrape_every_days)
                )
            ):
                if settings.smart_recrawl:
                    recrawl_project(
                        self.projects,
                        self.runner,
                        pid,
                        trigger="scheduled",
                        http=self.http,
                        now=moment,
                    )
                else:
                    try:
                        self.runner.submit_all(pid, started_by="scheduled monitoring")
                    except ValueError:  # no website has addresses to scrape
                        continue
                self.store.set_watch(
                    pid, self.store.watch(pid).model_copy(update={"last_rescrape_at": moment})
                )
                done["rescraped"].append(pid)
        return done
