"""Unit tests for TaskLedger methods (mocking database connection)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from src.modules.content_processor.contracts import (
    Region,
    Resource,
    RunEvent,
    State,
)
from src.modules.content_processor.enums import (
    EntityStatus,
    TaskKind,
)
from src.modules.content_processor.ledger import sql
from src.modules.content_processor.ledger.tasks import TaskLedger, new_run_id


def _mock_ledger():
    with patch("psycopg.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        ledger = TaskLedger("postgresql://test:test@localhost/test")
        return ledger, mock_conn, mock_cursor


def test_new_run_id():
    rid = new_run_id()
    assert rid.startswith("run-")
    assert len(rid) > 10


def test_upsert_resource_carries_expected_mime():
    ledger, conn, cur = _mock_ledger()
    res = Resource(
        resource_id="res-1",
        run_id="run-1",
        requested_url="https://h.example/file.pdf",
        schedule_key="https://h.example/file.pdf",
        expected_mime="application/pdf",
        expected_mime_source="link_type",
    )
    ledger.upsert_resource(res, "h.example")
    cur.execute.assert_called_once()
    args = cur.execute.call_args[0]
    assert args[0] == sql.SQL_UPSERT_RESOURCE
    params = args[1]
    assert params["expected_mime"] == "application/pdf"
    assert params["expected_mime_source"] == "link_type"
    conn.commit.assert_called_once()


def test_record_edge():
    ledger, conn, cur = _mock_ledger()
    ledger.record_edge("run-1", "parent-1", "child-1", "embeds", evidence_id="ev-1")
    cur.execute.assert_called_once_with(
        sql.SQL_RECORD_EDGE,
        {
            "run_id": "run-1",
            "parent_id": "parent-1",
            "child_id": "child-1",
            "edge_kind": "embeds",
            "evidence_id": "ev-1",
        },
    )
    conn.commit.assert_called_once()


def test_find_resource_by_schedule_key():
    ledger, conn, cur = _mock_ledger()
    res = Resource(
        resource_id="res-1",
        run_id="run-1",
        requested_url="https://h.example/page",
        schedule_key="https://h.example/page",
    )
    cur.fetchone.return_value = {"record": json.loads(res.model_dump_json())}
    found = ledger.find_resource_by_schedule_key("run-1", "https://h.example/page")
    assert found is not None
    assert found.resource_id == "res-1"

    cur.fetchone.return_value = None
    assert ledger.find_resource_by_schedule_key("run-1", "missing") is None


def test_find_resource_by_payload():
    ledger, conn, cur = _mock_ledger()
    res = Resource(
        resource_id="res-1",
        run_id="run-1",
        requested_url="https://h.example/doc",
        schedule_key="https://h.example/doc",
        payload_sha256="abc123sha",
    )
    cur.fetchone.return_value = {"record": json.loads(res.model_dump_json())}
    found = ledger.find_resource_by_payload("run-1", "abc123sha")
    assert found is not None
    assert found.payload_sha256 == "abc123sha"

    cur.fetchone.return_value = None
    assert ledger.find_resource_by_payload("run-1", "missing") is None


def test_states_persistence():
    ledger, conn, cur = _mock_ledger()
    st = State(
        state_id="st-1",
        run_id="run-1",
        resource_id="res-1",
        state_key="default",
        status=EntityStatus.DISCOVERED,
    )
    ledger.upsert_state(st)
    cur.execute.assert_called_once()
    assert cur.execute.call_args[0][0] == sql.SQL_UPSERT_STATE

    cur.fetchall.return_value = [{"record": json.loads(st.model_dump_json())}]
    states = ledger.get_states("run-1", "res-1")
    assert len(states) == 1
    assert states[0].state_id == "st-1"


def test_regions_persistence():
    ledger, conn, cur = _mock_ledger()
    reg = Region(
        region_id="reg-1",
        run_id="run-1",
        resource_id="res-1",
        region_key="main",
        status=EntityStatus.DISCOVERED,
    )
    ledger.upsert_region(reg)
    cur.execute.assert_called_once()
    assert cur.execute.call_args[0][0] == sql.SQL_UPSERT_REGION

    cur.fetchall.return_value = [{"record": json.loads(reg.model_dump_json())}]
    regions = ledger.get_regions("run-1", "res-1")
    assert len(regions) == 1
    assert regions[0].region_id == "reg-1"


def test_list_resources():
    ledger, conn, cur = _mock_ledger()
    res1 = Resource(
        resource_id="res-1",
        run_id="run-1",
        requested_url="https://h.example/1",
        schedule_key="https://h.example/1",
    )
    res2 = Resource(
        resource_id="res-2",
        run_id="run-1",
        requested_url="https://h.example/2",
        schedule_key="https://h.example/2",
    )
    cur.fetchall.return_value = [
        {"record": json.loads(res1.model_dump_json())},
        {"record": json.loads(res2.model_dump_json())},
    ]
    resources = ledger.list_resources("run-1")
    assert len(resources) == 2
    assert resources[0].resource_id == "res-1"
    assert resources[1].resource_id == "res-2"


def test_get_summaries():
    ledger, conn, cur = _mock_ledger()
    res = Resource(
        resource_id="res-1",
        run_id="run-1",
        requested_url="https://h.example/1",
        schedule_key="https://h.example/1",
    )
    record = json.loads(res.model_dump_json())
    record["render"] = {"width": 1280}
    record["explore"] = {"tabs": 2}
    cur.fetchone.return_value = {"record": record}
    summaries = ledger.get_summaries("res-1")
    assert summaries == {"render": {"width": 1280}, "explore": {"tabs": 2}}

    cur.fetchone.return_value = None
    assert ledger.get_summaries("missing") == {}


def test_get_events():
    ledger, conn, cur = _mock_ledger()
    now = datetime.now(UTC)
    cur.fetchall.return_value = [
        {
            "seq": 1,
            "run_id": "run-1",
            "entity_type": "resource",
            "entity_id": "res-1",
            "old_state": None,
            "new_state": "DISCOVERED",
            "inventory_revision": 0,
            "reason": "initial",
            "attempt": 1,
            "evidence_ids": ["ev-1"],
            "at": now,
        }
    ]
    events = ledger.get_events("run-1", "res-1")
    assert len(events) == 1
    assert isinstance(events[0], RunEvent)
    assert events[0].seq == 1
    assert events[0].entity_id == "res-1"
    assert cur.execute.call_args[0][0] == sql.SQL_GET_EVENTS_FOR_ENTITY

    events_all = ledger.get_events("run-1")
    assert len(events_all) == 1
    assert cur.execute.call_args[0][0] == sql.SQL_GET_EVENTS_FOR_RUN


def test_outstanding_resource_ids():
    ledger, conn, cur = _mock_ledger()
    assert ledger.outstanding_resource_ids("run-1", []) == set()

    cur.fetchall.return_value = [{"res_id": "res-1"}, {"res_id": "res-2"}]
    res_ids = ledger.outstanding_resource_ids("run-1", [TaskKind.FETCH, TaskKind.RENDER])
    assert res_ids == {"res-1", "res-2"}
    assert cur.execute.call_args[0][0] == sql.SQL_OUTSTANDING_RESOURCE_IDS
