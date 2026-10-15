from __future__ import annotations

from pathlib import Path

from src.modules.content_processor.contracts import Authorization, RunScope
from src.modules.content_processor.enums import PriorityClass, TaskKind
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scope.frontier import build_frontier, host_in_scope

SEEDS = Path(__file__).resolve().parents[3] / "fixtures" / "seeds" / "johnhancock_2026-10-15.txt"


def _public_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


def test_host_in_scope_matches_subdomains_only():
    assert host_in_scope("www.example.com", ["example.com"])
    assert host_in_scope("example.com", ["example.com"])
    assert not host_in_scope("notexample.com", ["example.com"])
    assert not host_in_scope("example.com.evil.net", ["example.com"])


def test_frontier_dedupes_and_keeps_every_requested_url(public_auth: Authorization):
    scope = RunScope(
        tenant_id="t1",
        input_urls=[
            "https://example.com/a?utm_source=x",
            "https://example.com/a",
            "http://other.example/b",
            "mailto:x@y.z",
            "https://example.com/docs/report.pdf",
        ],
        mandatory_urls=["https://example.com/pricing"],
        hosts_in_scope=["example.com"],
        authorization=public_auth,
    )
    plan = build_frontier(scope, resolver=_public_resolver)
    keys = [e.schedule_key for e in plan.entries]
    assert keys == [
        "https://example.com/pricing",
        "https://example.com/a",
        "https://example.com/docs/report.pdf",
    ]
    a = next(e for e in plan.entries if e.schedule_key == "https://example.com/a")
    assert a.aliases == ["https://example.com/a"]
    assert a.dropped_params == ["utm_source"]
    assert plan.entries[0].mandatory and plan.entries[0].priority is PriorityClass.MANDATORY
    assert plan.entries[2].priority is PriorityClass.DOCUMENT
    reasons = {x.requested_url: x.reason for x in plan.excluded}
    assert reasons["http://other.example/b"] == "out_of_scope_host"
    assert reasons["mailto:x@y.z"] == "unsupported_scheme:mailto"
    assert plan.requested_count == 6
    assert plan.hosts == {"example.com": 3}


def test_frontier_denies_private_hosts(public_auth: Authorization):
    scope = RunScope(
        tenant_id="t1",
        input_urls=["https://example.com/a"],
        hosts_in_scope=["example.com"],
        authorization=public_auth,
    )
    plan = build_frontier(scope, resolver=lambda _h: ["10.0.0.1"])
    assert plan.entries == []
    assert plan.excluded[0].reason == "egress_denied:denied:PRIVATE"


def test_johnhancock_seed_plans_34_fetches_on_one_host(public_auth: Authorization):
    urls = [line.strip() for line in SEEDS.read_text(encoding="utf-8").splitlines() if line.strip()]
    auth = public_auth.model_copy(update={"hosts": ["johnhancock.com"]})
    scope = RunScope(
        tenant_id="t1",
        input_urls=urls,
        hosts_in_scope=["johnhancock.com"],
        authorization=auth,
    )
    plan = build_frontier(scope, resolver=_public_resolver)
    assert len(urls) == 34
    assert len(plan.entries) == 34
    assert plan.excluded == []
    assert plan.hosts == {"www.johnhancock.com": 34}
    tasks = fetch_tasks_for("run-x", plan)
    assert len(tasks) == 34
    assert len({t.idempotency_key for t in tasks}) == 34
    assert all(t.kind is TaskKind.FETCH for t in tasks)
    # Re-planning produces identical keys, so a re-run collapses onto existing rows.
    assert [t.idempotency_key for t in fetch_tasks_for("run-x", plan)] == [
        t.idempotency_key for t in tasks
    ]
