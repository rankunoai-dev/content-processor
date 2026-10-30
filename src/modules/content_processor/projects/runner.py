"""Scraping runs for projects: a queue worked by one background thread.

A run takes the project's URLs at the moment it starts and works them in batches
(`BATCH_SIZE`). Each batch is one `cli dryrun` in its own process, so a browser
crash cannot take the app down. A batch that leaves pages unfinished is run again
for those pages only, up to `ATTEMPTS` times. The URL history is used as in every
run: pages already captured and unchanged are reused. When all batches are done,
the batch records are merged into `<run id>.records.jsonl` and the report bundle
is written to the run's `reports/` folder.

Runs survive a restart of the app: a queued or interrupted run is picked up again,
and batch attempts that already finished are not repeated.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from src.core.config import REPO_ROOT
from src.core.logger import get_logger
from src.modules.content_processor.projects.models import RUN_ACTIVE, ProjectRecord, RunState
from src.modules.content_processor.projects.store import ProjectStore, slug

__all__ = [
    "ATTEMPTS",
    "BATCH_SIZE",
    "FINISHED_BADGES",
    "BatchCommand",
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

# One batch: (scope file, output json, cancel flag) -> process exit code.
BatchCommand = Callable[[Path, Path, threading.Event], int]


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


def subprocess_command(scope: Path, out: Path, cancel: threading.Event) -> int:
    """Run one batch with the scraper's command line; stop it when `cancel` is set."""
    log = out.with_suffix(".log")
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


class ScrapeRunner:
    """Queues runs and works them one at a time in a background thread."""

    def __init__(
        self,
        store: ProjectStore,
        *,
        tenant_id: str = DEFAULT_TENANT,
        command: BatchCommand = subprocess_command,
        report: Callable[[Path, Path], int] | None = None,
        batch_size: int = BATCH_SIZE,
        attempts: int = ATTEMPTS,
    ) -> None:
        """Bind to a store; `command` runs one batch, `report` writes the bundle."""
        self._store = store
        self._tenant = tenant_id
        self._command = command
        self._report = report or _report_subprocess
        self._batch_size = batch_size
        self._attempts = attempts
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
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
                if state.status in RUN_ACTIVE:
                    if state.status == "running":
                        state = state.model_copy(
                            update={"status": "queued", "message": "Resumed after a restart."}
                        )
                        self._store.save_run(state)
                    self._enqueue(state)
            self._thread = threading.Thread(target=self._work, name="scrape-runner", daemon=True)
            self._thread.start()

    def _enqueue(self, state: RunState) -> None:
        self._cancel[state.run_id] = threading.Event()
        self._queue.put((state.project_id, state.run_id))

    def submit(self, project_id: str, *, started_by: str) -> RunState:
        """Queue a run over the project's current URLs."""
        project = self._store.get(project_id)
        urls = [u.url for u in self._store.urls(project_id)]
        if not urls:
            raise ValueError("Add at least one URL before starting.")
        if any(r.status in RUN_ACTIVE for r in self._store.runs(project_id)):
            raise ValueError("A run of this project is already queued or running.")
        now = datetime.now(UTC)
        run_id = f"{slug(project.name, limit=20)}_{now:%Y%m%d-%H%M%S}"
        batches = (len(urls) + self._batch_size - 1) // self._batch_size
        state = RunState(
            run_id=run_id,
            project_id=project_id,
            created_at=now,
            started_by=started_by,
            url_count=len(urls),
            batch_size=self._batch_size,
            batches_total=batches,
            message="Waiting for its turn." if not self._queue.empty() else "Starting.",
        )
        folder = self._store.run_dir(project_id, run_id)
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "urls.json").write_text(json.dumps(urls), encoding="utf-8")
        self._store.save_run(state)
        self._enqueue(state)
        return state

    def cancel(self, project_id: str, run_id: str) -> RunState:
        """Stop a queued or running run; finished batches are kept."""
        state = self._store.run(project_id, run_id)
        if state.status not in RUN_ACTIVE:
            return state
        event = self._cancel.get(run_id)
        if event is not None:
            event.set()
        if state.status == "queued":
            state = state.model_copy(
                update={
                    "status": "cancelled",
                    "finished_at": datetime.now(UTC),
                    "message": "Cancelled before it started.",
                }
            )
            self._store.save_run(state)
        return state

    def wait_idle(self, timeout_s: float = 30.0) -> bool:
        """For tests: wait until the queue is empty and the current run is done."""
        done = threading.Event()

        def drained() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=drained, daemon=True).start()
        return done.wait(timeout_s)

    def _work(self) -> None:
        while True:
            project_id, run_id = self._queue.get()
            try:
                self._execute(project_id, run_id)
            except Exception as exc:  # noqa: BLE001 - one failed run must not stop the queue
                _logger.exception("project_run_failed", extra={"run_id": run_id})
                try:
                    state = self._store.run(project_id, run_id)
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
                self._queue.task_done()

    # -- one run --------------------------------------------------------------

    def _execute(self, project_id: str, run_id: str) -> None:
        state = self._store.run(project_id, run_id)
        if state.status != "queued":
            return
        cancel = self._cancel.setdefault(run_id, threading.Event())
        project = self._store.get(project_id)
        folder = self._store.run_dir(project_id, run_id)
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
        self._finish(
            state.model_copy(update={"reports_ready": ready, "current_batch": None}),
            "finished" if ready else "failed",
            "Done." if ready else "Scraping finished but the reports could not be written.",
        )

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
