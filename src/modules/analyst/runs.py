"""Resolve a card's inputs, check it can run, build its prompt, run it, keep the record.

Two backends:

- `pack` ($0, no model): runs the card's prefetch tools and writes a **context
  pack**: the full analyst prompt, the task and the retrieved data, with the
  citation rules. A person can give it to Claude in a Claude app (subject to the
  data policy), and the pack works as a reproducible record of what was asked.
- `claude`: the same prompt and prefetched data go to Claude through the API,
  which may call the card's tools for more (`agent.py`), within the budget.
  The deliverable is then checked by code (`verify.py`) and its sources appended.

Every run keeps a folder `<analyst root>/runs/<run id>/`: `run.json` (inputs,
card version, backend, model, usage, cost, verification), `prompt.md`,
`deliverable.md` or `context_pack.md`, `ledger.json` and `events.jsonl`.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.modules.analyst.library import ANALYST_RULES
from src.modules.analyst.models import ActionCard, Backend, Preflight, RunRecord
from src.modules.analyst.playground import Playground, ScopeError
from src.modules.analyst.policy import Budget, DataPolicy, SpendLog
from src.modules.analyst.tools import TOOLS, anthropic_tools, run_tool
from src.modules.analyst.verify import sources_section, verify_deliverable
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.scope.normalize import normalize_url
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

__all__ = [
    "AnalystSettings",
    "CardRunner",
    "RunStore",
    "compile_prompt",
    "render",
    "resolve_inputs",
]

_SLOT = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_TEXT = 300


@dataclass(frozen=True)
class AnalystSettings:
    """The Claude switches and caps (from `.env`), passed in so tests can set them."""

    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: str = "high"
    run_cap_usd: float = 2.0
    daily_cap_usd: float = 10.0
    monthly_cap_usd: float = 0.0
    max_tool_calls: int = 40
    fallbacks: bool = True

    @classmethod
    def from_env(cls) -> AnalystSettings:
        """Read the `CP_CLAUDE_*` settings."""
        from src.core.config import get_settings

        s = get_settings()
        return cls(
            enabled=s.cp_claude_enabled,
            model=s.cp_claude_model,
            effort=s.cp_claude_effort,
            run_cap_usd=s.cp_claude_run_cap_usd,
            daily_cap_usd=s.cp_claude_daily_cap_usd,
            monthly_cap_usd=s.cp_claude_monthly_cap_usd,
            max_tool_calls=s.cp_claude_max_tool_calls,
        )


# -- inputs -----------------------------------------------------------------------------


def _clean_text(value: str) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())[:_MAX_TEXT]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _project_sites(play: Playground, project_id: str) -> tuple[str | None, list[str]]:
    """The project's main client website key and its competitor keys.

    The client's other websites or brands are neither: they are never competitors.
    """
    project = play.store.get(project_id)
    client: str | None = None
    competitors: list[str] = []
    for site in project.sites:
        if not site.domains:
            continue
        website = play.graph.find(site.domains[0])
        if website is None:
            continue
        if site.role == "client":
            client = client or website.key
        else:
            competitors.append(website.key)
    return client, competitors


def resolve_inputs(
    card: ActionCard, raw: dict[str, Any], play_for: Callable[[list[str]], Playground]
) -> tuple[dict[str, Any], Playground | None, list[str]]:
    """Typed, checked inputs; the run's playground; and the problems found.

    Websites become their keys (domains), addresses are normalised and must belong
    to a website in scope, text is cleaned (no control characters, at most 300).
    Defaults `client` and `competitors` come from the project.
    """
    problems: list[str] = []
    project_params = [p for p in card.parameters if p.kind == "project"]
    project_ids = [str(raw.get(p.name) or "").strip() for p in project_params]
    if not project_ids or not all(project_ids):
        return {}, None, ["choose a project"]
    try:
        play = play_for(project_ids)
    except ScopeError as exc:
        return {}, None, [str(exc)]
    client, competitors = _project_sites(play, project_ids[0])
    values: dict[str, Any] = {}
    for param in card.parameters:
        if param.kind == "project":
            values[param.name] = str(raw.get(param.name)).strip()
            continue
        given = raw.get(param.name)
        if given in (None, "", []):
            if param.default == "client":
                given = client
            elif param.default == "competitors":
                given = competitors
            else:
                given = param.default
        if given in (None, "", []):
            if param.required:
                problems.append(f"{param.label} is required")
            values[param.name] = [] if param.many else None
            continue
        items = _as_list(given) if param.many else [str(given)]
        resolved: list[str] = []
        before = len(problems)
        for item in items:
            try:
                if param.kind == "website":
                    resolved.append(play.website(item).key)
                elif param.kind == "url":
                    url = normalize_url(item.strip()).schedule_key
                    play.website_of_url(url)
                    resolved.append(url)
                elif param.kind == "choice":
                    if item not in param.choices:
                        problems.append(f"{param.label} must be one of {', '.join(param.choices)}")
                        continue
                    resolved.append(item)
                else:
                    text = _clean_text(item)
                    if not text:
                        problems.append(f"{param.label} is empty")
                        continue
                    resolved.append(text)
            except (ScopeError, ValueError) as exc:
                problems.append(f"{param.label}: {exc}")
        values[param.name] = resolved if param.many else (resolved[0] if resolved else None)
        if param.required and not resolved and len(problems) == before:
            problems.append(f"{param.label} is required")
    websites = [
        v for p in card.parameters if p.kind == "website" for v in _as_list(values.get(p.name))
    ]
    if len(websites) != len(set(websites)):
        problems.append("the same website is chosen twice (client and competitor?)")
    return values, play, problems


# -- prompt -------------------------------------------------------------------------------


def _show(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(value) if value else "(none)"
    return "(none)" if value in (None, "") else str(value)


def render(template: str, values: dict[str, Any]) -> str:
    """Fill `{{name}}` slots; an unknown slot raises `KeyError` (a card bug, never silent)."""

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(name)
        return _show(values[name])

    return _SLOT.sub(fill, template)


def _format(card: ActionCard, values: dict[str, Any]) -> str:
    out = card.output_format
    for name, value in values.items():
        out = out.replace("{" + name + "}", _show(value))
    return out


def compile_prompt(card: ActionCard, values: dict[str, Any], tools: list[str]) -> tuple[str, str]:
    """(system prompt, task). The system prompt is stable per card (cache-friendly)."""
    system = "\n\n".join(
        [
            card.persona,
            ANALYST_RULES,
            "Deliverable format (follow it exactly):\n" + card.output_format,
            "Tools you may call: " + ", ".join(sorted(tools)) + ".",
        ]
    )
    task = "\n\n".join(
        [
            f"Action card: {card.title} (v{card.version}). Deliverable: {card.deliverable}.",
            render(card.prompt_template, values),
            "Inputs:\n" + json.dumps(values, indent=1, ensure_ascii=False),
            "Use this format, with the inputs filled in:\n" + _format(card, values),
            f"Today is {datetime.now(UTC).date().isoformat()}.",
        ]
    )
    return system, task


def _prefetch_args(args: Any, values: dict[str, Any]) -> Any:
    """Fill slots in prefetch arguments; a slot that is the whole string keeps lists as lists."""
    if isinstance(args, dict):
        return {k: _prefetch_args(v, values) for k, v in args.items()}
    if isinstance(args, list):
        out: list[Any] = []
        for item in args:
            filled = _prefetch_args(item, values)
            out.extend(filled if isinstance(filled, list) else [filled])
        return [x for x in out if x not in (None, "")]
    if isinstance(args, str):
        whole = _SLOT.fullmatch(args)
        if whole:
            return values.get(whole.group(1))
        return render(args, values)
    return args


# -- run records ------------------------------------------------------------------------------


class RunStore:
    """Run folders under `<root>/runs/`."""

    def __init__(self, root: Path) -> None:
        """Bind to the analyst folder."""
        self.root = root
        self._lock = threading.Lock()

    def folder(self, run_id: str) -> Path:
        """A run's folder (the id is checked, so it cannot escape the root)."""
        if not re.fullmatch(r"run-[0-9a-f]{12}", run_id):
            raise KeyError(run_id)
        return self.root / "runs" / run_id

    def save(self, record: RunRecord) -> None:
        """Write `run.json` atomically."""
        folder = self.folder(record.run_id)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "run.json.tmp"
        with self._lock:
            tmp.write_text(record.model_dump_json(indent=1), encoding="utf-8")
            tmp.replace(folder / "run.json")

    def get(self, run_id: str) -> RunRecord:
        """A run's record; raises `KeyError`."""
        path = self.folder(run_id) / "run.json"
        if not path.exists():
            raise KeyError(run_id)
        with self._lock:
            return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def all(self) -> list[RunRecord]:
        """Every run, newest first."""
        runs_dir = self.root / "runs"
        if not runs_dir.exists():
            return []
        out = []
        for path in runs_dir.glob("run-*/run.json"):
            try:
                out.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda r: r.created_at, reverse=True)

    def event(self, run_id: str, event: dict[str, Any]) -> None:
        """Append an event to `events.jsonl`."""
        folder = self.folder(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.now(UTC).isoformat(), **event}, default=str)
        with self._lock, (folder / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Events from line `after` on."""
        path = self.folder(run_id) / "events.jsonl"
        if not path.exists():
            return []
        with self._lock:
            lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(x) for x in lines[after:] if x.strip()]


# -- the runner ---------------------------------------------------------------------------------


ClientFactory = Callable[[], Any]


class CardRunner:
    """Preflights and runs cards for one app: its projects, knowledge and analyst folder."""

    def __init__(
        self,
        store: ProjectStore,
        access: KnowledgeAccess,
        root: Path,
        *,
        settings: AnalystSettings | None = None,
        client_factory: ClientFactory | None = None,
        evidence_root: Path | None = None,
    ) -> None:
        """`client_factory` makes the Anthropic client (tests pass a fake)."""
        self.store = store
        self.access = access
        self.root = root
        self.settings = settings or AnalystSettings.from_env()
        self.runs = RunStore(root)
        self.spend = SpendLog(root)
        self.policy = DataPolicy(root)
        self._client_factory = client_factory
        self._evidence_root = evidence_root

    def playground(self, project_ids: list[str]) -> Playground:
        """A playground bound to these projects."""
        return Playground(self.store, self.access, project_ids, evidence_root=self._evidence_root)

    def _budget(self) -> Budget:
        s = self.settings
        return Budget(
            self.spend,
            enabled=s.enabled,
            run_cap=s.run_cap_usd,
            daily_cap=s.daily_cap_usd,
            monthly_cap=s.monthly_cap_usd,
        )

    def preflight(
        self, card: ActionCard, raw: dict[str, Any], backend: Backend = "pack"
    ) -> tuple[Preflight, Playground | None]:
        """Whether the card can run with these inputs on this backend, and why not."""
        missing_required = [t for t in card.required_tools if not TOOLS[t].built]
        missing_optional = [t for t in card.optional_tools if not TOOLS[t].built]
        if missing_required:
            modules = sorted({TOOLS[t].module for t in missing_required})
            check = Preflight(
                card_id=card.id,
                runnable=False,
                problems=[
                    f"needs the {', '.join(modules)} module, which is not built yet "
                    f"(tools: {', '.join(missing_required)})"
                ],
                missing_tools=missing_required + missing_optional,
            )
            return check, None
        values, play, problems = resolve_inputs(card, raw, self.playground)
        no_research_warning: str | None = None
        uses = [*card.required_tools, *card.optional_tools]
        if play is not None and any(TOOLS[t].module == "research" for t in uses):
            from src.modules.research.service import research_store

            if research_store(self.store).latest(play.project_ids[0]) is None:
                if any(TOOLS[t].module == "research" for t in card.required_tools):
                    problems.append(
                        "this card needs search data: import a Semrush Organic Positions export "
                        "or pull from Semrush in the Research tab first"
                    )
                else:
                    no_research_warning = (
                        "runs without search data (volumes, rankings): this project has no "
                        "research yet"
                    )
        warnings: list[str] = []
        tools = [t for t in [*card.required_tools, *card.optional_tools] if TOOLS[t].built]
        if play is not None:
            websites = [
                v
                for p in card.parameters
                if p.kind == "website"
                for v in _as_list(values.get(p.name))
            ]
            for p in card.parameters:
                if p.kind == "url" and values.get(p.name):
                    websites.append(play.website_of_url(str(values[p.name])).key)
            compared = {
                _prefetch_args(step.args, values).get("website")
                for step in card.prefetch
                if step.tool == "get_changes"
            }
            for key in dict.fromkeys(websites):
                website = play.website(key)
                data = play.site_data(website)
                if data is None:
                    problems.append(
                        f"{website.name} has no finished scrape with its knowledge space built; "
                        "scrape it in the project first"
                    )
                    continue
                store = play.open_store(data)
                try:
                    model = store.meta().get("model", "")
                finally:
                    store.close()
                if str(model).startswith("hashing-"):
                    warnings.append(
                        f"{website.name} is searched by shared words only (offline embedder); "
                        "install Ollama with bge-m3 and rebuild for search by meaning"
                    )
                if key in compared and len(play.ready_runs(data)) < 2:
                    problems.append(f"{website.name} needs two scrapes to compare; it has one")
            if backend == "claude":
                refusal = self._budget().refusal()
                if refusal:
                    problems.append(refusal)
                touched = set(play.project_ids) | {
                    fresh.project_id for w in play.websites() if (fresh := w.freshest())
                }
                blocked = self.policy.blocked(sorted(touched))
                if blocked:
                    problems.append(
                        "these projects are marked client_confidential and may not be sent to "
                        f"Claude: {', '.join(blocked)} (use the context pack instead)"
                    )
        if no_research_warning:
            warnings.append(no_research_warning)
        if missing_optional:
            warnings.append(
                "runs without " + ", ".join(missing_optional) + " (module not built yet)"
            )
        check = Preflight(
            card_id=card.id,
            runnable=not problems,
            inputs=values,
            problems=problems,
            warnings=warnings,
            tools=tools,
            missing_tools=missing_optional,
        )
        return check, play

    def start(self, card: ActionCard, raw: dict[str, Any], backend: Backend) -> RunRecord:
        """Create the run record (queued, or refused with the preflight's problems)."""
        check, _play = self.preflight(card, raw, backend)
        record = RunRecord(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            card_id=card.id,
            card_version=card.version,
            backend=backend,
            status="queued" if check.runnable else "refused",
            created_at=datetime.now(UTC),
            inputs=check.inputs or dict(raw),
            project_ids=[
                str(check.inputs.get(p.name)) for p in card.parameters if p.kind == "project"
            ]
            if check.inputs
            else [],
            model=self.settings.model if backend == "claude" else None,
            message="; ".join(check.problems) if check.problems else "; ".join(check.warnings),
        )
        if not check.runnable:
            record = record.model_copy(update={"finished_at": datetime.now(UTC)})
        self.runs.save(record)
        return record

    def execute(self, card: ActionCard, record: RunRecord) -> RunRecord:
        """Run a queued record to the end; never raises (failures go into the record)."""
        runs = self.runs
        record = record.model_copy(update={"status": "running"})
        runs.save(record)
        folder = runs.folder(record.run_id)
        try:
            check, play = self.preflight(card, record.inputs, record.backend)
            if not check.runnable or play is None:
                return self._end(record, "refused", "; ".join(check.problems))
            values = check.inputs
            system, task = compile_prompt(card, values, check.tools)
            (folder / "prompt.md").write_text(
                f"# System\n\n{system}\n\n# Task\n\n{task}\n", encoding="utf-8"
            )
            prefetched = self._prefetch(card, values, play, record.run_id)
            data_block = "\n\n".join(
                f"<tool_result tool=\"{name}\" args='{json.dumps(args, ensure_ascii=False)}'>\n"
                f"{text}\n</tool_result>"
                for name, args, text, _err in prefetched
            )
            first = (
                f"{task}\n\nData already retrieved for you (cite the `ref` values):\n\n{data_block}"
                if prefetched
                else task
            )
            files = {"prompt": "prompt.md"}
            if record.backend == "pack":
                pack = (
                    f"# Context pack: {card.title}\n\n"
                    "Paste everything below into Claude (or another assistant your data policy "
                    "allows). Citations like [chunk:...] refer to the data in this pack; check "
                    "the answer against them.\n\n"
                    f"## Instructions\n\n{system}\n\n## Task and data\n\n{first}\n"
                )
                (folder / "context_pack.md").write_text(pack, encoding="utf-8")
                (folder / "ledger.json").write_text(play.ledger.to_json(), encoding="utf-8")
                files.update({"context_pack": "context_pack.md", "ledger": "ledger.json"})
                record = record.model_copy(update={"files": files, "tool_calls": len(prefetched)})
                return self._end(
                    record, "finished", "; ".join(check.warnings) or "context pack written"
                )
            return self._claude(card, record, play, system, first, check.tools, files)
        except Exception as exc:  # noqa: BLE001 - a run failure is reported, never raised
            runs.event(record.run_id, {"type": "error", "message": str(exc)[:500]})
            return self._end(record, "failed", f"stopped on an error: {str(exc)[:300]}")

    def _prefetch(
        self, card: ActionCard, values: dict[str, Any], play: Playground, run_id: str
    ) -> list[tuple[str, Any, str, bool]]:
        out: list[tuple[str, Any, str, bool]] = []
        for step in card.prefetch:
            items = _as_list(values.get(step.for_each)) if step.for_each else [None]
            for item in items:
                bound = {**values, "item": item} if item is not None else values
                args = _prefetch_args(step.args, bound)
                text, is_error = run_tool(play, step.tool, args)
                self.runs.event(
                    run_id, {"type": "prefetch", "tool": step.tool, "args": args, "error": is_error}
                )
                out.append((step.tool, args, text, is_error))
        return out

    def _claude(
        self,
        card: ActionCard,
        record: RunRecord,
        play: Playground,
        system: str,
        first: str,
        tools: list[str],
        files: dict[str, str],
    ) -> RunRecord:
        from src.modules.analyst.agent import AgentError, ClaudeAgent

        folder = self.runs.folder(record.run_id)
        budget = self._budget()
        if self._client_factory is not None:
            client = self._client_factory()
        else:
            import anthropic

            client = anthropic.Anthropic(max_retries=4)
        agent = ClaudeAgent(
            client,
            model=self.settings.model,
            effort=self.settings.effort,
            budget=budget,
            spend=self.spend,
            run_id=record.run_id,
            card_id=card.id,
            max_tool_calls=self.settings.max_tool_calls,
            fallbacks=self.settings.fallbacks,
            emit=lambda event: (
                None if event.get("type") == "text" else self.runs.event(record.run_id, event)
            ),
        )
        record = record.model_copy(update={"cap_usd": budget.cap})
        self.runs.save(record)
        try:
            result = agent.run(
                system,
                first,
                anthropic_tools(tools),
                lambda name, args: (
                    run_tool(play, name, args)
                    if name in tools
                    else (
                        json.dumps({"error": f"tool {name!r} is not enabled for this card"}),
                        True,
                    )
                ),
            )
        except AgentError as exc:
            (folder / "ledger.json").write_text(play.ledger.to_json(), encoding="utf-8")
            update = {
                "cost_usd": round(budget.spent, 6),
                "files": {**files, "ledger": "ledger.json"},
            }
            return self._end(record.model_copy(update=update), "failed", str(exc))
        check = verify_deliverable(result.text, play.ledger)
        deliverable = result.text + "\n" + sources_section(result.text, play.ledger, check)
        if result.truncated:
            deliverable = (
                "> This deliverable was cut short (output or budget limit); it may be "
                "incomplete.\n\n" + deliverable
            )
        (folder / "deliverable.md").write_text(deliverable, encoding="utf-8")
        (folder / "ledger.json").write_text(play.ledger.to_json(), encoding="utf-8")
        record = record.model_copy(
            update={
                "model": result.model,
                "turns": result.turns,
                "tool_calls": result.tool_calls,
                "usage": result.usage,
                "cost_usd": result.cost_usd,
                "verification": check,
                "files": {**files, "deliverable": "deliverable.md", "ledger": "ledger.json"},
            }
        )
        message = "verified" if check.passed else "finished; the verification needs review"
        if result.truncated:
            message += " (cut short)"
        return self._end(record, "finished", message)

    def _end(self, record: RunRecord, status: str, message: str) -> RunRecord:
        done = record.model_copy(
            update={"status": status, "message": message, "finished_at": datetime.now(UTC)}
        )
        self.runs.save(done)
        self.runs.event(done.run_id, {"type": "end", "status": status, "message": message})
        return done
