"""Reviewer decisions saved from the web app: the guarded write, the overlay, the carry-forward."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.modules.content_processor.enums import GapType, UrlBadge
from src.modules.content_processor.export.commands import register
from src.modules.content_processor.export.decisions import (
    Decision,
    append_decision,
    contradiction_decisions,
    current_decisions,
    decisions_path,
    merge_carried,
    read_decisions,
    review_decisions,
)
from src.modules.content_processor.webapi.app import WRITE_HEADER, WRITE_HEADER_VALUE, create_app
from tests.modules.content_processor.l5_factory import gap, record, report_path

BASE = "http://127.0.0.1"
HEADERS = {WRITE_HEADER: WRITE_HEADER_VALUE}


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    records = [
        record("http://h/pricing", ["The annual plan costs $120 per year."]),
        record("http://h/plans", ["The annual plan costs $150 per year."]),
        record(
            "http://h/wall",
            [],
            badge=UrlBadge.BLOCKED,
            gaps=[gap("wall", GapType.ACCESS_BLOCKED, "lead_gen_wall")],
        ),
    ]
    text = "\n".join(r.model_dump_json() for r in records) + "\n"
    (tmp_path / "demo.records.jsonl").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(exports: Path) -> TestClient:
    return TestClient(create_app(exports), base_url=BASE)


def _pair_id(client: TestClient) -> str:
    items = client.get("/api/runs/demo/contradictions").json()["items"]
    assert len(items) == 1
    return str(items[0]["pair_id"])


def _post(client: TestClient, body: dict[str, Any], **headers: str) -> Any:
    return client.post("/api/runs/demo/decisions", json=body, headers={**HEADERS, **headers})


# -- the log ------------------------------------------------------------------------------


def test_log_keeps_every_line_and_the_latest_wins(tmp_path: Path):
    path = decisions_path(tmp_path, "demo")
    assert path.name == "demo.decisions.jsonl" and read_decisions(path) == []
    for d in (
        Decision(reviewer="ana", kind="contradiction", key="p1", decision="confirmed"),
        Decision(reviewer="raj", kind="contradiction", key="p1", decision="context_differs"),
        Decision(reviewer="ana", kind="review", key="http://h/wall", decision="retry_later"),
        Decision(reviewer="ana", kind="review", key="http://h/x", decision="exclude", note="dup"),
        Decision(reviewer="ana", kind="review", key="http://h/x"),  # clears
    ):
        append_decision(path, d)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"half written\n')
    log = read_decisions(path)
    assert len(log) == 5, "a damaged line is skipped, never fatal"
    assert set(current_decisions(log)) == {("contradiction", "p1"), ("review", "http://h/wall")}
    assert contradiction_decisions(log) == {"p1": "context_differs"}
    assert review_decisions(log) == {"http://h/wall": ("retry_later", "")}
    assert merge_carried({"p1": "confirmed"}, {"p1": "context_differs", "p2": "confirmed"}) == {
        "p1": "confirmed",
        "p2": "confirmed",
    }


@pytest.mark.parametrize(
    ("kind", "decision", "note", "match"),
    [
        ("contradiction", "maybe", "", "unknown contradiction decision"),
        ("review", "confirmed", "", "unknown review decision"),
        ("contradiction", "confirmed", "why", "no note"),
    ],
)
def test_unknown_decisions_are_refused(kind: str, decision: str, note: str, match: str):
    with pytest.raises(ValueError, match=match):
        Decision(reviewer="a", kind=kind, key="k", decision=decision, note=note)  # type: ignore[arg-type]


# -- the API -----------------------------------------------------------------------------


def test_saving_a_contradiction_decision(client: TestClient, exports: Path):
    pair = _pair_id(client)
    saved = _post(
        client, {"kind": "contradiction", "key": pair, "decision": "Confirmed", "reviewer": " Ana "}
    )
    assert saved.status_code == 200
    assert saved.json()["decision"] == "confirmed" and saved.json()["reviewer"] == "Ana"
    item = client.get("/api/runs/demo/contradictions").json()["items"][0]
    assert item["reviewer_decision"] == "confirmed"
    assert item["decided_by"] == "Ana" and item["decided_at"]
    assert (exports / "demo.decisions.jsonl").is_file()
    cleared = _post(
        client, {"kind": "contradiction", "key": pair, "decision": "", "reviewer": "Raj"}
    )
    assert cleared.status_code == 200
    item = client.get("/api/runs/demo/contradictions").json()["items"][0]
    assert item["reviewer_decision"] == "" and item["decided_by"] == "Raj"
    history = client.get("/api/runs/demo/decisions").json()
    assert history["path"] == "demo.decisions.jsonl"
    assert [h["reviewer"] for h in history["items"]] == ["Raj", "Ana"], "newest first"


def test_saving_a_review_decision_with_a_note(client: TestClient):
    body = {
        "kind": "review",
        "key": "http://h/wall",
        "decision": "obtain_manually",
        "note": "Ask the client for the PDF.",
        "reviewer": "Ana",
    }
    assert _post(client, body).status_code == 200
    item = next(
        i
        for i in client.get("/api/runs/demo/review").json()["items"]
        if i["url"] == "http://h/wall"
    )
    assert item["operator_action"] == "obtain_manually"
    assert item["operator_note"] == "Ask the client for the PDF."
    assert item["decided_by"] == "Ana"


@pytest.mark.parametrize(
    ("body", "headers", "status", "detail"),
    [
        (
            {"kind": "review", "key": "http://h/nope", "decision": "resolved", "reviewer": "a"},
            {},
            404,
            "not part of this run",
        ),
        (
            {"kind": "review", "key": "http://h/wall", "decision": "confirmed", "reviewer": "a"},
            {},
            422,
            "unknown review decision",
        ),
        (
            {"kind": "review", "key": "http://h/wall", "decision": "resolved", "reviewer": "   "},
            {},
            422,
            "Not saved",
        ),
        (
            {"kind": "review", "key": "http://h/wall", "decision": "resolved", "reviewer": "a"},
            {"Origin": "https://evil.example"},
            403,
            "locally",
        ),
    ],
    ids=["unknown-page", "wrong-vocabulary", "blank-reviewer", "foreign-origin"],
)
def test_bad_saves_are_refused(
    client: TestClient, body: dict[str, Any], headers: dict[str, str], status: int, detail: str
):
    response = _post(client, body, **headers)
    assert response.status_code == status
    assert detail in str(response.json()["detail"])


def test_only_the_web_app_can_write(client: TestClient, exports: Path):
    body = {"kind": "review", "key": "http://h/wall", "decision": "resolved", "reviewer": "a"}
    # No proof header: a plain cross-site form or script cannot save.
    assert client.post("/api/runs/demo/decisions", json=body).status_code == 403
    form = client.post("/api/runs/demo/decisions", data={"kind": "review"}, headers=HEADERS)
    assert form.status_code == 415
    local = _post(client, body, Origin="http://localhost:8780")
    assert local.status_code == 200
    # Everything else stays read-only.
    assert client.post("/api/runs/demo/review", json=body, headers=HEADERS).status_code == 405
    assert client.put("/api/runs/demo/decisions", json=body, headers=HEADERS).status_code == 405
    assert client.delete("/api/runs/demo/decisions", headers=HEADERS).status_code == 405
    assert len(read_decisions(exports / "demo.decisions.jsonl")) == 1


# -- carried into the next report ------------------------------------------------------------


def test_report_command_carries_saved_decisions(client: TestClient, exports: Path, capsys):
    pair = _pair_id(client)
    _post(
        client,
        {
            "kind": "contradiction",
            "key": pair,
            "decision": "not_a_contradiction",
            "reviewer": "Ana",
        },
    )
    _post(
        client,
        {
            "kind": "review",
            "key": "http://h/wall",
            "decision": "exclude",
            "note": "gated",
            "reviewer": "Ana",
        },
    )
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "report",
            "--records",
            str(exports / "demo.records.jsonl"),
            "--out",
            str(exports / "demo_reports"),
        ]
    )
    assert args.func(args) == 0
    capsys.readouterr()
    out = exports / "demo_reports"
    r4 = [
        json.loads(x)
        for x in report_path(out, "r4_contradictions.jsonl").read_text("utf-8").splitlines()
    ]
    assert [r["reviewer_decision"] for r in r4] == ["not_a_contradiction"]
    queue = [
        json.loads(x)
        for x in report_path(out, "review_queue.jsonl").read_text("utf-8").splitlines()
    ]
    wall = next(r for r in queue if r["url"] == "http://h/wall")
    assert (wall["operator_action"], wall["operator_note"]) == ("exclude", "gated")
    # The API now reads the saved report and still shows who decided.
    item = client.get("/api/runs/demo/contradictions").json()
    assert item["source"] == "report" and item["items"][0]["decided_by"] == "Ana"
