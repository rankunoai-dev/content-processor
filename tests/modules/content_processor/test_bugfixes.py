"""Regression tests for the six bugs found in the 2026-10-20 codebase audit."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpcore
import httpx
import pytest
import uvicorn
from PIL import Image

from fixtures.server import app
from src.integrations import http_client
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher, PinningBackend, accept_encoding
from src.modules.content_processor.adapters import ocr
from src.modules.content_processor.adapters.image import extract_image
from src.modules.content_processor.adapters.ocr import OcrResult
from src.modules.content_processor.analysis.claims import claims_from_records, codes_of
from src.modules.content_processor.analysis.contradictions import find_contradictions
from src.modules.content_processor.cli import EgressGuard
from src.modules.content_processor.contracts import RunScope, Task
from src.modules.content_processor.enums import (
    AccessTier,
    BlockKind,
    EntityStatus,
    GapType,
    PriorityClass,
    TaskKind,
    TaskState,
)
from src.modules.content_processor.governor.escalation import (
    CIRCUIT_OPEN_FOR,
    FREE_TIER_CEILING,
    ActionKind,
    CircuitState,
    initial_host_ceiling,
)
from src.modules.content_processor.ledger import sql
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.ledger.tasks import TaskLedger
from src.modules.content_processor.workers.fetch_worker import FetchWorker
from src.modules.content_processor.workers.ports import RobotsCache
from tests.modules.content_processor.l5_factory import record

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _png(size: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), "white").save(buf, format="PNG")
    return buf.getvalue()


# -- 1: a failed OCR is never "verified no content" ---------------------------------------


def test_ocr_error_is_partial_with_a_gap_not_verified_noncontent():
    def broken(image: bytes, *, packs: Any, psm: int) -> OcrResult:
        return OcrResult(text="", confidence=0.0, error="TesseractError: boom")

    out = extract_image(
        _png(),
        resource_id="r",
        run_id="run",
        evidence_id="e",
        ocr_runner=broken,
        ocr_languages=["eng"],
    )
    assert out.status is EntityStatus.PARTIAL
    assert out.status is not EntityStatus.VERIFIED_NONCONTENT
    (gap,) = out.gaps
    assert gap.gap_type is GapType.UNSUPPORTED_FORMAT
    assert gap.reason.startswith("ocr_error:TesseractError: boom")
    assert gap.retry_eligible


def test_empty_ocr_reports_zero_confidence(monkeypatch: pytest.MonkeyPatch):
    empty = {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []}
    fake = SimpleNamespace(Output=SimpleNamespace(DICT="dict"), image_to_data=lambda *a, **k: empty)
    monkeypatch.setattr(ocr, "is_ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "_tesseract", lambda: fake)
    result = ocr.run_ocr(_png(), packs=["eng"])
    assert result.text == "" and result.confidence == 0.0


# -- 2: brotli is advertised only when it can be decoded -------------------------------------


def test_accept_encoding_omits_br_without_a_decoder(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_client.importlib.util, "find_spec", lambda name: None)
    assert accept_encoding() == "gzip, deflate"
    monkeypatch.setattr(
        http_client.importlib.util, "find_spec", lambda name: object() if name == "brotli" else None
    )
    assert accept_encoding() == "gzip, deflate, br"


async def test_t1_request_headers_match_the_installed_decoders():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ae"] = request.headers["accept-encoding"]
        return httpx.Response(200, text="<html><body>ok</body></html>")

    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "t"),
        user_agent="t",
        transport=httpx.MockTransport(handler),
    )
    await fetcher.fetch("http://h.test/")
    await fetcher.aclose()
    assert seen["ae"] == accept_encoding()


# -- 3: an open circuit closes again in durable runs ----------------------------------------


def test_lease_sql_admits_an_expired_open_circuit():
    assert "h.circuit_opened_at <= now() - make_interval(secs => %(circuit_open_s)s)" in (
        sql.SQL_LEASE_WITH_SLOT
    )
    ledger, cur = _mock_task_ledger()
    cur.fetchone.return_value = None
    ledger.lease("w", [TaskKind.FETCH])
    params = cur.execute.call_args_list[0][0][1]
    assert params["circuit_open_s"] == CIRCUIT_OPEN_FOR.total_seconds()


def _fixture_scope() -> RunScope:
    raw = json.loads((ROOT / "fixtures" / "scopes" / "local_fixtures.json").read_text("utf-8"))
    return RunScope.model_validate(raw)


def _fetch_env(tmp: Path) -> tuple[MemoryLedger, FetchWorker, HttpFetcher, Task]:
    scope = _fixture_scope()
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.set_robots(
        scope.tenant_id, "127.0.0.1", RobotsCache(body="", status=200, fetched_at=NOW)
    )
    task = Task(
        id=1,
        run_id="run-b",
        kind=TaskKind.FETCH,
        idempotency_key="k1",
        host="127.0.0.1",
        priority=PriorityClass.HTML_PAGE,
        state=TaskState.LEASED,
        attempts=1,
        lease_owner="w",
        payload={
            "schedule_key": "http://127.0.0.1:8765/pages/static_ok",
            "requested_url": "http://127.0.0.1:8765/pages/static_ok",
        },
    )
    ledger._tasks[1] = task  # noqa: SLF001 - place a leased task directly
    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "fixture"),
        user_agent="t",
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=1_000_000, total_timeout_s=5),
    )
    worker = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=LocalEvidenceStore(tmp, scope.tenant_id),
        scope=scope,
        run_id="run-b",
        worker_id="w",
    )
    return ledger, worker, fetcher, task


async def test_fetch_worker_probes_an_expired_circuit_as_half_open(tmp_path: Path):
    ledger, worker, fetcher, task = _fetch_env(tmp_path)
    state = ledger.load_host_state("t", "127.0.0.1")
    state.circuit = CircuitState.OPEN
    state.circuit_opened_at = datetime.now(UTC) - CIRCUIT_OPEN_FOR - timedelta(seconds=1)
    await worker.process(task)
    await fetcher.aclose()
    assert ledger.hosts["127.0.0.1"].circuit is CircuitState.CLOSED  # probe succeeded
    (res,) = ledger.resources.values()
    assert res.status is EntityStatus.EXTRACTED_UNVERIFIED


async def test_fetch_worker_holds_back_while_the_circuit_is_open(tmp_path: Path):
    ledger, worker, fetcher, task = _fetch_env(tmp_path)
    state = ledger.load_host_state("t", "127.0.0.1")
    state.circuit = CircuitState.OPEN
    state.circuit_opened_at = datetime.now(UTC)
    await worker.process(task)
    await fetcher.aclose()
    assert ledger._tasks[1].last_error == "circuit_open"  # noqa: SLF001
    assert ledger._tasks[1].state is TaskState.PENDING  # noqa: SLF001
    assert ledger.resources == {}  # nothing was fetched


# -- 4: paid tiers always need a per-host approval ---------------------------------------


@pytest.mark.parametrize(
    ("scope_ceiling", "expected"),
    [
        (AccessTier.T1_POLITE_HTTP, AccessTier.T1_POLITE_HTTP),
        (AccessTier.T3_FIDELITY_BROWSER, AccessTier.T3_FIDELITY_BROWSER),
        (AccessTier.T4_PROXIED, FREE_TIER_CEILING),
        (AccessTier.T5_MANAGED_UNBLOCKER, FREE_TIER_CEILING),
    ],
)
def test_initial_host_ceiling_caps_at_the_free_tiers(scope_ceiling, expected):
    assert initial_host_ceiling(scope_ceiling) is expected


def test_a_t5_scope_still_asks_before_entering_a_paid_tier():
    ledger = MemoryLedger(ceiling=AccessTier.T5_MANAGED_UNBLOCKER)
    state = ledger.load_host_state("t", "h")
    assert state.ceiling is AccessTier.T3_FIDELITY_BROWSER
    state.tier = AccessTier.T3_FIDELITY_BROWSER
    action = state.record_block(BlockKind.JS_CHALLENGE, NOW)
    assert action.kind is ActionKind.NEEDS_APPROVAL
    assert action.proposed_tier in (AccessTier.T4_PROXIED, AccessTier.T5_MANAGED_UNBLOCKER)
    assert state.tier is AccessTier.T3_FIDELITY_BROWSER  # no paid tier entered


def _mock_task_ledger() -> tuple[TaskLedger, MagicMock]:
    with patch("psycopg.connect") as connect:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        connect.return_value = conn
        return TaskLedger("postgresql://t:t@localhost/t"), cur


def test_durable_host_rows_start_at_the_free_ceiling():
    ledger, cur = _mock_task_ledger()
    ledger.ensure_host("t", "h", AccessTier.T5_MANAGED_UNBLOCKER)
    assert cur.execute.call_args[0][1]["tier_ceiling"] == "T3_FIDELITY_BROWSER"


# -- 5: connections go to the IPs the egress check approved -----------------------------------


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, refuse: set[str] | None = None) -> None:
        self.targets: list[str] = []
        self.refuse = refuse or set()

    async def connect_tcp(self, host: str, port: int, **kw: Any) -> httpcore.AsyncNetworkStream:
        self.targets.append(host)
        if host in self.refuse:
            raise httpcore.ConnectError(f"refused:{host}")
        return MagicMock(spec=httpcore.AsyncNetworkStream)

    async def sleep(self, seconds: float) -> None:
        return None


async def test_pinning_backend_connects_to_approved_addresses_only():
    inner = RecordingBackend(refuse={"::1"})
    backend = PinningBackend(inner)
    with pytest.raises(httpcore.ConnectError, match="egress_not_pinned:evil.test"):
        await backend.connect_tcp("evil.test", 80)
    backend.pin("Shop.Test", ["::1", "203.0.113.7"])
    await backend.connect_tcp("shop.test", 443)
    assert inner.targets == ["::1", "203.0.113.7"]  # falls back to the next approved address
    backend.pin("shop.test", [])
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("shop.test", 443)
    with pytest.raises(httpcore.ConnectError, match="unix_socket"):
        await backend.connect_unix_socket("egress.sock")
    await backend.sleep(0)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live_port():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield port
    server.should_exit = True
    thread.join(timeout=5)


async def test_t1_connects_to_the_pinned_ip_without_resolving_the_name(live_port: int):
    """`pinned.test` does not resolve anywhere: success proves the pinned IP was used."""
    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "approved"),
        user_agent="t",
        pinner=lambda host: ["127.0.0.1"] if host == "pinned.test" else [],
    )
    result = await fetcher.fetch(f"http://pinned.test:{live_port}/pages/static_ok")
    unpinned = await fetcher.fetch(f"http://other.test:{live_port}/pages/static_ok")
    await fetcher.aclose()
    assert result.http_status == 200 and b"Eligibility applies" in result.body
    assert result.final_url == f"http://pinned.test:{live_port}/pages/static_ok"
    assert unpinned.transport_error is not None and "egress_not_pinned" in unpinned.transport_error


def test_egress_guard_remembers_only_approved_addresses():
    guard = EgressGuard(_fixture_scope())
    assert guard("127.0.0.1") == (True, "allowed_private_fixture")
    assert guard.pinned("127.0.0.1") == ["127.0.0.1"]
    strict = EgressGuard(_fixture_scope().model_copy(update={"egress_allow_private": False}))
    allowed, _ = strict("127.0.0.1")
    assert not allowed and strict.pinned("127.0.0.1") == []
    assert guard.pinned("never-checked.test") == []


# -- 6: periodic snapshots and different entities are not contradictions (K19) -------------------


def test_as_of_snapshots_are_points_in_time_not_date_conflicts():
    recs = [
        record("http://h/q1", ["The fund's NAV distribution rate was 6.5% as of March 31, 2024."]),
        record("http://h/q3", ["The fund's NAV distribution rate was 7.1% as of March 31, 2025."]),
    ]
    a, b = claims_from_records(recs)
    assert a.as_of is not None and a.context.effective_from is None
    result = find_contradictions(recs)
    assert all(p.bucket == "context_differs" for p in result.pairs)
    assert not any(f.startswith("date_conflict") for p in result.pairs for f in p.flags)


def test_year_to_date_through_is_a_snapshot():
    recs = [
        record(
            "http://h/a",
            ["Cumulative total return for the fiscal year through March 31, 2024 was 4%."],
        ),
        record(
            "http://h/b",
            ["Cumulative total return for the fiscal year through September 30, 2024 was 9%."],
        ),
    ]
    assert all(c.as_of is not None for c in claims_from_records(recs))
    assert all(p.bucket == "context_differs" for p in find_contradictions(recs).pairs)


def test_different_entity_codes_are_different_context():
    recs = [
        record(
            "http://h/a",
            ["Under the PDT Plan, the Fund makes monthly distributions of $0.0825 per share."],
        ),
        record(
            "http://h/b",
            ["Under the HTD Plan, the Fund makes monthly distributions of $0.1580 per share."],
        ),
    ]
    (pair,) = find_contradictions(recs).pairs
    assert pair.bucket == "context_differs" and pair.context["codes"] == "differs(PDT|HTD)"


def test_a_claim_without_a_code_inherits_its_page_codes():
    recs = [
        record(
            "http://h/pdt",
            [
                "John Hancock Premium Dividend Fund (NYSE: PDT) announced today.",
                "PDT shareholders receive the notice.",
                "Under the Plan, the Fund makes fixed monthly distributions of $0.0825 per share.",
            ],
        ),
        record(
            "http://h/htd",
            ["Under the HTD Plan, the Fund makes monthly distributions of $0.1580 per share."],
        ),
    ]
    plan = next(c for c in claims_from_records(recs) if "fixed monthly" in c.text)
    assert plan.codes == frozenset({"PDT"})
    assert all(p.bucket == "context_differs" for p in find_contradictions(recs).pairs)


def test_same_policy_with_different_effective_dates_still_conflicts():
    recs = [
        record(
            "http://h/a",
            ["Effective from 1 March 2026 the new returns policy applies to all orders."],
        ),
        record(
            "http://h/b",
            ["Effective from 1 June 2026 the new returns policy applies to all orders."],
        ),
    ]
    (pair,) = find_contradictions(recs).pairs
    assert pair.bucket == "context_matched"
    assert pair.flags == ["date_conflict:from:2026-03-01!=2026-06-01"]


def test_codes_ignore_common_acronyms():
    assert codes_of("The NAV in the US is reported to the NYSE by the CEO") == frozenset()
    assert codes_of("Our IRA and ROTH forms") == frozenset({"IRA", "ROTH"})
