"""Scraping runs for projects: one shared queue, worked one scrape at a time.

Anyone may create projects and add any number of URLs, but the instance runs **one
scrape at a time**; every other scrape waits in one queue shared by all projects
and people, in the order it was queued. Each queued scrape says what happens when
its turn comes (`RunState.on_turn`):

* `auto`: it starts by itself;
* `notify`: the person is emailed and the scrape waits (status `ready`) without
  holding up the others; when started it runs next, before the waiting line. One
  not started within `ready_hours` is cancelled.

Either kind may also email the person when it finishes (`email_when_done`). The
address is kept in the run's folder (`notify.json`), never shown in full. A
scrape queued while the engine is free and nobody waits starts at once.

A run belongs to one website of a project and takes that website's URLs at the
moment it starts and works them in batches
(`BATCH_SIZE`). Each batch is one `cli dryrun` in its own process, so a browser
crash cannot take the app down. A batch that leaves pages unfinished is run again
for those pages only, up to `ATTEMPTS` times. The URL history is used as in every
run: pages already captured and unchanged are reused. When all batches are done,
the batch records are merged into `<run id>.records.jsonl` and the report bundle
is written to the run's `reports/` folder.

Then the website's knowledge space is built (`stamp`, `catalog`, `index` into the
namespace `<tenant>.<project>.<site>`), so the bot can search it on its own or
together with other websites. A failed knowledge step does not fail the run.

Runs survive a restart of the app: a queued or interrupted run is picked up again,
and batch attempts that already finished are not repeated.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from src.core.config import REPO_ROOT
from src.core.logger import get_logger
from src.modules.content_processor.knowledge.federated import space_namespace
from src.modules.content_processor.projects.models import (
    RUN_ACTIVE,
    OnTurn,
    ProjectRecord,
    RunState,
)
from src.modules.content_processor.projects.notify import Notifier, check_email, hide_email
from src.modules.content_processor.projects.store import ProjectNotFoundError, ProjectStore

__all__ = [
    "ATTEMPTS",
    "BATCH_SIZE",
    "FINISHED_BADGES",
    "SECONDS_PER_URL",
    "BatchCommand",
    "QueueEntry",
    "ScrapeRunner",
    "build_scope",
    "subprocess_command",
]

_logger = get_logger("content_processor.projects.runner")

BATCH_SIZE = 25
ATTEMPTS = 3
MAX_WAIT_S = 360
FINISHED_BADGES = frozenset(
    {"VERIFIED_WITHIN_SCOPE", "PARTIAL", "NEEDS_REVIEW", "BLOCKED", "FAILED", "EXCLUDED"}
)
DEFAULT_TENANT = "rankuno-dev"
SECONDS_PER_URL = 30.0  # wait estimates before any scrape has finished on this instance
READY_HOURS = 72.0

Key = tuple[str, str, str]  # (project id, site id, run id)

# One batch: (scope file, output json, cancel flag) -> process exit code.
BatchCommand = Callable[[Path, Path, threading.Event], int]
# Knowledge step: (merged records file, namespace) -> (built?, plain message).
KnowledgeStep = Callable[[Path, str], tuple[bool, str]]


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def build_scope(
    project: ProjectRecord, urls: Sequence[str], *, tenant_id: str = DEFAULT_TENANT
) -> dict[str, object]:
    """The run scope for a batch: public pages of the URLs' sites, free tiers only, $0."""
    hosts = sorted({_site(u) for u in urls if _site(u)})
    return {
        "tenant_id": tenant_id,
        "profile": "development",
        "mode": "SUPPLIED_URLS_PLUS_EMBEDDED",
        "input_urls": list(urls),
        "hosts_in_scope": hosts,
        "authorization": {
            "kind": "PUBLIC_THIRD_PARTY",
            "reference": f"project:{project.project_id}",
            "approved_by": project.owner_name,
            "approved_at": datetime.now(UTC).isoformat(),
            "hosts": hosts,
        },
        "access_tier_ceiling": "T3_FIDELITY_BROWSER",
        "locale": "en-US",
        "timezone": "America/New_York",
        "geography": "US",
        "budgets": {"cost_ceiling_usd": 0.0, "max_runtime_s": 7200},
    }


