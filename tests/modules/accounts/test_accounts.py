"""Team accounts: sign-in, roles at the gate, forced password change, sessions, tokens, CLI."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.modules.accounts import commands as account_commands
from src.modules.accounts.access import SessionSigner, required_role
from src.modules.accounts.store import AccountError, AccountStore
from src.modules.content_processor import cli
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi import __main__ as webapi_main
from src.modules.content_processor.webapi.app import create_app

WRITE = {"X-Requested-With": "content-processor"}
ADMIN_PW = "admin-password-1"
NEW_PROJECT = {
    "name": "Life cover",
    "owner_name": "Anna",
    "client_name": "Example",
    "client_website": "example.com",
}


class World:
    def __init__(self, tmp: Path) -> None:
        self.data = tmp / "app_data"
        exports = tmp / "exports"
        exports.mkdir()
        self.projects = ProjectStore(self.data)
        self.accounts = AccountStore(self.data / "accounts")
        signer = TokenSigner(b"k" * 32)
        self.app = create_app(
            exports, projects=(self.projects, ScrapeRunner(self.projects), signer)
        )

    def client(self) -> TestClient:
        return TestClient(self.app, base_url="http://127.0.0.1")

    def signed_in(self, username: str, password: str) -> TestClient:
        c = self.client()
        r = c.post(
            "/api/auth/login", json={"username": username, "password": password}, headers=WRITE
        )
        assert r.status_code == 200, r.text
        return c


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_the_gate_table() -> None:
    assert required_role("GET", "/") == "public"
    assert required_role("GET", "/assets/index.js") == "public"
    assert required_role("GET", "/api/auth/me") == "public"
    assert required_role("POST", "/api/auth/login") == "public"
    assert required_role("POST", "/api/auth/password") == "signed_in"
    assert required_role("GET", "/api/projects") == "viewer"
    assert required_role("POST", "/api/mcp/call") == "viewer"
    assert required_role("POST", "/api/runs/r1/decisions") == "reviewer"
    assert required_role("POST", "/api/projects/p/gaps/findings/gap-1/status") == "reviewer"
    assert required_role("POST", "/api/projects/p/sites/s/runs/r/verdicts") == "reviewer"
    assert required_role("POST", "/api/projects") == "editor"
    assert required_role("POST", "/api/projects/p/research/pull") == "editor"
    assert required_role("POST", "/api/projects/p/delete") == "admin"
    assert required_role("GET", "/api/accounts") == "admin"


def test_without_accounts_nothing_changes(world: World) -> None:
    c = world.client()
    assert c.get("/api/auth/me").json() == {"accounts": False, "account": None, "min_password": 10}
    assert c.get("/api/projects").status_code == 200
    no_password = c.post("/api/projects", json=NEW_PROJECT, headers=WRITE)
    assert no_password.status_code == 422 and "password" in no_password.json()["detail"]


def test_roles_sign_in_and_first_password_change(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    anon = world.client()
    assert anon.get("/api/projects").json() == {"detail": "Sign in to continue."}
    assert anon.get("/api/auth/me").json()["accounts"] is True
    wrong = anon.post(
        "/api/auth/login", json={"username": "gaurav", "password": "nope"}, headers=WRITE
    )
    assert wrong.status_code == 401 and "do not match" in wrong.json()["detail"]
    admin = world.signed_in("GAURAV", ADMIN_PW)  # names are case-insensitive
    assert admin.get("/api/auth/me").json()["account"]["role"] == "admin"
    for name, role in (("vera", "viewer"), ("rita", "reviewer"), ("eddy", "editor")):
        r = admin.post(
            "/api/accounts",
            json={
                "username": name,
                "display_name": name.title(),
                "role": role,
                "password": "temporary-pw-1",
            },
            headers=WRITE,
        )
        assert r.status_code == 200 and r.json()["must_change_password"] is True
    weak = admin.post(
        "/api/accounts",
        json={"username": "x1", "display_name": "X", "role": "viewer", "password": "short"},
        headers=WRITE,
    )
    assert weak.status_code == 422 and "10 characters" in weak.json()["detail"]

    eddy = world.signed_in("eddy", "temporary-pw-1")
    assert eddy.get("/api/projects").json() == {"detail": "Choose a new password first."}
    same = eddy.post(
        "/api/auth/password",
        json={"current": "temporary-pw-1", "new": "temporary-pw-1"},
        headers=WRITE,
    )
    assert same.status_code == 422
    changed = eddy.post(
        "/api/auth/password",
        json={"current": "temporary-pw-1", "new": "eddy-own-pass-1"},
        headers=WRITE,
    )
    assert changed.status_code == 200 and changed.json()["account"]["must_change_password"] is False
    # an editor changes projects without any project password or unlock token
    created = eddy.post("/api/projects", json=NEW_PROJECT, headers=WRITE)
    assert created.status_code == 200, created.text
    pid = created.json()["project"]["project_id"]
    renamed = eddy.post(f"/api/projects/{pid}/update", json={"name": "Life cover 2"}, headers=WRITE)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Life cover 2"
    assert eddy.post(f"/api/projects/{pid}/delete", json={}, headers=WRITE).status_code == 403
    assert eddy.get("/api/accounts").status_code == 403

    vera = world.signed_in("vera", "temporary-pw-1")
    vera.post(
        "/api/auth/password",
        json={"current": "temporary-pw-1", "new": "vera-own-pass-1"},
        headers=WRITE,
    )
    assert vera.get(f"/api/projects/{pid}").status_code == 200
    refused = vera.post(f"/api/projects/{pid}/update", json={"name": "x"}, headers=WRITE)
    assert refused.status_code == 403 and "viewer" in refused.json()["detail"]

    rita = world.signed_in("rita", "temporary-pw-1")
    rita.post(
        "/api/auth/password",
        json={"current": "temporary-pw-1", "new": "rita-own-pass-1"},
        headers=WRITE,
    )
    assert (
        rita.post(f"/api/projects/{pid}/update", json={"name": "x"}, headers=WRITE).status_code
        == 403
    )
    review = rita.post(
        f"/api/projects/{pid}/gaps/findings/gap-aaaaaaaaaaaa/status",
        json={"status": "accepted", "note": ""},
        headers=WRITE,
    )
    assert review.status_code != 403  # past the gate (404: no gap analysis in this test)

    assert admin.post(f"/api/projects/{pid}/delete", json={}, headers=WRITE).status_code == 200


def test_sessions_end_on_password_change_disable_and_sign_out(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    world.accounts.create("anna", "Anna", "editor", "anna-password-1", must_change_password=False)
    admin = world.signed_in("gaurav", ADMIN_PW)
    laptop = world.signed_in("anna", "anna-password-1")
    phone = world.signed_in("anna", "anna-password-1")
    assert (
        laptop.post(
            "/api/auth/password",
            json={"current": "anna-password-1", "new": "anna-password-2"},
            headers=WRITE,
        ).status_code
        == 200
    )
    assert laptop.get("/api/projects").status_code == 200  # this device stays signed in
    assert phone.get("/api/projects").status_code == 401  # the others are signed out
    assert (
        admin.post("/api/accounts/anna/update", json={"disabled": True}, headers=WRITE).status_code
        == 200
    )
    assert laptop.get("/api/projects").status_code == 401
    blocked = world.client().post(
        "/api/auth/login", json={"username": "anna", "password": "anna-password-2"}, headers=WRITE
    )
    assert blocked.status_code == 401
    reset = admin.post(
        "/api/accounts/anna/password", json={"password": "anna-temporary-9"}, headers=WRITE
    )
    assert reset.json()["must_change_password"] is True
    admin.post("/api/auth/logout", json={}, headers=WRITE)
    assert admin.get("/api/projects").status_code == 401


def test_the_last_admin_stays_and_nobody_disables_themselves(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    admin = world.signed_in("gaurav", ADMIN_PW)
    demote = admin.post("/api/accounts/gaurav/update", json={"role": "editor"}, headers=WRITE)
    assert demote.status_code == 409 and "at least one" in demote.json()["detail"]
    world.accounts.create("ola", "Ola", "admin", "ola-password-1", must_change_password=False)
    itself = admin.post("/api/accounts/gaurav/update", json={"disabled": True}, headers=WRITE)
    assert itself.status_code == 409 and "your own" in itself.json()["detail"]
    assert (
        admin.post("/api/accounts/ola/update", json={"role": "reviewer"}, headers=WRITE).json()[
            "role"
        ]
        == "reviewer"
    )
    assert (
        admin.post(
            "/api/accounts/nobody/update", json={"role": "viewer"}, headers=WRITE
        ).status_code
        == 404
    )


def test_tokens_act_with_their_role(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    token = world.accounts.create_token("gaurav", "Claude Desktop", "viewer")
    c = world.client()
    bearer = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/projects", headers=bearer).status_code == 200
    assert c.post("/api/projects", json=NEW_PROJECT, headers={**WRITE, **bearer}).status_code == 403
    assert c.get("/api/projects", headers={"Authorization": "Bearer cpat_wrong"}).status_code == 401
    world.accounts.revoke_token(world.accounts.tokens("gaurav")[0].token_id)
    assert c.get("/api/projects", headers=bearer).status_code == 401
    world.accounts.create("vera", "Vera", "viewer", "vera-password-1")
    with pytest.raises(AccountError, match="cannot create a token with the admin role"):
        world.accounts.create_token("vera", "x", "admin")


def test_wrong_passwords_slow_down(world: World) -> None:
    world.accounts.create("gaurav", "Gaurav", "admin", ADMIN_PW, must_change_password=False)
    c = world.client()
    codes = [
        c.post(
            "/api/auth/login", json={"username": "gaurav", "password": f"bad-{i}"}, headers=WRITE
        ).status_code
        for i in range(7)
    ]
    assert codes[:5] == [401] * 5 and 429 in codes[5:]


def test_session_cookie_is_signed_and_expires() -> None:
    signer = SessionSigner(b"s" * 32, hours=1)
    from datetime import UTC, datetime

    from src.modules.accounts.store import Account

    a = Account(
        username="a",
        display_name="A",
        role="viewer",
        password_hash="x",
        created_at=datetime.now(UTC),
    )
    value = signer.issue(a, now=1000.0)
    assert signer.read(value, now=1001.0) == ("a", 0)
    assert signer.read(value, now=1000.0 + 3601) is None
    assert signer.read(value[:-2] + "xx", now=1001.0) is None
    assert SessionSigner(b"t" * 32).read(value, now=1001.0) is None
    assert signer.read(None) is None and signer.read("nonsense") is None


def test_cli_creates_the_first_admin_and_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = ["--app-data", str(tmp_path)]
    monkeypatch.setattr(account_commands.sys, "stdin", io.StringIO("first-admin-pass\n"))
    assert (
        cli.main(["accounts", "create-admin", "--username", "gaurav", "--name", "Gaurav", *data])
        == 0
    )
    assert "Sign-in is now required" in capsys.readouterr().out
    store = AccountStore(tmp_path / "accounts")
    assert store.get("gaurav") is not None and store.get("gaurav").must_change_password is False  # type: ignore[union-attr]
    monkeypatch.setattr(account_commands.sys, "stdin", io.StringIO("temp-password-1\n"))
    assert cli.main(["accounts", "add", "--username", "anna", "--role", "reviewer", *data]) == 0
    capsys.readouterr()
    assert store.get("anna").must_change_password is True  # type: ignore[union-attr]
    assert cli.main(["accounts", "list", *data]) == 0
    listed: list[dict[str, Any]] = json.loads(capsys.readouterr().out)
    assert [(a["username"], a["role"]) for a in listed] == [
        ("anna", "reviewer"),
        ("gaurav", "admin"),
    ]
    assert (
        cli.main(
            [
                "accounts",
                "token",
                "--username",
                "gaurav",
                "--label",
                "Claude",
                "--token-role",
                "viewer",
                *data,
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.startswith("cpat_")
    monkeypatch.setattr(account_commands.sys, "stdin", io.StringIO("short\n"))
    assert cli.main(["accounts", "add", "--username", "bob", *data]) == 1
    assert "10 characters" in capsys.readouterr().err


def test_the_server_will_not_listen_beyond_this_machine_without_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    served: list[str] = []
    monkeypatch.setattr(
        webapi_main.uvicorn, "run", lambda app, host, port, log_level: served.append(host)
    )
    monkeypatch.setattr(webapi_main.ScrapeRunner, "start", lambda self: None)
    monkeypatch.setattr(webapi_main, "MonitorWatcher", None, raising=False)
    argv = ["--exports", str(exports), "--app-data", str(tmp_path / "data"), "--host", "0.0.0.0"]  # noqa: S104
    assert webapi_main.main(argv) == 2 and served == []
    assert "Create the first admin first" in capsys.readouterr().err
    AccountStore(tmp_path / "data" / "accounts").create(
        "gaurav", "G", "admin", ADMIN_PW, must_change_password=False
    )
    assert webapi_main.main(argv) == 0 and served == ["0.0.0.0"]  # noqa: S104
