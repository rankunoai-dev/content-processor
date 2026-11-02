"""Senior Analyst Action Cards: library, inputs, preflight, tools, checks, budget, runs, API."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import anthropic
import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from src.modules.analyst import commands as analyst_commands
from src.modules.analyst.agent import AgentError, ClaudeAgent, _api_error
from src.modules.analyst.api import AnalystWorker
from src.modules.analyst.library import ANALYST_RULES, CARDS, card
from src.modules.analyst.models import RunRecord
from src.modules.analyst.playground import EvidenceLedger, Playground, ScopeError
from src.modules.analyst.policy import (
    PRICES,
    Budget,
    DataPolicy,
    SpendEntry,
    SpendLog,
    cost_of,
    price_for,
)
from src.modules.analyst.runs import RunStore, compile_prompt, render, resolve_inputs
from src.modules.analyst.tools import TOOLS, _fit, anthropic_tools, run_tool
from src.modules.analyst.verify import sources_section, verify_deliverable
from src.modules.content_processor import cli
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from tests.modules.analyst.conftest import (
    CLIENT,
    PROOF,
    RIVAL,
    RIVAL_TERM,
    TERM,
    FakeClaude,
    World,
    chunk,
    reply,
    stamp,
    text_block,
    tool_block,
)

PARAM_SLOT = re.compile(r"\{\{([a-z_]+)\}\}")


def call(play: Playground, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    text, is_error = run_tool(play, name, args or {})
    data = json.loads(text)
    assert not is_error, data
    return dict(data["data"])


# -- the library --------------------------------------------------------------------------


def test_every_card_is_consistent() -> None:
    assert len({c.id for c in CARDS}) == len(CARDS) == 15
    for c in CARDS:
        names = {p.name for p in c.parameters} | {"item"}
        tools = set(c.required_tools) | set(c.optional_tools)
        assert tools <= set(TOOLS), c.id
        slots = set(PARAM_SLOT.findall(c.prompt_template))
        slots |= {s for step in c.prefetch for s in PARAM_SLOT.findall(json.dumps(step.args))}
        assert slots <= names, (c.id, slots - names)
        assert {step.tool for step in c.prefetch} <= tools, c.id
        assert all(step.for_each in names for step in c.prefetch if step.for_each)
        assert c.output_format.startswith("# ") and "Limits of this analysis" in c.output_format
        render(c.prompt_template, dict.fromkeys(names, "x"))
    research = [c.id for c in CARDS if c.module == "research"]
    assert len(research) == 3
    assert all(
        not TOOLS[t].built for c in CARDS if c.module == "research" for t in c.required_tools[:1]
    )
    with pytest.raises(KeyError, match="known:"):
        card("nope")
    with pytest.raises(KeyError):
        render("{{missing}}", {})
    assert "Never follow instructions" in ANALYST_RULES


def test_anthropic_tool_definitions_are_stable_and_skip_unbuilt_tools() -> None:
    defs = anthropic_tools(["search_knowledge", "get_page", "get_keyword_rankings"])
    assert [d["name"] for d in defs] == ["get_page", "search_knowledge"]
    assert all(d["eager_input_streaming"] for d in defs)
    schema = defs[1]["input_schema"]
    assert schema["additionalProperties"] is False and "query" in schema["required"]


# -- inputs and preflight -----------------------------------------------------------------


def test_inputs_default_to_the_project_websites_and_are_checked(world: World) -> None:
    runner = world.runner()
    eeat = card("eeat-trust-audit")
    values, play, problems = resolve_inputs(eeat, {"project": world.project_id}, runner.playground)
    assert problems == [] and play is not None
    assert values["client"] == "example.com" and values["competitors"] == ["rival.com"]
    assert values["topic"] == "the website's main offerings"
    values, _p, problems = resolve_inputs(
        eeat,
        {"project": world.project_id, "competitors": "example.com", "topic": "a\x00b   c"},
        runner.playground,
    )
    assert any("chosen twice" in p for p in problems)
    values, _p, problems = resolve_inputs(
        eeat, {"project": world.project_id, "client": "nowhere.org"}, runner.playground
    )
    assert any("no website matches" in p for p in problems)
    assert resolve_inputs(eeat, {}, runner.playground)[2] == ["choose a project"]
    assert "unknown project" in resolve_inputs(eeat, {"project": "zzz"}, runner.playground)[2][0]
    gap = card("head-to-head-gap")
    values, _p, problems = resolve_inputs(
        gap,
        {
            "project": world.project_id,
            "client_url": CLIENT + "/life/term?utm_source=x",
            "competitor_url": "https://evil.example.org/x",
            "topic": "  term   life ",
        },
        runner.playground,
    )
    assert values["client_url"] == CLIENT + "/life/term" and values["topic"] == "term life"
    assert len([p for p in problems if "Competitor page" in p]) == 1
    _v, _p, problems = resolve_inputs(gap, {"project": world.project_id}, runner.playground)
    assert "Client page is required" in problems and "Topic is required" in problems


def test_preflight_names_every_blocker(world: World) -> None:
    runner = world.runner(enabled=False)
    check, _ = runner.preflight(card("serp-intent-opportunity"), {"project": world.project_id})
    assert not check.runnable and "research module" in check.problems[0]
    check, _ = runner.preflight(card("eeat-trust-audit"), {"project": world.project_id})
    assert check.runnable and any("shared words only" in w for w in check.warnings)
    check, _ = runner.preflight(card("eeat-trust-audit"), {"project": world.project_id}, "claude")
    assert not check.runnable and "switched off" in check.problems[0]
    check, _ = world.runner(monthly_cap_usd=0).preflight(
        card("eeat-trust-audit"), {"project": world.project_id}, "claude"
    )
    assert "no monthly cap" in check.problems[0]
    runner = world.runner()
    runner.policy.set(world.project_id, "client_confidential")
    check, _ = runner.preflight(card("eeat-trust-audit"), {"project": world.project_id}, "claude")
    assert any("client_confidential" in p for p in check.problems)
    assert runner.preflight(card("eeat-trust-audit"), {"project": world.project_id})[0].runnable
    radar = card("competitor-strategy-radar")
    check, _ = runner.preflight(radar, {"project": world.project_id, "competitor": "rival.com"})
    assert check.problems == ["Rival needs two scrapes to compare; it has one"]
    brief = card("content-brief")
    check, _ = runner.preflight(brief, {"project": world.project_id, "keyword": "term life"})
    assert check.runnable and any("get_serp_overview" in w for w in check.warnings)


def test_a_website_without_a_built_knowledge_space_blocks_the_card(world: World) -> None:
    world.store.add_site(
        world.project_id,
        __import__("src.modules.content_processor.projects.store", fromlist=["SiteSpec"]).SiteSpec(
            name="Third", website="https://third.net"
        ),
    )
    runner = world.runner()
    check, _ = runner.preflight(
        card("offering-coverage-matrix"),
        {"project": world.project_id, "competitors": "rival.com,third.net"},
    )
    assert any("Third has no finished scrape" in p for p in check.problems)


# -- tools --------------------------------------------------------------------------------


def test_tools_read_only_inside_the_scope_and_enter_the_ledger(world: World) -> None:
    play = world.runner().playground([world.project_id])
    sites = call(play, "list_websites")["websites"]
    assert {s["website"] for s in sites} == {"example.com", "rival.com"}
    assert {s["role_in_this_run"] for s in sites} == {"client", "competitor"}
    assert all(s["searchable"] and not s["search_by_meaning"] for s in sites)

    found = call(play, "search_knowledge", {"query": "convert term policy", "per_site": 1})
    assert {p["website"] for p in found["passages"]} == {"example.com", "rival.com"}
    assert found["search_ref"].startswith("fact:search-")
    ref = found["passages"][0]["ref"]
    assert play.ledger.entries[ref].evidence_ids[0].startswith("evidence:")
    only = call(play, "search_knowledge", {"query": "term", "websites": ["rival.com"], "top_k": 1})
    assert [p["website"] for p in only["passages"]] == ["rival.com"]
    faq = call(play, "search_knowledge", {"query": "convert term policy", "role": "faq"})
    assert all(p["chunk_role"] == "faq" for p in faq["passages"])

    page = call(play, "get_page", {"url": CLIENT + "/life/term"})
    assert page["sections"][0]["text"] == TERM and page["facts"]["ctas"][0]["text"] == "Get a quote"
    facts = call(play, "get_page_facts", {"url": RIVAL + "/term-life"})
    assert facts["facts"]["faq_questions"] == ["Can I convert my term policy?"]
    pages = call(play, "list_pages", {"website": "example.com"})
    assert pages["total_pages"] == 3 and all(p["in_knowledge_base"] for p in pages["pages"])
    catalog = call(play, "get_offering_catalog", {"website": "example.com"})
    assert [o["name"] for o in catalog["offerings"]] == ["Life insurance", "Term life insurance"]
    issues = call(play, "get_seo_issues", {"website": "example.com"})
    assert [i["code"] for i in issues["issues"]] == ["missing_canonical", "title_too_long"]
    assert issues["issues"][1]["pages"] == 2 and issues["pages_captured"] == 2
    pairs = call(play, "find_similar_pages", {"website": "example.com", "min_similarity": 0.9})
    assert {(p["url_a"], p["url_b"]) for p in pairs["pairs"]} == {
        (CLIENT + "/life/term", CLIENT + "/life/term-copy")
    }
    assert pairs["pairs"][0]["same_title"] is True

    for name, args, message in [
        ("get_page", {"url": "https://other.org/x"}, "no website matches"),
        ("get_page", {"url": CLIENT + "/never"}, "was not captured"),
        ("get_page_facts", {"url": CLIENT + "/never"}, "no page facts"),
        ("get_offering_catalog", {"website": "rival.com"}, "no offering catalog"),
        ("get_seo_issues", {"website": "rival.com"}, "no records to inventory"),
        ("get_changes", {"website": "rival.com"}, "changes need two"),
        ("search_knowledge", {"query": ""}, "invalid input"),
        ("search_knowledge", {"query": "x", "websites": ["mars.org"]}, "no website matches"),
        ("get_keyword_rankings", {"website": "example.com"}, "not available"),
        ("no_such_tool", {}, "not available"),
    ]:
        text, is_error = run_tool(play, name, args)
        assert is_error and message in json.loads(text)["error"], (name, text)


def test_changes_between_two_scrapes(world: World) -> None:
    r1 = stamp(RIVAL, "/term-life", "Rival term life: now from $19", "run-r2", numbers=["$19"])
    r2 = stamp(RIVAL, "/new-product", "Rival whole life", "run-r2")
    world.add_run(
        "competitor",
        "run-r2",
        [r1, r2],
        [
            chunk(r1, "r-term2", RIVAL_TERM.replace("$25", "$19"), 0, ["Rival term life"]),
            chunk(r2, "r-new", "Whole life lasts for life.", 0, ["Rival whole life"]),
        ],
        offset_h=5,
    )
    play = world.runner().playground([world.project_id])
    changes = call(play, "get_changes", {"website": "rival.com"})
    assert [p["url"] for p in changes["new_pages"]] == [RIVAL + "/new-product"]
    changed = changes["changed_pages"][0]
    assert changed["head_changes"]["title"]["after"] == "Rival term life: now from $19"
    assert changed["new_numbers"] == ["$19"] and "$19" in changed["added_text"][0]
    runner = world.runner()
    check, _ = runner.preflight(
        card("competitor-strategy-radar"), {"project": world.project_id, "competitor": "rival.com"}
    )
    assert check.runnable


def test_large_results_are_cut_to_fit_with_a_note() -> None:
    payload = {"tool": "t", "data": {"pages": [{"x": "y" * 100}] * 2000, "n": 1}}
    text = _fit(payload)
    data = json.loads(text)
    assert len(text) <= 40_000 and len(data["data"]["pages"]) < 2000
    assert "narrow the request" in data["notes"][-1]


# -- verification -------------------------------------------------------------------------


def test_verification_resolves_refs_checks_quotes_and_flags_uncited_claims() -> None:
    ledger = EvidenceLedger()
    ref = ledger.chunk(
        "c1",
        website="example.com",
        url=CLIENT + "/a",
        project="P",
        headings=["H"],
        text=PROOF,
        block_ids=["b1"],
        evidence_ids=["evidence:abc"],
    )
    fact = ledger.fact("k", kind="page", website="example.com", title="About", url=CLIENT + "/a")
    good = (
        "# Report\n"
        f'Example Life says it is "rated A+ by A.M. Best for financial strength" [{ref}].\n'
        f"| Signal | Client |\n|---|---|\n| Rating | A+ [{ref}] |\n"
        f"The page has 3 headings [{fact}].\n"
        "## Recommendations\n- Add 12 customer reviews to every product page right away now.\n"
    )
    check = verify_deliverable(good, ledger)
    assert check.passed and check.quotes_checked == 1 and check.unknown_refs == []
    assert check.resolved == check.references == 3
    sources = sources_section(good, ledger, check)
    assert "**passed**" in sources and "abc" in sources and CLIENT + "/a" in sources

    bad = (
        "# Report\n"
        f'It says "rated AAA by everyone everywhere" [{ref}].\n'
        "A made-up claim [chunk:invented-1].\n"
        "Competitors publish 40 calculators and 12 guides.\n"
        "Another long uncited sentence that goes on and on about things without any source.\n"
        "Yet another 5 pages claim this. Plus 7 more.\nAnd 9.\n"
    )
    check = verify_deliverable(bad, ledger)
    assert not check.passed and check.unknown_refs == ["chunk:invented-1"]
    assert len(check.quotes_failed) == 1 and len(check.uncited_lines) >= 4
    text = sources_section(bad, ledger, check)
    assert "needs review" in text and "Unknown citations" in text and "Uncited:" in text
    assert "no citations" in sources_section("# Empty\n", ledger, verify_deliverable("", ledger))
    assert json.loads(ledger.to_json())[ref]["evidence_ids"] == ["evidence:abc"]


# -- budget and policy --------------------------------------------------------------------


def test_costs_caps_and_data_policy(tmp_path: Path) -> None:
    usage = cost_of(
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    assert usage.usd == pytest.approx(4 + 2 + 0.2 + 5)
    assert price_for("claude-unknown") == PRICES["claude-fable-5-1"]
    log = SpendLog(tmp_path)
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    for at, usd in ((now, 3.0), (now - timedelta(days=3), 4.0), (now - timedelta(days=40), 100.0)):
        log.charge(SpendEntry(at=at, run_id="r", card_id="c", model="m", usd=usd))
    assert log.spent(since=now.replace(hour=0)) == 3.0
    budget = Budget(log, enabled=True, run_cap=2.0, daily_cap=4.0, monthly_cap=10.0, now=now)
    assert budget.cap == 1.0 and budget.refusal() is None  # daily room is $1
    assert budget.max_output_tokens("claude-opus-5-5", 10_000, 32_000) == 32_000
    budget.add(0.99)
    assert budget.max_output_tokens("claude-opus-5-5", 10_000, 32_000) == 0
    assert Budget(log, enabled=False, run_cap=2, daily_cap=4, monthly_cap=10).refusal()
    assert "no monthly cap" in (
        Budget(log, enabled=True, run_cap=2, daily_cap=4, monthly_cap=0).refusal() or ""
    )
    used = Budget(log, enabled=True, run_cap=2, daily_cap=3.0, monthly_cap=10, now=now)
    assert "used up" in (used.refusal() or "")
    policy = DataPolicy(tmp_path)
    assert policy.data_class("p") == "public" and policy.blocked(["p"]) == []
    policy.set("p", "client_confidential")
    assert DataPolicy(tmp_path).blocked(["p", "q"]) == ["p"]


# -- the Claude loop ----------------------------------------------------------------------


def _agent(world: World, client: FakeClaude, **kw: Any) -> ClaudeAgent:
    root = world.root / "analyst"
    budget = Budget(
        SpendLog(root), enabled=True, run_cap=kw.pop("cap", 2.0), daily_cap=10, monthly_cap=50
    )
    return ClaudeAgent(
        cast(anthropic.Anthropic, client),
        model="claude-opus-5-5",
        effort="high",
        budget=budget,
        spend=SpendLog(root),
        run_id="run-x",
        card_id="c",
        max_tool_calls=kw.pop("max_calls", 40),
        **kw,
    )


def test_agent_runs_tools_then_finishes_and_charges_every_call(world: World) -> None:
    client = FakeClaude(
        [
            reply(
                [
                    text_block("Looking."),
                    tool_block("t1", "search_knowledge", {"query": "x"}),
                    tool_block("t2", "get_page", {"url": "u"}),
                ],
                "tool_use",
            ),
            reply([], "pause_turn"),
            reply([text_block("# Done")], "end_turn"),
        ]
    )
    calls: list[str] = []
    events: list[dict[str, Any]] = []
    agent = _agent(world, client, emit=events.append)

    def on_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
        calls.append(name)
        return json.dumps({"ok": 1}), False

    result = agent.run(
        "system",
        "task",
        [{"name": "search_knowledge"}],
        on_tool,
    )
    assert result.text == "# Done" and result.turns == 3 and result.tool_calls == 2
    assert calls == ["search_knowledge", "get_page"]
    second = client.requests[1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in second] == ["t1", "t2"]  # all results in one message
    first = client.requests[0]
    assert first["betas"] == ["server-side-fallback-2026-07-01"] and first["fallbacks"] == "default"
    assert first["output_config"] == {"effort": "high"} and first["tool_choice"] == {"type": "auto"}
    assert result.cost_usd == pytest.approx(3 * (1000 * 4 + 500 * 20) / 1e6)
    assert len(SpendLog(world.root / "analyst").entries()) == 3
    assert any(e["type"] == "text" for e in events) and any(e["type"] == "tool" for e in events)


def test_agent_switches_to_writing_when_limits_near(world: World) -> None:
    client = FakeClaude(
        [
            reply([tool_block("t1", "a", {}), tool_block("t2", "b", {})], "tool_use"),
            reply([text_block("# Final")], "end_turn"),
        ]
    )
    result = _agent(world, client, max_calls=1).run(
        "s", "t", [], lambda n, a: (json.dumps({"ok": n}), False)
    )
    assert result.text == "# Final" and result.tool_calls == 1
    last = client.requests[1]
    assert last["tool_choice"] == {"type": "none"}
    content = last["messages"][-1]["content"]
    assert content[1]["is_error"] is True and "Stop calling tools" in content[-1]["text"]


def test_agent_failures_are_reported(world: World) -> None:
    refusal = reply([], "refusal", details=type("D", (), {"category": "cyber"})())
    with pytest.raises(AgentError, match="category: cyber"):
        _agent(world, FakeClaude([refusal])).run("s", "t", [], lambda n, a: ("{}", False))
    with pytest.raises(AgentError, match="without writing"):
        _agent(world, FakeClaude([reply([], "end_turn")])).run(
            "s", "t", [], lambda n, a: ("{}", False)
        )
    with pytest.raises(AgentError, match="cannot pay"):
        _agent(world, FakeClaude([]), cap=0.01).run("s", "t" * 100, [], lambda n, a: ("{}", False))
    with pytest.raises(AgentError, match="could not be read twice"):
        _agent(world, FakeClaude([ValueError("bad"), ValueError("bad")])).run(
            "s", "t", [], lambda n, a: ("{}", False)
        )
    result = _agent(
        world, FakeClaude([ValueError("bad"), reply([text_block("ok")], "max_tokens")])
    ).run("s", "t", [], lambda n, a: ("{}", False))
    assert result.truncated and result.text == "ok"
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def status(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
        return cls("boom", response=httpx.Response(code, request=request), body=None)

    with pytest.raises(AgentError, match="credentials"):
        _agent(world, FakeClaude([status(anthropic.AuthenticationError, 401)])).run(
            "s", "t", [], lambda n, a: ("{}", False)
        )
    assert "rate limit" in _api_error(status(anthropic.RateLimitError, 429))
    assert "not found" in _api_error(status(anthropic.NotFoundError, 404))
    assert "may not use" in _api_error(status(anthropic.PermissionDeniedError, 403))
    assert "invalid" in _api_error(status(anthropic.BadRequestError, 400))
    assert "overloaded" in _api_error(status(anthropic.InternalServerError, 529))
    assert "API error 409" in _api_error(status(anthropic.ConflictError, 409))
    assert "network" in _api_error(anthropic.APIConnectionError(request=request))
    assert "timed out" in _api_error(anthropic.APITimeoutError(request=request))
    disabled = ClaudeAgent(
        cast(anthropic.Anthropic, FakeClaude([])),
        model="m",
        effort="high",
        budget=Budget(SpendLog(world.root), enabled=False, run_cap=1, daily_cap=1, monthly_cap=1),
        spend=SpendLog(world.root),
        run_id="r",
        card_id="c",
        max_tool_calls=1,
    )
    with pytest.raises(AgentError, match="switched off"):
        disabled.run("s", "t", [], lambda n, a: ("{}", False))


# -- whole runs ---------------------------------------------------------------------------


def test_a_pack_run_writes_the_context_pack_and_ledger(world: World) -> None:
    runner = world.runner(enabled=False)
    record = runner.start(card("eeat-trust-audit"), {"project": world.project_id}, "pack")
    done = runner.execute(card("eeat-trust-audit"), record)
    assert done.status == "finished" and done.tool_calls == 2
    folder = runner.runs.folder(done.run_id)
    pack = (folder / done.files["context_pack"]).read_text(encoding="utf-8")
    assert (
        "E-E-A-T" in pack
        and "Evidence only" in pack
        and '<tool_result tool="search_knowledge"' in pack
    )
    assert "chunk:c-proof" in pack or "chunk:r-term" in pack
    assert json.loads((folder / "ledger.json").read_text(encoding="utf-8"))
    assert (folder / "prompt.md").exists() and runner.runs.events(done.run_id)[-1]["type"] == "end"
    system, task = compile_prompt(card("eeat-trust-audit"), done.inputs, ["get_page"])
    assert "Today is" not in system and "Today is" in task  # the system prompt stays cacheable
    assert "rival.com" in task and "{client}" not in task


def test_a_claude_run_is_verified_and_sourced(world: World) -> None:
    def script(prefetched_ref: str) -> list[Any]:
        return [
            reply([tool_block("t1", "get_page", {"url": CLIENT + "/about"})], "tool_use"),
            reply(
                [
                    text_block(
                        "# Trust & Credibility Matrix: example.com vs competitors\n"
                        "Example Life is "
                        '"rated A+ by A.M. Best for financial strength" [chunk:c-proof].\n'
                        f"Rival also shows a rating [{prefetched_ref}].\n"
                    )
                ],
                "end_turn",
            ),
        ]

    runner = world.runner()
    record = runner.start(card("eeat-trust-audit"), {"project": world.project_id}, "claude")
    world.clients.append(FakeClaude(script("chunk:r-term")))
    done = runner.execute(card("eeat-trust-audit"), record)
    assert done.status == "finished", done.message
    assert done.verification is not None and done.verification.passed, done.verification
    text = (runner.runs.folder(done.run_id) / "deliverable.md").read_text(encoding="utf-8")
    assert "## Sources" in text and "c-proof" in text and done.cost_usd > 0 and done.turns == 2
    assert done.message == "verified" and done.cap_usd == 2.0

    record = runner.start(card("eeat-trust-audit"), {"project": world.project_id}, "claude")
    world.clients.append(
        FakeClaude([reply([text_block("# X\nInvented [chunk:nope].\n")], "end_turn")])
    )
    flagged = runner.execute(card("eeat-trust-audit"), record)
    assert flagged.status == "finished" and "needs review" in flagged.message

    record = runner.start(card("eeat-trust-audit"), {"project": world.project_id}, "claude")
    world.clients.append(FakeClaude([reply([], "refusal")]))
    failed = runner.execute(card("eeat-trust-audit"), record)
    assert failed.status == "failed" and "declined" in failed.message

    refused = world.runner(enabled=False).start(
        card("eeat-trust-audit"), {"project": world.project_id}, "claude"
    )
    assert refused.status == "refused" and "switched off" in refused.message


def test_a_crash_inside_a_run_is_recorded_not_raised(world: World) -> None:
    runner = world.runner()
    record = runner.start(card("eeat-trust-audit"), {"project": world.project_id}, "claude")
    world.clients.append(None)  # the agent will fail calling a missing client
    done = runner.execute(card("eeat-trust-audit"), record)
    assert done.status == "failed" and "stopped on an error" in done.message
    with pytest.raises(KeyError):
        runner.runs.folder("../../etc")
    assert RunStore(world.root / "nothing").all() == []


# -- API ----------------------------------------------------------------------------------


@pytest.fixture
def api(world: World, tmp_path: Path) -> tuple[TestClient, TokenSigner, World]:
    from src.modules.content_processor.projects.runner import ScrapeRunner

    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(world.store, ScrapeRunner(world.store), signer),
        knowledge=world.access,
        analyst=world.runner(enabled=False),
    )
    return TestClient(app, base_url="http://127.0.0.1"), signer, world


WRITE = {"X-Requested-With": "content-processor"}


def test_api_lists_preflights_and_runs_cards(api: tuple[TestClient, TokenSigner, World]) -> None:
    client, signer, world = api
    cards = client.get("/api/analyst/cards").json()
    assert len(cards) == 15 and {c["needs_modules"][0] for c in cards if not c["available"]} == {
        "research"
    }
    assert client.get("/api/analyst/cards/eeat-trust-audit").json()["version"] == 1
    assert client.get("/api/analyst/cards/nope").status_code == 404
    base = f"/api/projects/{world.project_id}/analyst"
    check = client.post(f"{base}/preflight", json={"card": "eeat-trust-audit"}, headers=WRITE)
    assert check.status_code == 200 and check.json()["runnable"]
    assert client.post(f"{base}/preflight", json={"card": "x"}, headers=WRITE).status_code == 404
    assert (
        client.post(
            "/api/projects/zzz/analyst/preflight", json={"card": "x"}, headers=WRITE
        ).status_code
        == 404
    )
    assert client.post(f"{base}/runs", json={"card": "eeat-trust-audit"}).status_code == 403
    assert (
        client.post(f"{base}/runs", json={"card": "eeat-trust-audit"}, headers=WRITE).status_code
        == 401
    )
    token, _exp = signer.issue(world.project_id)
    started = client.post(
        f"{base}/runs",
        json={"card": "eeat-trust-audit"},
        headers={**WRITE, "X-Project-Token": token},
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    for _ in range(100):
        detail = client.get(f"{base}/runs/{run_id}").json()
        if detail["record"]["status"] == "finished":
            break
        time.sleep(0.05)
    assert detail["record"]["status"] == "finished" and "# Context pack" in detail["text"]
    assert [r["run_id"] for r in client.get(f"{base}/runs").json()] == [run_id]
    with client.stream("GET", f"{base}/runs/{run_id}/events") as events:
        body = "".join(events.iter_text())
    assert '"type": "end"' in body
    assert client.get(f"{base}/runs/run-000000000000").status_code == 404
    refused = client.post(
        f"{base}/runs",
        json={"card": "eeat-trust-audit", "backend": "claude"},
        headers={**WRITE, "X-Project-Token": token},
    ).json()
    assert refused["status"] == "refused"


def test_worker_marks_interrupted_runs(world: World) -> None:
    runner = world.runner()
    record = runner.start(card("eeat-trust-audit"), {"project": world.project_id}, "pack")
    runner.runs.save(record.model_copy(update={"status": "running"}))
    worker = AnalystWorker(runner)
    assert runner.runs.get(record.run_id).status == "failed"
    assert "interrupted" in runner.runs.get(record.run_id).message
    worker.shutdown()


# -- CLI ----------------------------------------------------------------------------------


def test_cli_analyze(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(analyst_commands, "_runner", lambda args: world.runner(enabled=False))
    base = ["--app-data", str(world.root)]
    assert cli.main(["analyze", "cards", "--module", "research"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 3
    assert cli.main(["analyze", "show", "content-brief"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "content-brief"
    assert cli.main(["analyze", "show", "nope"]) == 2
    assert "unknown card" in capsys.readouterr().err
    assert cli.main(["analyze", "preflight", "eeat-trust-audit", *base]) == 2
    assert (
        cli.main(["analyze", "preflight", "eeat-trust-audit", "--project", world.project_id, *base])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["runnable"] is True
    assert (
        cli.main(["analyze", "preflight", "serp-intent-opportunity", "--project", world.project_id])
        == 1
    )
    assert (
        cli.main(
            ["analyze", "run", "eeat-trust-audit", "--project", world.project_id, "--set", "bad"]
        )
        == 2
    )
    assert "name=value" in capsys.readouterr().err
    assert (
        cli.main(
            [
                "analyze",
                "run",
                "eeat-trust-audit",
                "--project",
                world.project_id,
                "--set",
                "topic=term life",
                *base,
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "finished" and out["open"].endswith("context_pack.md")
    assert cli.main(["analyze", "runs", *base]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    assert cli.main(["analyze", "policy", *base]) == 2
    assert (
        cli.main(
            [
                "analyze",
                "policy",
                "--project",
                world.project_id,
                "--data-class",
                "client_confidential",
                *base,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["data_class"] == "client_confidential"
    assert cli.main(["analyze", "spend", *base]) == 0
    assert json.loads(capsys.readouterr().out)["calls"] == 0


def test_run_records_round_trip(world: World) -> None:
    store = RunStore(world.root / "analyst")
    record = RunRecord(
        run_id="run-abcdef123456",
        card_id="c",
        card_version=1,
        backend="pack",
        status="finished",
        created_at=datetime.now(UTC),
    )
    store.save(record)
    assert store.get(record.run_id) == record
    with pytest.raises(KeyError):
        store.get("run-000000000000")
    assert store.events(record.run_id) == []
    play = world.runner().playground([world.project_id])
    with pytest.raises(ScopeError, match="not a web address"):
        play.website_of_url("not a url")
