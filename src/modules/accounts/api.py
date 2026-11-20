"""HTTP routes of accounts, and installing the gate.

POST /api/auth/login                    sign in (sets the session cookie)
POST /api/auth/logout                   sign out
GET  /api/auth/me                       who is signed in, and whether accounts are on
POST /api/auth/password                 change one's own password
GET  /api/accounts                      every account (admin)
POST /api/accounts                      add an account with a temporary password (admin)
POST /api/accounts/{username}/update    role, name, disable or enable (admin)
POST /api/accounts/{username}/password  set a temporary password (admin)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.accounts.access import (
    COOKIE,
    SessionSigner,
    decide,
    identify,
    required_role,
)
from src.modules.accounts.store import (
    MIN_ACCOUNT_PASSWORD,
    Account,
    AccountError,
    AccountStore,
    Role,
)
from src.modules.content_processor.projects.security import ACTING, AttemptLimiter

__all__ = ["AccountOut", "install_accounts"]


class AccountOut(StrictModel):
    """An account as the app shows it (never the password hash)."""

    username: str
    display_name: str
    role: Role
    must_change_password: bool
    disabled: bool
    created_at: datetime
    created_by: str
    last_login_at: datetime | None


class MeOut(StrictModel):
    """Whether sign-in is required, and who is signed in."""

    accounts: bool
    account: AccountOut | None = None
    min_password: int = MIN_ACCOUNT_PASSWORD


class LoginIn(StrictModel):
    """Sign-in."""

    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


class PasswordIn(StrictModel):
    """Change one's own password."""

    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


class AccountIn(StrictModel):
    """A new account (admin)."""

    username: str = Field(min_length=2, max_length=40)
    display_name: str = Field(min_length=1, max_length=120)
    role: Role
    password: str = Field(min_length=1, max_length=200, description="Temporary password")


class AccountUpdate(StrictModel):
    """Changes to an account (admin); fields left out are kept."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role: Role | None = None
    disabled: bool | None = None


class TempPasswordIn(StrictModel):
    """A temporary password set by an admin."""

    password: str = Field(min_length=1, max_length=200)


def _out(a: Account) -> AccountOut:
    return AccountOut.model_validate(
        a.model_dump(exclude={"password_hash", "session_epoch", "password_changed_at"})
    )


def _secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"


def install_accounts(app: FastAPI, *, accounts: AccountStore, sessions: SessionSigner) -> None:
    """Add the gate (a middleware) and the account routes."""
    attempts = AttemptLimiter()

    @app.middleware("http")
    async def gate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not accounts.active():
            return await call_next(request)
        path = request.url.path
        need = required_role(request.method, path)
        caller = identify(
            accounts,
            sessions,
            cookie=request.cookies.get(COOKIE),
            authorization=request.headers.get("authorization"),
        )
        refusal = decide(need, caller, path)
        if refusal is not None:
            return JSONResponse({"detail": refusal[1]}, status_code=refusal[0])
        request.state.caller = caller
        reset = ACTING.set(caller.account.username if caller else None)
        try:
            return await call_next(request)
        finally:
            ACTING.reset(reset)

    def caller_of(request: Request) -> Any:
        return getattr(request.state, "caller", None)

    @app.get("/api/auth/me")
    def me(request: Request) -> MeOut:
        if not accounts.active():
            return MeOut(accounts=False)
        caller = identify(
            accounts,
            sessions,
            cookie=request.cookies.get(COOKIE),
            authorization=request.headers.get("authorization"),
        )
        return MeOut(accounts=True, account=_out(caller.account) if caller else None)

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response) -> MeOut:
        if not accounts.active():
            raise HTTPException(status_code=409, detail="No accounts have been created yet.")
        name = body.username.strip().lower()
        client = request.client.host if request.client else "?"
        wait = max(attempts.wait_s(f"u:{name}"), attempts.wait_s(f"ip:{client}"))
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong passwords. Try again in {int(wait) + 1} seconds.",
            )
        account = accounts.verify(name, body.password)
        if account is None:
            attempts.failed(f"u:{name}")
            attempts.failed(f"ip:{client}")
            raise HTTPException(status_code=401, detail="That user name and password do not match.")
        attempts.succeeded(f"u:{name}")
        attempts.succeeded(f"ip:{client}")
        account = accounts.update(account.username, last_login_at=datetime.now(UTC))
        response.set_cookie(
            COOKIE,
            sessions.issue(account),
            max_age=sessions.ttl_s,
            httponly=True,
            samesite="lax",
            secure=_secure(request),
            path="/",
        )
        return MeOut(accounts=True, account=_out(account))

    @app.post("/api/auth/logout")
    def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE, path="/")
        return {"signed_out": True}

    @app.post("/api/auth/password")
    def change_password(body: PasswordIn, request: Request, response: Response) -> MeOut:
        caller = caller_of(request)
        if caller is None or caller.via != "session":
            raise HTTPException(status_code=403, detail="Sign in to change your password.")
        if accounts.verify(caller.account.username, body.current) is None:
            raise HTTPException(status_code=401, detail="The current password is not right.")
        if body.new == body.current:
            raise HTTPException(status_code=422, detail="Choose a password you have not used here.")
        try:
            account = accounts.set_password(caller.account.username, body.new, must_change=False)
        except AccountError as exc:
            raise HTTPException(status_code=422, detail=f"Not saved: {exc}.") from None
        response.set_cookie(  # the old sessions ended; this device stays signed in
            COOKIE,
            sessions.issue(account),
            max_age=sessions.ttl_s,
            httponly=True,
            samesite="lax",
            secure=_secure(request),
            path="/",
        )
        return MeOut(accounts=True, account=_out(account))

    # -- admin ---------------------------------------------------------------------------

    @app.get("/api/accounts")
    def list_accounts() -> list[AccountOut]:
        return [_out(a) for a in accounts.all()]

    @app.post("/api/accounts")
    def add_account(body: AccountIn, request: Request) -> AccountOut:
        caller = caller_of(request)
        try:
            account = accounts.create(
                body.username,
                body.display_name,
                body.role,
                body.password,
                created_by=caller.account.username if caller else "",
            )
        except AccountError as exc:
            raise HTTPException(status_code=422, detail=f"Not added: {exc}.") from None
        return _out(account)

    @app.post("/api/accounts/{username}/update")
    def update_account(username: str, body: AccountUpdate, request: Request) -> AccountOut:
        caller = caller_of(request)
        current = accounts.get(username)
        if current is None:
            raise HTTPException(status_code=404, detail="There is no such account.")
        losing_admin = current.role == "admin" and (
            (body.role is not None and body.role != "admin") or body.disabled
        )
        if losing_admin and accounts.admins_left(current.username) == 0:
            raise HTTPException(status_code=409, detail="Keep at least one enabled admin account.")
        if caller and caller.account.username == current.username and body.disabled:
            raise HTTPException(status_code=409, detail="You cannot disable your own account.")
        changes: dict[str, object] = {k: v for k, v in body.model_dump().items() if v is not None}
        if ("role" in changes and changes["role"] != current.role) or changes.get("disabled"):
            changes["session_epoch"] = current.session_epoch + 1  # sign out everywhere
        return _out(accounts.update(current.username, **changes))

    @app.post("/api/accounts/{username}/password")
    def reset_password(username: str, body: TempPasswordIn) -> AccountOut:
        try:
            return _out(accounts.set_password(username, body.password, must_change=True))
        except AccountError as exc:
            status = 404 if "no account" in str(exc) else 422
            raise HTTPException(status_code=status, detail=f"Not saved: {exc}.") from None