SKIP_FILE = "skip_fetch.json"


def subprocess_command(scope: Path, out: Path, cancel: threading.Event) -> int:
    """Run one batch with the scraper's command line; stop it when `cancel` is set.

    A `skip_fetch.json` next to the batch's scope (written by a recrawl plan) lists
    pages that are not due: they are carried forward without a request.
    """
    log = out.with_suffix(".log")
    skip = scope.parent / SKIP_FILE
    extra = ["--skip-fetch", str(skip)] if skip.is_file() else []
    with log.open("w", encoding="utf-8") as sink:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "src.modules.content_processor.cli",
                "dryrun",
                "--scope",
                str(scope),
                "--out",
                str(out),
                "--group-assets",
                "--max-wait",
                str(MAX_WAIT_S),
                "--no-reports",
                *extra,
            ],
            cwd=REPO_ROOT,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        while True:
            try:
                return process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if cancel.is_set():
                    process.kill()
                    process.wait()
                    return -1


def _records(path: Path) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                out.setdefault(str(record["requested_url"]), record)
    return out


@dataclass(frozen=True)
class QueueEntry:
    """One scrape in the shared queue: the run, its names, place in line, estimated wait."""

    state: RunState
    project_name: str
    site_name: str
    position: int
    wait_s: float


class ScrapeRunner:
    """One shared queue of scrapes, worked one at a time by a background thread."""

    def __init__(
        self,
        store: ProjectStore,
        *,
        tenant_id: str = DEFAULT_TENANT,
        command: BatchCommand = subprocess_command,
        report: Callable[[Path, Path], int] | None = None,
        knowledge: KnowledgeStep | None = None,
        batch_size: int = BATCH_SIZE,
        attempts: int = ATTEMPTS,
        notifier: Notifier | None = None,
        ready_hours: float = READY_HOURS,
    ) -> None:
        """Bind to a store; `command` runs one batch, `report` writes the bundle.

        `knowledge` builds a website's knowledge space after its reports; pass a
        no-op to switch that off. `notifier` sends the queue's emails (default: write
        them to `<store root>/outbox/`).
        """
        self._store = store
        self._tenant = tenant_id
        self._command = command
        self._report = report or _report_subprocess
        self._knowledge = knowledge or _knowledge_subprocess
        self._batch_size = batch_size
        self._attempts = attempts
        self._notifier = notifier or Notifier(outbox=store.root / "outbox")
        self._ready_for = timedelta(hours=ready_hours)
        self._pending: set[Key] = set()
        self._turned: list[RunState] = []
        self._current: Key | None = None
        self._cond = threading.Condition()
        self._cancel: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- queue ----------------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread and pick up runs left queued or running last time."""
        with self._lock:
            if self._thread is not None:
                return
            for state in sorted(self._store.all_runs(), key=lambda s: s.created_at):
                if state.status in ("queued", "running"):
                    if state.status == "running":
                        state = state.model_copy(
                            update={"status": "queued", "message": "Resumed after a restart."}
                        )
                        self._store.save_run(state)
                    self._enqueue(state)
            self._thread = threading.Thread(target=self._work, name="scrape-runner", daemon=True)
            self._thread.start()

    def _enqueue(self, state: RunState) -> None:
        with self._cond:
            self._cancel.setdefault(state.run_id, threading.Event())
            self._pending.add((state.project_id, state.site_id, state.run_id))
            self._cond.notify_all()

    def _idle(self) -> bool:
        with self._cond:
            return self._current is None and not self._pending

    def namespace(self, project_id: str, site_id: str) -> str:
        """The knowledge namespace of one website of one project."""
        return space_namespace(self._tenant, project_id, site_id)

    def submit(
        self,
        project_id: str,
        site_id: str,
        *,
        started_by: str,
        on_turn: OnTurn = "auto",
        email: str | None = None,
        email_when_done: bool = False,
        skip_fetch: Sequence[str] = (),
        skip_reason: str = "",
    ) -> RunState:
        """Queue a run over one website's current URLs.

        `skip_fetch` (from a recrawl plan) lists pages that are not due: they are
        carried forward from their last capture without a request.

        `on_turn` notify (email when its turn comes, then wait to be started) and
        `email_when_done` need an `email`.
        """
        address = check_email(email) if email and email.strip() else ""
        if (on_turn == "notify" or email_when_done) and not address:
            raise ValueError("Give an email address to be told by email.")
        if on_turn == "notify" and self._idle():
            on_turn = "auto"  # nothing to wait for: the person is here, start now
        site = self._store.site(project_id, site_id)
        urls = [u.url for u in self._store.urls(project_id, site_id)]
        if not urls:
            raise ValueError(f"Add at least one URL to {site.name} before starting.")
        if any(r.status in RUN_ACTIVE for r in self._store.runs(project_id, site_id)):
            raise ValueError(f"A scrape of {site.name} is already queued or running.")
        now = datetime.now(UTC)
        run_id = f"{site_id}_{now:%Y%m%d-%H%M%S}"
        # Two scrapes of one website started within the same second get distinct names.
        suffix = 1
        while self._store.run_dir(project_id, site_id, run_id).exists():
            suffix += 1
            run_id = f"{site_id}_{now:%Y%m%d-%H%M%S}-{suffix}"
        batches = (len(urls) + self._batch_size - 1) // self._batch_size
        state = RunState(
            run_id=run_id,
            project_id=project_id,
            site_id=site_id,
            created_at=now,
            started_by=started_by,
            url_count=len(urls),
            batch_size=self._batch_size,
            batches_total=batches,
            message="Starting." if self._idle() else "Waiting for its turn in the queue.",
            on_turn=on_turn,
            email_when_done=email_when_done and bool(address),
            notify_to=hide_email(address) if address else "",
        )
        folder = self._store.run_dir(project_id, site_id, run_id)
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "urls.json").write_text(json.dumps(urls), encoding="utf-8")
        if skip_fetch:
            (folder / SKIP_FILE).write_text(
                json.dumps({"urls": list(skip_fetch), "reason": skip_reason}), encoding="utf-8"
            )
        if address:
            (folder / "notify.json").write_text(json.dumps({"email": address}), encoding="utf-8")
        self._store.save_run(state)
        self._enqueue(state)
        return state

    def submit_all(
        self,
        project_id: str,
        *,
        started_by: str,
        on_turn: OnTurn = "auto",
        email: str | None = None,
        email_when_done: bool = False,
    ) -> list[RunState]:
        """Queue a run for every website that has URLs and is not being scraped already."""
        started: list[RunState] = []
        for site in self._store.sites(project_id):
            busy = any(r.status in RUN_ACTIVE for r in self._store.runs(project_id, site.site_id))
            if busy or not self._store.urls(project_id, site.site_id):
                continue
            started.append(
                self.submit(
                    project_id,
                    site.site_id,
                    started_by=started_by,
                    on_turn=on_turn,
                    email=email,
                    email_when_done=email_when_done,
                )
            )
        if not started:
            raise ValueError("No website has URLs waiting to be scraped.")
        return started

    def cancel(self, project_id: str, site_id: str, run_id: str) -> RunState:
        """Stop a queued or running run; finished batches are kept."""
        state = self._store.run(project_id, site_id, run_id)
        if state.status not in RUN_ACTIVE:
            return state
        event = self._cancel.get(run_id)
        if event is not None:
            event.set()
        if state.status in ("queued", "ready"):
            state = state.model_copy(
                update={
                    "status": "cancelled",
                    "finished_at": datetime.now(UTC),
                    "message": "Cancelled before it started.",
                }
            )
            self._store.save_run(state)
            with self._cond:
                self._pending.discard((project_id, site_id, run_id))
        return state

    def start_ready(self, project_id: str, site_id: str, run_id: str) -> RunState:
        """Start a scrape whose turn came: it runs next, before the waiting line."""
        state = self._store.run(project_id, site_id, run_id)
        if state.status != "ready":
            raise ValueError("Only a scrape whose turn has come can be started this way.")
        state = state.model_copy(
            update={
                "status": "queued",
                "jump": True,
                "message": "Starts as soon as the current scrape ends."
                if not self._idle()
                else "Starting.",
            }
        )
        self._store.save_run(state)
        self._enqueue(state)
        return state

    # -- the shared queue, as people see it ------------------------------------

    @property
    def ready_hours(self) -> float:
        """How long a scrape whose turn came waits to be started before it is cancelled."""
        return self._ready_for.total_seconds() / 3600

    @property
    def email_sends(self) -> bool:
        """True when queue emails are sent (an SMTP server is set), not only saved."""
        return self._notifier.mailer is not None

    def seconds_per_url(self) -> float:
        """Average scrape time per URL over the last 20 finished scrapes (for estimates)."""
        done = [
            r
            for r in self._store.all_runs()
            if r.status == "finished" and r.started_at and r.finished_at and r.url_count
        ]
        done = sorted(done, key=lambda r: r.finished_at or r.created_at)[-20:]
        urls = sum(r.url_count for r in done)
        if not urls:
            return SECONDS_PER_URL
        secs = 0.0
        for r in done:
            if r.finished_at is not None and r.started_at is not None:
                secs += (r.finished_at - r.started_at).total_seconds()
        return max(secs / urls, 1.0)

    def _order(self, states: list[RunState]) -> list[RunState]:
        """Queued scrapes in the order they run: started-after-their-turn first, then by time."""
        return sorted(
            (s for s in states if s.status == "queued"),
            key=lambda s: (not s.jump, s.turn_at or s.created_at, s.created_at),
        )

    def queue(self) -> tuple[list[QueueEntry], list[QueueEntry], list[QueueEntry]]:
        """(running, waiting in order with estimated waits, turn came and not started)."""
        states = [s for s in self._store.all_runs() if s.status in RUN_ACTIVE]
        rate = self.seconds_per_url()
        names: dict[tuple[str, str], tuple[str, str]] = {}

        def entry(state: RunState, position: int, wait_s: float) -> QueueEntry:
            key = (state.project_id, state.site_id)
            if key not in names:
                try:
                    project = self._store.get(state.project_id)
                    site = next((x.name for x in project.sites if x.site_id == state.site_id), "")
                    names[key] = (project.name, site or state.site_id)
                except ProjectNotFoundError:
                    names[key] = (state.project_id, state.site_id)
            return QueueEntry(state, *names[key], position=position, wait_s=wait_s)

        running = [entry(s, 0, 0.0) for s in states if s.status == "running"]
        ahead = sum(max(s.url_count - s.pages_done, 0) for s in states if s.status == "running")
        waiting = []
        for position, state in enumerate(self._order(states), 1):
            waiting.append(entry(state, position, ahead * rate))
            ahead += state.url_count
        ready = sorted(
            (entry(s, 0, 0.0) for s in states if s.status == "ready"),
            key=lambda e: e.state.turn_at or e.state.created_at,
        )
        return running, waiting, ready

    def wait_idle(self, timeout_s: float = 30.0) -> bool:
        """For tests: wait until nothing is waiting to run and the current run is done."""
        with self._cond:
            return self._cond.wait_for(
                lambda: self._current is None and not self._pending, timeout=timeout_s
            )

    def _next(self) -> Key | None:
        """The scrape to run now; a `notify` scrape whose turn comes is set aside first."""
        self._expire_ready()
        while self._pending:
            states: list[RunState] = []
            for key in list(self._pending):
                try:
                    state = self._store.run(*key)
                except ProjectNotFoundError:
                    state = None
                if state is None or state.status != "queued":
                    self._pending.discard(key)
                    continue
                states.append(state)
            order = self._order(states)
            if not order:
                return None
            head = order[0]
            key = (head.project_id, head.site_id, head.run_id)
            if head.on_turn == "notify" and not head.jump:
                self._pending.discard(key)
                ready = head.model_copy(
                    update={
                        "status": "ready",
                        "turn_at": datetime.now(UTC),
                        "message": "Its turn came: waiting to be started.",
                    }
                )
                self._store.save_run(ready)
                self._turned.append(ready)
                continue
            self._pending.discard(key)
            return key
        return None

    def _its_turn(self, state: RunState) -> None:
        """Email the person whose scrape is waiting for them (outside the queue lock)."""
        note = self._email(
            state,
            "turn",
            "Your scrape can start now",
            "It is your turn to scrape {site} (project {project}).\n\n"
            "Open {link} and press Start: it runs next, before the waiting line.\n"
            f"If it is not started within {self._ready_for.total_seconds() / 3600:.0f} hours "
            "it is cancelled and can be queued again.",
        )
        try:
            current = self._store.run(state.project_id, state.site_id, state.run_id)
        except ProjectNotFoundError:
            return
        if current.status == "ready":
            self._store.save_run(
                current.model_copy(
                    update={"message": f"Its turn came: waiting to be started ({note})."}
                )
            )

    def _expire_ready(self) -> None:
        now = datetime.now(UTC)
        for state in self._store.all_runs():
            if state.status == "ready" and state.turn_at and now - state.turn_at > self._ready_for:
                self._store.save_run(
                    state.model_copy(
                        update={
                            "status": "cancelled",
                            "finished_at": now,
                            "message": "Cancelled: it was not started after its turn came.",
                        }
                    )
                )

    def _email(self, state: RunState, kind: str, subject: str, body: str) -> str:
        """Send one queue email to the address of the run; returns what happened."""
        try:
            folder = self._store.run_dir(state.project_id, state.site_id, state.run_id)
            address = json.loads((folder / "notify.json").read_text(encoding="utf-8"))["email"]
            project = self._store.get(state.project_id)
        except (OSError, KeyError, ValueError, ProjectNotFoundError):
            return "no email address kept for this scrape"
        site = next((s.name for s in project.sites if s.site_id == state.site_id), state.site_id)
        text = body.format(
            site=site,
            project=project.name,
            link=self._notifier.link(state.project_id, state.site_id),
        )
        return self._notifier.send(
            address, f"{subject}: {site} ({project.name})", text, tag=f"{kind}_{state.run_id}"
        )

    def _done_email(self, project_id: str, site_id: str, run_id: str) -> None:
        try:
            state = self._store.run(project_id, site_id, run_id)
        except ProjectNotFoundError:
            return
        if not state.email_when_done or state.status in RUN_ACTIVE:
            return
        words = {
            "finished": "finished",
            "failed": "stopped on an error",
            "cancelled": "was cancelled",
        }
        note = self._email(
            state,
            "done",
            f"Your scrape {words.get(state.status, state.status)}",
            f"The scrape of {{site}} (project {{project}}) {words.get(state.status, state.status)}"
            f": {state.message}\n\nReports and results: {{link}}",
        )
        _logger.info("queue_done_email", extra={"run_id": run_id, "note": note})

    def _work(self) -> None:
        while True:
            with self._cond:
                key = self._next()
                self._current = key
                turned, self._turned = self._turned, []
            for state in turned:
                try:
                    self._its_turn(state)
                except Exception:  # noqa: BLE001 - an email never stops the queue
                    _logger.exception("queue_turn_email_failed", extra={"run_id": state.run_id})
            if key is None:
                with self._cond:
                    self._cond.wait_for(lambda: bool(self._pending), timeout=60)
                continue
            project_id, site_id, run_id = key
            try:
                self._execute(project_id, site_id, run_id)
            except Exception as exc:  # noqa: BLE001 - one failed run must not stop the queue
                _logger.exception("project_run_failed", extra={"run_id": run_id})
                try:
                    state = self._store.run(project_id, site_id, run_id)
                    self._store.save_run(
                        state.model_copy(
                            update={
                                "status": "failed",
                                "finished_at": datetime.now(UTC),
                                "message": f"The run stopped on an error: {str(exc)[:200]}",
                            }
                        )
                    )
                except Exception:  # noqa: BLE001 - the project may have been deleted
                    _logger.warning("project_run_state_lost", extra={"run_id": run_id})
            finally:
                try:
                    self._done_email(project_id, site_id, run_id)
                except Exception:  # noqa: BLE001 - an email never stops the queue
                    _logger.exception("queue_done_email_failed", extra={"run_id": run_id})
                with self._cond:
                    self._current = None
                    self._cond.notify_all()

    # -- one run --------------------------------------------------------------

    def _execute(self, project_id: str, site_id: str, run_id: str) -> None:
        state = self._store.run(project_id, site_id, run_id)
        if state.status != "queued":
            return
        cancel = self._cancel.setdefault(run_id, threading.Event())
        project = self._store.get(project_id)
        folder = self._store.run_dir(project_id, site_id, run_id)
        urls: list[str] = json.loads((folder / "urls.json").read_text(encoding="utf-8"))
        batches = [urls[i : i + state.batch_size] for i in range(0, len(urls), state.batch_size)]
        state = state.model_copy(
            update={
                "status": "running",
                "started_at": state.started_at or datetime.now(UTC),
                "message": "Scraping.",
            }
        )
        self._store.save_run(state)

        for number, batch in enumerate(batches, 1):
            for attempt in range(1, self._attempts + 1):
                if cancel.is_set():
                    self._finish(state, "cancelled", "Cancelled; finished batches are kept.")
                    return
                left = self._unfinished(folder, number, batch)
                if not left:
                    break
                out = folder / f"b{number}_a{attempt}.json"
                if out.with_suffix(".records.jsonl").exists():
                    continue  # this attempt already ran (resumed after a restart)
                state = state.model_copy(
                    update={
                        "current_batch": number,
                        "current_attempt": attempt,
                        "message": f"Batch {number} of {len(batches)}"
                        + (
                            f", try {attempt} for {len(left)} unfinished pages"
                            if attempt > 1
                            else ""
                        ),
                    }
                )
                self._store.save_run(state)
                scope = folder / f"b{number}_a{attempt}_scope.json"
                scope.write_text(
                    json.dumps(build_scope(project, left, tenant_id=self._tenant), indent=1),
                    encoding="utf-8",
                )
                code = self._command(scope, out, cancel)
                if cancel.is_set():
                    self._finish(state, "cancelled", "Cancelled; finished batches are kept.")
                    return
                _logger.info(
                    "project_batch_done",
                    extra={"run_id": run_id, "batch": number, "attempt": attempt, "code": code},
                )
                state = self._progress(state, folder, batches, done_through=number - 1)
            state = self._progress(state, folder, batches, done_through=number)

        merged = self._merge(folder, run_id, batches)
        if not merged.read_text(encoding="utf-8").strip():
            logs = sorted(p.name for p in folder.glob("b*_a*.log"))
            self._finish(
                state.model_copy(update={"current_batch": None}),
                "failed",
                "No page could be scraped: every batch stopped on an error"
                + (f" (details in {logs[-1]} in the run's folder)." if logs else "."),
            )
            return
        state = state.model_copy(update={"message": "Writing the reports."})
        self._store.save_run(state)
        code = self._report(merged, folder / "reports")
        ready = code == 0 and any((folder / "reports").glob("*"))
        state = state.model_copy(update={"reports_ready": ready, "current_batch": None})
        if not ready:
            self._finish(state, "failed", "Scraping finished but the reports could not be written.")
            return
        state = state.model_copy(
            update={
                "message": "Building the knowledge space for the bot.",
                "knowledge_status": "building",
            }
        )
        self._store.save_run(state)
        try:
            built, note = self._knowledge(merged, self.namespace(project_id, site_id))
        except Exception as exc:  # noqa: BLE001 - the reports stand even if this step breaks
            built, note = False, f"stopped on an error: {str(exc)[:200]}"
        state = state.model_copy(
            update={"knowledge_status": "ready" if built else "failed", "knowledge_message": note}
        )
        self._finish(state, "finished", "Done.")

    def _finish(self, state: RunState, status: str, message: str) -> None:
        self._store.save_run(
            state.model_copy(
                update={
                    "status": status,
                    "finished_at": datetime.now(UTC),
                    "message": message,
                    "current_attempt": None,
                }
            )
        )

    def _batch_records(self, folder: Path, number: int) -> dict[str, dict[str, object]]:
        """Best record per URL across the batch's attempts (a finished one wins)."""
        best: dict[str, dict[str, object]] = {}
        for attempt in range(1, self._attempts + 1):
            for url, record in _records(folder / f"b{number}_a{attempt}.records.jsonl").items():
                if url not in best or (
                    best[url].get("badge") not in FINISHED_BADGES
                    and record.get("badge") in FINISHED_BADGES
                ):
                    best[url] = record
        return best

    def _unfinished(self, folder: Path, number: int, batch: Sequence[str]) -> list[str]:
        got = self._batch_records(folder, number)
        return [u for u in batch if got.get(u, {}).get("badge") not in FINISHED_BADGES]

    def _progress(
        self, state: RunState, folder: Path, batches: Sequence[Sequence[str]], *, done_through: int
    ) -> RunState:
        results: Counter[str] = Counter()
        for number, batch in enumerate(batches, 1):
            got = self._batch_records(folder, number)
            for url in batch:
                badge = got.get(url, {}).get("badge")
                if badge in FINISHED_BADGES:
                    results[str(badge)] += 1
        state = state.model_copy(
            update={
                "batches_done": done_through,
                "pages_done": sum(results.values()),
                "results": dict(results),
            }
        )
        self._store.save_run(state)
        return state

    def _merge(self, folder: Path, run_id: str, batches: Sequence[Sequence[str]]) -> Path:
        """One records file: each supplied URL's best record, then the pages found on them."""
        supplied: dict[str, dict[str, object]] = {}
        found: dict[str, dict[str, object]] = {}
        seeds = {u for batch in batches for u in batch}
        for number in range(1, len(batches) + 1):
            for url, record in self._batch_records(folder, number).items():
                if url in seeds:
                    supplied[url] = record
            for attempt in range(1, self._attempts + 1):
                for url, record in _records(folder / f"b{number}_a{attempt}.records.jsonl").items():
                    if url not in seeds:
                        found.setdefault(url, record)
        path = folder / f"{run_id}.records.jsonl"
        lines = [json.dumps(r, ensure_ascii=False) for r in [*supplied.values(), *found.values()]]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path


def _cli(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "src.modules.content_processor.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )


def _knowledge_subprocess(records: Path, namespace: str) -> tuple[bool, str]:
    """Stamp and chunk the pages, build the offering catalog, index into the namespace."""
    run_name = records.name.removesuffix(".records.jsonl")
    folder = records.parent
    steps = [
        ("stamp", ["stamp", "--records", str(records), "--tenant", DEFAULT_TENANT]),
        ("catalog", ["catalog", "--stamps", str(folder / f"{run_name}.stamps.jsonl")]),
        ("index", ["index", "--run", run_name, "--dir", str(folder), "--tenant", namespace]),
    ]
    for name, argv in steps:
        done = _cli(*argv)
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            return False, f"the {name} step failed: {tail[0][:200]}"
    return True, f"searchable by the bot (knowledge space {namespace})"


def _report_subprocess(records: Path, out_dir: Path) -> int:
    """Write the report bundle for merged records (its own process, like the batches)."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "src.modules.content_processor.cli",
            "report",
            "--records",
            str(records),
            "--out",
            str(out_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    ).returncode
