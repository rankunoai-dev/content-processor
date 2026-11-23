"""Sign-in fails closed beyond this machine; sign-ins and downloads are recorded."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from src.modules.accounts.api import LOCKED_DETAIL
from src.modules.accounts.audit import ActionLog
from src.modules.content_processor.contracts import RunScope
from tests.modules.accounts.test_accounts import ADMIN_PW, WRITE, World


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    report = tmp_path / "report.xlsx"
    report.write_bytes(b"cells")

    def download() -> FileResponse:
        return FileResponse(report, filename="report.xlsx")

    # ahead of the app's catch-all page route
    w.app.add_api_route("/api/test-download", download, methods=["GET"])
    w.app.router.routes.insert(0, w.app.router.routes.pop())
    return w


def actions(world: World) -> list[dict[str, object]]:
    return ActionLog(world.data / "audit").read()


def login(c: TestClient, password: str) -> int:
    r = c.post("/api/auth/login", json={"username": "gaurav", "password": password}, headers=WRITE)
    return r.status_code


def test_without_accounts_this_machine_stays_open(world: World) -> None:
    assert world.client().get("/api/projects").status_code == 200


def test_beyond_this_machine_a_missing_accounts_file_locks_every_api_route(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    world.app.state.accounts_required = True
    c = world.signed_in("gaurav", ADMIN_PW)
    assert c.get("/api/projects").status_code == 200

    (world.data / "accounts" / "accounts.json").unlink()  # the file goes missing
    for method, path in (
        ("GET", "/api/projects"),
        ("GET", "/api/auth/me"),
        ("POST", "/api/auth/login"),
        ("GET", "/api/test-download"),
    ):
        r = c.request(method, path, headers=WRITE, json={})
        assert r.status_code == 503, (path, r.status_code)
        assert r.json()["detail"] == LOCKED_DETAIL
    assert world.client().get("/").status_code != 503  # the page itself still loads
    locked = [a for a in actions(world) if a["event"] == "locked_no_accounts"]
    assert len(locked) == 1  # recorded once, not per request

    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    assert world.client().get("/api/projects").status_code == 401  # sign-in again
    assert world.signed_in("gaurav", ADMIN_PW).get("/api/projects").status_code == 200


def test_sign_ins_failures_and_sign_outs_are_recorded(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    c = world.client()
    assert login(c, "wrong-password-1") == 401
    assert login(c, ADMIN_PW) == 200
    assert c.post("/api/auth/logout", headers=WRITE, json={}).status_code == 200
    got = [(a["event"], a["user"], a.get("reason")) for a in actions(world)]
    assert got == [
        ("login_failed", "gaurav", "wrong_password"),
        ("login_succeeded", "gaurav", None),
        ("logout", "gaurav", None),
    ]
    text = (world.data / "audit" / "user_actions.jsonl").read_text(encoding="utf-8")
    assert ADMIN_PW not in text and "wrong-password-1" not in text
    assert all(a["ip"] for a in actions(world))


def test_throttled_sign_ins_are_recorded(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    c = world.client()
    codes = [login(c, "wrong-password-1") for _ in range(8)]
    assert 429 in codes
    reasons = {a.get("reason") for a in actions(world) if a["event"] == "login_failed"}
    assert reasons == {"wrong_password", "throttled"}


def test_downloads_are_recorded_with_the_signed_in_account(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    c = world.signed_in("gaurav", ADMIN_PW)
    assert c.get("/api/test-download").status_code == 200
    assert c.get("/api/projects").status_code == 200  # not a download: not recorded
    downloads = [a for a in actions(world) if a["event"] == "download"]
    assert len(downloads) == 1
    d = downloads[0]
    assert d["user"] == "gaurav" and d["via"] == "session" and d["status"] == 200
    assert d["path"] == "/api/test-download" and "report.xlsx" in str(d["file"])


def test_downloads_are_recorded_without_accounts_too(world: World) -> None:
    assert world.client().get("/api/test-download").status_code == 200
    (d,) = actions(world)
    assert d["event"] == "download" and d["user"] is None


def test_an_unwritable_action_log_never_stops_a_request(world: World, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    world.app.state.actions.path = blocker / "user_actions.jsonl"
    assert world.client().get("/api/test-download").status_code == 200


def test_the_scope_no_longer_carries_retention_or_pii_settings() -> None:
    fields = RunScope.model_fields
    assert "retention_class" not in fields and "pii_policy" not in fields


def test_the_container_puts_both_logs_on_the_data_volume() -> None:
    script = (Path(__file__).parents[3] / "deploy" / "start.sh").read_text(encoding="utf-8")
    assert 'AUDIT_LOG_PATH="${AUDIT_LOG_PATH:-$CP_DATA_ROOT/logs/audit.jsonl}"' in script
    assert 'CP_APP_DATA_ROOT="${CP_APP_DATA_ROOT:-$CP_DATA_ROOT/app_data}"' in script
