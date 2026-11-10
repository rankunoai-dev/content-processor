"""`project-report` subcommand and the subprocess that runs it for the web app.

    python -m src.modules.content_processor.cli project-report \
        --app-data app_data --project <id> [--include-competitors] [--built-by name]

A project report loads every page of all of the client's websites and compares
them pairwise, so it is built in its own process (like scrape batches and the
per-scrape `report` command): the web server's memory does not grow with the
largest project, and a build that runs away is stopped by a timeout instead of
taking the server with it. The child prints one JSON line on success
(`{"stamp": ..., "folder": ...}`); its stderr tail is written next to the output
by the parent (`<project>/reports/<stamp>.log`, or `failed-<started>.log`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.core.config import REPO_ROOT, get_settings
from src.modules.content_processor.projects.store import ProjectStore

__all__ = ["BuildOutcome", "Builder", "build_subprocess", "cmd_project_report", "register"]

STDERR_TAIL_CHARS = 8_000
"""How much of the child's stderr is kept (its end: the traceback is there)."""


@dataclass(frozen=True)
class BuildOutcome:
    """What the child process said."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """True when the bundle was written."""
        return self.returncode == 0 and not self.timed_out

    @property
    def stamp(self) -> str | None:
        """The bundle's stamp from the child's JSON line, when it printed one."""
        for line in reversed(self.stdout.strip().splitlines()):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("stamp"), str):
                return str(data["stamp"])
        return None

    @property
    def reason(self) -> str:
        """One line for the person who asked: the last line of stderr, or the timeout."""
        if self.timed_out:
            return "it took too long and was stopped"
        lines = [ln.strip() for ln in self.stderr.splitlines() if ln.strip()]
        if lines:
            return lines[-1][:300]
        return f"the build process ended with code {self.returncode}"


Builder = Callable[[Path, str, bool, str], BuildOutcome]
"""A build runner: (app data root, project id, include competitors, built by) -> outcome."""


def build_subprocess(
    app_data: Path,
    project_id: str,
    include_competitors: bool,
    built_by: str,
    *,
    timeout_s: float | None = None,
) -> BuildOutcome:
    """Run `cli project-report` in a child process, stopped after `timeout_s` seconds."""
    seconds = timeout_s if timeout_s is not None else get_settings().cp_project_report_timeout_s
    argv = [
        sys.executable,
        "-m",
        "src.modules.content_processor.cli",
        "project-report",
        "--app-data",
        str(app_data),
        "--project",
        project_id,
        "--built-by",
        built_by,
    ]
    if include_competitors:
        argv.append("--include-competitors")
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=seconds,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        return BuildOutcome(
            returncode=-1,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            timed_out=True,
        )
    return BuildOutcome(
        returncode=done.returncode, stdout=_text(done.stdout), stderr=_text(done.stderr)
    )


def _text(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    return text[-STDERR_TAIL_CHARS:]


def cmd_project_report(args: argparse.Namespace) -> int:
    """Build one project's report bundle (the child process of the web app, or by hand)."""
    from src.modules.content_processor.projects.project_reports import build_project_reports

    store = ProjectStore(Path(args.app_data))
    try:
        report = build_project_reports(
            store,
            args.project,
            include_competitors=bool(args.include_competitors),
            built_by=args.built_by or "",
        )
    except ValueError as exc:  # no finished scrape yet
        sys.stderr.write(f"{exc}\n")
        return 2
    except LookupError as exc:  # no such project (ProjectNotFoundError)
        sys.stderr.write(f"There is no project '{exc}'.\n")
        return 2
    sys.stdout.write(json.dumps({"stamp": report.stamp, "folder": str(report.folder)}) + "\n")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `project-report` subcommand."""
    p = sub.add_parser(
        "project-report",
        help="build a project's report bundle (all of the client's websites) from saved scrapes",
    )
    p.add_argument("--app-data", required=True, help="the app's data folder (projects live there)")
    p.add_argument("--project", required=True, help="project id")
    p.add_argument(
        "--include-competitors",
        action="store_true",
        help="add each competitor's own contradictions and duplicates",
    )
    p.add_argument("--built-by", default="", help="who asked (written to the manifest)")
    p.set_defaults(func=cmd_project_report)
