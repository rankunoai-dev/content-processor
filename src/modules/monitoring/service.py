"""Run and save checks (CLI, API, cards), and the background watcher of the app server.

The watcher wakes up every few minutes and, for every project:

- **auto-check** (on by default, local only): when a website of the project finished
  a new scrape since the last check, it runs a check. Nothing is fetched.
- **scheduled re-scrape** (off by default): when `rescrape_every_days` is set and the
  project's last scrape is older than that, it queues a scrape of every website.
  That contacts the client's and competitors' websites, so it is an opt-in per
  project, set by someone who unlocked the project.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from src.core.logger import get_logger
from src.modules.analyst.playground import Playground
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess
from src.modules.monitoring.check import run_check
from src.modules.monitoring.models import MonitorCheck
from src.modules.monitoring.store import MonitorStore, write_check_workbook

__all__ = ["MonitorWatcher", "monitor_store", "run_project_check"]

_logger = get_logger("rankuno.monitoring")
WAKE_S = 300.0
_ACTIVE = {"queued", "running"}


def monitor_store(projects: ProjectStore) -> MonitorStore:
    """The monitoring store next to the projects (`<app data>/monitoring`)."""
    return MonitorStore(Path(str(projects.root)) / "monitoring")


def run_project_check(
    projects: ProjectStore,
    access: KnowledgeAccess,
    project_id: str,
    *,
    trigger: Literal["manual", "auto"] = "manual",
) -> tuple[MonitorCheck, dict[str, str]]:
    """Run a check, save it and its workbook; returns both."""
    store = monitor_store(projects)
    play = Playground(projects, access, [project_id])
    settings = store.watch(project_id)
    check = run_check(
        play,
        project_id,
        store.latest(project_id),
        push_threshold=settings.push_threshold,
        trigger=trigger,
    )
    path = store.save(check)
    workbook = write_check_workbook(check, store.workbook(check))
    prefix = "\\\\?\\"  # Windows extended-length path prefix, not shown to people
    return check, {
        k: str(v).removeprefix(prefix) for k, v in {"check": path, "workbook": workbook}.items()
    }


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
    ) -> None:
        """Bind to the projects; `runner` queues scheduled re-scrapes (None: never)."""
        self.projects = projects
        self.access = access
        self.runner = runner
        self.store = monitor_store(projects)
        self.wake_s = wake_s
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
                self.store.set_watch(
                    pid, settings.model_copy(update={"last_auto_check_at": moment})
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
                try:
                    self.runner.submit_all(pid, started_by="scheduled monitoring")
                except ValueError:  # no website has addresses to scrape
                    continue
                self.store.set_watch(pid, settings.model_copy(update={"last_rescrape_at": moment}))
                done["rescraped"].append(pid)
        return done
